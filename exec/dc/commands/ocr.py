"""
dc ocr — OCR commands.

Subcommands:
  tesseract  — run Tesseract OCR, output hOCR + plain text
  ml         — run ML-based OCR (surya or docTR) as fallback
"""
from typing import Annotated

import typer

app = typer.Typer(help="OCR commands.", no_args_is_help=True)


@app.command()
def tesseract(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    lang: Annotated[str, typer.Option(help="Tesseract language code.")] = "eng",
    psm:  Annotated[int, typer.Option(help="Page segmentation mode.")] = 1,
    oem:  Annotated[int, typer.Option(help="OCR engine mode.")] = 3,
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Run Tesseract OCR, producing ocr/output.hocr and ocr/output.txt."""
    raise NotImplementedError("phase 0.5")


@app.command()
def ml(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Run ML-based OCR (surya or docTR) as a fallback for low-confidence Tesseract results."""
    raise NotImplementedError("phase 0.5")
