"""
digtk search — LlamaIndex RAG search commands.

Subcommands:
  index  — index a collection into pgvector via LlamaIndex
  query  — query the RAG index
"""
from typing import Annotated

import typer

from digtk.lib.config import CONFIG, SearchSpaceOption, SearchTopKOption

app = typer.Typer(help="RAG search index and query commands.", no_args_is_help=True)


@app.command()
def index(
    collection: Annotated[str, typer.Argument(help="Collection name to index.")],
    space: SearchSpaceOption = CONFIG["search"]["space"],
):
    """Index a collection's OCR text and metadata into the pgvector RAG store."""
    raise NotImplementedError("phase 0.7")


@app.command()
def query(
    text: Annotated[str, typer.Argument(help="Query string.")],
    collection: Annotated[str | None, typer.Option(help="Limit results to a collection.")] = None,
    space: SearchSpaceOption = CONFIG["search"]["space"],
    top_k: SearchTopKOption = CONFIG["search"]["top_k"],
):
    """Query the RAG index and return ranked results with ARKs."""
    raise NotImplementedError("phase 0.7")
