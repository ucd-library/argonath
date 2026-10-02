"""
digtk workspace — run the root-level tools on scans before they have an ARK.

Stages local folders into /workspace/[username]/[collection]/source/, runs any
of the root-level tools over them (outputs land in that collection's
derivatives/), pulls outputs back down for review, and promotes a reviewed
item's outputs into silver once it's been ingested under an ARK. See
digtk.models.workspace.

Subcommands:
  add      — stage a local folder into a workspace collection
  ls       — list workspace collections
  items    — list the item directories under a workspace path
  run      — run one or more tool steps over a folder, workspace path, or ARK
  pull     — download a CaskFS directory (e.g. review sheets) to local disk
  promote  — copy a workspace item's outputs into an ARK's silver location
"""
import subprocess
from typing import Annotated

import typer

from digtk.lib import items, utils
from digtk.lib.config import (
    CONFIG,
    AltMaxCharsOption,
    AltWantDescriptionOption,
    OcrLangOption,
    OcrOemOption,
    OcrPsmOption,
    ReportingCollectionOption,
    ReportingJobIdOption,
    SplitMaxPxOption,
    TargetArgument,
    WorkspaceCollectionOption,
    WorkspaceUserOption,
)
from digtk.models import workspace

app = typer.Typer(help="Run the root-level tools on staged workspace folders.", no_args_is_help=True)

# CalledProcessError: `cask cp` failing while staging a local folder.
_ERRORS = (LookupError, OSError, RuntimeError, ValueError, subprocess.CalledProcessError)


@app.command()
def add(
    source: Annotated[str, typer.Argument(help="Local folder to upload.")],
    collection: WorkspaceCollectionOption = None,
    user: WorkspaceUserOption = None,
    replace: Annotated[bool, typer.Option(help="Replace files that already exist.")] = False,
):
    """Stage a local folder into a workspace collection's source/ directory.

    Re-running over the same folder only uploads what changed.
    """
    try:
        root = workspace.stage(source, opts={"collection": collection, "username": user, "replace": replace})
        utils.result({"status": "success", "source": source, "dest": root})
    except _ERRORS as e:
        utils.error(str(e))


@app.command("ls")
def ls(user: Annotated[str | None, typer.Option(help="Only this user's collections.")] = None):
    """List workspace collections (used by Dagster's workspace partition sensor)."""
    try:
        collections = workspace.list_collections(opts={"username": user})
        for c in collections:
            utils.log(f"{c['key']}  ({c['path']})")
        utils.result({"status": "success", "collections": collections})
    except _ERRORS as e:
        utils.error(str(e))


@app.command("items")
def items_cmd(
    path: Annotated[str, typer.Argument(help="Workspace path, e.g. cask:/workspace/<user>/<collection>.")],
    files: Annotated[bool, typer.Option(
        "--files", help="Also list each item's image/PDF files (what Dagster fans out over, one step per file).",
    )] = False,
):
    """List the item directories under a workspace path (used by Dagster's
    process_workspace_job to fan out its steps)."""
    try:
        if files:
            found = workspace.list_items(path)
            for entry in found:
                utils.log(f"{entry['item']}  ({len(entry['files'])} file(s))")
            utils.result({"status": "success", "items": found})
        item_dirs = items.list_workspace_items(path)
        for item_dir in item_dirs:
            utils.log(item_dir)
        utils.result({"status": "success", "items": item_dirs})
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def run(
    target: TargetArgument,
    steps: Annotated[str, typer.Option(
        help=f"Comma-separated steps to run, always in this order: {','.join(workspace.STEP_ORDER)}. "
             "Run split-detect and split-apply separately to review gutters.json in between.",
    )],
    file: Annotated[list[str] | None, typer.Option(
        "--file", help="Only process this source file of each item (repeatable). "
                       "split-detect/split-apply are skipped, since they run per folder.",
    )] = None,
    collection: WorkspaceCollectionOption = None,
    user: WorkspaceUserOption = None,
    max_px: SplitMaxPxOption = CONFIG["split"]["max_px"],
    lang: OcrLangOption = CONFIG["ocr"]["lang"],
    psm: OcrPsmOption = CONFIG["ocr"]["psm"],
    oem: OcrOemOption = CONFIG["ocr"]["oem"],
    vision: Annotated[bool, typer.Option(help="ocr: also run Qwen vision OCR for the transcript.")] = True,
    collection_context: Annotated[str, typer.Option(help="alt-text: collection-specific prompt context.")] = "",
    want_description: AltWantDescriptionOption = CONFIG["alt"]["want_description"],
    max_alt_chars: AltMaxCharsOption = CONFIG["alt"]["max_chars"],
    doc_lang: Annotated[str, typer.Option(help="tag-pdf: document language (BCP-47, e.g. en-US).")] = "en-US",
    tables: Annotated[bool, typer.Option(help="tag-pdf: vision layout pass to tag data tables.")] = False,
    min_conf: Annotated[float, typer.Option(
        "--min-conf", help="tag-pdf: drop hOCR words below this confidence (0-100) from the text layer.",
    )] = CONFIG["accessibility"]["min_word_confidence"],
    validate: Annotated[bool, typer.Option(help="tag-pdf: run veraPDF on the tagged PDF.")] = True,
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Run one or more tool steps over every item a target covers.

    TARGET can be a local folder (staged into workspace first), a
    cask:/workspace/... path, or an ARK. A step that doesn't apply to an
    item (e.g. tag-pdf on a folder of images) is skipped for that item.
    """
    try:
        utils.result(workspace.run(target, opts={
            "steps": [s.strip() for s in steps.split(",") if s.strip()],
            "step_opts": {
                "split-detect": {"max_px": max_px},
                "ocr": {"lang": lang, "psm": psm, "oem": oem, "vision": vision},
                "alt-text": {
                    "collection_context": collection_context,
                    "want_description": want_description,
                    "max_alt_chars": max_alt_chars,
                },
                "tag-pdf": {"lang": doc_lang, "tables": tables, "min_conf": min_conf, "validate": validate},
            },
            "files": file or [],
            "collection": collection, "username": user,
            "reporting_job_id": reporting_job_id, "reporting_collection": reporting_collection,
        }))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def pull(
    path: Annotated[str, typer.Argument(help="CaskFS directory, e.g. cask:/workspace/<user>/<collection>/derivatives.")],
    dest: Annotated[str, typer.Argument(help="Local directory to download into.")],
):
    """Download every file at or under a CaskFS directory, keeping its layout."""
    try:
        result = workspace.pull(path, dest)
        utils.log(f"Downloaded {len(result['files'])} file(s) to {dest}")
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def promote(
    path: Annotated[str, typer.Argument(help="Workspace item directory, e.g. cask:/workspace/<user>/<collection>/source/folder-3.")],
    ark: Annotated[str, typer.Option(help="The item's ARK, once ingested to bronze.")],
    replace: Annotated[bool, typer.Option(help="Overwrite outputs already in silver.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be copied without copying.")] = False,
):
    """Copy a workspace item's outputs into the ARK's silver location.

    Copies as-is (no OCR/vision re-runs), re-pointing lineage at the
    matching bronze binaries. Every workspace source file must have a
    same-named binary under the ARK.
    """
    try:
        result = workspace.promote(path, ark, opts={"replace": replace, "dry_run": dry_run})
        verb = "Would copy" if dry_run else "Copied"
        utils.log(f"{verb} {len(result['promoted'])} output(s) to silver for {ark}")
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))
