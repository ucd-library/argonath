"""
digtk metadata — metadata generation commands.

Subcommands:
  generate  — generate AI metadata draft via Samwise/Qwen
  update    — merge human and AI metadata into canonical sidecar
"""
from typing import Annotated

import typer

from digtk.lib.config import SamwiseApiUrlOption

app = typer.Typer(help="Metadata generation and merge commands.", no_args_is_help=True)


@app.command()
def generate(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    samwise_api_url: SamwiseApiUrlOption = None,
):
    """Generate AI metadata draft using Samwise (Qwen) with vision + OCR context.

    Writes metadata/ai-metadata.jsonld.json under the item process directory.
    Does not modify the canonical sidecar directly.
    """
    raise NotImplementedError("phase 0.6")


@app.command()
def update(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
):
    """Merge ai-metadata and human-metadata into the canonical item sidecar.

    Human fields take precedence. Sets argonath:fieldSources on each field.
    """
    raise NotImplementedError("phase 0.6")
