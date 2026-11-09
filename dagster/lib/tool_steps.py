"""
The pike tool steps (split-spreads, OCR, alt text, figures, PDF tagging and
building) as Dagster runs them: `digtk workspace run <target> --steps ...
[--file <name>]` commands built from a lib.configs.ToolStepsConfig. Shared by
lib.pipeline_workspace.workspace_process (workspace folders) and
lib.pipeline_item.gold_dcs_process_item (bronze items, outputs to silver).
"""
from lib.fanout_batching import emit, relative_id
from lib.utils import exec

# ToolStepsConfig checkbox -> `digtk workspace run` step, run once per file.
FILE_STEP_FIELDS = {"ocr": "ocr", "ocr_correct": "ocr-correct", "alt_text": "alt-text",
                    "figures": "figures", "tag_pdf": "tag-pdf"}
# Steps run once per folder, alongside the file steps.
FOLDER_STEP_FIELDS = {"split_detect": "split-detect", "split_apply": "split-apply"}
# Steps run once per folder, after every file step.
DOCUMENT_STEP_FIELDS = {"pdf_build": "pdf-build"}
# Steps that only apply to PDFs (everything else applies to images and PDFs alike).
PDF_ONLY_STEPS = {"tag-pdf"}


def option_args(config):
    """`digtk workspace run` tool-option flags for a ToolStepsConfig (everything
    but --steps, which differs per fanned-out task).

    :param ToolStepsConfig config: The run's config.
    :rtype: list[str]
    """
    args = [
        "--max-px", str(config.max_px),
        "--lang", config.lang, "--psm", str(config.psm), "--oem", str(config.oem),
        "--vision" if config.vision else "--no-vision",
        "--max-alt-chars", str(config.max_alt_chars),
        "--doc-lang", config.doc_lang,
        "--tables" if config.tables else "--no-tables",
        "--strict" if config.strict else "--no-strict",
        "--paragraphs" if config.paragraphs else "--no-paragraphs",
        "--reuse-ocr" if config.reuse_ocr else "--no-reuse-ocr",
        "--correct" if config.correct else "--no-correct",
        "--mode", config.mode,
        "--figures" if config.figures else "--no-figures",
        "--geometry", config.geometry,
        "--embed-fonts" if config.embed_fonts else "--no-embed-fonts",
        "--lookahead", str(config.lookahead),
        "--min-conf", str(config.min_conf),
        "--validate" if config.validate else "--no-validate",
    ]
    if config.collection_context:
        args += ["--collection-context", config.collection_context]
    if config.want_description:
        args.append("--want-description")
    if config.want_long:
        args.append("--long")
    if config.ocr_enhance:
        args += ["--ocr-enhance", config.ocr_enhance]
    if config.ocr_text_pages:
        args.append("--ocr-text-pages")
    if config.skip_existing:
        args.append("--skip-existing")
    return args


def ticked(config, fields):
    """
    :param ToolStepsConfig config: The run's config.
    :param dict fields: {config field: step name}.
    :returns: Step names whose checkbox is on, in field order.
    :rtype: list[str]
    """
    return [step for field, step in fields.items() if getattr(config, field)]


def file_steps(config, filename):
    """The ticked per-file steps that apply to one file.

    :param ToolStepsConfig config: The run's config.
    :param str filename: The file's name or path.
    :returns: Step names; tag-pdf only for a PDF.
    :rtype: list[str]
    """
    is_pdf = filename.lower().endswith(".pdf")
    return [s for s in ticked(config, FILE_STEP_FIELDS) if is_pdf or s not in PDF_ONLY_STEPS]


def any_ticked(config):
    """Whether any tool step is ticked.

    :param ToolStepsConfig config: The run's config.
    :rtype: bool
    """
    fields = {**FOLDER_STEP_FIELDS, **FILE_STEP_FIELDS, **DOCUMENT_STEP_FIELDS}
    return bool(ticked(config, fields))


def folder_tool_tasks(target, folder_id, files, config, opts={}):
    """One folder's tool tasks: a split-spreads task, one task per file with
    ticked file steps, and a pdf-build task. Every task gets the same option args.

    :param str target: Item folder for `digtk workspace run`, e.g. 'cask:/bronze/...'.
    :param str folder_id: The folder's id, relative to the run's partition or collection.
    :param list[str] files: The folder's image/PDF files: bare names or full paths.
    :param ToolStepsConfig config: The run's config.
    :param dict opts: 'suffix' (str) appended to every task id, 'extra' (dict)
        merged into every task.
    :returns: {"folder": task|None, "files": [task], "document": task|None}. Each
        task is {"target", "file", "id", "steps", "args", ...extra}; a file task's
        "file" is its bare name and its id is folder_id plus the file's path
        relative to target.
    :rtype: dict
    """
    suffix = opts.get("suffix", "")
    extra = opts.get("extra") or {}
    args = option_args(config)
    root = target[len("cask:"):] if target.startswith("cask:") else target

    def task(file, id, steps):
        return {"target": target, "file": file, "id": id, "steps": steps, "args": args, **extra}

    folder_steps = ticked(config, FOLDER_STEP_FIELDS)
    document_steps = ticked(config, DOCUMENT_STEP_FIELDS)
    found = {"folder": None, "files": [], "document": None}
    if folder_steps:
        found["folder"] = task(None, f"{folder_id}{suffix}", folder_steps)
    # pdf-build only applies to folders of images.
    if document_steps and any(not f.lower().endswith(".pdf") for f in files):
        found["document"] = task(None, f"{folder_id}{suffix}", document_steps)
    for path in files:
        steps = file_steps(config, path)
        if steps:
            rel = relative_id(path, root)
            found["files"].append(task(path.rsplit("/", 1)[-1], f"{folder_id}/{rel}{suffix}", steps))
    return found


def report_run_result(context, label, result, opts={}):
    """Turn a `digtk workspace run` result into Dagster events: one
    AssetMaterialization per step (failed or not), plus a warning per page a
    tool skipped.

    :param context: Dagster op execution context.
    :param str label: What the run covered — a folder, or "folder#file".
    :param dict result: The command's parsed JSON result.
    :param dict opts: 'partition' (str) — the events' partition, see emit().
    :returns: "step: error" for each step that failed outright.
    :rtype: list[str]
    """
    errors = []
    for item_result in result.get("items", []):
        for step, step_result in item_result["steps"].items():
            if "skipped" in step_result:
                context.log.info(f"[{label}] {step}: skipped ({step_result['skipped']})")
                continue
            if step_result.get("status") == "error":
                errors.append(f"{step}: {step_result['error']}")
                emit(context, step.replace("-", "_"), label, None, status="failed",
                     partition=opts.get("partition"), error=step_result["error"])
                continue
            failed = step_result.get("failed") or []
            for failure in failed:
                page = f" page {failure['page']}" if failure.get("page") else ""
                context.log.warning(
                    f"[{label}] {step}: {failure['file']}{page} {failure['stage']} failed -> {failure['error']}"
                )
            emit(context, step.replace("-", "_"), label, step_result,
                 partition=opts.get("partition"), failed_pages=len(failed))
    return errors


def run_tool_task(context, task, opts={}):
    """Run one task's `digtk workspace run` and report its result as events
    labelled with the task's id.

    :param context: Dagster op execution context.
    :param dict task: {"target": item folder, "file": filename or None,
        "id": label, "steps": [str], "args": option_args()}.
    :param dict opts: 'collection' (str) — the reporting collection and event
        partition; defaults to the run's partition key.
    :returns: {"ok": False if the command failed outright, "errors": a
        message per failed step (pages failing inside a step aren't errors)}.
    :rtype: dict
    """
    collection = opts.get("collection") or context.partition_key
    cmd = ["digtk", "workspace", "run", task["target"], "--steps", ",".join(task["steps"])]
    if task["file"]:
        cmd += ["--file", task["file"]]
    cmd += task["args"] + ["--reporting-job-id", context.run_id, "--reporting-collection", collection]
    partition = opts.get("collection")
    try:
        result = exec(cmd)
    except Exception as e:
        emit(context, "workspace", task["id"], None, status="failed", partition=partition, error=str(e))
        return {"ok": False, "errors": [str(e)]}
    return {"ok": True, "errors": report_run_result(context, task["id"], result, {"partition": partition})}
