"""
Samwise AI client — Qwen model integration via OpenAI-compatible API.

Uses the openai SDK pointed at samwise.library.ucdavis.edu/api.
Handles metadata generation (vision + OCR context → schema.org fields)
and embedding generation (qwen3-embed:8b for RAG indexing).

Environment variables required:
  SAMWISE_API_KEY  — bearer token for Samwise API access
  SAMWISE_API_URL  — Samwise API base URL (default: see digtk.lib.config CONFIG["samwise"])
"""
import os
import base64
from openai import OpenAI

from digtk.lib.config import CONFIG

LLM_MODEL = CONFIG["samwise"]["llm_model"]
EMBED_MODEL = CONFIG["samwise"]["embed_model"]
_clients = {}

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
            - image: Optional image to include in the message (base64-encoded or URL)
    :returns: The model's response text.
    :rtype: str
    """
    client = get_client(opts)
    model = opts.get("model") or LLM_MODEL

    if isinstance(message, str):
        if 'image' in opts:
            # check if image is buffer, if so, convert to base64
            if isinstance(opts['image'], bytes):
                opts['image'] = base64.b64encode(opts['image']).decode('utf-8')
            message = [
                {"role": "user", "content": message, "image": opts['image']}
            ]
        else:
            message = [{"role": "user", "content": message}]

    response = client.chat.completions.create(model=model, messages=message)

    if( opts.get('full_response', False) ):
        return response

    return response.choices[0].message.content


def embed(texts, opts={}):
    """Generate embeddings for a list of text strings using qwen3-embed:8b.

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
