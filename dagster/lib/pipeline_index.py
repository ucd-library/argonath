"""
Showcase index jobs: a bulk collection reindex, the audit sensor's
non-processing changes, a collection delete and an item delete. Each op
shells out to digtk through lib.utils.exec().
"""
import json

from dagster import (
    AssetMaterialization, DynamicOut, DynamicOutput, In, MetadataValue, OpExecutionContext, Out, Output, job, op,
)

from lib.configs import CaskChangesConfig, DeleteItemIndexConfig, ReindexCollectionConfig, collections_partitions
from lib.fanout_batching import emit, log_batch, relative_id, step_key
from lib.pipeline_collection import pdc_index_collection
from lib.pipeline_fanout import fetch_items_for_collection
from lib.utils import celery_job_executor, default_run_config, exec


@op(out={"batches": DynamicOut(dict, is_required=False), "settings": Out(dict)})
def fan_out_reindex_tasks(context: OpExecutionContext, config: ReindexCollectionConfig):
    """Split the partition's gold item records (bronze item folders with
    rebuild) into batches of config.batch_size, one bulk request each.

    :param context: Dagster op execution context; partitioned by collection.
    :param config: ReindexCollectionConfig.
    """
    key = context.partition_key
    if config.rebuild:
        folders = fetch_items_for_collection(key)
    else:
        folders = exec(["digtk", "showcase", "es", "gold-items", key])["items"]
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
    """Bulk-index one batch; see _index_folders().

    :param context: Dagster op execution context; partitioned by collection.
    :param dict task: A fan_out_reindex_tasks batch.
    :returns: {"indexed": int, "skipped": int, "failed": [item id]}.
    :rtype: dict
    """
    return _index_folders(context, task)


def _index_folders(context, task):
    """Bulk-index folders (`digtk showcase es upsert-items`) and emit an
    event per item. Failures are returned, not raised.

    :param context: Dagster op execution context.
    :param dict task: {"folders", "ids", "rebuild", "collection_key"}.
    :returns: {"indexed": int, "skipped": int, "failed": [item id]}.
    :rtype: dict
    """
    key = task["collection_key"]
    if not task["folders"]:
        return {"indexed": 0, "skipped": 0, "failed": []}
    log_batch(context, task["ids"])
    cmd = ["digtk", "showcase", "es", "upsert-items", *task["folders"]]
    if task["rebuild"]:
        cmd.append("--rebuild")
    try:
        result = exec(cmd)
    except Exception as e:
        for item_id in task["ids"]:
            emit(context, "es-index", item_id, None, status="failed", partition=key, error=str(e))
        return {"indexed": 0, "skipped": 0, "failed": list(task["ids"])}

    for entry in result.get("indexed", []):
        emit(context, "es-index", relative_id(entry["folder"], key), entry, partition=key, item_ark=entry["ark"])
    for entry in result.get("failed", []):
        emit(
            context, "es-index", relative_id(entry["folder"], key), None, status="failed",
            partition=key, error=entry["error"],
        )
    for folder in result.get("skipped", []):
        context.log.warning(f"[{relative_id(folder, key)}] skipped: no gold record or ark")
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
        emit(context, "es-prune", orphan, result, partition=key)
    return {**summary, "prune": {"deleted": result.get("items", 0), "pages": result.get("pages", 0)}}


@op
def check_reindex_failures(summary: dict):
    """Fail the run if any item, the prune or the delete failed.

    :param dict summary: prune_collection_index's or delete_cask_removed's summary.
    :raises RuntimeError: If anything failed.
    """
    if summary["failed"]:
        raise RuntimeError(
            f"{len(summary['failed'])} of {summary['items']} item(s) failed to index: {', '.join(summary['failed'])}"
        )
    if summary.get("prune_error"):
        raise RuntimeError(f"prune failed: {summary['prune_error']}")
    if summary.get("delete_error"):
        raise RuntimeError(f"delete failed: {summary['delete_error']}")


@job(
    partitions_def=collections_partitions,
    tags={"group": "fanout", "argonath/kind": "collection-reindex", "dagster/max_runtime": "0"},
    executor_def=celery_job_executor(),
    config=default_run_config({"fan_out_reindex_tasks": ReindexCollectionConfig}),
)
def showcase_reindex_collection_job():
    """Bulk-reindex a collection's items into the showcase ES index without
    making derivatives, prune orphans, then refresh the collection document
    the same way showcase_collection_rollup_job does.
    """
    batches, settings = fan_out_reindex_tasks()
    results = batches.map(reindex_batch).collect()
    check_reindex_failures(pdc_index_collection(prune_collection_index(results, settings)))


def _log_changes(context, key, config):
    """One Events entry per changed item, plus a table of all of them.

    :param context: Dagster op execution context.
    :param str key: Collection key.
    :param config: CaskChangesConfig.
    """
    rows = [(relative_id(f, key), "reindex") for f in config.reindex]
    rows += [(relative_id(f, key), "delete") for f in config.cleanup]
    for item_id, action in rows:
        emit(context, "cask_change", item_id, {"action": action}, partition=key, action=action)
    if not rows:
        context.log.info(f"{key}: collection files changed, no items")
        return
    table = "\n".join(f"| {item_id} | {action} |" for item_id, action in rows)
    context.log_event(AssetMaterialization(
        asset_key="argonath_cask_changes", partition=key,
        metadata={
            "items": MetadataValue.md(f"| item | action |\n|---|---|\n{table}"),
            "deleted_arks": MetadataValue.text(", ".join(config.delete) or "none"),
        },
    ))


@op(out=Out(dict))
def plan_cask_changes(context: OpExecutionContext, config: CaskChangesConfig) -> dict:
    """Log the collection's reindex, delete and cleanup changes.
    Items to process run as showcase_process_item_job instead.

    :param context: Dagster op execution context; partitioned by collection.
    :param config: CaskChangesConfig.
    :returns: {"reindex", "delete", "cleanup", "batch_size"}.
    :rtype: dict
    """
    key = context.partition_key
    context.log.info(
        f"{key}: {len(config.reindex)} to reindex, {len(config.delete)} to delete, "
        f"{len(config.cleanup)} to clean up"
    )
    _log_changes(context, key, config)
    return {
        "reindex": list(config.reindex), "delete": list(config.delete),
        "cleanup": list(config.cleanup), "batch_size": max(config.batch_size, 1),
    }


@op
def index_cask_items(context: OpExecutionContext, settings: dict) -> dict:
    """Rebuild and index metadata-only edits (`digtk showcase es
    upsert-items --rebuild`), in chunks of batch_size.

    :param context: Dagster op execution context; partitioned by collection.
    :param dict settings: plan_cask_changes's settings.
    :returns: {"items", "indexed", "skipped", "failed"}.
    :rtype: dict
    """
    key = context.partition_key
    reindex = settings["reindex"]
    result = {"items": len(reindex), "indexed": 0, "skipped": 0, "failed": []}
    size = settings["batch_size"]
    for i in range(0, len(reindex), size):
        chunk = reindex[i:i + size]
        ids = [relative_id(f, key) for f in chunk]
        batch = _index_folders(context, {"folders": chunk, "ids": ids, "rebuild": True, "collection_key": key})
        result["indexed"] += batch["indexed"]
        result["skipped"] += batch["skipped"]
        result["failed"] += batch["failed"]
    context.log.info(
        f"{key}: {result['indexed']} indexed, {result['skipped']} skipped, {len(result['failed'])} failed"
    )
    return result


@op
def delete_cask_removed(context: OpExecutionContext, summary: dict, settings: dict) -> dict:
    """Delete the ES docs of items removed from bronze (`digtk showcase es delete`).

    :param context: Dagster op execution context; partitioned by collection.
    :param dict summary: index_cask_items's summary.
    :param dict settings: plan_cask_changes's delete list.
    :returns: The summary, plus "deleted" or "delete_error".
    :rtype: dict
    """
    key = context.partition_key
    if not settings["delete"]:
        return summary
    try:
        result = exec(["digtk", "showcase", "es", "delete", *settings["delete"]])
    except Exception as e:
        context.log.error(f"{key}: delete failed -> {e}")
        return {**summary, "delete_error": str(e)}
    for entry in result.get("deleted", []):
        emit(context, "es-delete", entry["ark"], entry, partition=key)
    if result.get("errors"):
        return {**summary, "delete_error": json.dumps(result["errors"])}
    return {**summary, "deleted": result.get("items", 0)}


@op
def clean_cask_removed(context: OpExecutionContext, summary: dict, settings: dict) -> dict:
    """Delete removed items' silver and gold directories (`digtk showcase
    clean-item`). Skipped if the ES delete failed.

    :param context: Dagster op execution context; partitioned by collection.
    :param dict summary: delete_cask_removed's summary.
    :param dict settings: plan_cask_changes's cleanup list.
    :returns: The summary, plus "cleaned" or "delete_error".
    :rtype: dict
    """
    key = context.partition_key
    if not settings["cleanup"]:
        return summary
    if summary.get("delete_error"):
        context.log.warning(f"{key}: silver/gold cleanup skipped, ES delete failed")
        return summary
    try:
        result = exec(["digtk", "showcase", "clean-item", *settings["cleanup"]])
    except Exception as e:
        context.log.error(f"{key}: silver/gold cleanup failed -> {e}")
        return {**summary, "delete_error": f"cleanup: {e}"}
    for entry in result.get("removed", []):
        context.log.info(f"[{relative_id(entry['folder'], key)}] removed {entry['path']} ({entry['files']} file(s))")
        emit(
            context, "cleanup", relative_id(entry["folder"], key), entry, partition=key,
            path=entry["path"], files=entry["files"],
        )
    for folder in result.get("kept", []):
        context.log.warning(f"[{relative_id(folder, key)}] back in bronze, silver/gold kept")
    return {**summary, "cleaned": len(result.get("removed", []))}


@job(
    partitions_def=collections_partitions,
    tags={"argonath/kind": "cask-changes", "dagster/max_runtime": "0"},
    executor_def=celery_job_executor(),
    config=default_run_config({"plan_cask_changes": CaskChangesConfig}),
)
def showcase_apply_cask_changes_job():
    """Apply bronze_cask_audit_sensor's non-processing changes for one collection.
    Reindexes metadata edits, deletes removed items from ES and silver/gold,
    then refreshes the collection doc.
    """
    settings = plan_cask_changes()
    summary = index_cask_items(settings)
    summary = clean_cask_removed(delete_cask_removed(summary, settings), settings)
    check_reindex_failures(pdc_index_collection(summary))


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
def showcase_delete_collection_index_job():
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
    config=default_run_config({"delete_item_index": DeleteItemIndexConfig}),
)
def showcase_delete_item_index_job():
    """Remove one item from the showcase ES indexes and refresh its collection
    doc. The item returns on the next reindex if it's still in bronze.
    """
    refresh_collection_index(delete_item_index())
