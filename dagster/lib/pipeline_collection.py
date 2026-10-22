"""
Collection-level orchestration of item runs, through the collection_state
queue (see lib.collection_state and digtk.models.collection_state).

Not wired up: collection_launch_job, item_queue_sensor and
collection_rollup_sensor aren't registered in defs.py, since
bronze_cask_audit_sensor drives bronze changes now. collection_rollup_job
can still be launched by hand. The original design:

  collection_launch_job (collection partition)
      -> `digtk state enqueue-collection`: one job_queue row per item in the
         collection's bronze tree/
  item_queue_sensor
      -> one process_item_job run per queued row, at most
         MAX_ITEM_RUNS_IN_FLIGHT item runs at a time
  collection_rollup_sensor (once a collection's queue is empty and none of its
  item runs are still going)
      -> collection_rollup_job (collection partition): `digtk state rollup` —
         the collection's item count and failed items, recorded in
         collection_state; then `digtk showcase build-collection` and
         `es upsert-collection` index the collection; the run fails if any
         item failed, listing them

Unlike lib.pipeline_fanout.process_collection_job (one run doing every item of
a collection), every item gets its own run here, retryable on its own, and the
same queue can take items from other producers later (e.g. Preservica change
polling — see docs/item-fanout-handoff.md).

The queue is read without being claimed: item_queue_sensor submits runs for
the oldest rows and stores their ids in its cursor, and the NEXT tick deletes
them (`digtk state ack`). Dagster saves a tick's cursor together with its run
requests, so a tick that crashes before submitting leaves its rows queued, and
a row submitted twice is dropped by Dagster because its run_key
("job_queue-<id>") was already used.
"""
import json
import os

import dagster as dg
from dagster import AssetMaterialization, MetadataValue, OpExecutionContext, job, op

from lib import collection_state
from lib.configs import ItemRunConfig, collections_partitions
from lib.pipeline_item import normalize_item, process_item_job
from lib.utils import default_run_config, exec

# Most process_item_job runs (queued or running, from any trigger) the queue
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
def enqueue_collection_items(context: OpExecutionContext):
    """Queue a process_item_job run for every item in the partition's
    collection. Items that have left its bronze tree/ are left alone here;
    reindex_collection_job prunes their ES docs.

    :param context: Dagster op execution context; partitioned by collection.
    """
    key = context.partition_key
    result = collection_state.enqueue_collection(key, {"batch": context.run_id})
    context.log.info(
        f"{key}: {result['items']} item(s) in bronze tree/ ({result['added']} new), "
        f"{result['enqueued']} run(s) queued"
    )
    context.log_event(AssetMaterialization(
        asset_key="argonath_collection_launch",
        partition=key,
        metadata={
            "items": MetadataValue.int(result["items"]),
            "added": MetadataValue.int(result["added"]),
            "enqueued": MetadataValue.int(result["enqueued"]),
        },
    ))


@job(partitions_def=collections_partitions, tags={"argonath/kind": "collection-launch"})
def collection_launch_job():
    """Queue one process_item_job run per item of a collection;
    item_queue_sensor launches them. Processing always replaces existing derivatives for now. See this
    module's docstring.
    """
    enqueue_collection_items()


def _item_run_request(row):
    """A process_item_job RunRequest for one job_queue row.

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


@dg.sensor(job=process_item_job, minimum_interval_seconds=30)
def item_queue_sensor(context: dg.SensorEvaluationContext):
    """Launch process_item_job runs from collection_state's queue, keeping at
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
def collection_rollup_job():
    """A collection's rollup after its item runs: item count and failed items,
    recorded in collection_state, then the collection's ES document. Launched
    by collection_rollup_sensor.
    """
    check_rollup_failures(pdc_index_collection(rollup_collection()))


@dg.sensor(job=collection_rollup_job, minimum_interval_seconds=300)
def collection_rollup_sensor(context: dg.SensorEvaluationContext):
    """Launch collection_rollup_job for every collection whose items have
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
