"""
digtk split-spreads — detect the binding gutter in a scanned two-page spread,
then cut confirmed spreads into separate left/right page images.

Top-level, not nested under `showcase`: unlike showcase's image/pdf/video/
audio commands, this isn't producing an asset for the public website — it's
a corrective, format-agnostic-in-spirit step on an item's own image binaries
(see digtk.models.split), same reasoning that keeps `accessibility`/`ocr`
top-level rather than nested under showcase image/pdf.

Subcommands:
  detect — detect the binding gutter in every not-yet-detected image binary of an item
  apply  — cut every confirmed spread per an item's (human-reviewed) gutters.json
"""
import subprocess

import typer

from digtk.lib import utils
from digtk.lib.config import (
    CONFIG,
    ReportingCollectionOption,
    ReportingJobIdOption,
    SplitMaxPxOption,
    TargetArgument,
    WorkspaceFolderOption,
    WorkspaceUserOption,
)
from digtk.models import split as split_model
from digtk.models import workspace

app = typer.Typer(help="Detect and cut scanned two-page spreads.", no_args_is_help=True)

# CalledProcessError: `cask cp` failing while staging a local folder target.
_ERRORS = (LookupError, OSError, RuntimeError, ValueError, subprocess.CalledProcessError)


@app.command()
def detect(
    target: TargetArgument,
    max_px: SplitMaxPxOption = CONFIG["split"]["max_px"],
    folder: WorkspaceFolderOption = None,
    user: WorkspaceUserOption = None,
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Detect the binding gutter in every not-yet-detected image binary of this item.

    Publishes a review strip and contact sheet(s) as derivatives of this
    item (silver for an ARK, workspace derivatives/ for a workspace path or
    local folder) for a human to check before running `apply` — `digtk
    workspace pull` downloads them. Resumable — never re-detects or
    overwrites an entry a human has already reviewed/corrected in gutters.json.
    """
    try:
        utils.log(f"Detecting spreads for {target}")
        utils.result(workspace.apply_to_targets(target, split_model.detect, opts={
            "stage": "split-detect",
            "fn_opts": {"max_px": max_px},
            "folder": folder, "username": user,
            "reporting_job_id": reporting_job_id, "reporting_collection": reporting_collection,
        }))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def apply(
    target: TargetArgument,
    folder: WorkspaceFolderOption = None,
    user: WorkspaceUserOption = None,
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Cut every confirmed spread per this item's (human-reviewed) gutters.json.

    Requires `detect` to have already run — reads gutters.json back rather
    than re-detecting anything. Publishes cut pages as derivatives of this
    item (or the original image unchanged, for non-spreads) — not new
    bronze items.
    """
    try:
        utils.log(f"Applying spread splits for {target}")
        utils.result(workspace.apply_to_targets(target, split_model.apply, opts={
            "stage": "split-apply",
            "folder": folder, "username": user,
            "reporting_job_id": reporting_job_id, "reporting_collection": reporting_collection,
        }))
    except _ERRORS as e:
        utils.error(str(e))
