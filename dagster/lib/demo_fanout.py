"""
Demo of the single-partition / op-fanout pattern discussed for large
collections: one partition per collection, items within it fanned out via
DynamicOut, each running through a bronze -> workbench -> silver op chain.

Registered in defs.py tagged group="demo" — it is NOT part of the real
pipeline (see lib/assets.py for that: an asset-based, MultiPartitionsDefinition
design). All data here is static/fake, so no CaskFS or digtk dependency; it
does run through the celery executor against this deployment's real broker
(see celery_config.yaml), matching how the real pipeline jobs will execute.

In the UI: Deployment > Jobs > argonath_demo_fanout_job > Launchpad, pick a
partition (one of the 3 collections below) and launch a run. A couple of
items are seeded to fail at a fixed stage so the failure path is visible
without relying on real randomness.
"""
import hashlib
import os
import time

import yaml
from dagster import (
    Config,
    DynamicOut,
    DynamicOutput,
    In,
    Out,
    OpExecutionContext,
    AssetMaterialization,
    MetadataValue,
    StaticPartitionsDefinition,
    job,
    op,
)
from dagster_celery import celery_executor


def _load_celery_run_config():
    """Load celery_config.yaml as this job's default run config.

    Without this, a run launched from Dagit with no run config falls back to
    celery_executor's built-in default broker (pyamqp://guest@localhost//)
    instead of this deployment's actual broker — connection refused, since
    nothing is listening on localhost:5672 wherever the run worker executes.

    :returns: Parsed run config dict (the `execution:` block).
    :rtype: dict
    """
    path = os.path.join(os.path.dirname(__file__), "..", "celery_config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Static demo data — 3 collections, 40+ items each, no source-system lookup.
# ---------------------------------------------------------------------------

COLLECTION_ITEM_COUNTS = {
    "yolo-county-photographs": 44,
    "shields-library-maps": 42,
    "aggie-yearbooks": 46,
}

collection_partitions_def = StaticPartitionsDefinition(list(COLLECTION_ITEM_COUNTS.keys()))

# Seeded (not random) so the failure path is reproducible run to run: maps
# collection -> item_id -> the stage that item is forced to fail at.
FAILING_ITEMS = {
    "yolo-county-photographs": {
        "yolo_county_photographs_0007": "bronze_extract",
        "yolo_county_photographs_0031": "workbench_transform",
    },
    "shields-library-maps": {
        "shields_library_maps_0012": "silver_normalize",
    },
    "aggie-yearbooks": {
        "aggie_yearbooks_0003": "bronze_extract",
        "aggie_yearbooks_0040": "silver_normalize",
    },
}


def fetch_items_for_collection(collection_id):
    """Stand-in for the real source-system item lookup.

    Item ids use underscores rather than the collection key's hyphens because
    they double as DynamicOutput mapping keys, which Dagster restricts to
    ``^[A-Za-z0-9_]+$``.

    :param str collection_id: Partition key (collection identifier).
    :returns: Static item_id list for the collection.
    :rtype: list[str]
    """
    count = COLLECTION_ITEM_COUNTS[collection_id]
    prefix = collection_id.replace("-", "_")
    return [f"{prefix}_{i:04d}" for i in range(1, count + 1)]


class ReprocessConfig(Config):
    """Config for fan_out_items.

    :param list[str] item_ids: Item ids to reprocess; empty means the whole collection.
    """
    item_ids: list[str] = []


@op(out=DynamicOut(str))
def fan_out_items(context: OpExecutionContext, config: ReprocessConfig):
    """Fan out one DynamicOutput per item in the partition's collection.

    :param context: Dagster op execution context; partitioned by collection.
    :param config: ReprocessConfig — optional subset of item_ids to reprocess.
    """
    collection_id = context.partition_key
    all_items = fetch_items_for_collection(collection_id)
    targets = [i for i in all_items if i in config.item_ids] if config.item_ids else all_items

    context.log.info(f"{collection_id}: fanning out {len(targets)} items")
    for item_id in targets:
        yield DynamicOutput(item_id, mapping_key=item_id)


def _emit(context, stage, item_id, ref, status="success", **extra):
    """Log an AssetMaterialization carrying per-item stage metadata for traceability.

    :param context: Dagster op execution context.
    :param str stage: Pipeline stage name (bronze, workbench, silver).
    :param str item_id: Item identifier (the op's mapping key).
    :param str ref: Content ref/hash produced by the stage; "" on failure.
    :param str status: "success" or "failed".
    :param extra: Additional string-coercible metadata (e.g. error=...).
    """
    metadata = {
        "item_id": MetadataValue.text(item_id),
        "stage": MetadataValue.text(stage),
        "content_ref": MetadataValue.text(ref),
        "status": MetadataValue.text(status),
        **{k: MetadataValue.text(str(v)) for k, v in extra.items()},
    }
    if status == "failed":
        context.log.error(f"[{item_id}] {stage}: FAILED -> {extra.get('error')}")
    else:
        context.log.info(f"[{item_id}] {stage}: ok -> {ref}")
    context.log_event(AssetMaterialization(
        asset_key=f"argonath_{stage}",
        partition=context.partition_key,
        metadata=metadata,
    ))


def _maybe_fail(context, stage, item_id):
    """Raise if this (item_id, stage) pair is a seeded demo failure.

    :param context: Dagster op execution context.
    :param str stage: Current stage name.
    :param str item_id: Current item id.
    :raises RuntimeError: If this item/stage combination is in FAILING_ITEMS.
    """
    forced_stage = FAILING_ITEMS.get(context.partition_key, {}).get(item_id)
    if forced_stage == stage:
        raise RuntimeError(f"simulated {stage} failure for {item_id}")


def _fake_ref(item_id, stage):
    """Deterministic fake content hash standing in for a real CaskFS write.

    :param str item_id: Item identifier.
    :param str stage: Stage name, mixed into the hash so refs differ per stage.
    :returns: sha256-prefixed hash string.
    :rtype: str
    """
    digest = hashlib.sha256(f"{item_id}:{stage}".encode()).hexdigest()[:16]
    return f"sha256:{digest}"


@op(ins={"item_id": In(str)}, out=Out(str))
def bronze_extract(context: OpExecutionContext, item_id: str) -> str:
    """Simulate raw extraction into bronze for one item.

    :param context: Dagster op execution context.
    :param str item_id: Item to extract (the mapping key).
    :returns: Fake content ref for the bronze artifact.
    """
    context.log.info(f"[{item_id}] bronze_extract: start")
    time.sleep(0.05)
    try:
        _maybe_fail(context, "bronze_extract", item_id)
        ref = _fake_ref(item_id, "bronze")
        _emit(context, "bronze", item_id, ref)
        return ref
    except Exception as e:
        _emit(context, "bronze", item_id, "", status="failed", error=str(e))
        raise


@op(ins={"bronze_ref": In(str)}, out=Out(str))
def workbench_transform(context: OpExecutionContext, bronze_ref: str) -> str:
    """Simulate transforming a bronze artifact in the workbench stage.

    item_id isn't passed through the .map() chain beyond the mapping key, so
    it's recovered via context.get_mapping_key() rather than threaded explicitly.

    :param context: Dagster op execution context.
    :param str bronze_ref: Content ref produced by bronze_extract.
    :returns: Fake content ref for the workbench artifact.
    """
    item_id = context.get_mapping_key()
    context.log.info(f"[{item_id}] workbench_transform: start")
    time.sleep(0.05)
    try:
        _maybe_fail(context, "workbench_transform", item_id)
        ref = _fake_ref(item_id, "workbench")
        _emit(context, "workbench", item_id, ref)
        return ref
    except Exception as e:
        _emit(context, "workbench", item_id, "", status="failed", error=str(e))
        raise


@op(ins={"workbench_ref": In(str)}, out=Out(str))
def silver_normalize(context: OpExecutionContext, workbench_ref: str) -> str:
    """Simulate normalizing a workbench artifact into silver.

    :param context: Dagster op execution context.
    :param str workbench_ref: Content ref produced by workbench_transform.
    :returns: Fake content ref for the silver artifact.
    """
    item_id = context.get_mapping_key()
    context.log.info(f"[{item_id}] silver_normalize: start")
    time.sleep(0.05)
    try:
        _maybe_fail(context, "silver_normalize", item_id)
        ref = _fake_ref(item_id, "silver")
        _emit(context, "silver", item_id, ref)
        return ref
    except Exception as e:
        _emit(context, "silver", item_id, "", status="failed", error=str(e))
        raise


@job(
    partitions_def=collection_partitions_def,
    tags={"group": "demo"},
    executor_def=celery_executor,
    config=_load_celery_run_config(),
)
def argonath_demo_fanout_job():
    """Single-partition (collection) fan-out demo: bronze -> workbench -> silver per item.

    Uses the celery executor (see celery_config.yaml, loaded as this job's
    default run config by _load_celery_run_config) so each mapped item/stage
    runs as its own celery task on a worker, same as the rest of this
    deployment.

    All progress is logged via context.log rather than print(): structured
    logs are written to the run/event storage (Postgres, per dagster.yaml),
    which every pod's Dagit reads from — whereas raw stdout is captured by
    LocalComputeLogManager to local disk on whatever pod actually ran the
    step. With celery, that's a worker pod, not the webserver pod serving the
    UI, so without a compute_logs backend shared across pods, print() output
    shows up as "No logs available" in Dagit even though it's really sitting
    in that worker's container logs.
    """
    fan_out_items().map(bronze_extract).map(workbench_transform).map(silver_normalize)