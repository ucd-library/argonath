"""
digtk public-dc es — index items into the DAMS website's Elasticsearch index.

Subcommands:
  upsert  — index a single item by ark

Distinct from `digtk search` (LlamaIndex/pgvector RAG indexing/querying) -
different index, different consumer. See digtk.lib.dams_index's module
docstring for why this is a separate command group rather than a `search`
subcommand.
"""
import json
from typing import Annotated

import typer

from digtk.lib import dams_index

app = typer.Typer(help="DAMS Elasticsearch indexing commands.", no_args_is_help=True)


@app.command()
def upsert(
    ark: Annotated[str, typer.Argument(help="Item ARK to index.")],
):
    """Index a single item's schema.org metadata into the DAMS search index."""
    result = dams_index.index_item(ark)
    print(json.dumps(result))
