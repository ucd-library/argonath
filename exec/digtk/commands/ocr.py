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
from typing import Annotated

import typer

from digtk.lib import reporting, utils
from digtk.lib.config import (
    CONFIG,
    OcrLangOption,
    OcrOemOption,
    OcrPsmOption,
    ReportingCollectionOption,
    ReportingJobIdOption,
)
from digtk.models import ocr as ocr_model

app = typer.Typer(help="OCR commands.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


@app.command()
def tesseract(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    lang: OcrLangOption = CONFIG["ocr"]["lang"],
    psm:  OcrPsmOption = CONFIG["ocr"]["psm"],
    oem:  OcrOemOption = CONFIG["ocr"]["oem"],
    vision: Annotated[bool, typer.Option(
        help="Also run Qwen vision OCR for the transcript text (default: on). "
             "Not a quality gate — Tesseract's word geometry never carries "
             "the transcript; turn this off only for fast local dev without "
             "SAMWISE_API_KEY, or deliberate cost control on a large batch.",
    )] = True,
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Run Tesseract (word geometry) and Qwen vision OCR (transcript) for
    every image binary, and every page of every PDF binary, in an item.

    Requires `digtk showcase image derivatives`/`digtk showcase pdf
    derivatives` to have already run — reads back their published OCR-prep
    image and display image rather than rasterizing anything itself. Writes
    silver/.../ocr/ocr.hocr + transcript.txt (+
    ocr-result.json with Tesseract's confidence) per image, or the same per
    PDF page, with CaskFS lineage to the images read. This is the command
    both ocr_job (per-item Dagster partition) and the collection-level
    fan-out job (dagster/lib/pipeline_fanout.py) call — the latter passing
    its own run id as --reporting-job-id.
    """
    try:
        utils.log(f"Running OCR for {ark}")
        with reporting.track(
            reporting_job_id, stage="ocr", item_id=ark, collection=reporting_collection,
            options={"lang": lang, "psm": psm, "oem": oem, "vision": vision},
        ):
            result = ocr_model.run_ocr(ark, opts={
                "lang": lang, "psm": psm, "oem": oem, "vision": vision,
            })
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))
