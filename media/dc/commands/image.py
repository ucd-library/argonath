"""
dc image — image derivative commands.

Subcommands:
  thumbnail  — generate thumbnail JPEGs at specified widths
  convert    — convert source to another format
  deskew     — correct skew in scanned images
  prep-ocr   — prepare image for OCR (2048px, 300 DPI, fuzz whiten)
"""
import json
from typing import Annotated

import typer

app = typer.Typer(help="Image processing commands.", no_args_is_help=True)

_STORAGE_OPTS = {
    "local_root": typer.Option(None, "--local-root", help="Local filesystem root (testing mode)."),
    "cask_root":  typer.Option(None, "--cask-root",  help="CaskFS HTTP root URL (production mode)."),
}


@app.command()
def thumbnail(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    widths: Annotated[str, typer.Option(help="Comma-separated widths in pixels.")] = "256,512,1024",
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Generate thumbnail JPEGs at each requested width."""
    raise NotImplementedError("phase 0.4")


@app.command()
def convert(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    format: Annotated[str, typer.Option(help="Target format (e.g. jpeg, png).")] = "jpeg",
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Convert source binary to the specified format."""
    raise NotImplementedError("phase 0.4")


@app.command()
def deskew(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Correct skew in a scanned image."""
    raise NotImplementedError("phase 0.4")


@app.command(name="prep-ocr")
def prep_ocr(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Prepare image for OCR: 2048px long edge, 300 DPI, fuzz whiten background."""
    raise NotImplementedError("phase 0.4")
