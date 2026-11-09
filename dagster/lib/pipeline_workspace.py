"""
Workspace fan-out: run the root-level digtk tools (split-spreads, OCR, alt
text, PDF tagging) over a folder of scans staged into CaskFS workspace before
it has an ARK or a bronze copy — see digtk.models.workspace.

Op-based DynamicOut fan-out (like lib/pipeline_item.py's
gold_dcs_process_item, over a whole folder), partitioned by lib.configs.workspace_partitions
("workspace/[username]/[folder]", kept in sync by
lib.sensors.workspace_partition_sync) instead of registered bronze
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
"""
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

from lib.configs import workspace_partitions, WorkspaceRunConfig
from lib.fanout_batching import batches, log_batch, log_batching, relative_id, step_key
from lib.tool_steps import (
    DOCUMENT_STEP_FIELDS,
    FILE_STEP_FIELDS,
    FOLDER_STEP_FIELDS,
    any_ticked,
    folder_tool_tasks,
    run_tool_task,
    ticked,
)
from lib.utils import celery_job_executor, default_run_config, exec


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
    :param config: WorkspaceRunConfig — which steps, their options (see
        lib.configs.ToolStepsConfig), and max_steps.
    :raises ValueError: If no step is ticked.
    """
    if not any_ticked(config):
        all_fields = {**FOLDER_STEP_FIELDS, **FILE_STEP_FIELDS, **DOCUMENT_STEP_FIELDS}
        raise ValueError(f"tick at least one step: {sorted(all_fields)}")
    folder_steps = ticked(config, FOLDER_STEP_FIELDS)
    any_file_steps = ticked(config, FILE_STEP_FIELDS)
    document_steps = ticked(config, DOCUMENT_STEP_FIELDS)

    path = _workspace_path(context.partition_key)
    listing = exec(["digtk", "workspace", "items", path, "--files"])["items"]
    context.log.info(
        f"{path}: {len(listing)} folder(s), {sum(len(e['files']) for e in listing)} file(s); "
        f"steps per folder: {folder_steps or 'none'}, per file: {any_file_steps or 'none'}, "
        f"per document: {document_steps or 'none'}"
    )

    folder_tasks, file_tasks, document_tasks = [], [], []
    for entry in listing:
        item_dir = entry["item"]
        found = folder_tool_tasks(item_dir, relative_id(item_dir, context.partition_key), entry["files"], config)
        if found["folder"]:
            folder_tasks.append(found["folder"])
        if found["document"]:
            document_tasks.append(found["document"])
        file_tasks.extend(found["files"])

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
    # "dagster/max_runtime": "0" disables duration-based auto-cancel: a run
    # covers a whole workspace folder, so it has no sane fixed ceiling.
    tags={"group": "workspace", "dagster/max_runtime": "0"},
    executor_def=celery_job_executor(),
    config=default_run_config({"fan_out_workspace": WorkspaceRunConfig}),
)
def workspace_process():
    """Process a workspace folder and its contents, one step per folder.
    Steps include split-spreads, OCR, alt text, PDF tagging, and others.
    Files are processed and saved to the workspace folder's derivatives/ subfolder.
    """
    folders, files, documents, documents_now = fan_out_workspace()
    folders.map(process_workspace_folder)
    files_done = files.map(process_workspace_file).collect()
    fan_out_documents(groups=documents, files_done=files_done).map(process_workspace_document)
    documents_now.map(process_workspace_document)

