"""
digtk ocr — OCR commands.

Subcommands:
  tesseract  — Tesseract word geometry + Qwen vision transcript, both engines,
               every image binary and PDF page in the item

digtk.commands.ocr.ml (an ML-fallback stub for low-confidence Tesseract
results) has been removed: there's no separate "fallback" step left for it
to be now that `tesseract` always runs both engines unconditionally (see
digtk.models.ocr's module docstring for why — not a confidence gate).
"""
import subprocess
from typing import Annotated

import typer

from digtk.lib import utils
from digtk.lib.config import (
    CONFIG,
    OcrLangOption,
    OcrOemOption,
    OcrPsmOption,
    ReportingCollectionOption,
    ReportingJobIdOption,
    TargetArgument,
    WorkspaceCollectionOption,
    WorkspaceUserOption,
)
from digtk.models import ocr as ocr_model
from digtk.models import workspace

app = typer.Typer(help="OCR commands.", no_args_is_help=True)

# CalledProcessError: `cask cp` failing while staging a local folder target.
_ERRORS = (LookupError, OSError, RuntimeError, ValueError, subprocess.CalledProcessError)


@app.command()
def tesseract(
    target: TargetArgument,
    lang: OcrLangOption = CONFIG["ocr"]["lang"],
    psm:  OcrPsmOption = CONFIG["ocr"]["psm"],
    oem:  OcrOemOption = CONFIG["ocr"]["oem"],
    vision: Annotated[bool, typer.Option(
        help="Also run Qwen vision OCR for the transcript text (default: on). "
             "Not a quality gate — Tesseract's word geometry never carries "
             "the transcript; turn this off only for fast local dev without "
             "SAMWISE_API_KEY, or deliberate cost control on a large batch.",
    )] = True,
    collection: WorkspaceCollectionOption = None,
    user: WorkspaceUserOption = None,
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Run Tesseract (word geometry) and Qwen vision OCR (transcript) for
    every image binary, and every page of every PDF binary, in an item.

    Prepares its own inputs from the source binaries (orientation pick,
    OCR-prep image, a resized image for the vision model) — no `digtk
    showcase` derivatives needed. Writes .../text/ocr.hocr + transcript.txt
    (+ ocr-result.json with Tesseract's confidence) per image, or the same
    per PDF page under text/page-NNN/, with CaskFS lineage to the source
    binary — in silver for an ARK, or the workspace collection's
    derivatives/ for a workspace path or local folder.
    """
    try:
        utils.log(f"Running OCR for {target}")
        utils.result(workspace.apply_to_targets(target, ocr_model.run_ocr, opts={
            "stage": "ocr",
            "fn_opts": {"lang": lang, "psm": psm, "oem": oem, "vision": vision},
            "collection": collection, "username": user,
            "reporting_job_id": reporting_job_id, "reporting_collection": reporting_collection,
        }))
    except _ERRORS as e:
        utils.error(str(e))
