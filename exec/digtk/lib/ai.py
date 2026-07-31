"""
Samwise AI client — Qwen model integration via OpenAI-compatible API.

Uses the openai SDK pointed at samwise.library.ucdavis.edu/api.
Handles metadata generation (vision + OCR context → schema.org fields),
embedding generation, and RAG chunking (chunk_and_embed — chunk + embed in one
batch, for pgvector indexing).

Environment variables required:
  SAMWISE_API_KEY  — bearer token for Samwise API access
  SAMWISE_API_URL  — Samwise API base URL (default: see digtk.lib.config CONFIG["samwise"])
"""
import os
import base64
import mimetypes
from dataclasses import dataclass

from openai import OpenAI

from digtk.lib import fs
from digtk.lib.config import CONFIG

LLM_MODEL = CONFIG["samwise"]["llm_model"]
EMBED_MODEL = CONFIG["samwise"]["embed_model"]
_clients = {}


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

    _clients[base_url] = OpenAI(api_key=api_key, base_url=base_url)
    return _clients[base_url]


def chat_completion(message, opts={}):
    """Generate a chat completion.

    :param str message: The user's message.
    :param dict opts: Additional options
        Supported keys:
            - model: Override the default model
            - image: Optional image to include in the message — a file path (str, local
              or 'cask:/'-prefixed) or an ai.Image(data=bytes, mimetype=str)
    :returns: The model's response text.
    :rtype: str
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

    response = client.chat.completions.create(model=model, messages=message)

    if( opts.get('full_response', False) ):
        return response

    return response.choices[0].message.content


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
