"""
Workspace fan-out: run the root-level digtk tools (split-spreads, OCR, alt
text, PDF tagging) over a folder of scans staged into CaskFS workspace before
it has an ARK or a bronze copy — see digtk.models.workspace.

Same op-based DynamicOut pattern as lib/pipeline_fanout.py's
showcase_process_collection_job, but partitioned by lib.configs.workspace_partitions
("workspace/[username]/[folder]", kept in sync by
lib.sensors.workspace_partition_sync_sensor) instead of registered bronze
collections: one run per workspace folder. fan_out_workspace lists every
item folder under its source/ and their files (`digtk workspace items --files`),
then fans out three ways from the one WorkspaceRunConfig:

  - one step per (folder, file) for OCR / alt text / PDF tagging — so a folder
    of 10 TIFFs and a PDF is 11 steps, each its own celery task and
    retryable on its own (`digtk workspace run <folder> --file <name>`;
    output naming still follows the whole folder, so per-file runs write the
    same paths a whole-folder run would);
  - one step per folder for split-spreads detect/apply, which keeps a single
    gutters.json and set of contact sheets per folder;
  - one step per folder of images for pdf-build, run after every file step
    so it can reuse their OCR.

Dagster can't nest fan-outs, so the per-file steps are one flat list. Steps
and events are named by the file's (or folder's) path relative to the
partition, e.g. "source/d3bv7b33r/page_001.tif". Each fan-out is capped at
WorkspaceRunConfig.max_steps steps: past that, several files (or folders) share
one step and run one after another, each still reporting its own result events
(see lib.fanout_batching). Every task passes this run's id as
--reporting-job-id, so each folder/file and step gets its own
digtk.lib.reporting row.

Getting files in is a separate, earlier step — Dagster's containers only reach
CaskFS over HTTP and can't see a host folder, so a folder is staged with
`digtk workspace add <folder>` (or uploaded into
/workspace/[username]/[folder]/source/ some other way) first. Outputs land
in that folder's derivatives/.

workspace_promote_job is the last step of that workflow: once a reviewed item
has been ingested to bronze under an ARK, it copies the item's outputs into the
ARK's silver location (`digtk workspace promote`, one call per {source, ark}
entry in PromoteWorkspaceConfig), dry run first by default.
"""
import re

from dagster import (
    DynamicOut,
    DynamicOutput,
    In,
    Nothing,
    OpExecutionContext,
    Out,
    Output,
    job,
    op,
)

from lib.configs import workspace_partitions, PromoteWorkspaceConfig, WorkspaceRunConfig
from lib.fanout_batching import batches, emit, log_batch, log_batching, relative_id, step_key
from lib.tool_steps import FILE_STEP_FIELDS, file_steps, option_args, run_tool_task, ticked
from lib.utils import celery_job_executor, default_run_config, exec

# WorkspaceRunConfig checkbox -> `digtk workspace run --steps` name, for the
# steps that run once per folder (file steps: lib.tool_steps).
_FOLDER_STEP_FIELDS = {"split_detect": "split-detect", "split_apply": "split-apply"}
# Folder steps run after file steps.
_DOCUMENT_STEP_FIELDS = {"pdf_build": "pdf-build"}


def _workspace_path(partition_key):
    """
    :param str partition_key: "workspace/[username]/[folder]" (an older
        "[username]/[folder]" key is accepted too).
    :returns: e.g. 'cask:/workspace/jdoe/box-12'.
    :rtype: str
    """
    key = partition_key.strip("/")
    if not key.startswith("workspace/"):
        key = f"workspace/{key}"
    return f"cask:/{key}"


def _folder_name(item_dir):
    """
    :param str item_dir: 'cask:/workspace/.../source[/...]'.
    :returns: The folder's path under source/, or 'root' for source/ itself.
    :rtype: str
    """
    return item_dir.split("/source", 1)[-1].strip("/") or "root"


@op(out={
    "folders": DynamicOut(list, is_required=False),
    "files": DynamicOut(list, is_required=False),
    "documents": Out(list, is_required=False),
    "documents_now": DynamicOut(list, is_required=False),
})
def fan_out_workspace(context: OpExecutionContext, config: WorkspaceRunConfig):
    """Fan out the partition's workspace folder: "folders" tasks (one per
    item folder, when a split-spreads step is ticked) and "files" tasks (one
    per image/PDF that a ticked OCR/alt-text/tag-pdf step applies to), each
    batched so there are at most config.max_steps steps (see lib.fanout_batching).

    Each task is {"target": str (item folder), "file": str|None, "id": str (its path
    relative to the partition, e.g. "source/d3bv7b33r/page_001.tif"),
    "steps": [str], "args": [str]}; each DynamicOutput is a list of tasks, its
    mapping key every task id joined with '__'. The process ops take no config
    of their own, so the launchpad has one place for options.

    :param context: Dagster op execution context; partitioned by workspace folder.
    :param config: WorkspaceRunConfig — which steps, their options, and max_steps.
    :raises ValueError: If no step is ticked.
    """
    folder_steps = ticked(config, _FOLDER_STEP_FIELDS)
    any_file_steps = ticked(config, FILE_STEP_FIELDS)
    document_steps = ticked(config, _DOCUMENT_STEP_FIELDS)
    if not folder_steps and not any_file_steps and not document_steps:
        all_fields = {**_FOLDER_STEP_FIELDS, **FILE_STEP_FIELDS, **_DOCUMENT_STEP_FIELDS}
        raise ValueError(f"tick at least one step: {sorted(all_fields)}")

    path = _workspace_path(context.partition_key)
    listing = exec(["digtk", "workspace", "items", path, "--files"])["items"]
    args = option_args(config)
    context.log.info(
        f"{path}: {len(listing)} folder(s), {sum(len(e['files']) for e in listing)} file(s); "
        f"steps per folder: {folder_steps or 'none'}, per file: {any_file_steps or 'none'}, "
        f"per document: {document_steps or 'none'}"
    )

    folder_tasks, file_tasks, document_tasks = [], [], []
    for entry in listing:
        item_dir = entry["item"]
        folder_id = relative_id(item_dir, context.partition_key)
        if folder_steps:
            folder_tasks.append({"target": item_dir, "file": None, "id": folder_id, "steps": folder_steps, "args": args})
        if document_steps and any(not f.lower().endswith(".pdf") for f in entry["files"]):
            document_tasks.append({"target": item_dir, "file": None, "id": folder_id, "steps": document_steps, "args": args})
        for filename in entry["files"]:
            steps = file_steps(config, filename)
            if steps:
                file_tasks.append({
                    "target": item_dir, "file": filename, "id": f"{folder_id}/{filename}",
                    "steps": steps, "args": args,
                })

    used = set()
    if document_tasks:
        grouped = batches(document_tasks, config.max_steps)
        log_batching(context, "documents", len(document_tasks), grouped, config.max_steps)
        if file_tasks:  # wait for the file steps
            yield Output(grouped, output_name="documents")
        else:
            used = set()
            for group in grouped:
                yield DynamicOutput(group, output_name="documents_now",
                                    mapping_key=step_key([t["id"] for t in group], used))

    for output_name, tasks in (("folders", folder_tasks), ("files", file_tasks)):
        if not tasks:
            continue
        grouped = batches(tasks, config.max_steps)
        log_batching(context, output_name, len(tasks), grouped, config.max_steps)
        for group in grouped:
            yield DynamicOutput(
                group, output_name=output_name, mapping_key=step_key([t["id"] for t in group], used),
            )


def _run_batch(context, tasks):
    """Run one step's tasks (a single one, or a batch) one after another; a
    failed task doesn't stop the rest, but fails the step at the end.

    :param context: Dagster op execution context; partitioned by workspace folder.
    :param list[dict] tasks: fan_out_workspace tasks.
    :raises RuntimeError: If any task's command failed outright.
    """
    log_batch(context, [t["id"] for t in tasks])
    failed = [t["id"] for t in tasks if not run_tool_task(context, t)["ok"]]
    if failed:
        raise RuntimeError(f"{len(failed)} of {len(tasks)} task(s) failed: {', '.join(failed)}")


@op(ins={"tasks": In(list)})
def process_workspace_folder(context: OpExecutionContext, tasks: list):
    """Run the ticked split-spreads step(s) for one step's item folder(s).

    :param context: Dagster op execution context; partitioned by workspace folder.
    :param list[dict] tasks: fan_out_workspace "folders" tasks.
    """
    _run_batch(context, tasks)


@op(ins={"tasks": In(list)})
def process_workspace_file(context: OpExecutionContext, tasks: list):
    """Run the ticked OCR / alt-text / tag-pdf step(s) for one step's file(s).

    :param context: Dagster op execution context; partitioned by workspace folder.
    :param list[dict] tasks: fan_out_workspace "files" tasks.
    """
    _run_batch(context, tasks)


@op(ins={"groups": In(list), "files_done": In(Nothing)}, out=DynamicOut(list))
def fan_out_documents(context: OpExecutionContext, groups: list):
    """Fan out the pdf-build batches once every file step has finished.

    :param context: Dagster op execution context; partitioned by workspace folder.
    :param list[list[dict]] groups: Batches of fan_out_workspace "documents" tasks.
    """
    used = set()
    for group in groups:
        yield DynamicOutput(group, mapping_key=step_key([t["id"] for t in group], used))


@op(ins={"tasks": In(list)})
def process_workspace_document(context: OpExecutionContext, tasks: list):
    """Build one step's folder(s) of images into a tagged PDF, after the file steps.

    :param context: Dagster op execution context; partitioned by workspace folder.
    :param list[dict] tasks: fan_out_workspace "documents" tasks.
    """
    _run_batch(context, tasks)


@job(
    partitions_def=workspace_partitions,
    # Same reasoning as showcase_process_collection_job's tags: a run covers a whole
    # workspace folder, so its duration has no sane fixed ceiling.
    tags={"group": "workspace", "dagster/max_runtime": "0"},
    executor_def=celery_job_executor(),
    config=default_run_config({"fan_out_workspace": WorkspaceRunConfig}),
)
def workspace_process_job():
    """Workspace-folder-partitioned fan-out: one step per folder for
    split-spreads and one step per file for OCR / alt text / PDF tagging, all
    from fan_out_workspace's single WorkspaceRunConfig — one Dagster run per
    workspace folder, each mapped step its own celery task. See this module's docstring.
    """
    folders, files, documents, documents_now = fan_out_workspace()
    folders.map(process_workspace_folder)
    files_done = files.map(process_workspace_file).collect()
    fan_out_documents(groups=documents, files_done=files_done).map(process_workspace_document)
    documents_now.map(process_workspace_document)


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
    """Promote each configured workspace item of the partition's folder
    into its ARK's silver location.

    Each entry runs as its own `digtk workspace promote` call; one failing (no
    bronze item for the ARK, mismatched filenames, outputs already in silver
    without replace) is logged and the rest still run. Fails only if every
    entry failed.

    :param context: Dagster op execution context; partitioned by workspace folder.
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
            emit(context, "promote", item_dir, None, status="failed", error=result.get("error") or result, target_ark=ark)
            continue

        promoted = result.get("promoted", [])
        context.log.info(f"[{item_dir}] -> {ark}: {verb} {len(promoted)} file(s)")
        for entry in promoted:
            context.log.info(f"  {entry['to']}")
        emit(context, "promote", item_dir, result, target_ark=ark, dry_run=config.dry_run, files=len(promoted))

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
def workspace_promote_job():
    """Copy reviewed workspace items' outputs into their ARKs' silver locations
    — one run per workspace folder. Defaults to a dry run; see this
    module's docstring and PromoteWorkspaceConfig.
    """
    promote_workspace_items()
