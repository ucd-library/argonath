"""
gold_dcs_process_item: run one item's showcase derivatives, plus any ticked
tool steps, on demand. The job is unpartitioned, and the item is addressed by
its bronze tree folder (ItemRunConfig.item, e.g.
"bronze/preservica/haeger/latest/tree/quarter-mile") throughout the
derivative, tool and finalize steps, which never need an ARK.
pdc_generate_item_record is the first step to use one (read from the folder's
identifiers.json when it builds the gold item.jsonld.json), and
pdc_index_item's Elasticsearch upsert uses that ARK.

lib/pipeline_collection.py's gold_dcs_process_collection queues one run of
this job per item. The graph:

  pdc_plan_item -> pdc_process_batch (mapped) -> collect -> pdc_finalize_item
      -> pdc_build_item_pdf -> pdc_generate_item_record -> pdc_index_item
      -> pdc_check_tool_failures

pdc_plan_item lists the item's files (`digtk showcase formats --files`, which
keeps Preservation masters over their Access copies) and fans out:
  - showcase derivative tasks, always: one per image/PDF file, one per
    video/audio item (lib/pipeline_fanout.py's item_tasks());
  - tool tasks, only for the ToolStepsConfig steps ticked in the launchpad
    (lib/tool_steps.py): one per file for OCR / alt text / figures / tag-pdf,
    and one for split-spreads. They run `digtk workspace run <item>`, which
    writes a bronze item's outputs to its silver folder (text/,
    accessibility/, split-spreads/). split_detect and split_apply are meant to
    be separate runs, with gutters.json reviewed in between.

pdc_finalize_item fails the run if any derivative failed, and only finalizes
items with image or PDF outputs. pdc_build_item_pdf then runs pdf-build, if
ticked, now that the file steps are done. pdc_generate_item_record runs
`digtk showcase build-item <item>` (it reads any alt text the tool steps
wrote) and needs an ark, so an ark-less item fails here. pdc_index_item runs
`digtk showcase es upsert <ark>` (ItemRunConfig.index_es). A failed tool step
doesn't stop the item being published: pdc_check_tool_failures fails the run
afterwards instead.

Task ids, events and digtk reporting rows use the item's collection key (the
first three path segments, "bronze/[group]/[collection]"). Runs are tagged
argonath/item and argonath/collection, plus argonath/ark once
pdc_generate_item_record has built the record, so they're searchable in the
Runs screen. Queued runs (see lib.pipeline_collection.item_queue) already have
argonath/item and argonath/collection at launch.

Each run's outcome is recorded as the item's gold_dcs_process_item result in
collection_state (see lib.collection_state): "success" once the item is
indexed with no tool failures, and "failed" from the op that fails. A recorded
result also tags the run argonath/result=<status>. process_item_failure
records "failed" for failed runs without that tag, e.g. a crashed worker or an
unexpected error, using the run's step errors. In-flight state isn't recorded:
it lives in Dagster's own run tracking.
"""
import dagster as dg
from dagster import (
    DynamicOut,
    DynamicOutput,
    In,
    Out,
    OpExecutionContext,
    Output,
    job,
    op,
)

from lib import collection_state
from lib.configs import ItemRunConfig
from lib.fanout_batching import batches, log_batch, log_batching, relative_id, step_key
from lib.pipeline_fanout import (
    _PER_FILE_FORMATS,
    _error_task,
    _run_derivative_task,
    build_item_record,
    finalize_one_item,
    item_tasks,
)
from lib.tool_steps import folder_tool_tasks, run_tool_task
from lib.utils import celery_job_executor, default_run_config, exec


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
def pdc_plan_item(context: OpExecutionContext, config: ItemRunConfig):
    """List the item's files and fan out its derivative tasks, plus a tool
    task per file (and one for split-spreads) when tool steps are ticked,
    batched so there are at most config.max_steps steps (see lib.fanout_batching).

    An item that can't be listed at all (e.g. its bronze folder has no media)
    becomes a single error task, so the run reports it and fails. The item is
    addressed by its bronze path only — its ARK isn't read until
    pdc_generate_item_record (see module docstring).

    :param context: Dagster op execution context (unpartitioned).
    :param config: ItemRunConfig: the item folder, widths, max_steps, index_es
        and the ToolStepsConfig steps and options.
    :raises ValueError: If config.item is empty or isn't a bronze item folder.
    """
    if not config.item.strip():
        raise ValueError("config.item is required: the item's bronze folder, e.g. bronze/preservica/haeger/latest/tree/quarter-mile")
    item, collection_key = normalize_item(config.item)
    item_id = relative_id(item, collection_key)

    context.instance.add_run_tags(context.run_id, {"argonath/item": item, "argonath/collection": collection_key})

    tools = {"folder": None, "files": [], "document": None}
    listing = exec(["digtk", "showcase", "formats", item, "--files"], {"check": False})
    if listing.get("status") == "error" or listing.get("error"):
        error = listing.get("error") or f"listing formats failed: {listing}"
        context.log.error(f"[{item_id}] {error}")
        tasks = [_error_task(item, item_id, item_id, error, collection_key, config)]
    else:
        tasks = item_tasks(item, listing, collection_key, config)
        for task in tasks:
            if task["error"]:
                context.log.error(f"[{task['item_id']}] {task['error']}")
        files = listing.get("files") or {}
        tools = folder_tool_tasks(
            f"cask:/{item}", item_id, (files.get("image") or []) + (files.get("pdf") or []), config,
            {"suffix": " (tools)", "extra": {"kind": "tool", "collection_key": collection_key}},
        )

    for task in tasks:
        task["kind"] = "derivative"
    tool_tasks = ([tools["folder"]] if tools["folder"] else []) + tools["files"]

    per_file = sum(1 for t in tasks if t["file"])
    context.log.info(
        f"{item}: {per_file} image/PDF task(s), {len(tasks) - per_file} item-level task(s), "
        f"{len(tool_tasks)} tool task(s){', then pdf-build' if tools['document'] else ''}"
    )
    all_tasks = tasks + tool_tasks
    grouped = batches(all_tasks, config.max_steps)
    log_batching(context, "derivatives and tools", len(all_tasks), grouped, config.max_steps)
    used = set()
    for group in grouped:
        yield DynamicOutput(group, output_name="tasks", mapping_key=step_key([t["id"] for t in group], used))

    yield Output({
        "item": item,
        "collection_key": collection_key,
        "id": item_id,
        "index_es": config.index_es,
        "document": tools["document"],
    }, output_name="item")


def _run_task(context, task):
    """Run one derivative or tool task. Never raises.

    :param context: Dagster op execution context.
    :param dict task: A pdc_plan_item task ("kind" is "derivative" or "tool").
    :returns: The task's outcome: a derivative outcome (see
        lib.pipeline_fanout._run_derivative_task()) or {"id", "ok", "errors"},
        plus "kind".
    :rtype: dict
    """
    if task["kind"] == "tool":
        result = run_tool_task(context, task, {"collection": task["collection_key"]})
        return {"kind": "tool", "id": task["id"], "ok": result["ok"] and not result["errors"], "errors": result["errors"]}
    return {**_run_derivative_task(context, task), "kind": "derivative"}


@op(ins={"tasks": In(list)}, out=Out(list))
def pdc_process_batch(context: OpExecutionContext, tasks: list) -> list:
    """Run one step's tasks — a single file's derivatives or tool steps, or a
    batch of them. Failures are returned, not raised, so one bad file doesn't
    stop the rest; pdc_finalize_item and pdc_check_tool_failures act on them.

    :param context: Dagster op execution context.
    :param list[dict] tasks: pdc_plan_item tasks.
    :returns: One outcome per task (see _run_task()).
    :rtype: list[dict]
    """
    log_batch(context, [t["id"] for t in tasks])
    return [_run_task(context, task) for task in tasks]


def _tool_errors(outcomes):
    """
    :param list[dict] outcomes: Tool task outcomes.
    :returns: "id: error" for each failed tool step.
    :rtype: list[str]
    """
    return [f"{o['id']}: {e}" for o in outcomes if not o["ok"] for e in (o["errors"] or ["failed"])]


@op
def pdc_finalize_item(context: OpExecutionContext, results: list, item: dict) -> dict:
    """Fail the run if any derivative task failed; otherwise write the item's
    item-level outputs (`digtk showcase finalize`) if it has image or PDF
    outputs. A video/audio-only item has nothing to finalize. Failed tool
    tasks don't block: they're passed on as "tool_errors".

    :param context: Dagster op execution context.
    :param list[list[dict]] results: Every pdc_process_batch's outcomes.
    :param dict item: pdc_plan_item's item output.
    :returns: The item dict plus "tool_errors" (list[str]).
    :rtype: dict
    :raises RuntimeError: If any derivative task failed, or finalize failed.
    """
    all_outcomes = [o for batch in results for o in batch]
    outcomes = [o for o in all_outcomes if o["kind"] == "derivative"]
    item = {**item, "tool_errors": _tool_errors([o for o in all_outcomes if o["kind"] == "tool"])}
    for error in item["tool_errors"]:
        context.log.error(f"[{item['id']}] tool step failed: {error}")

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

    error = finalize_one_item(context, item["item"], item["id"], item["collection_key"])
    if error is not None:
        error = f"{item['id']} failed to finalize: {error}"
        _record(context, item, "failed", {"error": error})
        raise RuntimeError(error)
    return item


@op
def pdc_build_item_pdf(context: OpExecutionContext, item: dict) -> dict:
    """Build the item's images into one tagged PDF (pdf-build), if ticked, now
    that every file step (e.g. OCR it can reuse) has finished. A failure is
    added to "tool_errors" rather than raised.

    :param context: Dagster op execution context.
    :param dict item: pdc_finalize_item's item dict.
    :returns: The same item dict, with any pdf-build error added.
    :rtype: dict
    """
    task = item["document"]
    if not task:
        return item
    log_batch(context, [task["id"]])
    result = run_tool_task(context, task, {"collection": item["collection_key"]})
    if result["ok"] and not result["errors"]:
        return item
    return {**item, "tool_errors": item["tool_errors"] + _tool_errors([{**result, "id": task["id"], "ok": False}])}


@op
def pdc_generate_item_record(context: OpExecutionContext, item: dict) -> dict:
    """Assemble and write the item's complete, canonical gold item.jsonld.json
    (`digtk showcase build-item`) from its now-finalized derivatives, any alt
    text the tool steps wrote, and bronze identifiers/metadata.

    Unlike the steps before it, this one requires an ark and fails outright
    without one — the canonical public record needs a stable identifier (see
    digtk.models.digital_website.item_record.build()'s docstring). It's the
    first step that knows the item's ark: it's read from the built record,
    tagged on the run (argonath/ark) and passed on to pdc_index_item.

    :param context: Dagster op execution context.
    :param dict item: pdc_build_item_pdf's item dict.
    :returns: The item dict plus its "ark", passed on to pdc_index_item.
    :rtype: dict
    :raises RuntimeError: If building the item record failed (including: no ark).
    """
    error, ark = build_item_record(context, item["item"], item["id"], item["collection_key"])
    if error is not None:
        error = f"{item['id']} failed to build its item record: {error}"
        _record(context, item, "failed", {"error": error})
        raise RuntimeError(error)
    context.instance.add_run_tags(context.run_id, {"argonath/ark": ark})
    return {**item, "ark": ark}


@op
def pdc_index_item(context: OpExecutionContext, item: dict) -> dict:
    """Index the item into the showcase Elasticsearch index (`digtk showcase es
    upsert`) if ItemRunConfig.index_es is set, then record the job as a
    success unless a tool step failed (see pdc_check_tool_failures).

    :param context: Dagster op execution context.
    :param dict item: pdc_generate_item_record's item dict (with "ark").
    :returns: The same item dict.
    :rtype: dict
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
    if not item["tool_errors"]:
        _record(context, item, "success")
    return item


@op
def pdc_check_tool_failures(context: OpExecutionContext, item: dict):
    """Fail the run if any tool step failed, after the item was published,
    and record "failed" with those errors.

    :param context: Dagster op execution context.
    :param dict item: pdc_index_item's item dict.
    :raises RuntimeError: If any tool step failed.
    """
    errors = item["tool_errors"]
    if not errors:
        return
    error = f"{len(errors)} tool step(s) failed for {item['id']}: {'; '.join(errors)}"
    _record(context, item, "failed", {"error": error})
    raise RuntimeError(error)


def _record(context, item, status, opts={}):
    """Record this run's result for the item in collection_state (best-effort)
    and tag the run with it.

    :param context: Dagster op execution context.
    :param dict item: pdc_plan_item's item dict.
    :param str status: "success" or "failed".
    :param dict opts: 'error' (str).
    """
    result = collection_state.item_result(context, item, status, opts)
    collection_state.record_item_result(item["item"], result, {"log": context.log, "context": context})


@job(
    # "dagster/max_runtime": "0": no duration-based auto-cancel. A large item
    # can have hundreds of pages. project-anduin's base image sets a global
    # max_runtime_seconds: 600; dead-run detection still applies.
    tags={"argonath/kind": "item", "dagster/max_runtime": "0"},
    executor_def=celery_job_executor(),
    config=default_run_config({"pdc_plan_item": ItemRunConfig}),
)
def gold_dcs_process_item():
    """Builds an items derivatives in silver, the metadata records in gold, moves
    download files to gold, and updates elastic with the item record.
    Usually this is triggered from the item_queue sensor, by the
    bronze_collection_cask_audit sensor or the gold_dcs_process_collection job.
    Pike tool outputs (OCR, alt text, figures, PDF tagging/building, split-spreads) are also
    built if set in the launchpad.
    """
    tasks, item = pdc_plan_item()
    results = tasks.map(pdc_process_batch).collect()
    item = pdc_build_item_pdf(pdc_finalize_item(results, item))
    pdc_check_tool_failures(pdc_index_item(pdc_generate_item_record(item)))


@dg.run_failure_sensor(
    monitored_jobs=[gold_dcs_process_item], 
    description=(
        "Listens for failed item runs triggered by `gold_dcs_process_item`. That job does finalize and report success and failures, "
        "but if it failed unexpectedly (ie a crashed worker) and didn't record a result, "
        "the `gold_dcs_collection_rollup_trigger` will never be triggered and the collection's gold record won't be updated. "
    ),
)
def process_item_failure(context: dg.RunFailureSensorContext):
    """Record "failed" in collection_state for failed gold_dcs_process_item runs
    that didn't record a result themselves — no argonath/result tag, e.g. a
    crashed celery worker or an unexpected error in an op. The error is taken
    from the run's step failures. Runs whose op already recorded "failed" are
    left alone, so that op's more specific error is kept. Canceled runs don't
    count as failures, so they're not recorded here.

    The item comes from the run's argonath/item tag. A run that failed before
    pdc_plan_item tagged it falls back to its run config.

    :param context: Dagster run failure sensor context.
    """
    run = context.dagster_run
    if run.tags.get(collection_state.RESULT_TAG):
        return
    item = run.tags.get("argonath/item")
    if not item:
        configured = (
            ((run.run_config or {}).get("ops") or {}).get("pdc_plan_item", {}).get("config", {}).get("item")
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
