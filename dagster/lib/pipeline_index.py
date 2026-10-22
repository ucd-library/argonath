"""
Showcase index jobs: a bulk collection reindex (with orphan prune), the
audit sensor's per-collection changes (processing included), a collection
delete and an item delete. Each op shells out to digtk through lib.utils.exec().
"""
import json

from dagster import (
    AssetMaterialization, DynamicOut, DynamicOutput, In, MetadataValue, OpExecutionContext, Out, Output, job, op,
)
from dagster_celery import celery_executor

from lib.configs import CaskChangesConfig, DeleteItemIndexConfig, ReindexCollectionConfig, collections_partitions
from lib.fanout_batching import batches, log_batch, log_batching, relative_id, step_key
from lib.pipeline_collection import pdc_index_collection
from lib.pipeline_fanout import (
    _PER_FILE_FORMATS,
    _emit,
    _error_task,
    _run_derivative_task,
    build_item_record,
    fetch_items_for_collection,
    finalize_one_item,
    item_tasks,
)
from lib.utils import default_run_config, exec


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
        _emit(context, "es-prune", orphan, result, partition=key)
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


def _log_changes(context, key, config):
    """One Events entry per changed item, plus a table of all of them.

    :param context: Dagster op execution context.
    :param str key: Collection key.
    :param config: CaskChangesConfig.
    """
    rows = [(relative_id(f, key), "process") for f in config.process]
    rows += [(relative_id(f, key), "reindex") for f in config.reindex]
    rows += [(relative_id(f, key), "delete") for f in config.cleanup]
    for item_id, action in rows:
        _emit(context, "cask_change", item_id, {"action": action}, partition=key, action=action)
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


@op(out={"batches": DynamicOut(list), "settings": Out(dict)})
def fan_out_cask_changes(context: OpExecutionContext, config: CaskChangesConfig):
    """Fan the items to process out into derivative tasks, batched by
    max_steps. Always yields at least one, possibly empty, batch.

    :param context: Dagster op execution context; partitioned by collection.
    :param config: CaskChangesConfig.
    """
    key = context.partition_key
    context.log.info(
        f"{key}: {len(config.process)} to process, {len(config.reindex)} to reindex, "
        f"{len(config.delete)} to delete, {len(config.cleanup)} to clean up"
    )
    _log_changes(context, key, config)
    tasks = []
    for folder in config.process:
        try:
            listing = exec(["digtk", "showcase", "formats", folder, "--files"])
            tasks.extend(item_tasks(folder, listing, key, config))
        except Exception as e:
            item_id = relative_id(folder, key)
            tasks.append(_error_task(folder, item_id, item_id, f"listing formats failed: {e}", key, config))
    grouped = batches(tasks, config.max_steps) or [[]]
    log_batching(context, "derivatives", len(tasks), grouped, config.max_steps)
    used = set()
    for group in grouped:
        yield DynamicOutput(group, output_name="batches", mapping_key=step_key([t["id"] for t in group], used))
    yield Output({
        "process": list(config.process), "reindex": list(config.reindex), "delete": list(config.delete),
        "cleanup": list(config.cleanup), "batch_size": max(config.batch_size, 1), "max_steps": config.max_steps,
    }, output_name="settings")


@op(ins={"tasks": In(list)}, out=Out(list))
def cask_derivative_batch(context: OpExecutionContext, tasks: list) -> list:
    """Run one step's derivative tasks; failures are returned, not raised.

    :param context: Dagster op execution context; partitioned by collection.
    :param list[dict] tasks: fan_out_cask_changes tasks (may be empty).
    :returns: One outcome per task (see pipeline_fanout._run_derivative_task()).
    :rtype: list[dict]
    """
    if tasks:
        log_batch(context, [t["id"] for t in tasks])
    return [_run_derivative_task(context, task) for task in tasks]


@op(out={"items": DynamicOut(list), "summary": Out(dict)})
def fan_out_cask_finalize(context: OpExecutionContext, results: list, settings: dict):
    """Batch every item whose derivative tasks all succeeded for finalize and
    build-item; the rest are reported as failed. Always yields one batch.

    :param context: Dagster op execution context; partitioned by collection.
    :param list[list[dict]] results: Every cask_derivative_batch's outcomes.
    :param dict settings: fan_out_cask_changes's settings.
    """
    by_item = {}
    for outcome in (o for batch in results for o in batch):
        by_item.setdefault(outcome["item_id"], []).append(outcome)

    ready, failed = [], []
    for item_id, outcomes in by_item.items():
        bad = [o for o in outcomes if not o["ok"]]
        if bad:
            context.log.warning(f"[{item_id}] not finalized: {', '.join(o['id'] for o in bad)} failed")
            failed.append(item_id)
            continue
        ready.append({
            "target": outcomes[0]["target"], "id": item_id, "collection_key": outcomes[0]["collection_key"],
            "per_file": any(o["format"] in _PER_FILE_FORMATS for o in outcomes),
        })

    grouped = batches(ready, settings["max_steps"]) or [[]]
    log_batching(context, "finalize", len(ready), grouped, settings["max_steps"])
    used = set()
    for group in grouped:
        yield DynamicOutput(group, output_name="items", mapping_key=step_key([t["id"] for t in group], used))
    yield Output({"failed": failed}, output_name="summary")


@op(ins={"tasks": In(list)}, out=Out(dict))
def cask_finalize_batch(context: OpExecutionContext, tasks: list) -> dict:
    """Finalize (image/PDF items) and build-item each item, one at a time.
    Failures are returned, not raised.

    :param context: Dagster op execution context; partitioned by collection.
    :param list[dict] tasks: fan_out_cask_finalize tasks (may be empty).
    :returns: {"built": [bronze folder], "failed": [item id]}.
    :rtype: dict
    """
    built, failed = [], []
    for t in tasks:
        if t["per_file"] and finalize_one_item(context, t["target"], t["id"], t["collection_key"]) is not None:
            failed.append(t["id"])
            continue
        error, _ = build_item_record(context, t["target"], t["id"], t["collection_key"])
        if error is not None:
            failed.append(t["id"])
            continue
        built.append(t["target"])
    return {"built": built, "failed": failed}


@op
def index_cask_items(context: OpExecutionContext, finalized: list, summary: dict, settings: dict) -> dict:
    """Index processed items from their fresh gold records and rebuild then
    index metadata-only edits (`digtk showcase es upsert-items`), in chunks.

    :param context: Dagster op execution context; partitioned by collection.
    :param list[dict] finalized: Every cask_finalize_batch result.
    :param dict summary: fan_out_cask_finalize's failed items.
    :param dict settings: fan_out_cask_changes's settings.
    :returns: {"items", "indexed", "skipped", "failed"}.
    :rtype: dict
    """
    key = context.partition_key
    built = [f for r in finalized for f in r["built"]]
    result = {
        "items": len(settings["process"]) + len(settings["reindex"]),
        "indexed": 0, "skipped": 0,
        "failed": list(summary["failed"]) + [i for r in finalized for i in r["failed"]],
    }
    size = settings["batch_size"]
    work = [(built[i:i + size], False) for i in range(0, len(built), size)]
    work += [(settings["reindex"][i:i + size], True) for i in range(0, len(settings["reindex"]), size)]
    for chunk, rebuild in work:
        ids = [relative_id(f, key) for f in chunk]
        batch = _index_folders(context, {"folders": chunk, "ids": ids, "rebuild": rebuild, "collection_key": key})
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
    :param dict settings: fan_out_cask_changes's delete list.
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
        _emit(context, "es-delete", entry["ark"], entry, partition=key)
    if result.get("errors"):
        return {**summary, "delete_error": json.dumps(result["errors"])}
    return {**summary, "deleted": result.get("items", 0)}


@op
def clean_cask_removed(context: OpExecutionContext, summary: dict, settings: dict) -> dict:
    """Delete removed items' silver and gold directories (`digtk showcase
    clean-item`), logging each one. Skipped if the ES delete failed.

    :param context: Dagster op execution context; partitioned by collection.
    :param dict summary: delete_cask_removed's summary.
    :param dict settings: fan_out_cask_changes's cleanup list.
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
        _emit(
            context, "cleanup", relative_id(entry["folder"], key), entry, partition=key,
            path=entry["path"], files=entry["files"],
        )
    for folder in result.get("kept", []):
        context.log.warning(f"[{relative_id(folder, key)}] back in bronze, silver/gold kept")
    return {**summary, "cleaned": len(result.get("removed", []))}


@job(
    partitions_def=collections_partitions,
    tags={"group": "fanout", "argonath/kind": "cask-changes", "dagster/max_runtime": "0"},
    executor_def=celery_executor,
    config=default_run_config({"fan_out_cask_changes": CaskChangesConfig}),
)
def apply_cask_changes_job():
    """Apply bronze_cask_audit_sensor's changes for one collection: process
    new or changed items, reindex metadata edits, delete removed items from ES
    and their silver/gold, then refresh the collection document.
    """
    derivative_batches, settings = fan_out_cask_changes()
    outcomes = derivative_batches.map(cask_derivative_batch).collect()
    items, failed = fan_out_cask_finalize(outcomes, settings)
    finalized = items.map(cask_finalize_batch).collect()
    summary = index_cask_items(finalized, failed, settings)
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
