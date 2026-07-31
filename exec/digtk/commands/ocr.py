"""
digtk ocr — OCR commands.

Subcommands:
  tesseract  — run Tesseract OCR, output hOCR + plain text
  ml         — run ML-based OCR (surya or docTR) as fallback
"""
from typing import Annotated

import typer

from digtk.lib.config import CONFIG, OcrLangOption, OcrOemOption, OcrPsmOption

app = typer.Typer(help="OCR commands.", no_args_is_help=True)


@app.command()
def tesseract(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    lang: OcrLangOption = CONFIG["ocr"]["lang"],
    psm:  OcrPsmOption = CONFIG["ocr"]["psm"],
    oem:  OcrOemOption = CONFIG["ocr"]["oem"],
):
    """Run Tesseract OCR, producing ocr/output.hocr and ocr/output.txt."""
    raise NotImplementedError("phase 0.5")


@app.command()
def ml(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
):
    """Run ML-based OCR (surya or docTR) as a fallback for low-confidence Tesseract results."""
    raise NotImplementedError("phase 0.5")
