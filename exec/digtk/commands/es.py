"""
digtk showcase es — index items and collections into the showcase website's Elasticsearch indexes.

Subcommands:
  upsert             — index a single item by ark
  upsert-collection  — index a collection from its gold record
  upsert-items       — bulk-index items by bronze folder
  gold-items         — list the bronze folders of a collection's gold item records
  bulk-upsert        — bulk-index a whole collection, prune orphans, refresh the collection
  prune              — delete indexed items no longer in bronze
  delete             — delete one or more items by ark
  delete-collection  — delete a collection and all its items

Distinct from `digtk search` (LlamaIndex/pgvector RAG indexing/querying) -
different index, different consumer. See digtk.lib.showcase_index's module
docstring for why this is a separate command group rather than a `search`
subcommand.
"""
import json
from typing import Annotated, List

import httpx
import typer

from digtk.lib import showcase_index, utils
from digtk.lib.config import CONFIG, ShowcaseGoldProjectOption
from digtk.models.digital_website import collection_index, collection_record

app = typer.Typer(help="Showcase Elasticsearch indexing commands.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError, httpx.HTTPError)

CollectionKeyArgument = Annotated[str, typer.Argument(help="Collection key: [bronze/]<group>/<collection>.")]
RebuildOption = Annotated[bool, typer.Option("--rebuild", help="Rebuild each gold item record first.")]


@app.command()
def upsert(
    ark: Annotated[str, typer.Argument(help="Item ARK to index.")],
):
    """Index a single item's schema.org metadata into the showcase search index."""
    result = showcase_index.index_item(ark)
    print(json.dumps(result))


@app.command("upsert-collection")
def upsert_collection(
    key: CollectionKeyArgument,
    project: ShowcaseGoldProjectOption = CONFIG["showcase"]["gold_project"],
):
    """Index a collection into the showcase collection index. Run
    `digtk showcase build-collection` first."""
    _, name = collection_record.parse_key(key)
    result = showcase_index.index_collection(name, {"project": project})
    print(json.dumps(result))


@app.command("upsert-items")
def upsert_items(
    folders: Annotated[List[str], typer.Argument(help="Bronze item folders.")],
    rebuild: RebuildOption = False,
    project: ShowcaseGoldProjectOption = CONFIG["showcase"]["gold_project"],
):
    """Bulk-index items by bronze folder from their gold records; folders without one are skipped.
    Exits 0 with failures listed in the result, so callers can report each item."""
    try:
        result = collection_index.reindex_items(folders, {"rebuild": rebuild, "project": project})
        for failure in result["failed"]:
            utils.warn(f"{failure['folder']}: {failure['error']}")
        status = "partial" if result["failed"] else "success"
        utils.result({"status": status, **result})
    except _ERRORS as e:
        utils.error(str(e))


@app.command("gold-items")
def gold_items(
    key: CollectionKeyArgument,
    project: ShowcaseGoldProjectOption = CONFIG["showcase"]["gold_project"],
):
    """List the bronze folder of every item with a gold record in the collection."""
    try:
        folders = collection_index.gold_item_folders(key, {"project": project})
        utils.log(f"{len(folders)} gold item record(s)")
        utils.result({"status": "success", "collection": key, "items": folders})
    except _ERRORS as e:
        utils.error(str(e))


@app.command("bulk-upsert")
def bulk_upsert(
    key: CollectionKeyArgument,
    rebuild: RebuildOption = False,
    prune: Annotated[bool, typer.Option("--prune/--no-prune", help="Delete indexed items no longer in bronze.")] = True,
    force_prune: Annotated[bool, typer.Option("--force-prune", help="Skip the prune safety guard.")] = False,
    batch_size: Annotated[int, typer.Option(help="Items per bulk request.")] = collection_index.DEFAULT_BATCH_SIZE,
    project: ShowcaseGoldProjectOption = CONFIG["showcase"]["gold_project"],
):
    """Reindex every item in a collection without making derivatives, prune
    orphans, then refresh the collection document. Fails if any item failed."""
    try:
        result = collection_index.reindex_collection(key, {
            "rebuild": rebuild, "prune": prune, "force_prune": force_prune,
            "batch_size": batch_size, "project": project,
        })
        for failure in result["failed"]:
            utils.warn(f"{failure['folder']}: {failure['error']}")
        if result["failed"]:
            utils.error(f"{len(result['failed'])} of {result['items']} item(s) failed, see warnings above")
        utils.result({"status": "success", **result})
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def prune(
    key: CollectionKeyArgument,
    force: Annotated[bool, typer.Option("--force", help="Skip the prune safety guard.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="List orphans without deleting.")] = False,
    project: ShowcaseGoldProjectOption = CONFIG["showcase"]["gold_project"],
):
    """Delete indexed items whose ARK is no longer in the collection's bronze items."""
    try:
        result = collection_index.prune_collection(key, {"force": force, "dry_run": dry_run, "project": project})
        for orphan in result["orphans"]:
            utils.log(f"{'would delete' if dry_run else 'deleted'} {orphan}")
        utils.result({"status": "success", **result})
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def delete(
    arks: Annotated[List[str], typer.Argument(help="Item ARK(s) to delete.")],
    project: ShowcaseGoldProjectOption = CONFIG["showcase"]["gold_project"],
):
    """Delete items and their page documents. An item comes back on the next
    reindex if it's still in bronze."""
    try:
        result = collection_index.delete_items(arks, {"project": project})
        # single-ark callers read "collection"
        collection = result["deleted"][0]["collection"] if len(arks) == 1 else None
        utils.result({"status": "success", "collection": collection, **result})
    except _ERRORS as e:
        utils.error(str(e))


@app.command("delete-collection")
def delete_collection(
    key: CollectionKeyArgument,
    project: ShowcaseGoldProjectOption = CONFIG["showcase"]["gold_project"],
):
    """Delete a collection, its items and their pages from ES only."""
    try:
        utils.result({"status": "success", **collection_index.delete_collection(key, {"project": project})})
    except _ERRORS as e:
        utils.error(str(e))
