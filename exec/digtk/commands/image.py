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

from digtk.lib import utils
from digtk.lib.config import (
    CONFIG,
    GoldAppOption,
    ImageConvertFormatOption,
    ImageDeskewThresholdOption,
    ImageDpiOption,
    ImageFuzzThresholdOption,
    ImageThumbnailQualityOption,
    ImageThumbnailWidthsOption,
)
from digtk.models.digital_website import image

app = typer.Typer(help="Image processing commands.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


@app.command()
def thumbnail(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    widths: ImageThumbnailWidthsOption = CONFIG["image"]["thumbnail_widths"],
    quality: ImageThumbnailQualityOption = CONFIG["image"]["thumbnail_quality"],
    app: GoldAppOption = CONFIG["gold"]["app"],
):
    """Generate thumbnail JPEGs at each requested width."""
    try:
        utils.log(f"Generating thumbnails for {ark} at widths {widths}")
        utils.result(image.thumbnails(ark, opts={"widths": widths, "quality": quality, "app": app}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def convert(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    format: ImageConvertFormatOption = CONFIG["image"]["convert_format"],
    app: GoldAppOption = CONFIG["gold"]["app"],
):
    """Convert source binary to the specified format."""
    try:
        utils.log(f"Converting {ark} to {format}")
        utils.result(image.convert(ark, opts={"format": format, "app": app}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def deskew(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    threshold: ImageDeskewThresholdOption = CONFIG["image"]["deskew_threshold"],
    quality: ImageThumbnailQualityOption = CONFIG["image"]["thumbnail_quality"],
    app: GoldAppOption = CONFIG["gold"]["app"],
):
    """Correct skew in a scanned image."""
    try:
        utils.log(f"Deskewing {ark} (threshold {threshold}%)")
        utils.result(image.deskew(ark, opts={"threshold": threshold, "quality": quality, "app": app}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command(name="prep-ocr")
def prep_ocr(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    dpi: ImageDpiOption = CONFIG["image"]["dpi"],
    fuzz_threshold: ImageFuzzThresholdOption = CONFIG["image"]["fuzz_threshold"],
    deskew: Annotated[bool, typer.Option(help="Apply a deskew pass before OCR-prep.")] = False,
):
    """Prepare image for OCR: 2048px long edge, 300 DPI, fuzz whiten background."""
    try:
        utils.log(f"Preparing {ark} for OCR (dpi={dpi}, deskew={deskew})")
        utils.result(image.prep_ocr(ark, opts={"dpi": dpi, "fuzz": fuzz_threshold, "deskew": deskew}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def derivatives(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    widths: ImageThumbnailWidthsOption = CONFIG["image"]["thumbnail_widths"],
    deskew_threshold: ImageDeskewThresholdOption = CONFIG["image"]["deskew_threshold"],
    fuzz_threshold: ImageFuzzThresholdOption = CONFIG["image"]["fuzz_threshold"],
    app: GoldAppOption = CONFIG["gold"]["app"],
):
    """Generate the full derivative set, auto-detecting scan orientation via OCR.

    Runs a real Tesseract comparison between a deskewed and non-deskewed
    candidate to decide the correct orientation once, then applies that
    decision to every thumbnail width and the published OCR-prep image. This
    is the command Dagster's derivatives_job calls.
    """
    try:
        utils.log(f"Generating derivatives for {ark} at widths {widths}")
        result = image.generate(ark, opts={
            "widths": widths,
            "threshold": deskew_threshold,
            "fuzz": fuzz_threshold,
            "app": app,
        })
        for stem, orientation in result["orientation"].items():
            utils.log(f"  {stem}: {'deskewed' if orientation['deskew'] else 'original'}")
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))
