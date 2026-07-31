"""
digtk ai — ad hoc Samwise AI queries, for testing prompts, models, and connectivity.

Subcommands:
  chat   — send a message and/or an image through chat completion
  embed  — send a message through the embedding model
  chunk  — chunk text (or a file) and embed every chunk, for testing RAG chunking
"""
import json
from enum import Enum
from typing import Annotated

import typer

from digtk.lib import ai, fs, utils
from digtk.lib.config import (
    CONFIG,
    SamwiseApiUrlOption,
    SearchChunkBufferOption,
    SearchChunkSizeOption,
    SearchEmbedModelOption,
)

app = typer.Typer(help="Ad hoc Samwise AI queries, for testing.", no_args_is_help=True)


class OutputFormat(str, Enum):
    """Output shape for `digtk ai chat`/`embed`: extracted 'response' vs the full API response."""
    text = "text"
    json = "json"


OutputFormatOption = Annotated[OutputFormat, typer.Option(
    "--output", help="'text': print just the extracted reply/vector (default). "
    "'json': print the full Samwise API response as JSON.",
)]


@app.command()
def chat(
    message: Annotated[str | None, typer.Option("--message", help="Message to send.")] = None,
    image: Annotated[str | None, typer.Option(
        "--image", help="Path to an image file to include (local, or 'cask:/'-prefixed).",
    )] = None,
    model: Annotated[str | None, typer.Option(
        "--model", help=f"Override the chat model. Default: {CONFIG['samwise']['llm_model']}.",
    )] = None,
    samwise_api_url: SamwiseApiUrlOption = None,
    output: OutputFormatOption = OutputFormat.text,
):
    """Send a message and/or an image to Samwise chat completions and print the response."""
    if not message and not image:
        utils.error("Provide --message and/or --image.")

    opts = {"full_response": output == OutputFormat.json}
    if model:
        opts["model"] = model
    if samwise_api_url:
        opts["base_url"] = samwise_api_url
    if image:
        opts["image"] = image

    try:
        response = ai.chat_completion(message or "Describe this image.", opts=opts)
    except (ValueError, OSError) as e:
        utils.error(str(e))

    if output == OutputFormat.json:
        print(json.dumps(response.model_dump()))
    else:
        print(response)


@app.command()
def embed(
    message: Annotated[str | None, typer.Option("--message", help="Text to embed.")] = None,
    path: Annotated[str | None, typer.Option(
        "--path", help="Path to a UTF-8 text file to embed (local, or 'cask:/'-prefixed).",
    )] = None,
    model: Annotated[str | None, typer.Option(
        "--model", help=f"Override the embed model. Default: {CONFIG['samwise']['embed_model']}.",
    )] = None,
    samwise_api_url: SamwiseApiUrlOption = None,
    output: OutputFormatOption = OutputFormat.text,
):
    """Send a message or a file's contents to Samwise embeddings and print the resulting vector."""
    if not message and not path:
        utils.error("Provide --message or --path.")
    if message and path:
        utils.error("Provide only one of --message or --path.")

    if path:
        try:
            message = fs.read(path).decode("utf-8")
        except OSError as e:
            utils.error(f"Could not read '{path}': {e}")

    opts = {"full_response": output == OutputFormat.json}
    if model:
        opts["model"] = model
    if samwise_api_url:
        opts["base_url"] = samwise_api_url

    response = ai.embed([message], opts=opts)
    if output == OutputFormat.json:
        print(json.dumps(response.model_dump()))
    else:
        print(json.dumps(response[0]))


@app.command()
def chunk(
    text: Annotated[str | None, typer.Option("--text", help="Raw text to chunk.")] = None,
    path: Annotated[str | None, typer.Option(
        "--path", help="Path to a UTF-8 text file to chunk (local, or 'cask:/'-prefixed).",
    )] = None,
    chunk_size: SearchChunkSizeOption = CONFIG["search"]["chunk_size"],
    chunk_buffer: SearchChunkBufferOption = CONFIG["search"]["chunk_buffer"],
    model: SearchEmbedModelOption = None,
    samwise_api_url: SamwiseApiUrlOption = None,
):
    """Chunk text (or a file) and embed every chunk; prints {model, chunks} as JSON."""
    if not text and not path:
        utils.error("Provide --text or --path.")

    opts = {"chunk_size": chunk_size, "buffer": chunk_buffer}
    if model:
        opts["model"] = model
    if samwise_api_url:
        opts["base_url"] = samwise_api_url

    try:
        result = ai.chunk_and_embed(text=text, path=path, opts=opts)
    except (ValueError, OSError) as e:
        utils.error(str(e))

    utils.log(f"model: {result['model']}")
    utils.log(f"chunks: {len(result['chunks'])}")
    utils.result({"status": "success", **result})
