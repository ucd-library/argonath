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
is `digtk showcase image|pdf derivatives <item-path> --file <bronze path>`,
its own celery task, retryable on its own. Those per-file runs skip the
outputs that combine every file of an item (an image item's ocr/manifest.json),
so once every step is done a per-item
`digtk showcase finalize <item-path>` step writes them from what the per-file
steps published (see digtk.models.digital_website.finalize). Every item is
addressed by its bronze path throughout — an ARK is carried along only as
metadata (not every item has one) and is never required to run a step. Video
and audio stay one
step per item. Dagster can't nest fan-outs, so the graph is flat:
fan_out_derivative_tasks -> process_derivative_batch (one per image/PDF, or per
video/audio item) -> collect -> fan_out_finalize -> finalize_batch (one per
item with images/PDFs). An item with a failed file step isn't finalized, and
check_derivative_failures fails the run so a partial run isn't green.

Each fan-out is capped at CollectionDerivativesConfig.max_steps steps: past
that, several files (or items) share one step and run one after another inside
it, each still reporting its own result event (see lib.fanout_batching). Steps
and events are named by the file's or item's CaskFS path relative to the
partition (e.g. "latest/tree/x/Preservation/page_003.tif").

Each op shells out to a digtk command via lib.utils.exec(), the same
bridge lib/assets.py's per-item assets use.

lib/pipeline_item.py's process_item_job (one item, no partition) reuses this
module's per-item pieces: item_tasks() / _error_task() to build an item's
tasks, process_derivative_batch to run them, and finalize_one_item() to
finalize it. So every task carries its own `collection_key` (used for
--reporting-collection and the event partition) instead of reading
context.partition_key.
"""
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

from lib.configs import collections_partitions, CollectionDerivativesConfig
from lib.fanout_batching import batches, log_batch, log_batching, relative_id, step_key
from lib.utils import default_run_config, exec


def fetch_items_for_collection(collection_key):
    """Look up every current item's bronze folder path in a collection.

    Shells out to `digtk collection items` (see
    digtk.models.collection.list_item_folders()) rather than reading CaskFS
    directly -- same exec()-bridge convention every other Dagster/digtk
    interaction in this codebase uses. `--collection` only: collection names
    are unique among active registrations regardless of group (see
    register_collection()'s collection_registry_active_name_uq), so the group
    half of collection_key is only needed for this job's own partition
    key/logging, not for the digtk call itself.

    :param str collection_key: "bronze/[group]/[collection]" partition key (an
        older "[group]/[collection]" key works too — only the last segment is used).
    :returns: Item bronze folder paths currently in the collection.
    :rtype: list[str]
    """
    collection = collection_key.rstrip("/").rsplit("/", 1)[-1]
    result = exec(["digtk", "collection", "items", "--collection", collection])
    return result["items"]


def _emit(context, stage, item_id, result, status="success", partition=None, **extra):
    """Log an AssetMaterialization carrying per-item stage metadata for
    traceability -- the per-item progress signal this job uses in place of
    Dagster partition status (see module docstring). The authoritative
    success/failure record is digtk.lib.reporting's Postgres row, written by
    the digtk command itself; this is a Dagster-UI-visible complement to it.

    :param context: Dagster op execution context.
    :param str stage: Pipeline stage name (e.g. "derivatives").
    :param str item_id: What the event is about — a file/item path relative to
        the partition.
    :param result: digtk's parsed JSON result, or None on failure.
    :param str status: "success" or "failed".
    :param str|None partition: Event partition (a collection key); defaults to
        the run's partition key, or none for an unpartitioned run.
    :param extra: Additional string-coercible metadata (e.g. error=...).
    """
    if partition is None and context.has_partition_key:
        partition = context.partition_key
    metadata = {
        "item_id": MetadataValue.text(item_id),
        "stage": MetadataValue.text(stage),
        "status": MetadataValue.text(status),
        **{k: MetadataValue.text(str(v)) for k, v in extra.items()},
    }
    if status == "failed":
        context.log.error(f"[{item_id}] {stage}: FAILED -> {extra.get('error')}")
    else:
        context.log.info(f"[{item_id}] {stage}: ok -> {result}")
    context.log_event(AssetMaterialization(
        asset_key=f"argonath_fanout_{stage}",
        partition=partition,
        metadata=metadata,
    ))


# Formats generated one step per file (and finalized per item afterwards);
# every other format an item has (video, audio) is still one step per item.
_PER_FILE_FORMATS = ("image", "pdf")
# Fixed run order for an item's formats -- matches commands/showcase.py's own
# image/pdf/video/audio mount order.
_DERIVATIVE_FORMAT_ORDER = ["image", "pdf", "video", "audio"]


def _error_task(target, id, item_id, error, collection_key, config):
    """A task that only carries an error — reported (and failing the run) by
    _run_derivative_task like any other failed step.

    :param str target: Item bronze path (whatever identifies the item — never
        requires an ARK; an error task never actually runs a digtk command).
    :param str id: Task id (path relative to collection_key).
    :param str item_id: Item folder relative to collection_key.
    :param str error: What went wrong.
    :param str collection_key: "bronze/[group]/[collection]".
    :param config: CollectionDerivativesConfig (or a subclass) — for max_steps.
    :returns: A fan-out task (see fan_out_derivative_tasks).
    :rtype: dict
    """
    return {
        "target": target, "format": None, "file": None, "id": id, "item_id": item_id,
        "args": [], "error": error, "max_steps": config.max_steps, "collection_key": collection_key,
    }


def item_tasks(item_path, listing, collection_key, config):
    """One item's derivative tasks — one per image and per PDF file, one per
    item for video/audio — from its `digtk showcase formats --files` listing.
    An item with no media at all becomes a single error task.

    :param str item_path: Item's bronze folder path (also the fallback target
        if the listing somehow had no "dir").
    :param dict listing: `digtk showcase formats --files` result ({"files", "dir", ...}).
    :param str collection_key: "bronze/[group]/[collection]" — task ids are relative to it.
    :param config: CollectionDerivativesConfig (or a subclass) — widths and max_steps.
    :returns: Fan-out tasks (see fan_out_derivative_tasks), in format order. Each
        task's "target" (its bronze path) is what subsequent digtk commands
        address it by.
    :rtype: list[dict]
    """
    files = listing.get("files") or {}
    target = listing.get("dir") or item_path
    item_id = relative_id(target, collection_key)

    if not any(files.values()):
        # The item's bronze folder has no media at all (e.g. a tree/ item
        # whose Access/Preservation folders are missing) — report it as a
        # failed task so it's obvious in Events and the run fails.
        error = f"MISSING MEDIA: no image/PDF/video/audio files in bronze {item_id}/"
        return [_error_task(target, item_id, item_id, error, collection_key, config)]

    tasks = []
    for fmt in _DERIVATIVE_FORMAT_ORDER:
        if not files.get(fmt):
            continue
        if fmt in _PER_FILE_FORMATS:
            for path in files[fmt]:
                tasks.append({
                    "target": target, "format": fmt, "file": path, "id": relative_id(path, collection_key),
                    "item_id": item_id, "args": ["--widths", config.widths], "error": None,
                    "max_steps": config.max_steps, "collection_key": collection_key,
                })
        else:
            tasks.append({
                "target": target, "format": fmt, "file": None, "id": f"{item_id} ({fmt})",
                "item_id": item_id, "args": [], "error": None, "max_steps": config.max_steps,
                "collection_key": collection_key,
            })
    return tasks


@op(out=DynamicOut(list))
def fan_out_derivative_tasks(context: OpExecutionContext, config: CollectionDerivativesConfig):
    """Fan out the partition's collection into derivative tasks — one per image
    and per PDF file, and one per item for video/audio — batched so there are at
    most config.max_steps steps (see lib.fanout_batching).

    Each task is {"target": str (bronze path to address the item by), "format": str, "file":
    str|None (bronze path), "id": str (its path relative to the partition —
    the file, or the item folder for an item-level task), "item_id": str (the
    item folder), "args": [str], "error": str|None, "max_steps": int (carried
    through so fan_out_finalize batches by the same cap without a config of
    its own), "collection_key": str (the partition; see item_tasks())}; each
    DynamicOutput is a list of tasks, its
    mapping key every task id joined with '__'. An item whose formats can't be
    listed becomes a task carrying the error, so it's reported (and fails the
    run) like any other failed step.

    :param context: Dagster op execution context; partitioned by collection.
    :param config: CollectionDerivativesConfig — thumbnail widths and max_steps.
    """
    collection_key = context.partition_key
    item_paths = fetch_items_for_collection(collection_key)
    context.log.info(f"{collection_key}: listing files for {len(item_paths)} item(s)")

    tasks = []
    for item_path in item_paths:
        try:
            listing = exec(["digtk", "showcase", "formats", item_path, "--files"])
            new_tasks = item_tasks(item_path, listing, collection_key, config)
        except Exception as e:
            tasks.append(_error_task(item_path, item_path, item_path, f"listing formats failed: {e}", collection_key, config))
            continue
        for task in new_tasks:
            if task["error"]:
                context.log.error(f"[{task['item_id']}] {task['error']}")
        tasks.extend(new_tasks)

    per_file = sum(1 for t in tasks if t["file"])
    context.log.info(
        f"{collection_key}: {per_file} image/PDF task(s), {len(tasks) - per_file} item-level task(s)"
    )
    grouped = batches(tasks, config.max_steps)
    log_batching(context, "derivatives", len(tasks), grouped, config.max_steps)
    used = set()
    for group in grouped:
        yield DynamicOutput(group, mapping_key=step_key([t["id"] for t in group], used))


def _run_derivative_task(context, task):
    """Run one derivative task's `digtk showcase <format> derivatives` call and
    record its result as an event, labelled with the task's path id.

    :param context: Dagster op execution context.
    :param dict task: A fan_out_derivative_tasks / item_tasks() task.
    :returns: {"target", "format", "file", "id", "item_id", "max_steps",
        "collection_key", "ok": bool, "error": str|None}
    :rtype: dict
    """
    fmt, path, collection_key = task["format"], task["file"], task["collection_key"]
    outcome = {
        "target": task["target"], "format": fmt, "file": path, "id": task["id"],
        "item_id": task["item_id"], "max_steps": task["max_steps"], "collection_key": collection_key,
        "ok": False, "error": task.get("error"),
    }
    if task.get("error"):
        _emit(
            context, "derivatives", task["id"], None, status="failed", partition=collection_key,
            error=task["error"],
        )
        return outcome

    cmd = ["digtk", "showcase", fmt, "derivatives", task["target"]]
    if path:
        cmd += ["--file", path]
    cmd += task["args"] + [
        "--reporting-job-id", context.run_id, "--reporting-collection", collection_key,
    ]
    try:
        result = exec(cmd)
    except Exception as e:
        _emit(
            context, "derivatives", task["id"], None, status="failed", partition=collection_key,
            error=str(e), format=fmt,
        )
        return {**outcome, "error": str(e)}
    _emit(context, "derivatives", task["id"], result, partition=collection_key, format=fmt)
    return {**outcome, "ok": True}


@op(ins={"tasks": In(list)}, out=Out(list))
def process_derivative_batch(context: OpExecutionContext, tasks: list) -> list:
    """Run one step's derivative tasks — a single file, or a batch of them —
    one `digtk showcase … --file` command per file, so each still gets its own
    result event.

    Reports failures (an AssetMaterialization with status "failed" and ok=False)
    instead of raising, so one bad file doesn't stop the rest of its batch or
    every other item's finalize — fan_out_finalize skips just that file's item,
    and check_derivative_failures fails the run afterwards.

    :param context: Dagster op execution context; partitioned by collection.
    :param list[dict] tasks: fan_out_derivative_tasks tasks.
    :returns: One outcome per task (see _run_derivative_task()).
    :rtype: list[dict]
    """
    log_batch(context, [t["id"] for t in tasks])
    return [_run_derivative_task(context, task) for task in tasks]


@op(out={
    "items": DynamicOut(list, is_required=False),
    "summary": Out(dict),
})
def fan_out_finalize(context: OpExecutionContext, results: list):
    """Fan out finalize tasks for every item whose image/PDF tasks ALL
    succeeded — batched by the same max_steps the derivatives fan-out used
    (carried on each outcome, so it's set once in the launchpad) — and pass a
    failure summary on to check_derivative_failures.

    :param context: Dagster op execution context; partitioned by collection.
    :param list[list[dict]] results: Every process_derivative_batch's outcomes.
    """
    outcomes = [o for batch in results for o in batch]
    max_steps = outcomes[0]["max_steps"] if outcomes else 0
    # Grouped by item_id, not ark — an item may have no ark, and several
    # ark-less items in the same batch would otherwise collide under one key.
    by_item = {}
    for o in outcomes:
        by_item.setdefault(o["item_id"], []).append(o)

    tasks = []
    for item_id, item_outcomes in by_item.items():
        per_file = [o for o in item_outcomes if o["format"] in _PER_FILE_FORMATS or o["format"] is None]
        if not per_file:
            continue
        failed = [o for o in per_file if not o["ok"]]
        if failed:
            context.log.warning(
                f"[{item_id}] not finalized: {len(failed)} of {len(per_file)} task(s) failed "
                f"({', '.join(o['id'] for o in failed)})"
            )
            continue
        tasks.append({
            "target": per_file[0]["target"],
            "id": item_id, "collection_key": per_file[0]["collection_key"],
        })

    grouped = batches(tasks, max_steps)
    log_batching(context, "finalize", len(tasks), grouped, max_steps)
    used = set()
    for group in grouped:
        yield DynamicOutput(group, output_name="items", mapping_key=step_key([t["id"] for t in group], used))

    failures = [o for o in outcomes if not o["ok"]]
    context.log.info(f"{len(tasks)} item(s) to finalize; {len(failures)} of {len(outcomes)} task(s) failed")
    yield Output({"steps": len(outcomes), "failed": len(failures)}, output_name="summary")


def finalize_one_item(context, target, id, collection_key):
    """Write one item's item-level outputs (`digtk showcase finalize`) and
    record the result as an event. Never raises — the caller decides whether a
    failure fails its step.

    :param context: Dagster op execution context.
    :param str target: Item bronze path to finalize.
    :param str id: Item folder relative to collection_key (the event's item_id).
    :param str collection_key: "bronze/[group]/[collection]" — reporting
        collection and event partition.
    :returns: None on success, else the error message.
    :rtype: str|None
    """
    cmd = [
        "digtk", "showcase", "finalize", target,
        "--reporting-job-id", context.run_id, "--reporting-collection", collection_key,
    ]
    try:
        result = exec(cmd)
    except Exception as e:
        _emit(context, "finalize", id, None, status="failed", partition=collection_key, error=str(e))
        return str(e)
    _emit(context, "finalize", id, result, partition=collection_key)
    return None


def build_item_record(context, target, id, collection_key):
    """Assemble and write one item's complete gold item.jsonld.json (`digtk showcase
    build-item`) and record the result as an event. Never raises — the caller
    decides whether a failure fails its step.

    Unlike finalize_one_item() above, this genuinely can fail for an item with
    no ark — the canonical public record needs a stable identifier, and
    item_record.build() has no fallback for that (see its docstring). This is
    the first step the ark is used in, so it's read back from the record
    built here and recorded on the event.

    :param context: Dagster op execution context.
    :param str target: Item bronze path.
    :param str id: Item folder relative to collection_key (the event's item_id).
    :param str collection_key: "bronze/[group]/[collection]" — reporting
        collection and event partition.
    :returns: (error, ark) — error is None on success, else the message; ark is
        the built record's ark (None on failure).
    :rtype: tuple[str|None, str|None]
    """
    cmd = [
        "digtk", "showcase", "build-item", target,
        "--reporting-job-id", context.run_id, "--reporting-collection", collection_key,
    ]
    try:
        result = exec(cmd)
    except Exception as e:
        _emit(context, "build-item", id, None, status="failed", partition=collection_key, error=str(e))
        return str(e), None
    ark = result.get("ark")
    _emit(context, "build-item", id, result, partition=collection_key, item_ark=ark)
    return None, ark


@op(ins={"tasks": In(list)})
def finalize_batch(context: OpExecutionContext, tasks: list):
    """Write item-level outputs for one step's items (`digtk showcase
    finalize`, then `digtk showcase build-item`), one item at a time — each
    gets its own result event; a failed item doesn't stop the rest, but fails
    the step at the end. build-item only runs for an item whose finalize
    succeeded.

    :param context: Dagster op execution context; partitioned by collection.
    :param list[dict] tasks: fan_out_finalize tasks ({"target", "id", "collection_key"}).
    :raises RuntimeError: If any item in the batch failed to finalize or build its record.
    """
    log_batch(context, [t["id"] for t in tasks])
    failed = []
    for t in tasks:
        if finalize_one_item(context, t["target"], t["id"], t["collection_key"]) is not None:
            failed.append(t["id"])
            continue
        if build_item_record(context, t["target"], t["id"], t["collection_key"])[0] is not None:
            failed.append(t["id"])
    if failed:
        raise RuntimeError(f"{len(failed)} of {len(tasks)} item(s) failed to finalize: {', '.join(failed)}")


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
    config=default_run_config({"fan_out_derivative_tasks": CollectionDerivativesConfig}),
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
    results = fan_out_derivative_tasks().map(process_derivative_batch).collect()
    items, summary = fan_out_finalize(results)
    items.map(finalize_batch)
    check_derivative_failures(summary)
