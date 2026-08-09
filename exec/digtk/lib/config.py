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
CLI surface (digtk.lib.ai, digtk.lib.auth, digtk.lib.ark, ...) just reads CONFIG
directly and gets the same env override for free, without needing any Typer plumbing.

Secrets (bearer tokens, API keys) are deliberately kept out of CONFIG's resolved
values — only their *env var names* are recorded here (SAMWISE_API_KEY_ENV,
CASK_API_KEY_ENV) — see digtk.lib.auth for why a token must never be a CLI flag or a
default that could get logged (shell history, process listings). A secret set via
`digtk config set samwise.api_key` is written to the home config file (which gets the
same owner-only permissions as ~/.digtk/credentials.json) and is only ever surfaced
back to a caller as an env var, never as a CONFIG value.
"""
import json
import os
import stat
import sys
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

GATEWAY_URL_ENV = "DIGTK_GATEWAY_URL"
AUTH_LOGIN_TIMEOUT_ENV = "DIGTK_AUTH_LOGIN_TIMEOUT_S"

ARK_NAAN_ENV = "DIGTK_ARK_NAAN"
ARK_SHOULDER_ENV = "DIGTK_ARK_SHOULDER"
ARK_NOID_ALPHABET_ENV = "DIGTK_ARK_NOID_ALPHABET"

FS_CHUNK_SIZE_ENV = "DIGTK_FS_CHUNK_SIZE"

SEARCH_SPACE_ENV = "DIGTK_SEARCH_SPACE"
SEARCH_TOP_K_ENV = "DIGTK_SEARCH_TOP_K"
SEARCH_CHUNK_SIZE_ENV = "DIGTK_SEARCH_CHUNK_SIZE"
SEARCH_CHUNK_BUFFER_ENV = "DIGTK_SEARCH_CHUNK_BUFFER"
SEARCH_EMBED_MODEL_ENV = "DIGTK_SEARCH_EMBED_MODEL"

IMAGE_THUMBNAIL_WIDTHS_ENV = "DIGTK_IMAGE_THUMBNAIL_WIDTHS"
IMAGE_CONVERT_FORMAT_ENV = "DIGTK_IMAGE_CONVERT_FORMAT"

HARVEST_CONTENT_TAGS_ENV = "DIGTK_HARVEST_CONTENT_TAGS"
HARVEST_USER_AGENT_ENV = "DIGTK_HARVEST_USER_AGENT"
HARVEST_TIMEOUT_ENV = "DIGTK_HARVEST_TIMEOUT_S"
HARVEST_GROUP_ENV = "DIGTK_HARVEST_GROUP"

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
    {"dotpath": "ocr.confidence_threshold", "env": OCR_CONFIDENCE_THRESHOLD_ENV, "cast": float, "secret": False},
    {"dotpath": "samwise.api_url", "env": SAMWISE_API_URL_ENV, "cast": str, "secret": False},
    {"dotpath": "samwise.api_key", "env": SAMWISE_API_KEY_ENV, "cast": str, "secret": True},
    {"dotpath": "samwise.llm_model", "env": SAMWISE_LLM_MODEL_ENV, "cast": str, "secret": False},
    {"dotpath": "samwise.embed_model", "env": SAMWISE_EMBED_MODEL_ENV, "cast": str, "secret": False},
    {"dotpath": "ark.naan", "env": ARK_NAAN_ENV, "cast": str, "secret": False},
    {"dotpath": "ark.shoulder", "env": ARK_SHOULDER_ENV, "cast": str, "secret": False},
    {"dotpath": "ark.noid_alphabet", "env": ARK_NOID_ALPHABET_ENV, "cast": str, "secret": False},
    {"dotpath": "gateway.url", "env": GATEWAY_URL_ENV, "cast": str, "secret": False},
    {"dotpath": "auth.login_timeout_s", "env": AUTH_LOGIN_TIMEOUT_ENV, "cast": float, "secret": False},
    {"dotpath": "fs.chunk_size", "env": FS_CHUNK_SIZE_ENV, "cast": int, "secret": False},
    {"dotpath": "search.space", "env": SEARCH_SPACE_ENV, "cast": str, "secret": False},
    {"dotpath": "search.top_k", "env": SEARCH_TOP_K_ENV, "cast": int, "secret": False},
    {"dotpath": "search.chunk_size", "env": SEARCH_CHUNK_SIZE_ENV, "cast": int, "secret": False},
    {"dotpath": "search.chunk_buffer", "env": SEARCH_CHUNK_BUFFER_ENV, "cast": int, "secret": False},
    {"dotpath": "search.embed_model", "env": SEARCH_EMBED_MODEL_ENV, "cast": str, "secret": False},
    {"dotpath": "image.thumbnail_widths", "env": IMAGE_THUMBNAIL_WIDTHS_ENV, "cast": str, "secret": False},
    {"dotpath": "image.convert_format", "env": IMAGE_CONVERT_FORMAT_ENV, "cast": str, "secret": False},
    {"dotpath": "harvest.content_tags", "env": HARVEST_CONTENT_TAGS_ENV, "cast": str, "secret": False},
    {"dotpath": "harvest.user_agent", "env": HARVEST_USER_AGENT_ENV, "cast": str, "secret": False},
    {"dotpath": "harvest.timeout_s", "env": HARVEST_TIMEOUT_ENV, "cast": float, "secret": False},
    {"dotpath": "harvest.group", "env": HARVEST_GROUP_ENV, "cast": str, "secret": False},
]
_SETTINGS_BY_DOTPATH = {setting["dotpath"]: setting for setting in SETTINGS}

# Short, memorable stand-ins for settings people type ad hoc on the CLI a lot. Purely
# an input-side shorthand: resolved to the canonical dotpath before every lookup, so
# `config.json` and `digtk config show` only ever deal in canonical dotpaths.
ALIASES = {
    "host": "gateway.url",
}

HOME_CONFIG_PATH = Path(_env(CONFIG_PATH_ENV, str(Path.home() / ".digtk" / "config.json")))


def resolve_alias(dotpath):
    """Resolve a short alias (e.g. 'host') to its canonical dotpath.

    :param str dotpath: Setting name as typed on the CLI — an alias or already a canonical dotpath.
    :returns: The canonical dotpath, unchanged if `dotpath` wasn't a registered alias.
    :rtype: str
    """
    return ALIASES.get(dotpath, dotpath)


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

    :param str dotpath: Dotted setting name (e.g. 'samwise.api_key') or a registered alias.
    :returns: True if the setting is registered and marked secret.
    :rtype: bool
    """
    setting = _SETTINGS_BY_DOTPATH.get(resolve_alias(dotpath))
    return bool(setting and setting["secret"])


def set_setting(dotpath, value, path=None):
    """Persist a single setting to the home config file.

    :param str dotpath: Dotted setting name, e.g. 'samwise.api_url', or a registered alias
        (e.g. 'host'). Must resolve to a key in SETTINGS. Always stored under its canonical
        dotpath, regardless of which form was passed in.
    :param str value: Raw value to store, as typed on the CLI or entered at a prompt.
    :param Path path: Home config file to write. Defaults to HOME_CONFIG_PATH.
    :raises ValueError: If dotpath is not a recognized setting, or value fails its cast.
    """
    dotpath = resolve_alias(dotpath)
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
        "login_timeout_s": _env(AUTH_LOGIN_TIMEOUT_ENV, 300.0, float),
    },
    # Anduin auth gateway — all traffic to CaskFS, Dagster, and auth itself goes
    # through this one host. See `digtk auth login`; the stored CaskFS api_url is
    # gateway.url + "/cask".
    "gateway": {
        "url": _env(GATEWAY_URL_ENV, "https://argonath.library.ucdavis.edu"),
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
        # Chunk size/buffer are in characters. 1000/200 (20% buffer) are common RAG
        # chunking defaults (e.g. LangChain's RecursiveCharacterTextSplitter default).
        "chunk_size": _env(SEARCH_CHUNK_SIZE_ENV, 1000, int),
        "chunk_buffer": _env(SEARCH_CHUNK_BUFFER_ENV, 200, int),
        # Distinct from samwise.embed_model (ad hoc `digtk ai embed` default) — RAG
        # chunking favors the larger/more accurate embedding model.
        "embed_model": _env(SEARCH_EMBED_MODEL_ENV, _env(SAMWISE_EMBED_MODEL_ENV, "qwen3-embed:4b")),
    },
    "image": {
        "thumbnail_widths": _env(IMAGE_THUMBNAIL_WIDTHS_ENV, "256,512,1024"),
        "convert_format": _env(IMAGE_CONVERT_FORMAT_ENV, "jpeg"),
    },
    "harvest": {
        # Comma-separated CSS selectors/tag names, parsed by digtk.lib.http_harvest.
        "content_tags": _env(HARVEST_CONTENT_TAGS_ENV, "article,main"),
        "user_agent": _env(HARVEST_USER_AGENT_ENV, "digtk-harvest/0.1 (+https://library.ucdavis.edu)"),
        "timeout_s": _env(HARVEST_TIMEOUT_ENV, 30.0, float),
        # Group namespace for structured (--output fs/cask) bronze/silver harvest writes.
        "group": _env(HARVEST_GROUP_ENV, "rag"),
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

HarvestContentTagsOption = Annotated[str, typer.Option(
    "--tags", envvar=HARVEST_CONTENT_TAGS_ENV,
    help=f"Comma-separated CSS selectors/tag names to extract page content from. Env: {HARVEST_CONTENT_TAGS_ENV}.",
)]

HarvestUserAgentOption = Annotated[str, typer.Option(
    "--user-agent", envvar=HARVEST_USER_AGENT_ENV,
    help=f"User-Agent header sent with harvest requests. Env: {HARVEST_USER_AGENT_ENV}.",
)]

HarvestTimeoutOption = Annotated[float, typer.Option(
    "--timeout", envvar=HARVEST_TIMEOUT_ENV,
    help=f"HTTP request timeout in seconds. Env: {HARVEST_TIMEOUT_ENV}.",
)]

HarvestGroupOption = Annotated[str, typer.Option(
    "--group", envvar=HARVEST_GROUP_ENV,
    help=f"Group namespace for bronze/silver harvest paths (--output fs/cask only). Env: {HARVEST_GROUP_ENV}.",
)]
