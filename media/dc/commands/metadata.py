"""
dc metadata — metadata generation commands.

Subcommands:
  generate  — generate AI metadata draft via Samwise/Qwen
  update    — merge human and AI metadata into canonical sidecar
"""
from typing import Annotated

import typer

app = typer.Typer(help="Metadata generation and merge commands.", no_args_is_help=True)


@app.command()
def generate(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Generate AI metadata draft using Samwise (Qwen) with vision + OCR context.

    Writes metadata/ai-metadata.jsonld.json under the item process directory.
    Does not modify the canonical sidecar directly.
    """
    raise NotImplementedError("phase 0.6")


@app.command()
def update(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Merge ai-metadata and human-metadata into the canonical item sidecar.

    Human fields take precedence. Sets argonath:fieldSources on each field.
    """
    raise NotImplementedError("phase 0.6")
