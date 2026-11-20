"""
digtk ai — ad hoc Samwise AI queries, for testing prompts, models, and connectivity.

Subcommands:
  chat   — send a message and/or an image through chat completion
"""
import json
from enum import Enum
from typing import Annotated

import typer

from digtk.lib import ai, utils
from digtk.lib.config import CONFIG, SamwiseApiUrlOption

app = typer.Typer(help="Ad hoc Samwise AI queries, for testing.", no_args_is_help=True)


class OutputFormat(str, Enum):
    """Output shape for `digtk ai chat`: extracted 'response' vs the full API response."""
    text = "text"
    json = "json"


OutputFormatOption = Annotated[OutputFormat, typer.Option(
    "--output", help="'text': print just the extracted reply (default). "
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
