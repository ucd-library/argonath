"""
digtk showcase image — image derivative commands.

Subcommands:
  thumbnail    — generate thumbnail JPEGs at specified widths
  convert      — convert source to another format
  deskew       — correct skew in scanned images
  prep-ocr     — prepare image for OCR (2048px, 300 DPI, fuzz whiten)
  derivatives  — generate the full derivative set with orientation auto-detection
                 (thumbnails, deskew, OCR-prep, a tiled pyramid TIFF for future
                 IIIF-style deep-zoom serving, all sharing one OCR-driven
                 orientation decision) — this is what Dagster's derivatives_job
                 actually calls; the others above are deterministic single-recipe
                 commands for ad-hoc/manual use.
"""
from typing import Annotated

import typer

from digtk.lib import reporting, utils
from digtk.lib.config import (
    CONFIG,
    ImageConvertFormatOption,
    ImageDeskewThresholdOption,
    ImageDpiOption,
    ImageFuzzThresholdOption,
    ImageThumbnailQualityOption,
    ImageThumbnailWidthsOption,
    ReportingCollectionOption,
    ReportingJobIdOption,
    TargetArgument,
)
from digtk.models.digital_website import image

app = typer.Typer(help="Image processing commands.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


@app.command()
def thumbnail(
    target: TargetArgument,
    widths: ImageThumbnailWidthsOption = CONFIG["image"]["thumbnail_widths"],
    quality: ImageThumbnailQualityOption = CONFIG["image"]["thumbnail_quality"],
):
    """Generate thumbnail JPEGs at each requested width."""
    try:
        utils.log(f"Generating thumbnails for {target} at widths {widths}")
        utils.result(image.thumbnails(target, opts={"widths": widths, "quality": quality}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def convert(
    target: TargetArgument,
    format: ImageConvertFormatOption = CONFIG["image"]["convert_format"],
):
    """Convert source binary to the specified format."""
    try:
        utils.log(f"Converting {target} to {format}")
        utils.result(image.convert(target, opts={"format": format}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def deskew(
    target: TargetArgument,
    threshold: ImageDeskewThresholdOption = CONFIG["image"]["deskew_threshold"],
    quality: ImageThumbnailQualityOption = CONFIG["image"]["thumbnail_quality"],
):
    """Correct skew in a scanned image."""
    try:
        utils.log(f"Deskewing {target} (threshold {threshold}%)")
        utils.result(image.deskew(target, opts={"threshold": threshold, "quality": quality}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command(name="prep-ocr")
def prep_ocr(
    target: TargetArgument,
    dpi: ImageDpiOption = CONFIG["image"]["dpi"],
    fuzz_threshold: ImageFuzzThresholdOption = CONFIG["image"]["fuzz_threshold"],
    deskew: Annotated[bool, typer.Option(help="Apply a deskew pass before OCR-prep.")] = False,
):
    """Prepare image for OCR: 2048px long edge, 300 DPI, fuzz whiten background."""
    try:
        utils.log(f"Preparing {target} for OCR (dpi={dpi}, deskew={deskew})")
        utils.result(image.prep_ocr(target, opts={"dpi": dpi, "fuzz": fuzz_threshold, "deskew": deskew}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def derivatives(
    target: TargetArgument,
    widths: ImageThumbnailWidthsOption = CONFIG["image"]["thumbnail_widths"],
    deskew_threshold: ImageDeskewThresholdOption = CONFIG["image"]["deskew_threshold"],
    fuzz_threshold: ImageFuzzThresholdOption = CONFIG["image"]["fuzz_threshold"],
    file: Annotated[list[str] | None, typer.Option(
        "--file", help="Only process this source file (bronze path or filename; repeatable) — "
                       "one Dagster step per file. Skips the item-level outputs; run "
                       "`digtk showcase finalize` once every file is done.",
    )] = None,
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Generate the full derivative set, auto-detecting scan orientation via OCR.

    Runs a real Tesseract comparison between a deskewed and non-deskewed
    candidate to decide the correct orientation once, then applies that
    decision to every thumbnail width and the published OCR-prep image. This
    is the command both derivatives_job (per-item Dagster partition) and the
    collection-level fan-out job (dagster/lib/pipeline_fanout.py) call — the
    latter passing its own run id as --reporting-job-id.
    """
    try:
        utils.log(f"Generating derivatives for {target} at widths {widths}")
        item_label = f"{target}#{','.join(file)}" if file else target
        with reporting.track(
            reporting_job_id, stage="derivatives", item_id=item_label, collection=reporting_collection,
            options={"widths": widths, "deskew_threshold": deskew_threshold, "fuzz_threshold": fuzz_threshold},
        ):
            result = image.generate(target, opts={
                "widths": widths,
                "threshold": deskew_threshold,
                "fuzz": fuzz_threshold,
                "files": file or None,
            })
        for stem, orientation in result["orientation"].items():
            utils.log(f"  {stem}: {'deskewed' if orientation['deskew'] else 'original'}")
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))
