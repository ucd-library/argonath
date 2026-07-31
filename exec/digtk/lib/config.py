"""
Central configuration for the digtk CLI.

Precedence for every setting: built-in default -> environment variable -> explicit
Typer CLI flag. The env var override is applied once, at import time, into CONFIG — a
plain hierarchical dict that any module can import and read directly (e.g.
`CONFIG["ocr"]["lang"]`). Typer commands additionally declare `envvar=` on their Option
using the same env var name (this also documents it in --help); library code with no
CLI surface (digtk.lib.ai, digtk.lib.auth, digtk.lib.ark, ...) just reads CONFIG
directly and gets the same env override for free, without needing any Typer plumbing.

Secrets (bearer tokens, API keys) are deliberately kept out of CONFIG's resolved
values — only their *env var names* are recorded here (SAMWISE_API_KEY_ENV,
CASK_API_KEY_ENV) — see digtk.lib.auth for why a token must never be a CLI flag or a
default that could get logged (shell history, process listings).
"""
import os
from pathlib import Path
from typing import Annotated

import typer


def _env(name, default, cast=str):
    """Resolve a config value: environment variable if set, else the built-in default.

    :param str name: Environment variable name to check.
    :param default: Built-in default, used if the env var is unset.
    :param callable cast: Type to cast the raw env var string to (default: str).
    :returns: The cast env var value, or `default` if the env var is unset.
    """
    raw = os.environ.get(name)
    return cast(raw) if raw is not None else default


# Env var names, declared once and reused both to resolve CONFIG below and to wire
# each Typer Option's `envvar=` further down.
OCR_LANG_ENV = "DIGTK_OCR_LANG"
OCR_PSM_ENV = "DIGTK_OCR_PSM"
OCR_OEM_ENV = "DIGTK_OCR_OEM"
OCR_CONFIDENCE_THRESHOLD_ENV = "DIGTK_OCR_CONFIDENCE_THRESHOLD"

SAMWISE_API_URL_ENV = "SAMWISE_API_URL"
SAMWISE_API_KEY_ENV = "SAMWISE_API_KEY"
SAMWISE_LLM_MODEL_ENV = "DIGTK_SAMWISE_LLM_MODEL"
SAMWISE_EMBED_MODEL_ENV = "DIGTK_SAMWISE_EMBED_MODEL"

CASK_API_URL_ENV = "CASK_API_URL"
CASK_API_KEY_ENV = "CASK_API_KEY"
CREDENTIALS_PATH_ENV = "DIGTK_CREDENTIALS_PATH"

ARK_NAAN_ENV = "DIGTK_ARK_NAAN"
ARK_SHOULDER_ENV = "DIGTK_ARK_SHOULDER"
ARK_NOID_ALPHABET_ENV = "DIGTK_ARK_NOID_ALPHABET"

FS_CHUNK_SIZE_ENV = "DIGTK_FS_CHUNK_SIZE"

SEARCH_SPACE_ENV = "DIGTK_SEARCH_SPACE"
SEARCH_TOP_K_ENV = "DIGTK_SEARCH_TOP_K"

IMAGE_THUMBNAIL_WIDTHS_ENV = "DIGTK_IMAGE_THUMBNAIL_WIDTHS"
IMAGE_CONVERT_FORMAT_ENV = "DIGTK_IMAGE_CONVERT_FORMAT"


CONFIG = {
    "ocr": {
        "lang": _env(OCR_LANG_ENV, "eng"),
        "psm": _env(OCR_PSM_ENV, 1, int),
        "oem": _env(OCR_OEM_ENV, 3, int),
        "confidence_threshold": _env(OCR_CONFIDENCE_THRESHOLD_ENV, 60.0, float),
    },
    "samwise": {
        "api_url": _env(SAMWISE_API_URL_ENV, "http://samwise.library.ucdavis.edu/api"),
        "api_key_env": SAMWISE_API_KEY_ENV,
        "llm_model": _env(SAMWISE_LLM_MODEL_ENV, "qwen3.6-fast:35b"),
        "embed_model": _env(SAMWISE_EMBED_MODEL_ENV, "qwen3-embed:4b"),
    },
    "auth": {
        "credentials_path": _env(
            CREDENTIALS_PATH_ENV, str(Path.home() / ".digtk" / "credentials.json")
        ),
        "api_url_env": CASK_API_URL_ENV,
        "api_key_env": CASK_API_KEY_ENV,
    },
    "ark": {
        "naan": _env(ARK_NAAN_ENV, "87287"),
        "shoulder": _env(ARK_SHOULDER_ENV, "d7"),
        "noid_alphabet": _env(ARK_NOID_ALPHABET_ENV, "0123456789bcdfghjkmnpqrstvwxz"),
    },
    "fs": {
        "chunk_size": _env(FS_CHUNK_SIZE_ENV, 65536, int),
    },
    "search": {
        "space": _env(SEARCH_SPACE_ENV, "arda"),
        "top_k": _env(SEARCH_TOP_K_ENV, 10, int),
    },
    "image": {
        "thumbnail_widths": _env(IMAGE_THUMBNAIL_WIDTHS_ENV, "256,512,1024"),
        "convert_format": _env(IMAGE_CONVERT_FORMAT_ENV, "jpeg"),
    },
}


# --- Typer option definitions ------------------------------------------------
# Centralized so flag name / envvar / help text aren't redeclared per command.
# Each command supplies the actual default from CONFIG above (e.g.
# `lang: OcrLangOption = CONFIG["ocr"]["lang"]`), so the built-in default lives in
# exactly one place. Secrets are deliberately never exposed as a flag here — see
# digtk.lib.auth for why (a token passed as a CLI argument leaks into shell history
# and process listings).

OcrLangOption = Annotated[str, typer.Option(
    "--lang", envvar=OCR_LANG_ENV, help=f"Tesseract language code. Env: {OCR_LANG_ENV}.",
)]

OcrPsmOption = Annotated[int, typer.Option(
    "--psm", envvar=OCR_PSM_ENV, help=f"Page segmentation mode. Env: {OCR_PSM_ENV}.",
)]

OcrOemOption = Annotated[int, typer.Option(
    "--oem", envvar=OCR_OEM_ENV, help=f"OCR engine mode. Env: {OCR_OEM_ENV}.",
)]

SamwiseApiUrlOption = Annotated[str | None, typer.Option(
    "--samwise-api-url", envvar=SAMWISE_API_URL_ENV,
    help=f"Samwise API base URL. Env: {SAMWISE_API_URL_ENV}.",
)]

ImageThumbnailWidthsOption = Annotated[str, typer.Option(
    "--widths", envvar=IMAGE_THUMBNAIL_WIDTHS_ENV,
    help=f"Comma-separated widths in pixels. Env: {IMAGE_THUMBNAIL_WIDTHS_ENV}.",
)]

ImageConvertFormatOption = Annotated[str, typer.Option(
    "--format", envvar=IMAGE_CONVERT_FORMAT_ENV,
    help=f"Target format (e.g. jpeg, png). Env: {IMAGE_CONVERT_FORMAT_ENV}.",
)]

SearchSpaceOption = Annotated[str, typer.Option(
    "--space", envvar=SEARCH_SPACE_ENV, help=f"App space to search. Env: {SEARCH_SPACE_ENV}.",
)]

SearchTopKOption = Annotated[int, typer.Option(
    "--top-k", envvar=SEARCH_TOP_K_ENV,
    help=f"Number of results to return. Env: {SEARCH_TOP_K_ENV}.",
)]
