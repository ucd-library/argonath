"""
Central configuration for the digtk CLI.

Precedence for every setting: built-in default -> home config file -> environment
variable -> explicit Typer CLI flag. The home config file (~/.digtk/config.json,
written by `digtk config set`, dotpath keys e.g. "samwise.api_url") is read once at
import time and used to seed os.environ for any env var not already set — real
environment variables always win, since seeding only fills gaps. After seeding, the
existing env var override is applied, once, at import time, into CONFIG — a plain
hierarchical dict that any module can import and read directly (e.g.
`CONFIG["ocr"]["lang"]`). Typer commands additionally declare `envvar=` on their Option
using the same env var name (this also documents it in --help); library code with no
CLI surface (digtk.lib.ai, digtk.lib.auth, ...) just reads CONFIG
directly and gets the same env override for free, without needing any Typer plumbing.

Secrets (bearer tokens, API keys, passwords) are deliberately kept out of CONFIG's
resolved values — only their *env var names* are recorded here (SAMWISE_API_KEY_ENV,
CASK_API_KEY_ENV, PRESERVICA_PASSWORD_ENV) — see digtk.lib.auth for why a token must
never be a CLI flag or a default that could get logged (shell history, process
listings). A secret set via `digtk config set samwise.api_key` (or
`preservica.password`) is written to the home config file (which gets the same
owner-only permissions as ~/.digtk/credentials.json) with hidden-input prompting, and
is only ever surfaced back to a caller as an env var, never as a CONFIG value.
"""
import json
import os
import stat
import sys
from pathlib import Path
from typing import Annotated, Literal

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


def _cast_bool(raw):
    """Cast an env var / home-config string to bool.

    :param str raw: Raw value, e.g. "true", "1", "no".
    :returns: True if `raw` (case-insensitive) is one of "1", "true", "yes", "on".
    :rtype: bool
    """
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Env var names, declared once and reused both to resolve CONFIG below and to wire
# each Typer Option's `envvar=` further down.
OCR_LANG_ENV = "DIGTK_OCR_LANG"
OCR_PSM_ENV = "DIGTK_OCR_PSM"
OCR_OEM_ENV = "DIGTK_OCR_OEM"
# OpenMP threads per Tesseract process; 1 avoids oversubscribing parallel steps.
OCR_THREADS_ENV = "DIGTK_OCR_THREADS"
OCR_CONFIDENCE_THRESHOLD_ENV = "DIGTK_OCR_CONFIDENCE_THRESHOLD"
# Distinct from OCR_CONFIDENCE_THRESHOLD_ENV, which gates the Tesseract->ML fallback
# (phase 0.5): these two govern the image-derivatives orientation comparison (phase
# 0.4), which picks the deskewed vs. non-deskewed candidate by word count/confidence.
OCR_ORIENTATION_WORD_COUNT_THRESHOLD_ENV = "DIGTK_OCR_ORIENTATION_WORD_COUNT_THRESHOLD"
OCR_ORIENTATION_WORD_CONFIDENCE_THRESHOLD_ENV = "DIGTK_OCR_ORIENTATION_WORD_CONFIDENCE_THRESHOLD"
# Minimum detected skew angle (degrees) before the deskewed candidate is even
# eligible to win -- matches the reference implementation's DESKEW_ANGLE_THRESHOLD.
OCR_ORIENTATION_ANGLE_THRESHOLD_ENV = "DIGTK_OCR_ORIENTATION_ANGLE_THRESHOLD"

SAMWISE_API_URL_ENV = "SAMWISE_API_URL"
SAMWISE_API_KEY_ENV = "SAMWISE_API_KEY"
SAMWISE_LLM_MODEL_ENV = "DIGTK_SAMWISE_LLM_MODEL"
SAMWISE_EMBED_MODEL_ENV = "DIGTK_SAMWISE_EMBED_MODEL"

# Transport hardening, tuned values ported from the standalone digitization
# toolkit's vllm_client.py — proven against real samwise flakiness (backend
# wedges where a third of requests hang until the read timeout, while
# /v1/models still answers) across 6,000+ item batches.
SAMWISE_CONNECT_TIMEOUT_S_ENV = "DIGTK_SAMWISE_CONNECT_TIMEOUT_S"
SAMWISE_READ_TIMEOUT_S_ENV = "DIGTK_SAMWISE_READ_TIMEOUT_S"
SAMWISE_RETRIES_ENV = "DIGTK_SAMWISE_RETRIES"
# Streaming (see digtk.lib.ai.chat_completion()): with it on, read_timeout_s only
# fires when Samwise stops sending, so a long answer still arriving never times
# out; max_call_s is the separate overall cap on one call.
SAMWISE_STREAM_ENV = "DIGTK_SAMWISE_STREAM"
SAMWISE_MAX_CALL_S_ENV = "DIGTK_SAMWISE_MAX_CALL_S"
SAMWISE_PROGRESS_EVERY_S_ENV = "DIGTK_SAMWISE_PROGRESS_EVERY_S"
# HH:MM, both empty disables the pause. Same window shape as digtk's
# DIGTK_MAINT_START/END — a long-running batch pauses through a nightly
# maintenance window instead of burning retries against it.
SAMWISE_MAINT_START_ENV = "DIGTK_SAMWISE_MAINT_START"
SAMWISE_MAINT_END_ENV = "DIGTK_SAMWISE_MAINT_END"

# Global defaults for digtk.models.alt_utils's generate_alt() opts — collection-specific
# tuning (collection_context, name_people, transcribe_text, extra_instructions) stays
# per-run (CLI flag / Dagster job config) since there's no per-collection settings
# registry yet to back a persisted default for those.
ALT_MAX_CHARS_ENV = "DIGTK_ALT_MAX_CHARS"
ALT_WANT_DESCRIPTION_ENV = "DIGTK_ALT_WANT_DESCRIPTION"
ALT_WANT_LONG_ENV = "DIGTK_ALT_WANT_LONG"

CASK_API_URL_ENV = "CASK_API_URL"
CASK_API_KEY_ENV = "CASK_API_KEY"
CREDENTIALS_PATH_ENV = "DIGTK_CREDENTIALS_PATH"

GATEWAY_URL_ENV = "DIGTK_GATEWAY_URL"
AUTH_LOGIN_TIMEOUT_ENV = "DIGTK_AUTH_LOGIN_TIMEOUT_S"

# Preservica connection secrets (tenant/username/password) are resolved by
# digtk.lib.auth the same way as CASK_API_URL/CASK_API_KEY above — env var
# overrides ~/.digtk/credentials.json, never a CLI flag, never in CONFIG/SETTINGS.
PRESERVICA_TENANT_ENV = "PRESERVICA_TENANT"
PRESERVICA_USERNAME_ENV = "PRESERVICA_USERNAME"
PRESERVICA_PASSWORD_ENV = "PRESERVICA_PASSWORD"
PRESERVICA_BASE_URL_ENV = "PRESERVICA_BASE_URL"
PRESERVICA_REQUEST_TIMEOUT_S_ENV = "DIGTK_PRESERVICA_REQUEST_TIMEOUT_S"
PRESERVICA_CONCURRENCY_ENV = "DIGTK_PRESERVICA_CONCURRENCY"
PRESERVICA_PAGE_SIZE_ENV = "DIGTK_PRESERVICA_PAGE_SIZE"
PRESERVICA_TOKEN_REFRESH_BUFFER_S_ENV = "DIGTK_PRESERVICA_TOKEN_REFRESH_BUFFER_S"
PRESERVICA_MAX_RETRIES_ENV = "DIGTK_PRESERVICA_MAX_RETRIES"
PRESERVICA_RETRY_BACKOFF_S_ENV = "DIGTK_PRESERVICA_RETRY_BACKOFF_S"

FS_CHUNK_SIZE_ENV = "DIGTK_FS_CHUNK_SIZE"

SEARCH_SPACE_ENV = "DIGTK_SEARCH_SPACE"
SEARCH_TOP_K_ENV = "DIGTK_SEARCH_TOP_K"
SEARCH_CHUNK_SIZE_ENV = "DIGTK_SEARCH_CHUNK_SIZE"
SEARCH_CHUNK_BUFFER_ENV = "DIGTK_SEARCH_CHUNK_BUFFER"
SEARCH_EMBED_MODEL_ENV = "DIGTK_SEARCH_EMBED_MODEL"

PDF_TARGET_PIXEL_WIDTH_ENV = "DIGTK_PDF_TARGET_PIXEL_WIDTH"
PDF_DEFAULT_DENSITY_ENV = "DIGTK_PDF_DEFAULT_DENSITY"
PDF_RASTER_FORMAT_ENV = "DIGTK_PDF_RASTER_FORMAT"
PDF_PDFINFO_BIN_ENV = "DIGTK_PDF_PDFINFO_BIN"

ACCESSIBILITY_VERAPDF_BIN_ENV = "DIGTK_VERAPDF_BIN"
ACCESSIBILITY_GS_BIN_ENV = "DIGTK_GS_BIN"
ACCESSIBILITY_TAG_LOOKAHEAD_ENV = "DIGTK_TAG_LOOKAHEAD"
ACCESSIBILITY_MIN_WORD_CONFIDENCE_ENV = "DIGTK_MIN_WORD_CONFIDENCE"

SPLIT_MAX_PX_ENV = "DIGTK_SPLIT_MAX_PX"

IMAGE_THUMBNAIL_WIDTHS_ENV = "DIGTK_IMAGE_THUMBNAIL_WIDTHS"
IMAGE_CONVERT_FORMAT_ENV = "DIGTK_IMAGE_CONVERT_FORMAT"
IMAGE_DPI_ENV = "DIGTK_IMAGE_DPI"
IMAGE_DESKEW_THRESHOLD_ENV = "DIGTK_IMAGE_DESKEW_THRESHOLD"
IMAGE_FUZZ_THRESHOLD_ENV = "DIGTK_IMAGE_FUZZ_THRESHOLD"
IMAGE_THUMBNAIL_QUALITY_ENV = "DIGTK_IMAGE_THUMBNAIL_QUALITY"
IMAGE_TILE_SIZE_ENV = "DIGTK_IMAGE_TILE_SIZE"
IMAGE_TILE_MAX_WIDTH_ENV = "DIGTK_IMAGE_TILE_MAX_WIDTH"
IMAGE_TILE_QUALITY_ENV = "DIGTK_IMAGE_TILE_QUALITY"
IMAGE_OCR_PREP_QUALITY_ENV = "DIGTK_IMAGE_OCR_PREP_QUALITY"
IMAGE_OCR_PREP_SIZE_ENV = "DIGTK_IMAGE_OCR_PREP_SIZE"
IMAGE_DESKEW_WORKING_WIDTH_ENV = "DIGTK_IMAGE_DESKEW_WORKING_WIDTH"
IMAGE_TIMEOUT_S_ENV = "DIGTK_IMAGE_TIMEOUT_S"
IMAGE_USE_MAGICK_ENV = "DIGTK_IMAGE_USE_MAGICK"
IMAGE_CONVERT_BIN_ENV = "DIGTK_IMAGE_CONVERT_BIN"
IMAGE_IDENTIFY_BIN_ENV = "DIGTK_IMAGE_IDENTIFY_BIN"

VIDEO_RESOLUTIONS_ENV = "DIGTK_VIDEO_RESOLUTIONS"
VIDEO_CRF_ENV = "DIGTK_VIDEO_CRF"
VIDEO_SEGMENT_SECONDS_ENV = "DIGTK_VIDEO_SEGMENT_SECONDS"
VIDEO_GOP_SIZE_ENV = "DIGTK_VIDEO_GOP_SIZE"
VIDEO_VIDEO_CODEC_ENV = "DIGTK_VIDEO_VIDEO_CODEC"
VIDEO_PIX_FMT_ENV = "DIGTK_VIDEO_PIX_FMT"
VIDEO_AUDIO_CODEC_ENV = "DIGTK_VIDEO_AUDIO_CODEC"
VIDEO_AUDIO_SAMPLE_RATE_ENV = "DIGTK_VIDEO_AUDIO_SAMPLE_RATE"
VIDEO_TIMEOUT_S_ENV = "DIGTK_VIDEO_TIMEOUT_S"
VIDEO_FFMPEG_BIN_ENV = "DIGTK_VIDEO_FFMPEG_BIN"
VIDEO_FFPROBE_BIN_ENV = "DIGTK_VIDEO_FFPROBE_BIN"

AUDIO_TRANSCODE_FORMAT_ENV = "DIGTK_AUDIO_TRANSCODE_FORMAT"
AUDIO_TRANSCODE_BITRATE_ENV = "DIGTK_AUDIO_TRANSCODE_BITRATE"
AUDIO_WAVEFORM_WIDTH_ENV = "DIGTK_AUDIO_WAVEFORM_WIDTH"
AUDIO_WAVEFORM_HEIGHT_ENV = "DIGTK_AUDIO_WAVEFORM_HEIGHT"
AUDIO_WAVEFORM_COLOR_ENV = "DIGTK_AUDIO_WAVEFORM_COLOR"
AUDIO_TIMEOUT_S_ENV = "DIGTK_AUDIO_TIMEOUT_S"

CAPTIONS_LANGUAGE_ENV = "DIGTK_CAPTIONS_LANGUAGE"
CAPTIONS_MODEL_ENV = "DIGTK_CAPTIONS_MODEL"
CAPTIONS_WHISPER_BIN_ENV = "DIGTK_CAPTIONS_WHISPER_BIN"
CAPTIONS_TIMEOUT_S_ENV = "DIGTK_CAPTIONS_TIMEOUT_S"

SHOWCASE_GOLD_PROJECT_ENV = "DIGTK_SHOWCASE_GOLD_PROJECT"
SHOWCASE_ES_HOST_ENV = "SHOWCASE_ES_HOST"
SHOWCASE_ES_INDEX_ENV = "SHOWCASE_ES_INDEX"
SHOWCASE_ES_PAGE_SEARCH_INDEX_ENV = "SHOWCASE_ES_PAGE_SEARCH_INDEX"
SHOWCASE_ES_COLLECTION_INDEX_ENV = "SHOWCASE_ES_COLLECTION_INDEX"

# The shared postgres instance's default 'postgres' database — same one
# cask/anduin-gateway connect to (distinct from CONFIG["postgres"]'s own
# dedicated 'argonath' database above, and from Dagster's own run/event
# storage — see dagster/lib/utils.py's DAGSTER_POSTGRES_* connection),
# scoped to its own 'reporting' schema. See digtk.lib.reporting's module
# docstring for why job-tracking rows live here instead of either of those.
REPORTING_PG_HOST_ENV = "DIGTK_REPORTING_PG_HOST"
REPORTING_PG_PORT_ENV = "DIGTK_REPORTING_PG_PORT"
REPORTING_PG_DATABASE_ENV = "DIGTK_REPORTING_PG_DATABASE"
REPORTING_PG_USER_ENV = "DIGTK_REPORTING_PG_USER"
REPORTING_PG_PASSWORD_ENV = "DIGTK_REPORTING_PG_PASSWORD"
REPORTING_JOB_ID_ENV = "DIGTK_REPORTING_JOB_ID"

# CaskFS's own Postgres (audit_log), read by digtk.lib.cask_audit. Each
# setting falls back to the reporting connection's, the same shared database.
CASKFS_PG_HOST_ENV = "DIGTK_CASKFS_PG_HOST"
CASKFS_PG_PORT_ENV = "DIGTK_CASKFS_PG_PORT"
CASKFS_PG_DATABASE_ENV = "DIGTK_CASKFS_PG_DATABASE"
CASKFS_PG_USER_ENV = "DIGTK_CASKFS_PG_USER"
CASKFS_PG_PASSWORD_ENV = "DIGTK_CASKFS_PG_PASSWORD"
CASKFS_PG_SCHEMA_ENV = "DIGTK_CASKFS_PG_SCHEMA"

# dc-showcase's Postgres, read-only
SHOWCASE_PG_HOST_ENV = "DIGTK_SHOWCASE_PG_HOST"
SHOWCASE_PG_PORT_ENV = "DIGTK_SHOWCASE_PG_PORT"
SHOWCASE_PG_DATABASE_ENV = "DIGTK_SHOWCASE_PG_DATABASE"
SHOWCASE_PG_USER_ENV = "DIGTK_SHOWCASE_PG_USER"
SHOWCASE_PG_PASSWORD_ENV = "DIGTK_SHOWCASE_PG_PASSWORD"
SHOWCASE_PG_INIT_DIR_ENV = "DIGTK_SHOWCASE_PG_INIT_DIR"
SHOWCASE_CASK_INIT_DIR_ENV = "DIGTK_SHOWCASE_CASK_INIT_DIR"

# argonath's own app-level Postgres schema (collection registry, etc.) — a
# distinct database/credentials from Dagster's own run/event-log storage (see
# dagster/lib/utils.py's `conn`, configured via dagster/dagster.yaml's
# storage.postgres block instead of these settings).
POSTGRES_HOST_ENV = "DIGTK_POSTGRES_HOST"
POSTGRES_PORT_ENV = "DIGTK_POSTGRES_PORT"
POSTGRES_DATABASE_ENV = "DIGTK_POSTGRES_DB"
POSTGRES_USER_ENV = "DIGTK_POSTGRES_USER"
POSTGRES_PASSWORD_ENV = "DIGTK_POSTGRES_PASSWORD"

CONFIG_PATH_ENV = "DIGTK_CONFIG_PATH"


# --- Home config file (`digtk config set/show`) ------------------------------
# Registry of every setting that can be persisted to the home config file, keyed by
# the same dotpath notation the file and `digtk config show` use. Each entry maps a
# dotpath to the env var it seeds and the type its value should cast to. CaskFS
# connection settings (CASK_API_URL_ENV, CASK_API_KEY_ENV, CREDENTIALS_PATH_ENV) are
# deliberately excluded — `digtk auth login`/credentials.json already own those.
SETTINGS = [
    {"dotpath": "ocr.lang", "env": OCR_LANG_ENV, "cast": str, "secret": False},
    {"dotpath": "ocr.psm", "env": OCR_PSM_ENV, "cast": int, "secret": False},
    {"dotpath": "ocr.oem", "env": OCR_OEM_ENV, "cast": int, "secret": False},
    {"dotpath": "ocr.threads", "env": OCR_THREADS_ENV, "cast": int, "secret": False},
    {"dotpath": "ocr.confidence_threshold", "env": OCR_CONFIDENCE_THRESHOLD_ENV, "cast": float, "secret": False},
    {"dotpath": "samwise.api_url", "env": SAMWISE_API_URL_ENV, "cast": str, "secret": False},
    {"dotpath": "samwise.api_key", "env": SAMWISE_API_KEY_ENV, "cast": str, "secret": True},
    {"dotpath": "samwise.llm_model", "env": SAMWISE_LLM_MODEL_ENV, "cast": str, "secret": False},
    {"dotpath": "samwise.embed_model", "env": SAMWISE_EMBED_MODEL_ENV, "cast": str, "secret": False},
    {"dotpath": "samwise.connect_timeout_s", "env": SAMWISE_CONNECT_TIMEOUT_S_ENV, "cast": float, "secret": False},
    {"dotpath": "samwise.read_timeout_s", "env": SAMWISE_READ_TIMEOUT_S_ENV, "cast": float, "secret": False},
    {"dotpath": "samwise.retries", "env": SAMWISE_RETRIES_ENV, "cast": int, "secret": False},
    {"dotpath": "samwise.stream", "env": SAMWISE_STREAM_ENV, "cast": _cast_bool, "secret": False},
    {"dotpath": "samwise.max_call_s", "env": SAMWISE_MAX_CALL_S_ENV, "cast": float, "secret": False},
    {"dotpath": "samwise.progress_every_s", "env": SAMWISE_PROGRESS_EVERY_S_ENV, "cast": float, "secret": False},
    {"dotpath": "samwise.maint_start", "env": SAMWISE_MAINT_START_ENV, "cast": str, "secret": False},
    {"dotpath": "samwise.maint_end", "env": SAMWISE_MAINT_END_ENV, "cast": str, "secret": False},
    {"dotpath": "alt.max_chars", "env": ALT_MAX_CHARS_ENV, "cast": int, "secret": False},
    {"dotpath": "alt.want_description", "env": ALT_WANT_DESCRIPTION_ENV, "cast": _cast_bool, "secret": False},
    {"dotpath": "alt.want_long", "env": ALT_WANT_LONG_ENV, "cast": _cast_bool, "secret": False},
    {"dotpath": "gateway.url", "env": GATEWAY_URL_ENV, "cast": str, "secret": False},
    {"dotpath": "auth.login_timeout_s", "env": AUTH_LOGIN_TIMEOUT_ENV, "cast": float, "secret": False},
    {"dotpath": "fs.chunk_size", "env": FS_CHUNK_SIZE_ENV, "cast": int, "secret": False},
    {"dotpath": "search.space", "env": SEARCH_SPACE_ENV, "cast": str, "secret": False},
    {"dotpath": "search.top_k", "env": SEARCH_TOP_K_ENV, "cast": int, "secret": False},
    {"dotpath": "search.chunk_size", "env": SEARCH_CHUNK_SIZE_ENV, "cast": int, "secret": False},
    {"dotpath": "search.chunk_buffer", "env": SEARCH_CHUNK_BUFFER_ENV, "cast": int, "secret": False},
    {"dotpath": "search.embed_model", "env": SEARCH_EMBED_MODEL_ENV, "cast": str, "secret": False},
    {"dotpath": "pdf.target_pixel_width", "env": PDF_TARGET_PIXEL_WIDTH_ENV, "cast": int, "secret": False},
    {"dotpath": "pdf.default_density", "env": PDF_DEFAULT_DENSITY_ENV, "cast": int, "secret": False},
    {"dotpath": "pdf.raster_format", "env": PDF_RASTER_FORMAT_ENV, "cast": str, "secret": False},
    {"dotpath": "pdf.pdfinfo_bin", "env": PDF_PDFINFO_BIN_ENV, "cast": str, "secret": False},
    {"dotpath": "accessibility.verapdf_bin", "env": ACCESSIBILITY_VERAPDF_BIN_ENV, "cast": str, "secret": False},
    {"dotpath": "accessibility.gs_bin", "env": ACCESSIBILITY_GS_BIN_ENV, "cast": str, "secret": False},
    {"dotpath": "accessibility.tag_lookahead", "env": ACCESSIBILITY_TAG_LOOKAHEAD_ENV, "cast": int, "secret": False},
    {"dotpath": "accessibility.min_word_confidence", "env": ACCESSIBILITY_MIN_WORD_CONFIDENCE_ENV, "cast": float, "secret": False},
    {"dotpath": "split.max_px", "env": SPLIT_MAX_PX_ENV, "cast": int, "secret": False},
    {"dotpath": "image.thumbnail_widths", "env": IMAGE_THUMBNAIL_WIDTHS_ENV, "cast": str, "secret": False},
    {"dotpath": "image.convert_format", "env": IMAGE_CONVERT_FORMAT_ENV, "cast": str, "secret": False},
    {"dotpath": "image.dpi", "env": IMAGE_DPI_ENV, "cast": int, "secret": False},
    {"dotpath": "image.deskew_threshold", "env": IMAGE_DESKEW_THRESHOLD_ENV, "cast": int, "secret": False},
    {"dotpath": "image.fuzz_threshold", "env": IMAGE_FUZZ_THRESHOLD_ENV, "cast": int, "secret": False},
    {"dotpath": "image.thumbnail_quality", "env": IMAGE_THUMBNAIL_QUALITY_ENV, "cast": int, "secret": False},
    {"dotpath": "image.tile_size", "env": IMAGE_TILE_SIZE_ENV, "cast": int, "secret": False},
    {"dotpath": "image.tile_max_width", "env": IMAGE_TILE_MAX_WIDTH_ENV, "cast": int, "secret": False},
    {"dotpath": "image.tile_quality", "env": IMAGE_TILE_QUALITY_ENV, "cast": int, "secret": False},
    {"dotpath": "image.ocr_prep_quality", "env": IMAGE_OCR_PREP_QUALITY_ENV, "cast": int, "secret": False},
    {"dotpath": "image.ocr_prep_size", "env": IMAGE_OCR_PREP_SIZE_ENV, "cast": int, "secret": False},
    {"dotpath": "image.deskew_working_width", "env": IMAGE_DESKEW_WORKING_WIDTH_ENV, "cast": int, "secret": False},
    {"dotpath": "image.timeout_s", "env": IMAGE_TIMEOUT_S_ENV, "cast": float, "secret": False},
    {"dotpath": "image.use_magick", "env": IMAGE_USE_MAGICK_ENV, "cast": _cast_bool, "secret": False},
    {"dotpath": "image.convert_bin", "env": IMAGE_CONVERT_BIN_ENV, "cast": str, "secret": False},
    {"dotpath": "image.identify_bin", "env": IMAGE_IDENTIFY_BIN_ENV, "cast": str, "secret": False},
    {"dotpath": "video.resolutions", "env": VIDEO_RESOLUTIONS_ENV, "cast": str, "secret": False},
    {"dotpath": "video.crf", "env": VIDEO_CRF_ENV, "cast": int, "secret": False},
    {"dotpath": "video.segment_seconds", "env": VIDEO_SEGMENT_SECONDS_ENV, "cast": int, "secret": False},
    {"dotpath": "video.gop_size", "env": VIDEO_GOP_SIZE_ENV, "cast": int, "secret": False},
    {"dotpath": "video.video_codec", "env": VIDEO_VIDEO_CODEC_ENV, "cast": str, "secret": False},
    {"dotpath": "video.pix_fmt", "env": VIDEO_PIX_FMT_ENV, "cast": str, "secret": False},
    {"dotpath": "video.audio_codec", "env": VIDEO_AUDIO_CODEC_ENV, "cast": str, "secret": False},
    {"dotpath": "video.audio_sample_rate", "env": VIDEO_AUDIO_SAMPLE_RATE_ENV, "cast": int, "secret": False},
    {"dotpath": "video.timeout_s", "env": VIDEO_TIMEOUT_S_ENV, "cast": float, "secret": False},
    {"dotpath": "video.ffmpeg_bin", "env": VIDEO_FFMPEG_BIN_ENV, "cast": str, "secret": False},
    {"dotpath": "video.ffprobe_bin", "env": VIDEO_FFPROBE_BIN_ENV, "cast": str, "secret": False},
    {"dotpath": "audio.transcode_format", "env": AUDIO_TRANSCODE_FORMAT_ENV, "cast": str, "secret": False},
    {"dotpath": "audio.transcode_bitrate", "env": AUDIO_TRANSCODE_BITRATE_ENV, "cast": str, "secret": False},
    {"dotpath": "audio.waveform_width", "env": AUDIO_WAVEFORM_WIDTH_ENV, "cast": int, "secret": False},
    {"dotpath": "audio.waveform_height", "env": AUDIO_WAVEFORM_HEIGHT_ENV, "cast": int, "secret": False},
    {"dotpath": "audio.waveform_color", "env": AUDIO_WAVEFORM_COLOR_ENV, "cast": str, "secret": False},
    {"dotpath": "audio.timeout_s", "env": AUDIO_TIMEOUT_S_ENV, "cast": float, "secret": False},
    {"dotpath": "captions.language", "env": CAPTIONS_LANGUAGE_ENV, "cast": str, "secret": False},
    {"dotpath": "captions.model", "env": CAPTIONS_MODEL_ENV, "cast": str, "secret": False},
    {"dotpath": "captions.whisper_bin", "env": CAPTIONS_WHISPER_BIN_ENV, "cast": str, "secret": False},
    {"dotpath": "captions.timeout_s", "env": CAPTIONS_TIMEOUT_S_ENV, "cast": float, "secret": False},
    {"dotpath": "showcase.gold_project", "env": SHOWCASE_GOLD_PROJECT_ENV, "cast": str, "secret": False},
    {"dotpath": "showcase.es_host", "env": SHOWCASE_ES_HOST_ENV, "cast": str, "secret": False},
    {"dotpath": "showcase.es_index", "env": SHOWCASE_ES_INDEX_ENV, "cast": str, "secret": False},
    {"dotpath": "showcase.es_page_search_index", "env": SHOWCASE_ES_PAGE_SEARCH_INDEX_ENV, "cast": str, "secret": False},
    {"dotpath": "showcase.es_collection_index", "env": SHOWCASE_ES_COLLECTION_INDEX_ENV, "cast": str, "secret": False},
    {"dotpath": "ocr.orientation_word_count_threshold", "env": OCR_ORIENTATION_WORD_COUNT_THRESHOLD_ENV, "cast": int, "secret": False},
    {"dotpath": "ocr.orientation_word_confidence_threshold", "env": OCR_ORIENTATION_WORD_CONFIDENCE_THRESHOLD_ENV, "cast": float, "secret": False},
    {"dotpath": "ocr.orientation_angle_threshold", "env": OCR_ORIENTATION_ANGLE_THRESHOLD_ENV, "cast": float, "secret": False},
    {"dotpath": "postgres.host", "env": POSTGRES_HOST_ENV, "cast": str, "secret": False},
    {"dotpath": "postgres.port", "env": POSTGRES_PORT_ENV, "cast": int, "secret": False},
    {"dotpath": "postgres.database", "env": POSTGRES_DATABASE_ENV, "cast": str, "secret": False},
    {"dotpath": "postgres.user", "env": POSTGRES_USER_ENV, "cast": str, "secret": False},
    {"dotpath": "postgres.password", "env": POSTGRES_PASSWORD_ENV, "cast": str, "secret": True},
    {"dotpath": "showcase_db.host", "env": SHOWCASE_PG_HOST_ENV, "cast": str, "secret": False},
    {"dotpath": "showcase_db.port", "env": SHOWCASE_PG_PORT_ENV, "cast": int, "secret": False},
    {"dotpath": "showcase_db.database", "env": SHOWCASE_PG_DATABASE_ENV, "cast": str, "secret": False},
    {"dotpath": "showcase_db.user", "env": SHOWCASE_PG_USER_ENV, "cast": str, "secret": False},
    {"dotpath": "showcase_db.password", "env": SHOWCASE_PG_PASSWORD_ENV, "cast": str, "secret": True},
    {"dotpath": "showcase_db.init_dir", "env": SHOWCASE_PG_INIT_DIR_ENV, "cast": str, "secret": False},
    {"dotpath": "showcase_cask.init_dir", "env": SHOWCASE_CASK_INIT_DIR_ENV, "cast": str, "secret": False},
    {"dotpath": "preservica.tenant", "env": PRESERVICA_TENANT_ENV, "cast": str, "secret": False},
    {"dotpath": "preservica.username", "env": PRESERVICA_USERNAME_ENV, "cast": str, "secret": False},
    {"dotpath": "preservica.password", "env": PRESERVICA_PASSWORD_ENV, "cast": str, "secret": True},
    {"dotpath": "preservica.base_url", "env": PRESERVICA_BASE_URL_ENV, "cast": str, "secret": False},
    {"dotpath": "preservica.request_timeout_s", "env": PRESERVICA_REQUEST_TIMEOUT_S_ENV, "cast": float, "secret": False},
    {"dotpath": "preservica.concurrency", "env": PRESERVICA_CONCURRENCY_ENV, "cast": int, "secret": False},
    {"dotpath": "preservica.page_size", "env": PRESERVICA_PAGE_SIZE_ENV, "cast": int, "secret": False},
    {"dotpath": "preservica.token_refresh_buffer_s", "env": PRESERVICA_TOKEN_REFRESH_BUFFER_S_ENV, "cast": float, "secret": False},
    {"dotpath": "preservica.max_retries", "env": PRESERVICA_MAX_RETRIES_ENV, "cast": int, "secret": False},
    {"dotpath": "preservica.retry_backoff_s", "env": PRESERVICA_RETRY_BACKOFF_S_ENV, "cast": float, "secret": False},
]
_SETTINGS_BY_DOTPATH = {setting["dotpath"]: setting for setting in SETTINGS}

HOME_CONFIG_PATH = Path(_env(CONFIG_PATH_ENV, str(Path.home() / ".digtk" / "config.json")))


def read_home_config(path):
    """Read the home config file.

    :param Path path: Home config file path.
    :returns: Stored settings as {dotpath: value}, or {} if no file exists.
    :rtype: dict
    :raises ValueError: If the file exists but is not valid JSON.
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in digtk config file {path}: {e}") from e


def write_home_config(path, data):
    """Write the home config file with owner-only permissions.

    :param Path path: Home config file path.
    :param dict data: Full {dotpath: value} settings to persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def is_secret(dotpath):
    """Whether a setting's value should be treated as a secret.

    Secret settings are never displayed by `digtk config show` and are prompted for
    with hidden input by `digtk config set`.

    :param str dotpath: Dotted setting name (e.g. 'samwise.api_key').
    :returns: True if the setting is registered and marked secret.
    :rtype: bool
    """
    setting = _SETTINGS_BY_DOTPATH.get(dotpath)
    return bool(setting and setting["secret"])


def set_setting(dotpath, value, path=None):
    """Persist a single setting to the home config file.

    :param str dotpath: Dotted setting name, e.g. 'samwise.api_url'. Must be a key in SETTINGS.
    :param str value: Raw value to store, as typed on the CLI or entered at a prompt.
    :param Path path: Home config file to write. Defaults to HOME_CONFIG_PATH.
    :raises ValueError: If dotpath is not a recognized setting, or value fails its cast.
    """
    path = path or HOME_CONFIG_PATH
    setting = _SETTINGS_BY_DOTPATH.get(dotpath)
    if setting is None:
        valid = ", ".join(sorted(_SETTINGS_BY_DOTPATH))
        raise ValueError(f"Unknown setting '{dotpath}'. Valid settings: {valid}")
    try:
        setting["cast"](value)
    except ValueError as e:
        raise ValueError(f"Invalid value for '{dotpath}': {e}") from e

    data = read_home_config(path)
    data[dotpath] = value
    write_home_config(path, data)


def resolved_settings():
    """Resolve the current effective value of every known setting, in dotpath notation.

    Secret settings (e.g. samwise.api_key) are reported as set/not set only — never
    their actual value — matching how `digtk auth whoami` reports token presence.

    :returns: {dotpath: resolved value}, with secrets reported as '***set***'/'(not set)'.
    :rtype: dict
    """
    resolved = {}
    for setting in SETTINGS:
        dotpath = setting["dotpath"]
        if setting["secret"]:
            resolved[dotpath] = "***set***" if os.environ.get(setting["env"]) else "(not set)"
        else:
            section, key = dotpath.split(".", 1)
            resolved[dotpath] = CONFIG[section][key]
    return resolved


def _seed_env_from_home_config(path, settings):
    """Populate os.environ from the home config file for any setting not already set.

    Real environment variables always win: this only fills in vars that are still
    unset by the time it runs, so the home config file acts as a persisted default
    underneath the existing env var override, not a replacement for it.

    :param Path path: Home config file path.
    :param list[dict] settings: SETTINGS registry (dotpath -> env var mapping).
    """
    env_by_dotpath = {setting["dotpath"]: setting["env"] for setting in settings}
    for dotpath, value in read_home_config(path).items():
        env_var = env_by_dotpath.get(dotpath)
        if env_var is None:
            print(f"Warning: ignoring unknown digtk config key '{dotpath}' in {path}", file=sys.stderr)
            continue
        if env_var not in os.environ:
            os.environ[env_var] = str(value)


_seed_env_from_home_config(HOME_CONFIG_PATH, SETTINGS)

# Computed once, ahead of the CONFIG literal below, so 'preservica.base_url' can
# fall back to the tenant-derived default (Preservica's Starter tier derives the
# tenant from the hostname) without recomputing 'preservica.tenant' twice.
_PRESERVICA_TENANT_DEFAULT = _env(PRESERVICA_TENANT_ENV, "")
_PRESERVICA_BASE_URL_DEFAULT = _env(PRESERVICA_BASE_URL_ENV, "") or (
    f"https://{_PRESERVICA_TENANT_DEFAULT}.preservica.com" if _PRESERVICA_TENANT_DEFAULT else ""
)


CONFIG = {
    "ocr": {
        "lang": _env(OCR_LANG_ENV, "eng"),
        "psm": _env(OCR_PSM_ENV, 1, int),
        "oem": _env(OCR_OEM_ENV, 3, int),
        "threads": _env(OCR_THREADS_ENV, 1, int),
        "confidence_threshold": _env(OCR_CONFIDENCE_THRESHOLD_ENV, 60.0, float),
        # Orientation-comparison thresholds (phase 0.4 derivatives, not the
        # ML-fallback trigger above): a deskewed candidate is only picked over the
        # original if it has more words above this confidence AND a better median.
        "orientation_word_count_threshold": _env(OCR_ORIENTATION_WORD_COUNT_THRESHOLD_ENV, 100, int),
        "orientation_word_confidence_threshold": _env(OCR_ORIENTATION_WORD_CONFIDENCE_THRESHOLD_ENV, 50.0, float),
        # A deskewed candidate also needs a measured skew angle beyond this many
        # degrees to be considered worth correcting at all -- matches the reference
        # implementation's DESKEW_ANGLE_THRESHOLD default.
        "orientation_angle_threshold": _env(OCR_ORIENTATION_ANGLE_THRESHOLD_ENV, 1.1, float),
    },
    "samwise": {
        "api_url": _env(SAMWISE_API_URL_ENV, "http://samwise.library.ucdavis.edu/api"),
        "api_key_env": SAMWISE_API_KEY_ENV,
        "llm_model": _env(SAMWISE_LLM_MODEL_ENV, "qwen3.8-fp8-fast:27b"),
        "embed_model": _env(SAMWISE_EMBED_MODEL_ENV, "qwen3-embed:4b"),
        # Proven values from the standalone digitization toolkit's vllm_client.py.
        "connect_timeout_s": _env(SAMWISE_CONNECT_TIMEOUT_S_ENV, 15.0, float),
        "read_timeout_s": _env(SAMWISE_READ_TIMEOUT_S_ENV, 180.0, float),
        "retries": _env(SAMWISE_RETRIES_ENV, 6, int),
        "stream": _env(SAMWISE_STREAM_ENV, True, _cast_bool),
        # 15 minutes: a dense full-page transcript (~10-15k chars) at the ~30
        # chars/s Samwise has been producing fits comfortably; a runaway
        # repetition loop still stops.
        "max_call_s": _env(SAMWISE_MAX_CALL_S_ENV, 900.0, float),
        # How often a streamed call logs how much it has received, in seconds.
        "progress_every_s": _env(SAMWISE_PROGRESS_EVERY_S_ENV, 30.0, float),
        "maint_start": _env(SAMWISE_MAINT_START_ENV, ""),
        "maint_end": _env(SAMWISE_MAINT_END_ENV, ""),
    },
    "alt": {
        # max_chars matches digtk.models.alt_utils.DEFAULT_MAX_ALT_CHARS.
        "max_chars": _env(ALT_MAX_CHARS_ENV, 150, int),
        "want_description": _env(ALT_WANT_DESCRIPTION_ENV, False, _cast_bool),
        "want_long": _env(ALT_WANT_LONG_ENV, False, _cast_bool),
    },
    "auth": {
        "credentials_path": _env(
            CREDENTIALS_PATH_ENV, str(Path.home() / ".digtk" / "credentials.json")
        ),
        "api_url_env": CASK_API_URL_ENV,
        "api_key_env": CASK_API_KEY_ENV,
        "login_timeout_s": _env(AUTH_LOGIN_TIMEOUT_ENV, 300.0, float),
    },
    # Anduin auth gateway that `digtk auth login`'s browser-based OIDC flow talks to.
    # The stored CaskFS api_url is gateway.url + "/cask" — see digtk.lib.auth.login().
    "gateway": {
        "url": _env(GATEWAY_URL_ENV, "https://argonath.library.ucdavis.edu"),
    },
    "fs": {
        "chunk_size": _env(FS_CHUNK_SIZE_ENV, 65536, int),
    },
    "search": {
        "space": _env(SEARCH_SPACE_ENV, "arda"),
        "top_k": _env(SEARCH_TOP_K_ENV, 10, int),
        # Chunk size/buffer are in characters. 1000/200 (20% buffer) are common RAG
        # chunking defaults (e.g. LangChain's RecursiveCharacterTextSplitter default).
        "chunk_size": _env(SEARCH_CHUNK_SIZE_ENV, 1000, int),
        "chunk_buffer": _env(SEARCH_CHUNK_BUFFER_ENV, 200, int),
        # Distinct from samwise.embed_model (ad hoc `digtk ai embed` default) — RAG
        # chunking favors the larger/more accurate embedding model.
        "embed_model": _env(SEARCH_EMBED_MODEL_ENV, _env(SAMWISE_EMBED_MODEL_ENV, "qwen3-embed:4b")),
    },
    "pdf": {
        # target_pixel_width/default_density/raster_format match the reference
        # dams/services/image-utils pipeline's lib/config.js: pdf.tifResizeWidth (3072,
        # env TIF_RESIZE_WIDTH), pdf.extractDensity (600, env PDF_DENSITY), and its
        # rasterization step's TIFF output — see digtk.lib.pdf_render.rasterize_page(),
        # which shells out to ImageMagick's `convert` (PDF delegate -> Ghostscript) the
        # same way the reference pipeline's extract-pdf-page.js does.
        "target_pixel_width": _env(PDF_TARGET_PIXEL_WIDTH_ENV, 3072, int),
        "default_density": _env(PDF_DEFAULT_DENSITY_ENV, 600, int),
        "raster_format": _env(PDF_RASTER_FORMAT_ENV, "tif"),
        "pdfinfo_bin": _env(PDF_PDFINFO_BIN_ENV, "pdfinfo"),
    },
    "accessibility": {
        "verapdf_bin": _env(ACCESSIBILITY_VERAPDF_BIN_ENV, "verapdf"),
        # Ghostscript, for embedding fonts in place
        "gs_bin": _env(ACCESSIBILITY_GS_BIN_ENV, "gs"),
        # Matches digtk.models.pdf_tag.DEFAULT_LOOKAHEAD.
        "tag_lookahead": _env(ACCESSIBILITY_TAG_LOOKAHEAD_ENV, 40, int),
        # tag_pdf()'s searchable-PDF text layer: drop hOCR words below this
        # confidence (0-100) instead of baking OCR noise into the invisible
        # text layer. Default 0.0 (disabled) — opt in per collection/batch,
        # ported from the reference toolkit's words.py --min-conf.
        "min_word_confidence": _env(ACCESSIBILITY_MIN_WORD_CONFIDENCE_ENV, 0.0, float),
    },
    "split": {
        # Detection render width for the vision gutter call — matches the
        # reference toolkit's split.py default.
        "max_px": _env(SPLIT_MAX_PX_ENV, 1400, int),
    },
    "image": {
        "thumbnail_widths": _env(IMAGE_THUMBNAIL_WIDTHS_ENV, "256,512,1024"),
        "convert_format": _env(IMAGE_CONVERT_FORMAT_ENV, "jpeg"),
        "dpi": _env(IMAGE_DPI_ENV, 300, int),
        "deskew_threshold": _env(IMAGE_DESKEW_THRESHOLD_ENV, 40, int),
        "fuzz_threshold": _env(IMAGE_FUZZ_THRESHOLD_ENV, 80, int),
        "thumbnail_quality": _env(IMAGE_THUMBNAIL_QUALITY_ENV, 90, int),
        # tile_size/tile_max_width/tile_quality match the reference
        # dams/services/image-utils pipeline's generate-image-products.sh
        # TILED_TIF_OPTS exactly (256px tiles, 3072px max width, quality 90) — see
        # digtk.lib.image_magick.tile().
        "tile_size": _env(IMAGE_TILE_SIZE_ENV, 256, int),
        "tile_max_width": _env(IMAGE_TILE_MAX_WIDTH_ENV, 3072, int),
        "tile_quality": _env(IMAGE_TILE_QUALITY_ENV, 90, int),
        "ocr_prep_quality": _env(IMAGE_OCR_PREP_QUALITY_ENV, 100, int),
        "ocr_prep_size": _env(IMAGE_OCR_PREP_SIZE_ENV, 2048, int),
        "deskew_working_width": _env(IMAGE_DESKEW_WORKING_WIDTH_ENV, 2048, int),
        "timeout_s": _env(IMAGE_TIMEOUT_S_ENV, 120.0, float),
        # ImageMagick 7 replaces the legacy `convert`/`identify` binaries with a
        # single `magick` CLI (`magick <args>` for convert, `magick identify` for
        # identify) — deprecated but still present as compatibility shims on most
        # installs. use_magick opts into the unified CLI; convert_bin/identify_bin
        # let an install where the legacy binaries are named/aliased differently
        # override them without opting into magick.
        "use_magick": _env(IMAGE_USE_MAGICK_ENV, False, _cast_bool),
        "convert_bin": _env(IMAGE_CONVERT_BIN_ENV, "convert"),
        "identify_bin": _env(IMAGE_IDENTIFY_BIN_ENV, "identify"),
    },
    "video": {
        # crf/segment_seconds/gop_size/video_codec/pix_fmt/audio_codec/audio_sample_rate
        # and the per-resolution bitrate ladder (digtk.lib.ffmpeg_utils._RESOLUTION_LADDER)
        # match the reference dams/services/image-utils pipeline's lib/ffmpeg/convert.sh —
        # that script only exposes the resolution list itself as a config knob; everything
        # else was a hardcoded literal there, exposed as a setting here instead.
        "resolutions": _env(VIDEO_RESOLUTIONS_ENV, "360p,480p,720p,1080p"),
        "crf": _env(VIDEO_CRF_ENV, 20, int),
        "segment_seconds": _env(VIDEO_SEGMENT_SECONDS_ENV, 4, int),
        "gop_size": _env(VIDEO_GOP_SIZE_ENV, 48, int),
        "video_codec": _env(VIDEO_VIDEO_CODEC_ENV, "h264"),
        "pix_fmt": _env(VIDEO_PIX_FMT_ENV, "yuv420p"),
        "audio_codec": _env(VIDEO_AUDIO_CODEC_ENV, "aac"),
        "audio_sample_rate": _env(VIDEO_AUDIO_SAMPLE_RATE_ENV, 48000, int),
        "timeout_s": _env(VIDEO_TIMEOUT_S_ENV, 3600.0, float),
        "ffmpeg_bin": _env(VIDEO_FFMPEG_BIN_ENV, "ffmpeg"),
        "ffprobe_bin": _env(VIDEO_FFPROBE_BIN_ENV, "ffprobe"),
    },
    "audio": {
        # No reference precedent for standalone audio transcode/waveform generation —
        # reasonable new defaults.
        "transcode_format": _env(AUDIO_TRANSCODE_FORMAT_ENV, "mp3"),
        "transcode_bitrate": _env(AUDIO_TRANSCODE_BITRATE_ENV, "192k"),
        "waveform_width": _env(AUDIO_WAVEFORM_WIDTH_ENV, 1024, int),
        "waveform_height": _env(AUDIO_WAVEFORM_HEIGHT_ENV, 256, int),
        "waveform_color": _env(AUDIO_WAVEFORM_COLOR_ENV, "0x3b82f6"),
        "timeout_s": _env(AUDIO_TIMEOUT_S_ENV, 600.0, float),
    },
    "captions": {
        "language": _env(CAPTIONS_LANGUAGE_ENV, "en"),
        "model": _env(CAPTIONS_MODEL_ENV, "small"),
        "whisper_bin": _env(CAPTIONS_WHISPER_BIN_ENV, "whisper"),
        "timeout_s": _env(CAPTIONS_TIMEOUT_S_ENV, 3600.0, float),
    },
    "showcase": {
        "gold_project": _env(SHOWCASE_GOLD_PROJECT_ENV, "digital-dev"),
        # Elasticsearch the showcase website reads (see digtk.lib.showcase_index).
        "es_host": _env(SHOWCASE_ES_HOST_ENV, "http://elasticsearch:9200"),
        # fin's ElasticSearchModel base class (fin/services/fin/node-utils/lib/data-models/
        # elastic-search/index.js:19-20) writes through a "-write" alias and reads through
        # a "-read" alias - normally both point at the same underlying index, but diverge
        # during a reindex/mapping-change cutover (see fin's es-index-management service).
        # showcase_index is a write path, so it targets item-write, matching dbsync.
        "es_index": _env(SHOWCASE_ES_INDEX_ENV, "item-write"),
        # The website's page-level full-text index — one document per page of an image
        # list, holding that page's DjVu text layer — which its BookReader search
        # (/api/page-search/ia) queries. Same "-write" alias convention as es_index.
        "es_page_search_index": _env(SHOWCASE_ES_PAGE_SEARCH_INDEX_ENV, "page-search-write"),
        # The website's collection index. Same "-write" alias convention.
        "es_collection_index": _env(SHOWCASE_ES_COLLECTION_INDEX_ENV, "collection-write"),
    },
    "postgres": {
        # argonath's own app-level schema (collection registry, etc.) — the shared
        # Postgres/pgvector instance project-anduin provisions, not Dagster's own
        # run-storage database (dagster/lib/utils.py connects separately, via
        # DAGSTER_POSTGRES_* env vars, to that one). 'password' is marked secret in
        # SETTINGS (hidden-input prompt, never displayed by `digtk config show`) so
        # its resolved value is deliberately NOT stored here — only its env var
        # name is, mirroring CONFIG["preservica"] above — see digtk.lib.db.
        "host": _env(POSTGRES_HOST_ENV, "localhost"),
        "port": _env(POSTGRES_PORT_ENV, 5432, int),
        "database": _env(POSTGRES_DATABASE_ENV, "argonath"),
        "user": _env(POSTGRES_USER_ENV, "argonath"),
        "password_env": POSTGRES_PASSWORD_ENV,
    },
    "preservica": {
        # Unlike CaskFS's OIDC-issued session token (which has its own browser-login
        # flow and credentials.json), Preservica has no separate credential store —
        # tenant/username/base_url are plain dotpath settings like everything else in
        # CONFIG, persisted via `digtk config set preservica.<name> <value>` same as
        # e.g. samwise.api_url. 'password' is marked secret in SETTINGS (hidden-input
        # prompt, never displayed by `digtk config show`/`digtk preservica config`) so
        # its resolved value is deliberately NOT stored here — only its env var name
        # is, mirroring CONFIG["samwise"]["api_key_env"] — see digtk.lib.auth.
        "tenant": _PRESERVICA_TENANT_DEFAULT,
        "tenant_env": PRESERVICA_TENANT_ENV,
        "username": _env(PRESERVICA_USERNAME_ENV, ""),
        "username_env": PRESERVICA_USERNAME_ENV,
        "password_env": PRESERVICA_PASSWORD_ENV,
        "base_url": _PRESERVICA_BASE_URL_DEFAULT,
        "base_url_env": PRESERVICA_BASE_URL_ENV,
        # Non-secret tuning knobs. request_timeout_s/page_size/token_refresh_buffer_s
        # match preservica-etl's lib/config.js defaults (requestTimeoutMs, page size,
        # tokenRefreshBufferMs); concurrency is lowered from that port's 8 to 2 —
        # Preservica's Starter tier was observed to 502/drop connections on bitstream
        # `/content` downloads under 8-way concurrent load (see max_retries below,
        # which is the other half of the mitigation).
        "request_timeout_s": _env(PRESERVICA_REQUEST_TIMEOUT_S_ENV, 30.0, float),
        "concurrency": _env(PRESERVICA_CONCURRENCY_ENV, 2, int),
        "page_size": _env(PRESERVICA_PAGE_SIZE_ENV, 100, int),
        "token_refresh_buffer_s": _env(PRESERVICA_TOKEN_REFRESH_BUFFER_S_ENV, 60.0, float),
        # Not in preservica-etl: that port never retried, but Preservica's Starter
        # tier has turned out to intermittently 502/drop-connection on bitstream
        # `/content` downloads under concurrent load (observed in practice), so
        # PreservicaClient.request() retries transient failures with backoff.
        "max_retries": _env(PRESERVICA_MAX_RETRIES_ENV, 4, int),
        "retry_backoff_s": _env(PRESERVICA_RETRY_BACKOFF_S_ENV, 1.0, float),
    },
    "reporting": {
        # host/port default to a local-dev port-forward target (e.g.
        # `kubectl port-forward svc/postgres 5432:5432 -n argonath`), not a
        # real network path -- see digtk.lib.reporting's module docstring.
        # Compose/k8s override these to the in-cluster 'postgres' service.
        "host": _env(REPORTING_PG_HOST_ENV, "localhost"),
        "port": _env(REPORTING_PG_PORT_ENV, 5432, int),
        "database": _env(REPORTING_PG_DATABASE_ENV, "postgres"),
        "user": _env(REPORTING_PG_USER_ENV, "postgres"),
        "password_env": REPORTING_PG_PASSWORD_ENV,
    },
    "caskfs_db": {
        "host": _env(CASKFS_PG_HOST_ENV, _env(REPORTING_PG_HOST_ENV, "localhost")),
        "port": _env(CASKFS_PG_PORT_ENV, _env(REPORTING_PG_PORT_ENV, 5432, int), int),
        "database": _env(CASKFS_PG_DATABASE_ENV, _env(REPORTING_PG_DATABASE_ENV, "postgres")),
        "user": _env(CASKFS_PG_USER_ENV, _env(REPORTING_PG_USER_ENV, "postgres")),
        "password_env": CASKFS_PG_PASSWORD_ENV,
        "fallback_password_env": REPORTING_PG_PASSWORD_ENV,
        "schema": _env(CASKFS_PG_SCHEMA_ENV, "caskfs"),
    },
    "showcase_db": {
        # defaults match dc-showcase local-dev
        "host": _env(SHOWCASE_PG_HOST_ENV, "localhost"),
        "port": _env(SHOWCASE_PG_PORT_ENV, 5432, int),
        "database": _env(SHOWCASE_PG_DATABASE_ENV, "dcf"),
        "user": _env(SHOWCASE_PG_USER_ENV, "postgres"),
        "password_env": SHOWCASE_PG_PASSWORD_ENV,
        # dc-showcase's init sql, mounted from its repo
        "init_dir": _env(SHOWCASE_PG_INIT_DIR_ENV, "/opt/dc-showcase/init/postgres"),
    },
    "showcase_cask": {
        # dc-showcase's gold seed files, mounted from its repo
        "init_dir": _env(SHOWCASE_CASK_INIT_DIR_ENV, "/opt/dc-showcase/init/caskfs"),
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

ImageDpiOption = Annotated[int, typer.Option(
    "--dpi", envvar=IMAGE_DPI_ENV, help=f"OCR-prep image density (DPI). Env: {IMAGE_DPI_ENV}.",
)]

ImageDeskewThresholdOption = Annotated[int, typer.Option(
    "--deskew-threshold", envvar=IMAGE_DESKEW_THRESHOLD_ENV,
    help=f"Deskew angle-detection threshold, percent. Env: {IMAGE_DESKEW_THRESHOLD_ENV}.",
)]

ImageFuzzThresholdOption = Annotated[int, typer.Option(
    "--fuzz-threshold", envvar=IMAGE_FUZZ_THRESHOLD_ENV,
    help=f"OCR-prep fuzz-whiten tolerance, percent. Env: {IMAGE_FUZZ_THRESHOLD_ENV}.",
)]

ImageThumbnailQualityOption = Annotated[int, typer.Option(
    "--quality", envvar=IMAGE_THUMBNAIL_QUALITY_ENV,
    help=f"JPEG quality (1-100). Env: {IMAGE_THUMBNAIL_QUALITY_ENV}.",
)]

SplitMaxPxOption = Annotated[int, typer.Option(
    "--max-px", envvar=SPLIT_MAX_PX_ENV,
    help=f"Detection render width, in pixels. Env: {SPLIT_MAX_PX_ENV}.",
)]

AltMaxCharsOption = Annotated[int, typer.Option(
    "--max-alt-chars", envvar=ALT_MAX_CHARS_ENV,
    help=f"Maximum alt-text length in characters. Env: {ALT_MAX_CHARS_ENV}.",
)]

AltWantDescriptionOption = Annotated[bool, typer.Option(
    "--want-description", envvar=ALT_WANT_DESCRIPTION_ENV,
    help=f"Also generate a 2-4 sentence catalog description. Env: {ALT_WANT_DESCRIPTION_ENV}.",
)]

AltWantLongOption = Annotated[bool, typer.Option(
    "--long", envvar=ALT_WANT_LONG_ENV,
    help=f"Also write a long description for maps, charts, posters and other "
         f"information-dense images. Env: {ALT_WANT_LONG_ENV}.",
)]

OcrEnhanceOption = Annotated[Literal["auto", "gray"] | None, typer.Option(
    "--ocr-enhance",
    help="Contrast-boost the image Tesseract reads, for faded originals: "
         "'auto' keeps color, 'gray' also converts to grayscale.",
)]

TagModeOption = Annotated[Literal["auto", "inplace", "rebuild"], typer.Option(
    "--mode",
    help="auto: tag in place when any page has real text, else rebuild. inplace: keep "
         "every page as produced (fonts, vector art) and OCR only pages without text. "
         "rebuild: render pages to images and rebuild them with an OCR text layer.",
)]

EmbedFontsOption = Annotated[bool, typer.Option(
    help="In place only: rewrite PDFs that use unembedded fonts through Ghostscript "
         "first, since those fail PDF/UA.",
)]

OcrTextPagesOption = Annotated[bool, typer.Option(
    "--ocr-text-pages",
    help="Also OCR PDF pages that already have extractable text (skipped by default).",
)]

FiguresOption = Annotated[bool, typer.Option(
    help="Tag in-page figures (charts, photos, maps) with alt text and captions. "
         "Pages not yet run through `digtk accessibility figures` are detected now "
         "(one Samwise call per page plus one per figure, cached).",
)]

GeometryOption = Annotated[Literal["tesseract", "qwen"], typer.Option(
    "--geometry",
    help="Text-layer source: tesseract (word boxes; printed text) or qwen (vision line "
         "boxes; handwriting and faint typescript; one Samwise call per page, cached).",
)]

SkipExistingOption = Annotated[bool, typer.Option(
    "--skip-existing",
    help="Skip work whose outputs already exist (resume an interrupted run).",
)]

VideoResolutionsOption = Annotated[str, typer.Option(
    "--resolutions", envvar=VIDEO_RESOLUTIONS_ENV,
    help=f"Comma-separated HLS resolution rungs (e.g. 360p,720p). Env: {VIDEO_RESOLUTIONS_ENV}.",
)]

AudioTranscodeFormatOption = Annotated[str, typer.Option(
    "--format", envvar=AUDIO_TRANSCODE_FORMAT_ENV,
    help=f"Target audio format (e.g. mp3, aac). Env: {AUDIO_TRANSCODE_FORMAT_ENV}.",
)]

AudioTranscodeBitrateOption = Annotated[str, typer.Option(
    "--bitrate", envvar=AUDIO_TRANSCODE_BITRATE_ENV,
    help=f"Target audio bitrate (e.g. 192k). Env: {AUDIO_TRANSCODE_BITRATE_ENV}.",
)]

AudioWaveformWidthOption = Annotated[int, typer.Option(
    "--width", envvar=AUDIO_WAVEFORM_WIDTH_ENV,
    help=f"Waveform image width in pixels. Env: {AUDIO_WAVEFORM_WIDTH_ENV}.",
)]

AudioWaveformHeightOption = Annotated[int, typer.Option(
    "--height", envvar=AUDIO_WAVEFORM_HEIGHT_ENV,
    help=f"Waveform image height in pixels. Env: {AUDIO_WAVEFORM_HEIGHT_ENV}.",
)]

CaptionsLanguageOption = Annotated[str, typer.Option(
    "--language", envvar=CAPTIONS_LANGUAGE_ENV,
    help=f"Whisper language code (e.g. en, es). Env: {CAPTIONS_LANGUAGE_ENV}.",
)]

CaptionsModelOption = Annotated[str, typer.Option(
    "--model", envvar=CAPTIONS_MODEL_ENV,
    help=f"Whisper model size (tiny/base/small/medium/large). Env: {CAPTIONS_MODEL_ENV}.",
)]

ShowcaseGoldProjectOption = Annotated[str, typer.Option(
    "--project", envvar=SHOWCASE_GOLD_PROJECT_ENV,
    help=f"Gold 'app' segment to promote this item's record under (e.g. digital-dev, "
         f"digital-prod). Env: {SHOWCASE_GOLD_PROJECT_ENV}.",
)]

SearchSpaceOption = Annotated[str, typer.Option(
    "--space", envvar=SEARCH_SPACE_ENV, help=f"App space to search. Env: {SEARCH_SPACE_ENV}.",
)]

SearchTopKOption = Annotated[int, typer.Option(
    "--top-k", envvar=SEARCH_TOP_K_ENV,
    help=f"Number of results to return. Env: {SEARCH_TOP_K_ENV}.",
)]

SearchChunkSizeOption = Annotated[int, typer.Option(
    "--chunk-size", envvar=SEARCH_CHUNK_SIZE_ENV,
    help=f"Characters per chunk. Env: {SEARCH_CHUNK_SIZE_ENV}.",
)]

SearchChunkBufferOption = Annotated[int, typer.Option(
    "--chunk-buffer", envvar=SEARCH_CHUNK_BUFFER_ENV,
    help=f"Context characters added before/after each chunk. Env: {SEARCH_CHUNK_BUFFER_ENV}.",
)]

SearchEmbedModelOption = Annotated[str | None, typer.Option(
    "--model", envvar=SEARCH_EMBED_MODEL_ENV,
    help=f"Embedding model to chunk with. Env: {SEARCH_EMBED_MODEL_ENV}.",
)]

# Positional target for every root-level tool command (see
# digtk.models.workspace.resolve_targets()).
TargetArgument = Annotated[str, typer.Argument(
    help="An item ARK, a bronze item folder (e.g. "
         "bronze/preservica/haeger/latest/tree/quarter-mile), a cask:/workspace/... "
         "path, or a local folder (staged into workspace first). A workspace or local "
         "folder target covers every item under it; a bronze folder is one item.",
)]

# Shared by every root-level tool command that accepts a local folder target
# (see digtk.models.workspace.stage()) — where in workspace it gets staged.
WorkspaceFolderOption = Annotated[str | None, typer.Option(
    "--folder",
    help="Workspace folder to stage a local folder into. Defaults to the "
         "local folder's own name. Ignored for an ARK or cask:/ target.",
)]

WorkspaceUserOption = Annotated[str | None, typer.Option(
    "--user",
    help="Workspace username to stage a local folder under. Defaults to the "
         "current OS user. Ignored for an ARK or cask:/ target.",
)]

# Shared by every command digtk.lib.reporting.track() instruments. No CONFIG
# default (unlike the options above) -- reporting is opt-in per invocation:
# omitting --reporting-job-id means "don't record this command at all" (see
# digtk.lib.reporting's module docstring), so there's no sensible default
# other than None.
ReportingJobIdOption = Annotated[str | None, typer.Option(
    "--reporting-job-id", envvar=REPORTING_JOB_ID_ENV,
    help="Job identifier (e.g. a Dagster run ID) to record this command's "
         "outcome under in reporting.command. Omit to skip reporting entirely. "
         f"Env: {REPORTING_JOB_ID_ENV}.",
)]

ReportingCollectionOption = Annotated[str | None, typer.Option(
    "--reporting-collection",
    help="Collection key to record alongside this command, if known (e.g. "
         "Dagster's collection partition key). Purely descriptive -- has no "
         "effect unless --reporting-job-id is also set.",
)]
