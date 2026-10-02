"""
Workspace fan-out: run the root-level digtk tools (split-spreads, OCR, alt
text, PDF tagging) over a folder of scans staged into CaskFS workspace before
it has an ARK or a bronze copy — see digtk.models.workspace.

Same op-based DynamicOut pattern as lib/pipeline_fanout.py's
process_collection_job, but partitioned by lib.configs.workspace_partitions
("workspace/[username]/[collection]", kept in sync by
lib.sensors.workspace_partition_sync_sensor) instead of registered bronze
collections: one run per workspace collection. fan_out_workspace lists every
item folder under its source/ and their files (`digtk workspace items --files`),
then fans out two ways from the one WorkspaceRunConfig:

  - one step per (folder, file) for OCR / alt text / PDF tagging — so a folder
    of 10 TIFFs and a PDF is 11 steps, each its own celery task and
    retryable on its own (`digtk workspace run <folder> --file <name>`;
    output naming still follows the whole folder, so per-file runs write the
    same paths a whole-folder run would);
  - one step per folder for split-spreads detect/apply, which keeps a single
    gutters.json and set of contact sheets per folder.

Dagster can't nest fan-outs, so the per-file steps are one flat list, keyed
"<folder>__<file>". Every step passes this run's id as --reporting-job-id, so
each folder/file and step gets its own digtk.lib.reporting row.

Getting files in is a separate, earlier step — Dagster's containers only reach
CaskFS over HTTP and can't see a host folder, so a folder is staged with
`digtk workspace add <folder>` (or uploaded into
/workspace/[username]/[collection]/source/ some other way) first. Outputs land
in that collection's derivatives/.

promote_workspace_job is the last step of that workflow: once a reviewed item
has been ingested to bronze under an ARK, it copies the item's outputs into the
ARK's silver location (`digtk workspace promote`, one call per {source, ark}
entry in PromoteWorkspaceConfig), dry run first by default.
"""
import re

from dagster import (
    DynamicOut,
    DynamicOutput,
    In,
    OpExecutionContext,
    job,
    op,
)
from dagster_celery import celery_executor

from lib.configs import workspace_partitions, PromoteWorkspaceConfig, WorkspaceRunConfig
from lib.pipeline_fanout import _emit
from lib.utils import exec, load_celery_run_config

# WorkspaceRunConfig checkbox -> `digtk workspace run --steps` name, split by
# whether the step runs once per folder or once per file.
_FOLDER_STEP_FIELDS = {"split_detect": "split-detect", "split_apply": "split-apply"}
_FILE_STEP_FIELDS = {"ocr": "ocr", "alt_text": "alt-text", "tag_pdf": "tag-pdf"}
# Steps that only apply to PDFs (everything else applies to images and PDFs alike).
_PDF_ONLY_STEPS = {"tag-pdf"}


def _workspace_path(partition_key):
    """
    :param str partition_key: "workspace/[username]/[collection]" (an older
        "[username]/[collection]" key is accepted too).
    :returns: e.g. 'cask:/workspace/jdoe/box-12'.
    :rtype: str
    """
    key = partition_key.strip("/")
    if not key.startswith("workspace/"):
        key = f"workspace/{key}"
    return f"cask:/{key}"


def _mapping_key(index, *parts):
    """DynamicOutput mapping keys are restricted to ``^[A-Za-z0-9_]+$`` — a
    workspace path or filename is full of '/', '-' and '.', so sanitize it, and
    prefix the position so two names that sanitize alike (e.g. 'a-b' and 'a_b')
    still get distinct keys.

    :param int index: Position in the fan-out.
    :param str parts: Name segments, e.g. the folder and the file; joined with '__'.
    :rtype: str
    """
    name = "__".join(re.sub(r"[^A-Za-z0-9_]", "_", p) for p in parts)
    return f"i{index}_{name}"[:255]


def _folder_name(item_dir):
    """
    :param str item_dir: 'cask:/workspace/.../source[/...]'.
    :returns: The folder's path under source/, or 'root' for source/ itself.
    :rtype: str
    """
    return item_dir.split("/source", 1)[-1].strip("/") or "root"


def _option_args(config):
    """`digtk workspace run` tool-option flags for a WorkspaceRunConfig (everything
    but --steps, which differs per fanned-out task).

    :param WorkspaceRunConfig config: The fan-out op's config.
    :rtype: list[str]
    """
    args = [
        "--max-px", str(config.max_px),
        "--lang", config.lang, "--psm", str(config.psm), "--oem", str(config.oem),
        "--vision" if config.vision else "--no-vision",
        "--max-alt-chars", str(config.max_alt_chars),
        "--doc-lang", config.doc_lang,
        "--tables" if config.tables else "--no-tables",
        "--min-conf", str(config.min_conf),
        "--validate" if config.validate else "--no-validate",
    ]
    if config.collection_context:
        args += ["--collection-context", config.collection_context]
    if config.want_description:
        args.append("--want-description")
    return args


def _ticked(config, fields):
    """
    :param WorkspaceRunConfig config: The fan-out op's config.
    :param dict fields: {config field: step name}.
    :returns: Step names whose checkbox is on, in field order.
    :rtype: list[str]
    """
    return [step for field, step in fields.items() if getattr(config, field)]


@op(out={
    "folders": DynamicOut(dict, is_required=False),
    "files": DynamicOut(dict, is_required=False),
})
def fan_out_workspace(context: OpExecutionContext, config: WorkspaceRunConfig):
    """Fan out the partition's workspace collection: one "folders" task per item
    folder (when a split-spreads step is ticked) and one "files" task per
    image/PDF that a ticked OCR/alt-text/tag-pdf step applies to.

    Each task is {"item_dir": str, "file": str|None, "steps": [str], "args": [str]}
    — everything process_workspace_folder/process_workspace_file need, so they
    take no config of their own and the launchpad has one place for options.

    :param context: Dagster op execution context; partitioned by workspace collection.
    :param config: WorkspaceRunConfig — which steps, plus their options.
    :raises ValueError: If no step is ticked.
    """
    folder_steps = _ticked(config, _FOLDER_STEP_FIELDS)
    file_steps = _ticked(config, _FILE_STEP_FIELDS)
    if not folder_steps and not file_steps:
        raise ValueError(f"tick at least one step: {sorted({**_FOLDER_STEP_FIELDS, **_FILE_STEP_FIELDS})}")

    path = _workspace_path(context.partition_key)
    listing = exec(["digtk", "workspace", "items", path, "--files"])["items"]
    args = _option_args(config)
    context.log.info(
        f"{path}: {len(listing)} folder(s), {sum(len(e['files']) for e in listing)} file(s); "
        f"steps per folder: {folder_steps or 'none'}, per file: {file_steps or 'none'}"
    )

    file_index = 0
    for i, entry in enumerate(listing):
        item_dir = entry["item"]
        folder = _folder_name(item_dir)
        if folder_steps:
            yield DynamicOutput(
                {"item_dir": item_dir, "file": None, "steps": folder_steps, "args": args},
                output_name="folders", mapping_key=_mapping_key(i, folder),
            )
        for filename in entry["files"]:
            is_pdf = filename.lower().endswith(".pdf")
            steps = [s for s in file_steps if is_pdf or s not in _PDF_ONLY_STEPS]
            if not steps:
                continue
            yield DynamicOutput(
                {"item_dir": item_dir, "file": filename, "steps": steps, "args": args},
                output_name="files", mapping_key=_mapping_key(file_index, folder, filename),
            )
            file_index += 1


def _report_run_result(context, label, result):
    """Turn a `digtk workspace run` result into Dagster events: one
    AssetMaterialization per step (failed or not), plus a warning per page a
    tool skipped.

    :param context: Dagster op execution context.
    :param str label: What the run covered — a folder, or "folder#file".
    :param dict result: The command's parsed JSON result.
    """
    for item_result in result.get("items", []):
        for step, step_result in item_result["steps"].items():
            if "skipped" in step_result:
                context.log.info(f"[{label}] {step}: skipped ({step_result['skipped']})")
                continue
            if step_result.get("status") == "error":
                _emit(context, step.replace("-", "_"), label, None, status="failed", error=step_result["error"])
                continue
            failed = step_result.get("failed") or []
            for failure in failed:
                page = f" page {failure['page']}" if failure.get("page") else ""
                context.log.warning(
                    f"[{label}] {step}: {failure['file']}{page} {failure['stage']} failed -> {failure['error']}"
                )
            _emit(context, step.replace("-", "_"), label, step_result, failed_pages=len(failed))


def _run_task(context, task):
    """Run one fanned-out task's `digtk workspace run` and report its result.

    :param context: Dagster op execution context; partitioned by workspace collection.
    :param dict task: A fan_out_workspace task.
    """
    label = f"{task['item_dir']}#{task['file']}" if task["file"] else task["item_dir"]
    cmd = ["digtk", "workspace", "run", task["item_dir"], "--steps", ",".join(task["steps"])]
    if task["file"]:
        cmd += ["--file", task["file"]]
    cmd += task["args"] + [
        "--reporting-job-id", context.run_id, "--reporting-collection", context.partition_key,
    ]
    try:
        result = exec(cmd)
    except Exception as e:
        _emit(context, "workspace", label, None, status="failed", error=str(e))
        raise
    _report_run_result(context, label, result)


@op(ins={"task": In(dict)})
def process_workspace_folder(context: OpExecutionContext, task: dict):
    """Run the ticked split-spreads step(s) for one item folder.

    :param context: Dagster op execution context; partitioned by workspace collection.
    :param dict task: A fan_out_workspace "folders" task.
    """
    _run_task(context, task)


@op(ins={"task": In(dict)})
def process_workspace_file(context: OpExecutionContext, task: dict):
    """Run the ticked OCR / alt-text / tag-pdf step(s) for one file of an item folder.

    :param context: Dagster op execution context; partitioned by workspace collection.
    :param dict task: A fan_out_workspace "files" task.
    """
    _run_task(context, task)


@job(
    partitions_def=workspace_partitions,
    # Same reasoning as process_collection_job's tags: a run covers a whole
    # workspace collection, so its duration has no sane fixed ceiling.
    tags={"group": "workspace", "dagster/max_runtime": "0"},
    executor_def=celery_executor,
    config=load_celery_run_config(),
)
def process_workspace_job():
    """Workspace-collection-partitioned fan-out: one step per folder for
    split-spreads and one step per file for OCR / alt text / PDF tagging, all
    from fan_out_workspace's single WorkspaceRunConfig — one Dagster run per
    collection, each mapped step its own celery task. See this module's docstring.
    """
    folders, files = fan_out_workspace()
    folders.map(process_workspace_folder)
    files.map(process_workspace_file)


_NAAN_RE = re.compile(r"^\d+$")


def _promote_entries(config, item_dirs=()):
    """Every (source, ark) pair a PromoteWorkspaceConfig names, in order: the
    explicit ones (`ark`/`source`, then `items`), then — with ark_from_folder —
    one per remaining item folder, as ark:/<naan>/<folder name>.

    :param PromoteWorkspaceConfig config: The op's config.
    :param list[str] item_dirs: The partition's item folders
        ('cask:/workspace/.../source[/...]'), needed only for ark_from_folder.
    :returns: ([(source, ark)], [skipped notes]) — source is the folder path
        under source/ ("" for source/ itself), stripped of slashes; a note is a
        human-readable reason a listed folder wasn't included.
    :rtype: tuple[list[tuple[str, str]], list[str]]
    :raises ValueError: If nothing at all is named, or ark_from_folder is set
        without a numeric naan.
    """
    entries = []
    if config.ark:
        entries.append((config.source.strip("/"), config.ark))
    entries += [(item.source.strip("/"), item.ark) for item in config.items]

    skipped = []
    if config.ark_from_folder:
        naan = config.naan.strip().removeprefix("ark:/").strip("/")
        if not _NAAN_RE.match(naan):
            raise ValueError(f"ark_from_folder needs `naan` set to the ARKs' NAAN, e.g. '87293' (got {config.naan!r})")
        explicit = {source for source, _ in entries}
        for item_dir in item_dirs:
            source = _folder_name(item_dir)
            if source == "root":
                skipped.append(f"{item_dir}: files directly in source/ have no folder name to take an ARK from")
                continue
            if source in explicit:
                continue
            entries.append((source, f"ark:/{naan}/{source.rsplit('/', 1)[-1]}"))

    if not entries:
        raise ValueError(
            "set `ark` (or add {source, ark} entries under `items`, or set ark_from_folder + naan) "
            "to say what to promote"
        )
    return entries, skipped


@op
def promote_workspace_items(context: OpExecutionContext, config: PromoteWorkspaceConfig):
    """Promote each configured workspace item of the partition's collection
    into its ARK's silver location.

    Each entry runs as its own `digtk workspace promote` call; one failing (no
    bronze item for the ARK, mismatched filenames, outputs already in silver
    without replace) is logged and the rest still run. Fails only if every
    entry failed.

    :param context: Dagster op execution context; partitioned by workspace collection.
    :param config: PromoteWorkspaceConfig — which items, which ARKs, dry run or not.
    """
    path = _workspace_path(context.partition_key)
    root = f"{path}/source"
    item_dirs = exec(["digtk", "workspace", "items", path])["items"] if config.ark_from_folder else []
    entries, skipped = _promote_entries(config, item_dirs)
    for note in skipped:
        context.log.warning(f"skipped {note}")
    verb = "would copy" if config.dry_run else "copied"
    context.log.info(f"{path}: promoting {len(entries)} item(s){' (dry run)' if config.dry_run else ''}")
    failures = 0

    for source, ark in entries:
        item_dir = f"{root}/{source}" if source else root
        cmd = ["digtk", "workspace", "promote", item_dir, "--ark", ark]
        if config.replace:
            cmd.append("--replace")
        if config.dry_run:
            cmd.append("--dry-run")

        result = exec(cmd, opts={"check": False})
        if result.get("status") != "success":
            failures += 1
            _emit(context, "promote", item_dir, None, status="failed", error=result.get("error") or result, target_ark=ark)
            continue

        promoted = result.get("promoted", [])
        context.log.info(f"[{item_dir}] -> {ark}: {verb} {len(promoted)} file(s)")
        for entry in promoted:
            context.log.info(f"  {entry['to']}")
        _emit(context, "promote", item_dir, result, target_ark=ark, dry_run=config.dry_run, files=len(promoted))

    outcome = "ready to promote" if config.dry_run else "promoted"
    context.log.info(f"{path}: {len(entries) - failures} of {len(entries)} item(s) {outcome}, {failures} failed")
    if failures == len(entries):
        raise RuntimeError(f"all {failures} promote(s) failed — see the errors above")


# Pre-fills the launchpad so its fields are visible: without a job-level default,
# every PromoteWorkspaceConfig field being optional leaves the editor blank and
# "Scaffold missing config" adds nothing. Leaving everything empty still fails
# with _promote_entries()'s clear error rather than promoting nothing.
_PROMOTE_DEFAULT_CONFIG = {
    "ops": {
        "promote_workspace_items": {
            "config": {
                "ark": "", "source": "", "items": [],
                "ark_from_folder": False, "naan": "",
                "dry_run": True, "replace": False,
            },
        },
    },
}


@job(
    partitions_def=workspace_partitions,
    tags={"group": "workspace"},
    config=_PROMOTE_DEFAULT_CONFIG,
)
def promote_workspace_job():
    """Copy reviewed workspace items' outputs into their ARKs' silver locations
    — one run per workspace collection. Defaults to a dry run; see this
    module's docstring and PromoteWorkspaceConfig.
    """
    promote_workspace_items()
