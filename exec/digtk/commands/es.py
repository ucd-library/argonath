"""
digtk showcase es — index items into the showcase website's Elasticsearch index.

Subcommands:
  upsert  — index a single item by ark

Distinct from `digtk search` (LlamaIndex/pgvector RAG indexing/querying) -
different index, different consumer. See digtk.lib.showcase_index's module
docstring for why this is a separate command group rather than a `search`
subcommand.
"""
import json
from typing import Annotated

import typer

from digtk.lib import showcase_index

app = typer.Typer(help="Showcase Elasticsearch indexing commands.", no_args_is_help=True)


@app.command()
def upsert(
    ark: Annotated[str, typer.Argument(help="Item ARK to index.")],
):
    """Index a single item's schema.org metadata into the showcase search index."""
    result = showcase_index.index_item(ark)
    print(json.dumps(result))
