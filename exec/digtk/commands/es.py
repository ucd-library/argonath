"""
digtk showcase es — index items and collections into the showcase website's Elasticsearch indexes.

Subcommands:
  upsert             — index a single item by ark
  upsert-collection  — index a collection from its gold record

Distinct from `digtk search` (LlamaIndex/pgvector RAG indexing/querying) -
different index, different consumer. See digtk.lib.showcase_index's module
docstring for why this is a separate command group rather than a `search`
subcommand.
"""
import json
from typing import Annotated

import typer

from digtk.lib import showcase_index
from digtk.lib.config import CONFIG, ShowcaseGoldProjectOption
from digtk.models.digital_website import collection_record

app = typer.Typer(help="Showcase Elasticsearch indexing commands.", no_args_is_help=True)


@app.command()
def upsert(
    ark: Annotated[str, typer.Argument(help="Item ARK to index.")],
):
    """Index a single item's schema.org metadata into the showcase search index."""
    result = showcase_index.index_item(ark)
    print(json.dumps(result))


@app.command("upsert-collection")
def upsert_collection(
    key: Annotated[str, typer.Argument(help="Collection key: [bronze/]<group>/<collection>.")],
    project: ShowcaseGoldProjectOption = CONFIG["showcase"]["gold_project"],
):
    """Index a collection into the showcase collection index. Run
    `digtk showcase build-collection` first."""
    _, name = collection_record.parse_key(key)
    result = showcase_index.index_collection(name, {"project": project})
    print(json.dumps(result))
