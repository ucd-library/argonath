"""
Showcase Elasticsearch jobs that never touch derivatives: a bulk collection
reindex (with orphan prune), a collection delete and an item delete. Each op
shells out to `digtk showcase es` through lib.utils.exec().
"""
from dagster import DynamicOut, DynamicOutput, In, OpExecutionContext, Out, Output, job, op
from dagster_celery import celery_executor

from lib.configs import DeleteItemIndexConfig, ReindexCollectionConfig, collections_partitions
from lib.fanout_batching import log_batch, relative_id, step_key
from lib.pipeline_collection import pdc_index_collection
from lib.pipeline_fanout import _emit, fetch_items_for_collection
from lib.utils import default_run_config, exec


@op(out={"batches": DynamicOut(dict, is_required=False), "settings": Out(dict)})
def fan_out_reindex_tasks(context: OpExecutionContext, config: ReindexCollectionConfig):
    """Split the partition's item folders into batches of config.batch_size,
    one step and one bulk request each.

    :param context: Dagster op execution context; partitioned by collection.
    :param config: ReindexCollectionConfig.
    """
    key = context.partition_key
    folders = fetch_items_for_collection(key)
    size = max(config.batch_size, 1)
    context.log.info(f"{key}: {len(folders)} item(s) in batches of {size}")

    used = set()
    for start in range(0, len(folders), size):
        chunk = folders[start:start + size]
        ids = [relative_id(f, key) for f in chunk]
        yield DynamicOutput(
            {"folders": chunk, "ids": ids, "rebuild": config.rebuild, "collection_key": key},
            output_name="batches", mapping_key=step_key(ids, used),
        )
    yield Output(
        {"prune": config.prune, "force_prune": config.force_prune, "items": len(folders)},
        output_name="settings",
    )


@op(ins={"task": In(dict)}, out=Out(dict))
def reindex_batch(context: OpExecutionContext, task: dict) -> dict:
    """Bulk-index one batch (`digtk showcase es upsert-items`) and emit an
    event per item. Failures are returned, not raised.

    :param context: Dagster op execution context; partitioned by collection.
    :param dict task: A fan_out_reindex_tasks batch.
    :returns: {"indexed": int, "skipped": int, "failed": [item id]}.
    :rtype: dict
    """
    key = task["collection_key"]
    log_batch(context, task["ids"])
    cmd = ["digtk", "showcase", "es", "upsert-items", *task["folders"]]
    if task["rebuild"]:
        cmd.append("--rebuild")
    try:
        result = exec(cmd)
    except Exception as e:
        for item_id in task["ids"]:
            _emit(context, "es-index", item_id, None, status="failed", partition=key, error=str(e))
        return {"indexed": 0, "skipped": 0, "failed": list(task["ids"])}

    for entry in result.get("indexed", []):
        _emit(context, "es-index", relative_id(entry["folder"], key), entry, partition=key, item_ark=entry["ark"])
    for entry in result.get("failed", []):
        _emit(
            context, "es-index", relative_id(entry["folder"], key), None, status="failed",
            partition=key, error=entry["error"],
        )
    for folder in result.get("skipped", []):
        context.log.warning(f"[{relative_id(folder, key)}] skipped: no ark")
    return {
        "indexed": len(result.get("indexed", [])),
        "skipped": len(result.get("skipped", [])),
        "failed": [relative_id(e["folder"], key) for e in result.get("failed", [])],
    }


@op
def prune_collection_index(context: OpExecutionContext, results: list, settings: dict) -> dict:
    """Delete indexed items no longer in bronze (`digtk showcase es prune`).
    Skipped when disabled or when any batch failed.

    :param context: Dagster op execution context; partitioned by collection.
    :param list[dict] results: Every reindex_batch result.
    :param dict settings: fan_out_reindex_tasks's prune settings.
    :returns: {"items", "indexed", "skipped", "failed", "prune"|"prune_error"}.
    :rtype: dict
    """
    key = context.partition_key
    summary = {
        "items": settings["items"],
        "indexed": sum(r["indexed"] for r in results),
        "skipped": sum(r["skipped"] for r in results),
        "failed": [item_id for r in results for item_id in r["failed"]],
    }
    context.log.info(
        f"{key}: {summary['indexed']} indexed, {summary['skipped']} skipped, {len(summary['failed'])} failed"
    )
    if not settings["prune"]:
        context.log.info(f"{key}: prune disabled")
        return summary
    if summary["failed"]:
        context.log.warning(f"{key}: prune skipped, {len(summary['failed'])} item(s) failed")
        return summary

    cmd = ["digtk", "showcase", "es", "prune", key]
    if settings["force_prune"]:
        cmd.append("--force")
    try:
        result = exec(cmd)
    except Exception as e:
        context.log.error(f"{key}: prune failed -> {e}")
        return {**summary, "prune_error": str(e)}
    for orphan in result.get("orphans", []):
        _emit(context, "es-prune", orphan, result, partition=key)
    return {**summary, "prune": {"deleted": result.get("items", 0), "pages": result.get("pages", 0)}}


@op
def check_reindex_failures(summary: dict):
    """Fail the run if any item or the prune failed.

    :param dict summary: prune_collection_index's summary.
    :raises RuntimeError: If anything failed.
    """
    if summary["failed"]:
        raise RuntimeError(
            f"{len(summary['failed'])} of {summary['items']} item(s) failed to index: {', '.join(summary['failed'])}"
        )
    if summary.get("prune_error"):
        raise RuntimeError(f"prune failed: {summary['prune_error']}")


@job(
    partitions_def=collections_partitions,
    tags={"group": "fanout", "argonath/kind": "collection-reindex", "dagster/max_runtime": "0"},
    executor_def=celery_executor,
    config=default_run_config({"fan_out_reindex_tasks": ReindexCollectionConfig}),
)
def reindex_collection_job():
    """Bulk-reindex a collection's items into the showcase ES index without
    making derivatives, prune orphans, then refresh the collection document
    the same way collection_rollup_job does.
    """
    batches, settings = fan_out_reindex_tasks()
    results = batches.map(reindex_batch).collect()
    check_reindex_failures(pdc_index_collection(prune_collection_index(results, settings)))


@op
def delete_collection_index(context: OpExecutionContext):
    """Delete the partition's collection, items and pages from ES
    (`digtk showcase es delete-collection`).

    :param context: Dagster op execution context; partitioned by collection.
    """
    key = context.partition_key
    result = exec(["digtk", "showcase", "es", "delete-collection", key])
    context.log.info(
        f"{key}: deleted {result.get('items', 0)} item(s), {result.get('pages', 0)} page(s), "
        f"collection doc {'deleted' if result.get('deleted') else 'not found'}"
    )
    if result.get("errors"):
        raise RuntimeError(f"{len(result['errors'])} document(s) failed to delete: {result['errors']}")


@job(partitions_def=collections_partitions, tags={"argonath/kind": "collection-delete"})
def delete_collection_index_job():
    """Remove a collection and everything in it from the showcase ES indexes.
    Cask and Postgres are left alone.
    """
    delete_collection_index()


@op
def delete_item_index(context: OpExecutionContext, config: DeleteItemIndexConfig) -> str:
    """Delete one item and its pages from ES (`digtk showcase es delete`).

    :param context: Dagster op execution context.
    :param config: DeleteItemIndexConfig.
    :returns: The item's collection key, or "" if unknown.
    :rtype: str
    :raises ValueError: If no ark was given.
    """
    if not config.ark:
        raise ValueError("ark is required")
    result = exec(["digtk", "showcase", "es", "delete", config.ark])
    context.log.info(f"{config.ark}: deleted {result.get('items', 0)} item doc(s), {result.get('pages', 0)} page(s)")
    if result.get("errors"):
        raise RuntimeError(f"delete failed: {result['errors']}")
    return result.get("collection") or config.collection


@op
def refresh_collection_index(context: OpExecutionContext, collection_key: str):
    """Rewrite the collection doc so its itemCount drops right away
    (`digtk showcase es upsert-collection`).

    :param context: Dagster op execution context.
    :param str collection_key: The deleted item's collection key, or "".
    """
    if not collection_key:
        context.log.warning("collection unknown, skipping refresh (set config.collection)")
        return
    result = exec(["digtk", "showcase", "es", "upsert-collection", collection_key])
    context.log.info(f"{collection_key}: refreshed collection -> {result}")


@job(
    tags={"argonath/kind": "item-delete"},
    config=default_run_config({"delete_item_index": DeleteItemIndexConfig}, {"celery": False}),
)
def delete_item_index_job():
    """Remove one item from the showcase ES indexes and refresh its collection
    doc. The item returns on the next reindex if it's still in bronze.
    """
    refresh_collection_index(delete_item_index())
