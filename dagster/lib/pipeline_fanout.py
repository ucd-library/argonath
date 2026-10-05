"""
Collection -> item fan-out pipeline for real pipeline work (per-format
showcase derivatives), using the op-based DynamicOut pattern demonstrated in
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
migration; this module only covers derivatives (the `digtk showcase`
commands the webapp relies on). OCR runs separately via lib.jobs.ocr_job.

Derivatives run one step per IMAGE and per PDF file, not per item: each step
is `digtk showcase image|pdf derivatives <ark> --file <bronze path>`, its own
celery task, retryable on its own. Those per-file runs skip the outputs that
combine every file of an item (an image item's ocr/manifest.json, the item
sidecar's associatedMedia), so once every step is done a per-item
`digtk showcase finalize <ark>` step writes them from what the per-file steps
published (see digtk.models.digital_website.finalize). Video and audio stay one
step per item. Dagster can't nest fan-outs, so the graph is flat:
fan_out_derivative_tasks -> process_derivative_task (one per image/PDF, or per
video/audio item) -> collect -> fan_out_finalize -> finalize_item (one per
item with images/PDFs). An item with a failed file step isn't finalized, and
check_derivative_failures fails the run so a partial run isn't green.

Each op shells out to a digtk command via lib.utils.exec(), the same
bridge lib/assets.py's per-item assets use.
"""
import re

from dagster import (
    AssetMaterialization,
    DynamicOut,
    DynamicOutput,
    In,
    MetadataValue,
    Out,
    OpExecutionContext,
    Output,
    job,
    op,
)
from dagster_celery import celery_executor

from lib.configs import collections_partitions, DerivativesConfig
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

    :param str collection_key: "bronze/[group]/[collection]" partition key (an
        older "[group]/[collection]" key works too — only the last segment is used).
    :returns: Item ARKs currently in the collection.
    :rtype: list[str]
    """
    collection = collection_key.rstrip("/").rsplit("/", 1)[-1]
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


def _emit(context, stage, ark, result, status="success", **extra):
    """Log an AssetMaterialization carrying per-item stage metadata for
    traceability -- the per-item progress signal this job uses in place of
    Dagster partition status (see module docstring). The authoritative
    success/failure record is digtk.lib.reporting's Postgres row, written by
    the digtk command itself; this is a Dagster-UI-visible complement to it.

    :param context: Dagster op execution context.
    :param str stage: Pipeline stage name (e.g. "derivatives").
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


# Formats generated one step per file (and finalized per item afterwards);
# every other format an item has (video, audio) is still one step per item.
_PER_FILE_FORMATS = ("image", "pdf")
# Fixed run order for an item's formats -- matches commands/showcase.py's own
# image/pdf/video/audio mount order.
_DERIVATIVE_FORMAT_ORDER = ["image", "pdf", "video", "audio"]


def _task_key(index, *parts):
    """DynamicOutput mapping key for one derivative task: sanitized to
    ``^[A-Za-z0-9_]+$`` and prefixed with its position, so two names that
    sanitize alike still get distinct keys.

    :param int index: Position in the fan-out.
    :param str parts: Name segments (e.g. the ARK id and file name); joined with '__'.
    :rtype: str
    """
    name = "__".join(re.sub(r"[^A-Za-z0-9_]", "_", p) for p in parts)
    return f"i{index}_{name}"[:255]


@op(out=DynamicOut(dict))
def fan_out_derivative_tasks(context: OpExecutionContext, config: DerivativesConfig):
    """Fan out the partition's collection into derivative tasks: one per image
    and per PDF file, and one per item for video/audio.

    Each task is {"ark": str, "format": str, "file": str|None (bronze path),
    "args": [str], "error": str|None} — everything process_derivative_task
    needs. An item whose formats can't be listed becomes a single task carrying
    the error, so it's reported (and fails the run) like any other failed step.

    Shares lib.configs.DerivativesConfig with lib.assets.generate_derivatives,
    so a config change doesn't need to be kept in sync by hand across both
    designs. --widths now applies to PDFs as well as images.

    :param context: Dagster op execution context; partitioned by collection.
    :param config: DerivativesConfig with thumbnail widths.
    """
    collection_key = context.partition_key
    arks = fetch_items_for_collection(collection_key)
    context.log.info(f"{collection_key}: listing files for {len(arks)} item(s)")

    index = 0
    file_tasks = item_tasks = 0
    for ark in arks:
        ark_id = ark.rstrip("/").rsplit("/", 1)[-1]
        try:
            listing = exec(["digtk", "showcase", "formats", ark, "--files"])
            files = listing.get("files") or {}
        except Exception as e:
            yield DynamicOutput(
                {"ark": ark, "format": None, "file": None, "args": [], "error": f"listing formats failed: {e}"},
                mapping_key=_task_key(index, ark_id, "formats"),
            )
            index += 1
            continue

        for fmt in _DERIVATIVE_FORMAT_ORDER:
            if not files.get(fmt):
                continue
            args = ["--widths", config.widths] if fmt in _PER_FILE_FORMATS else []
            if fmt in _PER_FILE_FORMATS:
                for path in files[fmt]:
                    yield DynamicOutput(
                        {"ark": ark, "format": fmt, "file": path, "args": args, "error": None},
                        mapping_key=_task_key(index, ark_id, path.rsplit("/", 1)[-1]),
                    )
                    index += 1
                    file_tasks += 1
            else:
                yield DynamicOutput(
                    {"ark": ark, "format": fmt, "file": None, "args": args, "error": None},
                    mapping_key=_task_key(index, ark_id, fmt),
                )
                index += 1
                item_tasks += 1

    context.log.info(
        f"{collection_key}: {file_tasks} image/PDF step(s), {item_tasks} video/audio item step(s)"
    )


@op(ins={"task": In(dict)}, out=Out(dict))
def process_derivative_task(context: OpExecutionContext, task: dict) -> dict:
    """Run one derivative task's `digtk showcase <format> derivatives` call.

    Reports failure (an AssetMaterialization with status "failed" and a
    returned ok=False) instead of raising, so one bad file doesn't stop every
    other item from being finalized — fan_out_finalize skips just that file's
    item, and check_derivative_failures fails the run afterwards.

    :param context: Dagster op execution context; partitioned by collection.
    :param dict task: A fan_out_derivative_tasks task.
    :returns: {"ark", "format", "file", "ok": bool, "error": str|None}
    :rtype: dict
    """
    ark, fmt, path = task["ark"], task["format"], task["file"]
    label = f"{ark}#{path.rsplit('/', 1)[-1]}" if path else ark
    outcome = {"ark": ark, "format": fmt, "file": path, "ok": False, "error": task.get("error")}
    if task.get("error"):
        _emit(context, "derivatives", label, None, status="failed", error=task["error"])
        return outcome

    cmd = ["digtk", "showcase", fmt, "derivatives", ark]
    if path:
        cmd += ["--file", path]
    cmd += task["args"] + [
        "--reporting-job-id", context.run_id, "--reporting-collection", context.partition_key,
    ]
    try:
        result = exec(cmd)
    except Exception as e:
        _emit(context, "derivatives", label, None, status="failed", error=str(e), format=fmt)
        return {**outcome, "error": str(e)}
    _emit(context, "derivatives", label, result, format=fmt)
    return {**outcome, "ok": True}


@op(out={
    "items": DynamicOut(str, is_required=False),
    "summary": Out(dict),
})
def fan_out_finalize(context: OpExecutionContext, results: list):
    """Fan out one finalize task per item whose image/PDF steps ALL succeeded,
    and pass a failure summary on to check_derivative_failures.

    :param context: Dagster op execution context; partitioned by collection.
    :param list[dict] results: Every process_derivative_task outcome.
    """
    by_ark = {}
    for r in results:
        by_ark.setdefault(r["ark"], []).append(r)

    finalized = 0
    for ark, outcomes in by_ark.items():
        per_file = [o for o in outcomes if o["format"] in _PER_FILE_FORMATS or o["format"] is None]
        if not per_file:
            continue
        failed = [o for o in per_file if not o["ok"]]
        if failed:
            context.log.warning(
                f"[{ark}] not finalized: {len(failed)} of {len(per_file)} step(s) failed "
                f"({', '.join((o['file'] or o['format'] or 'formats').rsplit('/', 1)[-1] for o in failed)})"
            )
            continue
        yield DynamicOutput(ark, output_name="items", mapping_key=_mapping_key(ark))
        finalized += 1

    failures = [r for r in results if not r["ok"]]
    context.log.info(f"{finalized} item(s) to finalize; {len(failures)} of {len(results)} step(s) failed")
    yield Output({"steps": len(results), "failed": len(failures)}, output_name="summary")


@op(ins={"ark": In(str)})
def finalize_item(context: OpExecutionContext, ark: str):
    """Write one item's item-level outputs from its per-file derivatives
    (`digtk showcase finalize`).

    :param context: Dagster op execution context; partitioned by collection.
    :param str ark: Item ARK.
    """
    cmd = [
        "digtk", "showcase", "finalize", ark,
        "--reporting-job-id", context.run_id, "--reporting-collection", context.partition_key,
    ]
    try:
        result = exec(cmd)
    except Exception as e:
        _emit(context, "finalize", ark, None, status="failed", error=str(e))
        raise
    _emit(context, "finalize", ark, result)


@op
def check_derivative_failures(context: OpExecutionContext, summary: dict):
    """Fail the run if any derivative step failed, so a partial run isn't green
    — every other item has still been finalized independently.

    :param context: Dagster op execution context.
    :param dict summary: fan_out_finalize's {"steps", "failed"}.
    :raises RuntimeError: If any derivative step failed.
    """
    if summary["failed"]:
        raise RuntimeError(
            f"{summary['failed']} of {summary['steps']} derivative step(s) failed — see the FAILED "
            "events above; their items weren't finalized"
        )
    context.log.info(f"all {summary['steps']} derivative step(s) succeeded")


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
    """Collection-partitioned fan-out: one derivative step per image/PDF file
    (and per video/audio item) across the partition's collection, then one
    finalize step per image/PDF item — one Dagster run per collection, each
    step its own celery task. See this module's docstring for how this
    differs from lib.jobs.derivatives_job (the MultiPartitionsDefinition,
    per-item-partition design). OCR is not part of this job; run
    lib.jobs.ocr_job for that.
    """
    results = fan_out_derivative_tasks().map(process_derivative_task).collect()
    items, summary = fan_out_finalize(results)
    items.map(finalize_item)
    check_derivative_failures(summary)
