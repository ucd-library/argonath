"""
digtk public-dc pdf — PDF derivative commands.

Subcommands:
  page-count   — report each source PDF's page count (no derivatives generated)
  thumbnail    — generate thumbnail JPEGs at specified widths, for one page
  convert      — convert one page's rasterized image to another format
  deskew       — correct skew in one rasterized page
  prep-ocr     — prepare one rasterized page for OCR
  derivatives  — generate the full derivative set for every page of every source PDF,
                 with orientation auto-detection (thumbnails, deskew, OCR-prep, a
                 tiled pyramid TIFF for future IIIF-style deep-zoom serving) — this
                 is what Dagster's derivatives_job calls; the others above are
                 deterministic single-page recipes for ad-hoc/manual use.
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
from digtk.models.digital_website import pdf

app = typer.Typer(help="PDF derivative commands.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)

PageOption = Annotated[int, typer.Option("--page", help="0-indexed page number.")]


@app.command(name="page-count")
def page_count(ark: Annotated[str, typer.Argument(help="Item ARK identifier.")]):
    """Report each source PDF's page count."""
    try:
        utils.result(pdf.page_count(ark))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def thumbnail(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    page: PageOption = 0,
    widths: ImageThumbnailWidthsOption = CONFIG["image"]["thumbnail_widths"],
    quality: ImageThumbnailQualityOption = CONFIG["image"]["thumbnail_quality"],
    app: GoldAppOption = CONFIG["gold"]["app"],
):
    """Generate thumbnail JPEGs at each requested width, for one page."""
    try:
        utils.log(f"Generating thumbnails for {ark} page {page} at widths {widths}")
        utils.result(pdf.thumbnail(ark, opts={"page": page, "widths": widths, "quality": quality, "app": app}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def convert(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    page: PageOption = 0,
    format: ImageConvertFormatOption = CONFIG["image"]["convert_format"],
    app: GoldAppOption = CONFIG["gold"]["app"],
):
    """Convert one page's rasterized image to the specified format."""
    try:
        utils.log(f"Converting {ark} page {page} to {format}")
        utils.result(pdf.convert(ark, opts={"page": page, "format": format, "app": app}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def deskew(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    page: PageOption = 0,
    threshold: ImageDeskewThresholdOption = CONFIG["image"]["deskew_threshold"],
    quality: ImageThumbnailQualityOption = CONFIG["image"]["thumbnail_quality"],
    app: GoldAppOption = CONFIG["gold"]["app"],
):
    """Correct skew in one rasterized page."""
    try:
        utils.log(f"Deskewing {ark} page {page} (threshold {threshold}%)")
        utils.result(pdf.deskew(ark, opts={"page": page, "threshold": threshold, "quality": quality, "app": app}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command(name="prep-ocr")
def prep_ocr(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    page: PageOption = 0,
    dpi: ImageDpiOption = CONFIG["image"]["dpi"],
    fuzz_threshold: ImageFuzzThresholdOption = CONFIG["image"]["fuzz_threshold"],
    deskew: Annotated[bool, typer.Option(help="Apply a deskew pass before OCR-prep.")] = False,
):
    """Prepare one rasterized page for OCR: 2048px long edge, 300 DPI, fuzz whiten background."""
    try:
        utils.log(f"Preparing {ark} page {page} for OCR (dpi={dpi}, deskew={deskew})")
        utils.result(pdf.prep_ocr(ark, opts={"page": page, "dpi": dpi, "fuzz": fuzz_threshold, "deskew": deskew}))
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
    """Generate the full derivative set for every page of every source PDF, auto-detecting
    scan orientation via OCR independently per page.

    Runs a real Tesseract comparison between a deskewed and non-deskewed candidate to
    decide each page's orientation once, then applies that decision to every thumbnail
    width and the published OCR-prep image for that page.
    """
    try:
        utils.log(f"Generating derivatives for {ark} at widths {widths}")
        result = pdf.derivatives(ark, opts={
            "widths": widths,
            "threshold": deskew_threshold,
            "fuzz": fuzz_threshold,
            "app": app,
        })
        for stem, doc in result["pdfs"].items():
            for page_tag, orientation in doc["orientation"].items():
                utils.log(f"  {stem} {page_tag}: {'deskewed' if orientation['deskew'] else 'original'}")
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))
