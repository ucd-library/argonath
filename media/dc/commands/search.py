"""
dc search — LlamaIndex RAG search commands.

Subcommands:
  index  — index a collection into pgvector via LlamaIndex
  query  — query the RAG index
"""
from typing import Annotated

import typer

app = typer.Typer(help="RAG search index and query commands.", no_args_is_help=True)


@app.command()
def index(
    collection: Annotated[str, typer.Argument(help="Collection name to index.")],
    space: Annotated[str, typer.Option(help="App space to index from.")] = "arda",
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Index a collection's OCR text and metadata into the pgvector RAG store."""
    raise NotImplementedError("phase 0.7")


@app.command()
def query(
    text: Annotated[str, typer.Argument(help="Query string.")],
    collection: Annotated[str | None, typer.Option(help="Limit results to a collection.")] = None,
    space: Annotated[str, typer.Option(help="App space to search.")] = "arda",
    top_k: Annotated[int, typer.Option("--top-k", help="Number of results to return.")] = 10,
):
    """Query the RAG index and return ranked results with ARKs."""
    raise NotImplementedError("phase 0.7")
