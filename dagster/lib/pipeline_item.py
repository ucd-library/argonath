"""
process_item_job: run one item's showcase derivatives on demand. The job is
manual and unpartitioned, and the item is addressed by its bronze tree folder
(ItemRunConfig.item, e.g. "bronze/preservica/haeger/latest/tree/quarter-mile")
throughout — an ARK is read from the folder's item.jsonld.json by
`digtk showcase formats <folder> --files` if one exists, but is never required
to generate derivatives or finalize the item; only index_item's Elasticsearch
upsert genuinely needs one (see below).

It works like one item's slice of lib/pipeline_fanout.py's
process_collection_job and reuses that module's pieces: item_tasks() builds
the tasks (one per image/PDF file, one per video/audio item),
process_derivative_batch runs them, finalize_one_item() writes the item-level
derivative outputs, and build_item_record() assembles the canonical
item.jsonld.json. The graph:

  plan_item -> process_derivative_batch (mapped) -> collect -> finalize_item
            -> generate_item_record -> index_item

finalize_item fails the run if any file failed, and only finalizes items with
image or PDF outputs. generate_item_record then runs `digtk showcase
build-item <item>`, assembling the item's full descriptive/associatedMedia
record — this is the other step that hard-requires an ark (the canonical
public record needs a stable identifier), so it fails here for an ark-less
item, after derivatives/finalize already succeeded. index_item then runs
`digtk showcase es upsert <ark>` (ItemRunConfig.index_es), re-reading whatever
generate_item_record just wrote. An item whose formats listing itself fails
(not just a missing ark) becomes a single error task at plan_item, so the run
reports it and fails like any other failed step.

Task ids, events and digtk reporting rows use the item's collection key (the
first three path segments, "bronze/[group]/[collection]"), the same as
process_collection_job. Runs are tagged argonath/item, argonath/collection and
(when the item has one) argonath/ark, so they're searchable in the Runs
screen. Queued runs (see lib.pipeline_collection.item_queue_sensor) already
have argonath/item and argonath/collection at launch.

Each run's outcome is recorded as the item's process_item_job result in
collection_state (see lib.collection_state): "success" after index_item, and
"failed" from the op that fails. A recorded
result also tags the run argonath/result=<status>. process_item_failure_sensor
records "failed" for failed runs without that tag, e.g. a crashed worker or an
unexpected error, using the run's step errors. In-flight state isn't recorded:
it lives in Dagster's own run tracking.
"""
import dagster as dg
from dagster import (
    DynamicOut,
    DynamicOutput,
    Out,
    OpExecutionContext,
    Output,
    job,
    op,
)
from dagster_celery import celery_executor

from lib import collection_state
from lib.configs import ItemRunConfig
from lib.fanout_batching import batches, log_batching, relative_id, step_key
from lib.pipeline_fanout import (
    _PER_FILE_FORMATS,
    _error_task,
    build_item_record,
    finalize_one_item,
    item_tasks,
    process_derivative_batch,
)
from lib.utils import default_run_config, exec


def normalize_item(path):
    """An item's bronze folder in the form this job keys it by, plus its
    collection key.

    :param str path: e.g. "bronze/preservica/haeger/latest/tree/quarter-mile";
        a 'cask:' prefix and leading/trailing slashes are accepted.
    :returns: (item, collection_key), e.g.
        ("bronze/preservica/haeger/latest/tree/quarter-mile", "bronze/preservica/haeger").
    :rtype: tuple[str, str]
    :raises ValueError: If the path isn't a folder under bronze/[group]/[collection]/.
    """
    raw = (path or "").strip()
    raw = raw[len("cask:"):] if raw.startswith("cask:") else raw
    item = raw.strip("/")
    segments = item.split("/")
    if segments[0] != "bronze" or len(segments) <= 3 or not all(segments):
        raise ValueError(
            f"item must be a bronze item folder, e.g. bronze/preservica/haeger/latest/tree/quarter-mile (got {path!r})"
        )
    return item, "/".join(segments[:3])


@op(out={"tasks": DynamicOut(list), "item": Out(dict)})
def plan_item(context: OpExecutionContext, config: ItemRunConfig):
    """List the item's files and fan out its derivative tasks, batched so there
    are at most config.max_steps steps (see lib.fanout_batching).

    An item that can't be listed at all (e.g. its bronze folder has no media)
    becomes a single error task, so the run reports it and fails. A missing
    ARK is not by itself a listing failure — not every item has one, and
    derivative generation/finalize don't need one (see module docstring).

    :param context: Dagster op execution context (unpartitioned).
    :param config: ItemRunConfig: the item folder, widths, max_steps, index_es.
    :raises ValueError: If config.item is empty or isn't a bronze item folder.
    """
    if not config.item.strip():
        raise ValueError("config.item is required: the item's bronze folder, e.g. bronze/preservica/haeger/latest/tree/quarter-mile")
    item, collection_key = normalize_item(config.item)
    item_id = relative_id(item, collection_key)

    context.instance.add_run_tags(context.run_id, {"argonath/item": item, "argonath/collection": collection_key})

    listing = exec(["digtk", "showcase", "formats", item, "--files"], {"check": False})
    if listing.get("status") == "error" or listing.get("error"):
        error = listing.get("error") or f"listing formats failed: {listing}"
        context.log.error(f"[{item_id}] {error}")
        tasks = [_error_task(item, item_id, item_id, error, collection_key, config)]
        ark = None
    else:
        ark = listing.get("ark")
        if ark:
            context.instance.add_run_tags(context.run_id, {"argonath/ark": ark})
        tasks = item_tasks(item, listing, collection_key, config)
        for task in tasks:
            if task["error"]:
                context.log.error(f"[{task['item_id']}] {task['error']}")

    per_file = sum(1 for t in tasks if t["file"])
    context.log.info(f"{item}: {per_file} image/PDF task(s), {len(tasks) - per_file} item-level task(s)")
    grouped = batches(tasks, config.max_steps)
    log_batching(context, "derivatives", len(tasks), grouped, config.max_steps)
    used = set()
    for group in grouped:
        yield DynamicOutput(group, output_name="tasks", mapping_key=step_key([t["id"] for t in group], used))

    yield Output({
        "item": item,
        "collection_key": collection_key,
        "id": item_id,
        "ark": ark,
        "index_es": config.index_es,
    }, output_name="item")


@op
def finalize_item(context: OpExecutionContext, results: list, item: dict) -> dict:
    """Fail the run if any derivative task failed; otherwise write the item's
    item-level outputs (`digtk showcase finalize`) if it has image or PDF
    outputs. A video/audio-only item has nothing to finalize.

    :param context: Dagster op execution context.
    :param list[list[dict]] results: Every process_derivative_batch's outcomes.
    :param dict item: plan_item's item output.
    :returns: The same item dict, passed on to index_item.
    :rtype: dict
    :raises RuntimeError: If any task failed, or finalize failed.
    """
    outcomes = [o for batch in results for o in batch]
    failed = [o for o in outcomes if not o["ok"]]
    if failed:
        error = (
            f"{len(failed)} of {len(outcomes)} derivative task(s) failed, so {item['id']} wasn't finalized: "
            f"{', '.join(o['id'] for o in failed)}"
        )
        _record(context, item, "failed", {"error": error})
        raise RuntimeError(error)

    if not any(o["format"] in _PER_FILE_FORMATS for o in outcomes):
        context.log.info(f"[{item['id']}] no image/PDF outputs, nothing to finalize")
        return item

    error = finalize_one_item(context, item["item"], item["ark"], item["id"], item["collection_key"])
    if error is not None:
        error = f"{item['id']} failed to finalize: {error}"
        _record(context, item, "failed", {"error": error})
        raise RuntimeError(error)
    return item


@op
def generate_item_record(context: OpExecutionContext, item: dict) -> dict:
    """Assemble and write the item's complete, canonical item.jsonld.json
    (`digtk showcase build-item`) from its now-finalized derivatives and
    bronze identifiers/metadata.

    Unlike every other op in this job, this one genuinely requires an ark and
    fails outright without one — the canonical public record needs a stable
    identifier (see digtk.models.digital_website.item_record.build()'s
    docstring). This runs before index_item, so an ark-less item's run fails
    here rather than inside index_item's Elasticsearch upsert.

    :param context: Dagster op execution context.
    :param dict item: finalize_item's item dict.
    :returns: The same item dict, passed on to index_item.
    :rtype: dict
    :raises RuntimeError: If building the item record failed (including: no ark).
    """
    error = build_item_record(context, item["item"], item["ark"], item["id"], item["collection_key"])
    if error is not None:
        error = f"{item['id']} failed to build its item record: {error}"
        _record(context, item, "failed", {"error": error})
        raise RuntimeError(error)
    return item


@op
def index_item(context: OpExecutionContext, item: dict):
    """Index the item into the DAMS Elasticsearch index (`digtk showcase es
    upsert`) if ItemRunConfig.index_es is set, then record the job as a success.

    This is the one step in the job that genuinely requires an ark (the ES
    document's own id is ark-shaped) — an item with no ark fails here, after
    its derivatives and finalize have already succeeded, rather than earlier.

    :param context: Dagster op execution context.
    :param dict item: finalize_item's item dict.
    :raises RuntimeError: If index_es is set and the item has no ark.
    :raises Exception: If indexing failed (after recording "failed").
    """
    if item["index_es"]:
        if not item["ark"]:
            error = f"{item['id']} has no ark, cannot index"
            _record(context, item, "failed", {"error": error})
            raise RuntimeError(error)
        try:
            result = exec(["digtk", "showcase", "es", "upsert", item["ark"]])
        except Exception as e:
            _record(context, item, "failed", {"error": f"Elasticsearch upsert failed: {e}"})
            raise
        context.log.info(f"[{item['id']}] indexed {item['ark']} -> {result}")
    else:
        context.log.info(f"[{item['id']}] index_es off, not indexing")
    _record(context, item, "success")


def _record(context, item, status, opts={}):
    """Record this run's result for the item in collection_state (best-effort)
    and tag the run with it.

    :param context: Dagster op execution context.
    :param dict item: plan_item's item dict.
    :param str status: "success" or "failed".
    :param dict opts: 'error' (str).
    """
    result = collection_state.item_result(context, item, status, opts)
    collection_state.record_item_result(item["item"], result, {"log": context.log, "context": context})


@job(
    # "dagster/max_runtime": "0": no duration-based auto-cancel. A large item
    # can have hundreds of pages. See process_collection_job's comment in
    # lib/pipeline_fanout.py.
    tags={"argonath/kind": "item", "dagster/max_runtime": "0"},
    executor_def=celery_executor,
    config=default_run_config({"plan_item": ItemRunConfig}),
)
def process_item_job():
    """One item's derivatives, addressed by its bronze folder: one step per
    image/PDF file (and per video/audio format), then finalize, then building
    its canonical item.jsonld.json, then the Elasticsearch upsert. Launch it
    from the launchpad with ops.plan_item.config.item set, or let
    lib.pipeline_collection.item_queue_sensor launch it from the queue. See
    this module's docstring.
    """
    tasks, item = plan_item()
    results = tasks.map(process_derivative_batch).collect()
    index_item(generate_item_record(finalize_item(results, item)))


@dg.run_failure_sensor(monitored_jobs=[process_item_job])
def process_item_failure_sensor(context: dg.RunFailureSensorContext):
    """Record "failed" in collection_state for failed process_item_job runs
    that didn't record a result themselves — no argonath/result tag, e.g. a
    crashed celery worker or an unexpected error in an op. The error is taken
    from the run's step failures. Runs whose op already recorded "failed" are
    left alone, so that op's more specific error is kept. Canceled runs don't
    count as failures, so they're not recorded here.

    The item comes from the run's argonath/item tag. A run that failed before
    plan_item tagged it falls back to its run config.

    :param context: Dagster run failure sensor context.
    """
    run = context.dagster_run
    if run.tags.get(collection_state.RESULT_TAG):
        return
    item = run.tags.get("argonath/item")
    if not item:
        configured = (
            ((run.run_config or {}).get("ops") or {}).get("plan_item", {}).get("config", {}).get("item")
        )
        try:
            item, _ = normalize_item(configured)
        except ValueError:
            context.log.warning(f"run {run.run_id} has no valid item, nothing to record in collection_state")
            return
    errors = [
        f"{event.step_key}: {event.event_specific_data.error.message.strip()}"
        for event in context.get_step_failure_events()
        if event.event_specific_data and event.event_specific_data.error
    ]
    error = "; ".join(errors) or (context.failure_event.message if context.failure_event else "run failed")
    result = collection_state.item_result(
        context, {"ark": run.tags.get("argonath/ark")}, "failed", {"error": error, "run_id": run.run_id},
    )
    collection_state.record_item_result(item, result, {"log": context.log})
