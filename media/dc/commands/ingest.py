"""
dc ingest — ingest commands.

Subcommands:
  file   — ingest a single file into a CaskFS collection
  batch  — ingest all files in a directory into a CaskFS collection
"""
from typing import Annotated

import typer

app = typer.Typer(help="Ingest files into CaskFS collections.", no_args_is_help=True)


@app.command()
def file(
    path: Annotated[str, typer.Argument(help="Path to the source file.")],
    collection: Annotated[str, typer.Option(help="Collection name.")],
    space: Annotated[str, typer.Option(help="Target app space (e.g. digital-dev, arda).")] = "digital-dev",
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Ingest a single file, mint an ARK, write the initial schema.org sidecar."""
    raise NotImplementedError("phase 0.3")


@app.command()
def batch(
    directory: Annotated[str, typer.Argument(help="Directory of source files to ingest.")],
    collection: Annotated[str, typer.Option(help="Collection name.")],
    space: Annotated[str, typer.Option(help="Target app space (e.g. digital-dev, arda).")] = "digital-dev",
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Ingest all files in a directory as a batch."""
    raise NotImplementedError("phase 0.3")
