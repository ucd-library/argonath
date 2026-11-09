"""
Collection-level orchestration of item runs, through the collection_state
queue (see lib.collection_state and digtk.models.collection_state).
bronze_collection_cask_audit (changed items) and gold_dcs_process_collection
(every item, launched by hand) queue items, item_queue runs them as
gold_dcs_process_item, and gold_dcs_collection_rollup_trigger rolls each collection up.

  gold_dcs_process_collection (collection partition)
      -> `digtk state enqueue-collection`: one job_queue row per item
  item_queue
      -> one gold_dcs_process_item run per queued row, at most
         MAX_ITEM_RUNS_IN_FLIGHT item runs at a time
  gold_dcs_collection_rollup_trigger (queue empty, no item runs in flight)
      -> gold_dcs_collection_rollup: `digtk state rollup`, then the collection's
         gold record and ES doc; fails if any item failed

The queue is read without being claimed: item_queue stores submitted
row ids in its cursor and the next tick deletes them (`digtk state ack`).
A row submitted twice is dropped by Dagster's run_key ("job_queue-<id>").
"""
import json
import os

import dagster as dg
from dagster import AssetMaterialization, MetadataValue, OpExecutionContext, job, op

from lib import collection_state
from lib.configs import ItemRunConfig, ProcessCollectionConfig, collections_partitions
from lib.pipeline_item import normalize_item, gold_dcs_process_item
from lib.utils import default_run_config, exec

# Most gold_dcs_process_item runs (queued or running, from any trigger) the queue
# sensor lets exist at once. It only submits more while there's room, so the
# rest wait in collection_state.job_queue rather than in Dagster's run queue.
MAX_ITEM_RUNS_IN_FLIGHT = int(os.getenv("ARGONATH_MAX_ITEM_RUNS_IN_FLIGHT", "20"))

# Most rows one queue sensor tick submits, whatever the free capacity.
MAX_RUNS_PER_TICK = 50

_IN_FLIGHT = [
    dg.DagsterRunStatus.QUEUED,
    dg.DagsterRunStatus.NOT_STARTED,
    dg.DagsterRunStatus.STARTING,
    dg.DagsterRunStatus.STARTED,
]


@op
def enqueue_collection_items(context: OpExecutionContext, config: ProcessCollectionConfig):
    """Queue a gold_dcs_process_item run for every item in the partition's
    collection, tagged with this run's id as its batch. Items that have left
    its bronze tree/ are left alone; gold_dcs_reindex_collection prunes their ES docs.

    :param context: Dagster op execution context; partitioned by collection.
    :param ProcessCollectionConfig config: 'force' regenerates existing derivatives.
    """
    key = context.partition_key
    result = collection_state.enqueue_collection(key, {"batch": context.run_id, "force": config.force})
    context.log.info(
        f"{key}: {result['items']} item(s) in bronze tree/ ({result['added']} new), "
        f"{result['enqueued']} run(s) queued{' (force)' if config.force else ''}"
    )
    context.log.info("Items processing will be queued as new dagster runs per-item, followed by a collection rollup job to index the collection and update the item count.")
    context.log_event(AssetMaterialization(
        asset_key="argonath_process_collection",
        partition=key,
        metadata={
            "items": MetadataValue.int(result["items"]),
            "added": MetadataValue.int(result["added"]),
            "enqueued": MetadataValue.int(result["enqueued"]),
        },
    ))


@job(
    partitions_def=collections_partitions,
    tags={"argonath/kind": "collection-launch"},
    config=default_run_config({"enqueue_collection_items": ProcessCollectionConfig}),
)
def gold_dcs_process_collection():
    """Manually rebuilds derivatives and indexes all items and the collection in elastic, 
    then updates the item count by triggering the `gold_dcs_collection_rollup` job.
    Derivatives that already exist are skipped unless `force` is set on the launchpad.
    This is equivalent to what the `bronze_collection_cask_audit` sensor does automatically for changes in bronze cask.
    If you just need to reindex a collection, use `gold_dcs_reindex_collection` instead.
    """
    enqueue_collection_items()


def _item_run_request(row):
    """A gold_dcs_process_item RunRequest for one job_queue row.

    :param dict row: collection_state.pending() row ({"id", "item", "config"}).
    :returns: RunRequest keyed "job_queue-<id>".
    :rtype: dagster.RunRequest
    """
    queued = row["config"] or {}
    item, key = normalize_item(queued.get("item") or row["item"])
    run_config = default_run_config({"pdc_plan_item": ItemRunConfig})
    plan_config = run_config["ops"]["pdc_plan_item"]["config"]
    plan_config.update({k: v for k, v in queued.items() if k in ItemRunConfig.model_fields})
    plan_config["item"] = item
    tags = {
        "argonath/item": item,
        "argonath/collection": key,
        "argonath/trigger": queued.get("trigger", "queue"),
    }
    if queued.get("batch"):
        tags["argonath/batch"] = queued["batch"]
    return dg.RunRequest(run_key=f"job_queue-{row['id']}", run_config=run_config, tags=tags)


@dg.sensor(
    job=gold_dcs_process_item, 
    minimum_interval_seconds=30, 
    description=(
        "Listens to the Postgres `collection_state.job_queue` table and launches `gold_dcs_process_item` runs for queued items, "
        "keeping at most MAX_ITEM_RUNS_IN_FLIGHT item runs queued or running. "
    ),
)
def item_queue(context: dg.SensorEvaluationContext):
    """Launch gold_dcs_process_item runs from collection_state's queue, keeping at
    most MAX_ITEM_RUNS_IN_FLIGHT item runs queued or running. Rows submitted by
    one tick are removed from the queue by the next (see this module's docstring).

    :param context: Dagster sensor evaluation context.
    """
    submitted = json.loads(context.cursor)["submitted"] if context.cursor else []
    if submitted:
        collection_state.ack(submitted)
        context.update_cursor(json.dumps({"submitted": []}))

    in_flight = context.instance.get_runs_count(
        dg.RunsFilter(tags={"argonath/kind": "item"}, statuses=_IN_FLIGHT),
    )
    capacity = min(MAX_ITEM_RUNS_IN_FLIGHT - in_flight, MAX_RUNS_PER_TICK)
    if capacity <= 0:
        return dg.SkipReason(f"{in_flight} item run(s) in flight (max {MAX_ITEM_RUNS_IN_FLIGHT})")

    rows = collection_state.pending(collection_state.ITEM_JOB, capacity)
    if not rows:
        return dg.SkipReason(f"queue empty ({in_flight} item run(s) in flight)")

    requests = []
    for row in rows:
        try:
            requests.append(_item_run_request(row))
        except ValueError as e:
            # A row that can't become a run is dropped (acked next tick) rather
            # than blocking the queue forever.
            context.log.error(f"job_queue row {row['id']} skipped: {e}")
    context.update_cursor(json.dumps({"submitted": [row["id"] for row in rows]}))
    return requests


@op
def rollup_collection(context: OpExecutionContext) -> dict:
    """Recompute the partition's collection item count and failed items, and
    record them in collection_state.

    :param context: Dagster op execution context; partitioned by collection.
    :returns: collection_state.rollup()'s summary.
    :rtype: dict
    """
    key = context.partition_key
    summary = collection_state.rollup(key)
    context.log.info(
        f"{key}: {summary['item_count']} item(s): {summary['succeeded']} succeeded, "
        f"{summary['failed']} failed, {summary['never_run']} not run"
    )
    failed = summary["failed_items"]
    for f in failed:
        context.log.error(f"[{f['item']}] failed (run {f['run_id']}): {f['error']}")

    rows = "\n".join(f"| {f['item']} | {f['run_id']} | {(f['error'] or '').replace('|', '/')} |" for f in failed)
    context.log_event(AssetMaterialization(
        asset_key="argonath_collection_rollup",
        partition=key,
        metadata={
            "item_count": MetadataValue.int(summary["item_count"]),
            "succeeded": MetadataValue.int(summary["succeeded"]),
            "failed": MetadataValue.int(summary["failed"]),
            "never_run": MetadataValue.int(summary["never_run"]),
            "failed_items": MetadataValue.md(f"| item | run | error |\n|---|---|---|\n{rows}" if failed else "none"),
        },
    ))
    return summary


@op
def pdc_index_collection(context: OpExecutionContext, summary: dict) -> dict:
    """Build the collection's gold record and index it into the showcase
    collection index. Runs even if some items failed.

    :param context: Dagster op execution context; partitioned by collection.
    :param dict summary: rollup_collection's summary, passed through.
    :returns: The same summary.
    :rtype: dict
    """
    key = context.partition_key
    exec(["digtk", "showcase", "build-collection", key])
    result = exec(["digtk", "showcase", "es", "upsert-collection", key])
    context.log.info(f"{key}: indexed collection -> {result}")
    return summary


@op
def check_rollup_failures(summary: dict):
    """Fail the run if any item failed, so a partial collection isn't green.

    :param dict summary: rollup_collection's summary.
    :raises RuntimeError: If any item's latest run failed.
    """
    if summary["failed"]:
        raise RuntimeError(f"{summary['failed']} of {summary['item_count']} item(s) failed — see the errors above")


@job(partitions_def=collections_partitions, tags={"argonath/kind": "collection-rollup"})
def gold_dcs_collection_rollup():
    """Called by the `gold_dcs_collection_rollup_trigger` sensor after all items for a collection have finished processing.
    Updates elastic with the collection gold document, and updates the item count.
    """
    check_rollup_failures(pdc_index_collection(rollup_collection()))


@dg.sensor(
    job=gold_dcs_collection_rollup, 
    minimum_interval_seconds=300, 
    description=(
        "Listens for all items in a collection to finish processing, then indexes the collections' gold record and recalculates the item count. "
        "This runs as part of the `bronze_collection_cask_audit` sensor chain, after all item runs are complete and the queue is empty. "
    ),
)
def gold_dcs_collection_rollup_trigger(context: dg.SensorEvaluationContext):
    """Launch gold_dcs_collection_rollup for every collection whose items have
    results newer than its last rollup, once nothing is queued for it and none
    of its item runs are queued or running. Keyed by the newest item result, so
    the same results never roll up twice.

    :param context: Dagster sensor evaluation context.
    """
    requests = []
    for due in collection_state.rollups_due():
        key = due["collection"]
        in_flight = context.instance.get_runs_count(
            dg.RunsFilter(tags={"argonath/kind": "item", "argonath/collection": key}, statuses=_IN_FLIGHT),
        )
        if in_flight:
            continue
        if not context.instance.has_dynamic_partition(collections_partitions.name, key):
            context.instance.add_dynamic_partitions(collections_partitions.name, [key])
        requests.append(dg.RunRequest(partition_key=key, run_key=f"rollup-{key}-{due['latest_result_at']}"))
    if not requests:
        return dg.SkipReason("no collection rollups due")
    return requests
