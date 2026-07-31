"""
digtk image — image derivative commands.

Subcommands:
  thumbnail  — generate thumbnail JPEGs at specified widths
  convert    — convert source to another format
  deskew     — correct skew in scanned images
  prep-ocr   — prepare image for OCR (2048px, 300 DPI, fuzz whiten)
"""
from typing import Annotated

import typer

from digtk.lib.config import CONFIG, ImageConvertFormatOption, ImageThumbnailWidthsOption

app = typer.Typer(help="Image processing commands.", no_args_is_help=True)


@app.command()
def thumbnail(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    widths: ImageThumbnailWidthsOption = CONFIG["image"]["thumbnail_widths"],
):
    """Generate thumbnail JPEGs at each requested width."""
    raise NotImplementedError("phase 0.4")


@app.command()
def convert(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    format: ImageConvertFormatOption = CONFIG["image"]["convert_format"],
):
    """Convert source binary to the specified format."""
    raise NotImplementedError("phase 0.4")


@app.command()
def deskew(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
):
    """Correct skew in a scanned image."""
    raise NotImplementedError("phase 0.4")


@app.command(name="prep-ocr")
def prep_ocr(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
):
    """Prepare image for OCR: 2048px long edge, 300 DPI, fuzz whiten background."""
    raise NotImplementedError("phase 0.4")
