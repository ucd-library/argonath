"""
Collection -> item fan-out pipeline for real pipeline work (image
derivatives, then OCR), using the op-based DynamicOut pattern demonstrated in
lib/demo_fanout.py: one partition per COLLECTION (lib.configs.collections_partitions
-- the same dynamic partition set reconcile_collection_items registers into),
fanning out to every item inside that single run via .map(), instead of
giving every (collection, item) pair its own Dagster partition.

Distinct from lib/assets.py's MultiPartitionsDefinition-based per-item assets
(items_partitions): those give large collections thousands of partitions,
which is precise for backfill/lineage but expensive to browse -- exactly the
concern for a Preservica collection with hundreds of pages. This job trades
that per-item partition granularity for one run per collection, with per-item
progress tracked instead via AssetMaterialization events (see _emit() below)
and digtk.lib.reporting rows (see each op's exec() call, which passes this
run's own id as --reporting-job-id) -- not via Dagster partition status. Which
design a given phase should use is a per-phase choice, not an all-or-nothing
migration; this module only covers derivatives + OCR, matching what was
actually asked for.

Each per-item op shells out to a digtk command via lib.utils.exec(), the same
bridge lib/assets.py's per-item assets use.
"""
from dagster import (
    AssetMaterialization,
    DynamicOut,
    DynamicOutput,
    In,
    MetadataValue,
    Out,
    OpExecutionContext,
    job,
    op,
)
from dagster_celery import celery_executor

from lib.configs import collections_partitions, DerivativesConfig, OcrConfig
from lib.utils import exec, load_celery_run_config


def fetch_items_for_collection(collection_key):
    """Look up every current item ARK in a collection's tree/ layout.

    Shells out to `digtk collection items` (see
    digtk.models.collection.list_item_arks()) rather than reading CaskFS
    directly -- same exec()-bridge convention every other Dagster/digtk
    interaction in this codebase uses. `--collection` only: collection names
    are unique among active registrations regardless of group (see
    register_collection()'s collection_registry_active_name_uq), so the group
    half of collection_key is only needed for this job's own partition
    key/logging, not for the digtk call itself.

    :param str collection_key: "[group]/[collection]" partition key.
    :returns: Item ARKs currently in the collection.
    :rtype: list[str]
    """
    _, collection = collection_key.split("/", 1)
    result = exec(["digtk", "collection", "items", "--collection", collection])
    return result["arks"]


def _mapping_key(ark):
    """DynamicOutput mapping keys are restricted to ``^[A-Za-z0-9_]+$`` (see
    lib/demo_fanout.py) -- an ARK like 'ark:/87287/d7abc1234' isn't a valid
    one as-is.

    :param str ark: Item ARK.
    :returns: ARK with ':' and '/' replaced by '_'.
    :rtype: str
    """
    return ark.replace(":", "_").replace("/", "_")


@op(out=DynamicOut(str))
def fan_out_items(context: OpExecutionContext):
    """Fan out one DynamicOutput (the item ARK) per item in the partition's collection.

    :param context: Dagster op execution context; partitioned by collection.
    """
    collection_key = context.partition_key
    items = fetch_items_for_collection(collection_key)

    context.log.info(f"{collection_key}: fanning out {len(items)} items")
    for ark in items:
        yield DynamicOutput(ark, mapping_key=_mapping_key(ark))


def _emit(context, stage, ark, result, status="success", **extra):
    """Log an AssetMaterialization carrying per-item stage metadata for
    traceability -- the per-item progress signal this job uses in place of
    Dagster partition status (see module docstring). The authoritative
    success/failure record is digtk.lib.reporting's Postgres row, written by
    the digtk command itself; this is a Dagster-UI-visible complement to it.

    :param context: Dagster op execution context.
    :param str stage: Pipeline stage name ("derivatives", "ocr").
    :param str ark: Item ARK.
    :param result: digtk's parsed JSON result, or None on failure.
    :param str status: "success" or "failed".
    :param extra: Additional string-coercible metadata (e.g. error=...).
    """
    metadata = {
        "item_id": MetadataValue.text(ark),
        "stage": MetadataValue.text(stage),
        "status": MetadataValue.text(status),
        **{k: MetadataValue.text(str(v)) for k, v in extra.items()},
    }
    if status == "failed":
        context.log.error(f"[{ark}] {stage}: FAILED -> {extra.get('error')}")
    else:
        context.log.info(f"[{ark}] {stage}: ok -> {result}")
    context.log_event(AssetMaterialization(
        asset_key=f"argonath_fanout_{stage}",
        partition=context.partition_key,
        metadata=metadata,
    ))


@op(ins={"ark": In(str)}, out=Out(str))
def process_derivatives(context: OpExecutionContext, config: DerivativesConfig, ark: str) -> str:
    """Generate the full image-derivative set for one fanned-out item.

    Same command lib.assets.generate_derivatives calls (`digtk showcase image
    derivatives`) -- invoked per mapped item within this job's single
    collection-scoped run instead of as a separately-partitioned asset. Passes
    this run's own id as --reporting-job-id so the resulting reporting.command
    row (see digtk.lib.reporting) traces back to this exact Dagster run.
    Shares lib.configs.DerivativesConfig with generate_derivatives rather than
    a separate config class, so a config change (a new width, a new tuning
    knob) doesn't need to be kept in sync by hand across both designs.

    :param context: Dagster op execution context; partitioned by collection.
    :param config: DerivativesConfig with thumbnail widths and target gold app.
    :param str ark: Item ARK (this op's mapping key).
    :returns: The same ark, threaded to process_ocr.
    """
    collection_key = context.partition_key
    try:
        result = exec([
            "digtk", "showcase", "image", "derivatives", ark,
            "--widths", config.widths,
            "--app", config.app,
            "--reporting-job-id", context.run_id,
            "--reporting-collection", collection_key,
        ])
        _emit(context, "derivatives", ark, result)
        return ark
    except Exception as e:
        _emit(context, "derivatives", ark, None, status="failed", error=str(e))
        raise


@op(ins={"ark": In(str)}, out=Out(str))
def process_ocr(context: OpExecutionContext, config: OcrConfig, ark: str) -> str:
    """Run OCR (Tesseract + Qwen vision) for one fanned-out item.

    Same command lib.assets.run_ocr calls (`digtk ocr tesseract`), reading
    back the derivative process_derivatives just published. Shares
    lib.configs.OcrConfig with run_ocr for the same reason process_derivatives
    shares DerivativesConfig — see its docstring.

    :param context: Dagster op execution context; partitioned by collection.
    :param config: OcrConfig with lang, psm, oem, vision.
    :param str ark: Item ARK (this op's mapping key).
    :returns: The same ark.
    """
    collection_key = context.partition_key
    cmd = [
        "digtk", "ocr", "tesseract", ark,
        "--lang", config.lang,
        "--psm", str(config.psm),
        "--oem", str(config.oem),
        "--reporting-job-id", context.run_id,
        "--reporting-collection", collection_key,
    ]
    if not config.vision:
        cmd.append("--no-vision")
    try:
        result = exec(cmd)
        _emit(context, "ocr", ark, result)
        return ark
    except Exception as e:
        _emit(context, "ocr", ark, None, status="failed", error=str(e))
        raise


@job(
    partitions_def=collections_partitions,
    # "dagster/max_runtime": "0" disables run-monitoring's duration-based
    # auto-cancel for this job specifically -- project-anduin's base image
    # sets a global run_monitoring.max_runtime_seconds: 600 in its own
    # $DAGSTER_HOME/dagster.yaml (not this repo's dagster/dagster.yaml, which
    # isn't actually read as instance config), sized for quick per-item jobs.
    # A run here covers a whole collection's worth of items/pages, so its
    # duration scales with collection size and has no sane fixed ceiling --
    # 0 means "no timeout", matching run_monitoring_max_runtime_seconds's own
    # global-disable convention (see dagster's settings_methods.py). This
    # doesn't disable dead-run detection: check_run_worker_health (are the
    # run process/celery workers still alive) is a separate mechanism and
    # still applies regardless of this tag.
    tags={"group": "fanout", "dagster/max_runtime": "0"},
    executor_def=celery_executor,
    config=load_celery_run_config(),
)
def process_collection_job():
    """Collection-partitioned fan-out: generate derivatives, then run OCR,
    for every item in the partition's collection -- one Dagster run per
    collection, each mapped item/stage its own celery task. See this module's
    docstring for how this differs from lib.jobs.derivatives_job/ocr_job (the
    MultiPartitionsDefinition, per-item-partition design covering the same
    two phases today).
    """
    fan_out_items().map(process_derivatives).map(process_ocr)
