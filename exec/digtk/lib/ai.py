"""
Samwise AI client — Qwen model integration via OpenAI-compatible API.

Uses the openai SDK pointed at samwise.library.ucdavis.edu/api.
Handles metadata generation (vision + OCR context → schema.org fields),
embedding generation, and RAG chunking (chunk_and_embed — chunk + embed in one
batch, for pgvector indexing).

Environment variables required:
  SAMWISE_API_KEY  — bearer token for Samwise API access
  SAMWISE_API_URL  — Samwise API base URL (default: see digtk.lib.config CONFIG["samwise"])

Transport hardening (timeouts, retries, maintenance-window pause, <think>-stripping)
is ported from the standalone digitization toolkit's vllm_client.py — proven against
real samwise flakiness (a backend can wedge such that ~1/3 of requests hang until the
read timeout while /v1/models still answers; a short read timeout plus retries
re-rolls onto a healthy backend) across 6,000+ item batches. Retries are implemented
here rather than left to the openai SDK's own (narrower, HTTP-error-only) retry
logic, so `max_retries=0` is passed to the client and this module's own retry loop —
which catches broadly, matching what real runs needed — is authoritative.
"""
import datetime
import os
import re
import sys
import time
import base64
import mimetypes
from dataclasses import dataclass

import httpx
from openai import OpenAI

from digtk.lib import fs
from digtk.lib.config import CONFIG

LLM_MODEL = CONFIG["samwise"]["llm_model"]
EMBED_MODEL = CONFIG["samwise"]["embed_model"]
_clients = {}

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class Image:
    """An image to include in a chat completion request.

    :param bytes data: Raw image bytes.
    :param str mimetype: Image MIME type (e.g. 'image/jpeg').
    """
    data: bytes
    mimetype: str


def _resolve_image(image):
    """Resolve a chat_completion() 'image' opt to an Image (bytes + MIME type).

    :param image: A file path to read (local, or 'cask:/'-prefixed — see digtk.lib.fs),
        or an already-built Image.
    :type image: str or Image
    :returns: Resolved image.
    :rtype: Image
    :raises ValueError: If given a path whose extension has no known image/* MIME type.
    """
    if isinstance(image, Image):
        return image

    mimetype = mimetypes.guess_type(image)[0]
    if not mimetype or not mimetype.startswith("image/"):
        raise ValueError(
            f"Could not determine an image MIME type for '{image}' from its extension. "
            "Pass an ai.Image(data=..., mimetype=...) instead."
        )
    return Image(data=fs.read(image), mimetype=mimetype)

def get_client(opts={}):
    """Create and return an OpenAI client pointed at Samwise.

    :param dict opts: Optional overrides. 'base_url' overrides CONFIG["samwise"]["api_url"]
        (e.g. from a --samwise-api-url CLI flag). 'api_key' overrides SAMWISE_API_KEY —
        accepted for internal/test callers only; deliberately not exposed as a CLI flag,
        since a bearer token passed as a CLI argument leaks into shell history and process
        listings.
    :returns: Configured OpenAI client.
    :rtype: openai.OpenAI
    :raises RuntimeError: If SAMWISE_API_KEY is not set and opts['api_key'] is not given.
    """
    global _clients
    base_url = opts.get("base_url") or CONFIG["samwise"]["api_url"]
    if base_url in _clients:
        return _clients[base_url]

    api_key = opts.get("api_key") or os.getenv(CONFIG["samwise"]["api_key_env"])
    if not api_key:
        raise RuntimeError(f"{CONFIG['samwise']['api_key_env']} environment variable is not set and no override provided.")

    timeout = httpx.Timeout(
        CONFIG["samwise"]["read_timeout_s"], connect=CONFIG["samwise"]["connect_timeout_s"]
    )
    # max_retries=0: this module's own chat_completion() retry loop is authoritative
    # (see module docstring) — letting the SDK also retry would compound backoffs.
    _clients[base_url] = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
    return _clients[base_url]


def _wait_out_maintenance():
    """Sleep until outside the configured nightly maintenance window, if any.

    :param: None — reads CONFIG["samwise"]["maint_start"]/["maint_end"] (HH:MM,
        24-hour; either empty disables the pause).
    """
    start, end = CONFIG["samwise"]["maint_start"], CONFIG["samwise"]["maint_end"]
    if not start or not end:
        return
    start_min = int(start[:2]) * 60 + int(start[3:5])
    end_min = int(end[:2]) * 60 + int(end[3:5])
    while True:
        now = datetime.datetime.now()
        cur_min = now.hour * 60 + now.minute
        in_window = (start_min <= cur_min < end_min) if start_min < end_min else (cur_min >= start_min or cur_min < end_min)
        if not in_window:
            return
        time.sleep(60)


def _extract_content(message):
    """Pull the reply text out of a chat completion message, stripping <think> blocks.

    Some backend models are reasoning models that put text in a
    'reasoning_content'/'reasoning' field instead of 'content', and/or leak
    <think>...</think> blocks into whichever field they use — both observed
    against real samwise backends. Plain getattr (not e.g. model_dump()) so this
    works against both the real SDK's pydantic message object — whose model
    allows extra fields, exposed as attributes — and simple test doubles alike.

    :param message: The response's `choices[0].message`.
    :returns: Cleaned reply text.
    :rtype: str
    """
    text = getattr(message, "content", None) or getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None) or ""
    return _THINK.sub("", text).strip()


def chat_completion(message, opts={}):
    """Generate a chat completion.

    Retries transient failures (connection errors, timeouts, the response shape
    the backend happens to return that day — anything) with capped exponential
    backoff, waiting out any configured nightly maintenance window first — see
    the module docstring. Not applied when opts['full_response'] is set, since a
    raw response object has no single "did this fail" signal to retry on here;
    that caller is expected to be doing its own inspection (e.g. `digtk ai chat
    --output json`).

    :param str message: The user's message.
    :param dict opts: Additional options
        Supported keys:
            - model: Override the default model
            - image: Optional image to include in the message — a file path (str, local
              or 'cask:/'-prefixed) or an ai.Image(data=bytes, mimetype=str)
            - full_response: Return the raw SDK response object, unretried, unstripped.
    :returns: The model's response text (or the raw response, see full_response above).
    :rtype: str
    :raises RuntimeError: If every retry attempt fails.
    """
    client = get_client(opts)
    model = opts.get("model") or LLM_MODEL

    if isinstance(message, str):
        if 'image' in opts:
            image = _resolve_image(opts['image'])
            b64 = base64.b64encode(image.data).decode('utf-8')
            message = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": message},
                    {"type": "image_url", "image_url": {"url": f"data:{image.mimetype};base64,{b64}"}},
                ],
            }]
        else:
            message = [{"role": "user", "content": message}]

    if opts.get('full_response', False):
        return client.chat.completions.create(model=model, messages=message)

    _wait_out_maintenance()
    retries = CONFIG["samwise"]["retries"]
    last_error = None
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(model=model, messages=message)
            return _extract_content(response.choices[0].message)
        except Exception as e:  # noqa: BLE001 — a transient endpoint blip should retry
            last_error = e
            print(
                f"digtk: Samwise attempt {attempt + 1}/{retries} failed "
                f"({type(e).__name__}: {e}); retrying", file=sys.stderr,
            )
            time.sleep(min(60, 2 ** attempt))
    raise RuntimeError(f"Samwise chat completion failed after {retries} tries: {last_error}")


def embed(texts, opts={}):
    """Generate embeddings for a list of text strings using qwen3-embed:4b.

    :param list[str] texts: Text strings to embed.
    :param dict opts: Additional options (model override, batch size, etc.).
    :returns: List of embedding vectors (list of floats).
    :rtype: list[list[float]]
    """
    client = get_client(opts)
    model = opts.get("model") or EMBED_MODEL

    response = client.embeddings.create(model=model, input=texts)
    if opts.get('full_response', False):
        return response

    return [item.embedding for item in response.data]


def chunk_text(text, opts={}):
    """Split text into fixed-size, non-overlapping chunks with a surrounding context buffer.

    Chunks tile `text` end-to-end with no gaps or overlap between their core spans
    (`start`/`end`), so citations built from those offsets never double-count a
    character. Each chunk's `text` — what actually gets embedded — additionally
    includes up to `buffer` characters of context from just before `start` and just
    after `end` (clamped to the string bounds), since a little surrounding context
    measurably improves embedding quality at a chunk boundary, without the
    storage/dedup cost of overlapping the core chunks themselves.

    :param str text: Text to split.
    :param dict opts: 'chunk_size' (int, characters per core chunk, default
        CONFIG["search"]["chunk_size"]), 'buffer' (int, context characters added
        before/after each core chunk, default CONFIG["search"]["chunk_buffer"]).
    :returns: Chunk dicts, in source order, each with:
        - text: core chunk text plus its surrounding buffer (what gets embedded)
        - start/end: core chunk's character offsets in `text` (non-overlapping, tile the input)
        - buffer_start/buffer_end: character offsets of the buffered `text` in the source
    :rtype: list[dict]
    :raises ValueError: If chunk_size is not positive or buffer is negative.
    """
    chunk_size = opts.get("chunk_size", CONFIG["search"]["chunk_size"])
    buffer = opts.get("buffer", CONFIG["search"]["chunk_buffer"])
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    if buffer < 0:
        raise ValueError(f"buffer must not be negative, got {buffer}")

    chunks = []
    for start in range(0, len(text), chunk_size):
        end = min(start + chunk_size, len(text))
        buffer_start = max(0, start - buffer)
        buffer_end = min(len(text), end + buffer)
        chunks.append({
            "text": text[buffer_start:buffer_end],
            "start": start,
            "end": end,
            "buffer_start": buffer_start,
            "buffer_end": buffer_end,
        })
    return chunks


def chunk_and_embed(text=None, path=None, opts={}):
    """Chunk text (or a file's contents) and embed every chunk in one batch.

    :param str text: Raw text to chunk. Exactly one of `text`/`path` must be given.
    :param str path: Path to read as UTF-8 text (local, or 'cask:/'-prefixed — see
        digtk.lib.fs). Exactly one of `text`/`path` must be given.
    :param dict opts: 'chunk_size'/'buffer' (see chunk_text()); 'model' overrides
        CONFIG["search"]["embed_model"] (RAG chunking defaults to the larger embedding
        model — see digtk.lib.config — distinct from embed()'s ad hoc default).
    :returns: {"model": embedding model used, "chunks": [chunk_text() dicts, each with
        an added "vector" key]}
    :rtype: dict
    :raises ValueError: If both or neither of `text`/`path` are given.
    """
    if (text is None) == (path is None):
        raise ValueError("chunk_and_embed() requires exactly one of 'text' or 'path'")

    if path is not None:
        text = fs.read(path).decode("utf-8")

    chunks = chunk_text(text, opts)
    model = opts.get("model") or CONFIG["search"]["embed_model"]

    if chunks:
        vectors = embed([chunk["text"] for chunk in chunks], {**opts, "model": model})
        for chunk, vector in zip(chunks, vectors):
            chunk["vector"] = vector

    return {"model": model, "chunks": chunks}
