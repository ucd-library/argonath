"""
Samwise AI client — Qwen model integration via OpenAI-compatible API.

Uses the openai SDK pointed at samwise.library.ucdavis.edu/api.
Handles metadata generation (vision + OCR context → schema.org fields)
and embedding generation (qwen3-embed:8b for RAG indexing).

Environment variables required:
  SAMWISE_API_KEY  — bearer token for Samwise API access
  SAMWISE_API_URL  — Samwise API base URL (default: http://samwise.library.ucdavis.edu/api)
"""
import os
from openai import OpenAI

LLM_MODEL = "qwen3.6-fast:35b"
EMBED_MODEL = "qwen3-embed:8b"


def get_client():
    """Create and return an OpenAI client pointed at Samwise.

    :returns: Configured OpenAI client.
    :rtype: openai.OpenAI
    :raises RuntimeError: If SAMWISE_API_KEY is not set.
    """
    api_key = os.getenv("SAMWISE_API_KEY")
    if not api_key:
        raise RuntimeError("SAMWISE_API_KEY environment variable is not set.")
    base_url = os.getenv("SAMWISE_API_URL", "http://samwise.library.ucdavis.edu/api")
    return OpenAI(api_key=api_key, base_url=base_url)


def generate_metadata(image_path, ocr_text=None, opts={}):
    """Generate schema.org metadata fields from an image and optional OCR text.

    Sends the image (vision) and OCR text to Samwise (qwen3.6-fast:35b)
    and extracts structured metadata via tool use.

    :param str image_path: Path to the image file.
    :param str ocr_text: Optional OCR text to include as context.
    :param dict opts: Additional options (model override, etc.).
    :returns: Dict of schema.org field values.
    :rtype: dict
    """
    raise NotImplementedError("phase 0.6")


def embed(texts, opts={}):
    """Generate embeddings for a list of text strings using qwen3-embed:8b.

    :param list[str] texts: Text strings to embed.
    :param dict opts: Additional options (model override, batch size, etc.).
    :returns: List of embedding vectors (list of floats).
    :rtype: list[list[float]]
    """
    raise NotImplementedError("phase 0.7")
