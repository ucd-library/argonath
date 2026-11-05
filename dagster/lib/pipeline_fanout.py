"""
Shared per-item derivative pieces for lib/pipeline_item.py's
gold_dcs_process_item. A collection is processed by queueing one
gold_dcs_process_item run per item (lib/pipeline_collection.py's
gold_dcs_process_collection), so the collection-wide fan-out job that used to
live here is gone; only the per-item building blocks remain.

Derivatives run one step per IMAGE and per PDF file, not per item: each step
is `digtk showcase image|pdf derivatives <item-path> --file <bronze path>`,
its own celery task, retryable on its own. Those per-file runs skip the
outputs that combine every file of an item (an image item's ocr/manifest.json),
so once every step is done `digtk showcase finalize <item-path>` writes them
from what the per-file steps published (see digtk.models.digital_website.finalize).
Video and audio stay one step per item. Every item is addressed by its bronze
path; an ARK is never required to run a derivative step.

Steps are capped at CollectionDerivativesConfig.max_steps: past that, several
files share one step, each still reporting its own result event (see
lib.fanout_batching). Steps and events are named by the file's CaskFS path
relative to the collection (e.g. "latest/tree/x/Preservation/page_003.tif").
Every task carries its own `collection_key` (used for --reporting-collection
and the event partition).

Pieces: item_tasks() / _error_task() build an item's tasks,
process_derivative_batch runs them, finalize_one_item() finalizes the item,
and build_item_record() writes its gold item.jsonld.json. Each shells out to
a digtk command via lib.utils.exec().
"""
from dagster import In, Out, OpExecutionContext, op

from lib.fanout_batching import emit, log_batch, relative_id
from lib.utils import exec


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
    :returns: A derivative task (see item_tasks()).
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
    :returns: Derivative tasks, in format order. Each
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


def _run_derivative_task(context, task):
    """Run one derivative task's `digtk showcase <format> derivatives` call and
    record its result as an event, labelled with the task's path id.

    :param context: Dagster op execution context.
    :param dict task: An item_tasks() / _error_task() task.
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
        emit(
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
        emit(
            context, "derivatives", task["id"], None, status="failed", partition=collection_key,
            error=str(e), format=fmt,
        )
        return {**outcome, "error": str(e)}
    emit(context, "derivatives", task["id"], result, partition=collection_key, format=fmt)
    return {**outcome, "ok": True}


@op(ins={"tasks": In(list)}, out=Out(list))
def process_derivative_batch(context: OpExecutionContext, tasks: list) -> list:
    """Run one step's derivative tasks — a single file, or a batch of them —
    one `digtk showcase … --file` command per file, so each still gets its own
    result event.

    Reports failures (an AssetMaterialization with status "failed" and ok=False)
    instead of raising, so one bad file doesn't stop the rest of its batch;
    gold_dcs_process_item's pdc_finalize_item fails the run afterwards.

    :param context: Dagster op execution context.
    :param list[dict] tasks: item_tasks() tasks.
    :returns: One outcome per task (see _run_derivative_task()).
    :rtype: list[dict]
    """
    log_batch(context, [t["id"] for t in tasks])
    return [_run_derivative_task(context, task) for task in tasks]


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
        emit(context, "finalize", id, None, status="failed", partition=collection_key, error=str(e))
        return str(e)
    emit(context, "finalize", id, result, partition=collection_key)
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
        emit(context, "build-item", id, None, status="failed", partition=collection_key, error=str(e))
        return str(e), None
    # The record is a {"@id": <ark>, "@graph": [...]} document — its own
    # top-level @id is the item's bare ark (see item_record.build()).
    ark = result.get("@id")
    if not ark:
        error = f"build-item returned no ark for {target}"
        emit(context, "build-item", id, None, status="failed", partition=collection_key, error=error)
        return error, None
    emit(context, "build-item", id, result, partition=collection_key, item_ark=ark)
    return None, ark

