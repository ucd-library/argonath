"""
Shared Typer option definitions: built-in default -> env var override -> explicit CLI flag.

Precedence is enforced by Typer/Click itself via `envvar=` (an explicit flag always wins,
then the named env var, then the parameter's own default) — this module only centralizes
option definitions so they aren't redeclared per command. Secrets (bearer tokens, API keys)
are deliberately never exposed as a flag here — see digtk.lib.auth for why (a token passed as
a CLI argument leaks into shell history and process listings).
"""
from typing import Annotated

import typer

OcrLangOption = Annotated[str, typer.Option(
    "--lang", envvar="DIGTK_OCR_LANG", help="Tesseract language code. Env: DIGTK_OCR_LANG.",
)]

OcrPsmOption = Annotated[int, typer.Option(
    "--psm", envvar="DIGTK_OCR_PSM", help="Page segmentation mode. Env: DIGTK_OCR_PSM.",
)]

OcrOemOption = Annotated[int, typer.Option(
    "--oem", envvar="DIGTK_OCR_OEM", help="OCR engine mode. Env: DIGTK_OCR_OEM.",
)]

SamwiseApiUrlOption = Annotated[str | None, typer.Option(
    "--samwise-api-url", envvar="SAMWISE_API_URL",
    help="Samwise API base URL. Env: SAMWISE_API_URL.",
)]
