"""
digtk state — the collection_state processing-state store (see
digtk.models.collection_state and exec/digtk/sql/collection_state.sql).
Mostly called by Dagster (dagster/lib/collection_state.py), not by hand.

Subcommands:
  init-pg            — apply the collection_state Postgres schema. Safe to run repeatedly.
  enqueue-collection — queue an item run for every item in a collection's bronze
                       tree/
  enqueue-items      — queue an item run for specific item folders
  cask-changes       — settled bronze item changes from the CaskFS audit log
  pending            — list the oldest queued rows for a job (doesn't remove them)
  ack                — remove queued rows once their runs were submitted
  result             — record a job's latest result for an item or a collection
  item-ark           — the ARK last recorded for an item folder
  rollups-due        — collections whose items finished since their last rollup
  rollup             — recompute a collection's item count and failed items
"""
import json
from typing import Annotated

import psycopg2
import typer

from digtk.lib import utils
from digtk.models import cask_changes, collection_state

app = typer.Typer(help="collection_state queue and job results.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError, psycopg2.Error)


@app.command("init-pg")
def init_pg():
    """Apply the collection_state schema (creates the 'collection_state' schema,
    its tables and upsert_job_result() if they don't already exist)."""
    try:
        collection_state.init_pg()
        utils.log("Applied the collection_state schema.")
        utils.result({"status": "success", "schema": "collection_state"})
    except _ERRORS as e:
        utils.error(str(e))


@app.command("enqueue-collection")
def enqueue_collection(
    collection_key: Annotated[str, typer.Option(
        "--collection-key", help="Collection key, e.g. bronze/preservica/haeger.",
    )],
    batch: Annotated[str | None, typer.Option(
        help="Batch id recorded on every queued row (e.g. the launching Dagster run id).",
    )] = None,
):
    """Queue a process_item_job run for every item in the collection's bronze
    tree/."""
    try:
        result = collection_state.enqueue_collection(collection_key, {"batch": batch} if batch else {})
        utils.log(
            f"{result['collection']}: {result['items']} item(s) ({result['added']} new), "
            f"{result['enqueued']} queued"
        )
        utils.result({"status": "success", **result})
    except _ERRORS as e:
        utils.error(str(e))


@app.command("enqueue-items")
def enqueue_items(
    items: Annotated[list[str], typer.Option("--item", help="Bronze item folder (repeatable).")],
    trigger: Annotated[str, typer.Option(help="Recorded on each row and as a run tag.")] = "items",
    batch: Annotated[str | None, typer.Option(help="Batch id recorded on every queued row.")] = None,
):
    """Queue a process_item_job run for each item folder."""
    try:
        opts = {"trigger": trigger, **({"batch": batch} if batch else {})}
        result = collection_state.enqueue_items(items, opts)
        utils.log(f"{result['enqueued']} item run(s) queued")
        utils.result({"status": "success", **result})
    except _ERRORS as e:
        utils.error(str(e))


@app.command("cask-changes")
def cask_changes_cmd(
    cursor: Annotated[str | None, typer.Option(help="Cursor JSON from the previous call; omit to start now.")] = None,
    settle_seconds: Annotated[int, typer.Option(help="Quiet time before an item is acted on.")] = cask_changes.DEFAULT_SETTLE_SECONDS,
    limit: Annotated[int, typer.Option(help="Most audit rows to read.")] = 5000,
):
    """List settled bronze item changes from the CaskFS audit log. Read-only;
    with no cursor it returns a starting cursor at the newest event."""
    try:
        if cursor is None:
            utils.result({"status": "success", "process": [], "reindex": {}, "delete": {}, "cleanup": {}, "collections": [],
                          "skipped": [], "deferred": 0, "cursor": cask_changes.start_cursor()})
        result = cask_changes.changes(json.loads(cursor), {"settle_seconds": settle_seconds, "limit": limit})
        reindex = sum(len(v) for v in result["reindex"].values())
        delete = sum(len(v) for v in result["delete"].values())
        cleanup = sum(len(v) for v in result["cleanup"].values())
        utils.log(
            f"{len(result['process'])} to process, {reindex} to reindex, {delete} to delete, "
            f"{cleanup} to clean up, "
            f"{len(result['collections'])} collection(s), {result['deferred']} deferred"
        )
        utils.result({"status": "success", **result})
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def pending(
    job_name: Annotated[str, typer.Option("--job-name", help="Job name, e.g. process_item_job.")],
    limit: Annotated[int, typer.Option(help="Most rows to return.")] = 20,
):
    """List the oldest queued rows for a job, without removing them (see ack)."""
    try:
        rows = collection_state.pending(job_name, limit)
        utils.result({"status": "success", "rows": rows})
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def ack(
    ids: Annotated[list[int], typer.Option("--id", help="Queue row id (repeatable).")],
):
    """Remove queued rows whose runs were submitted."""
    try:
        removed = collection_state.ack(ids)
        utils.result({"status": "success", "removed": removed})
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def result(
    job_name: Annotated[str, typer.Option("--job-name", help="Job name, e.g. process_item_job.")],
    result_json: Annotated[str, typer.Option("--result", help="The result, as a JSON object.")],
    item: Annotated[str | None, typer.Option(help="Item bronze folder path.")] = None,
    collection: Annotated[str | None, typer.Option(help="Collection key.")] = None,
    ark: Annotated[str | None, typer.Option(help="Item ARK, added to its identifiers.")] = None,
):
    """Record a job's latest result for one item or one collection (exactly one
    of --item / --collection). An item that isn't tracked yet is added."""
    try:
        data = json.loads(result_json)
        if not isinstance(data, dict):
            raise ValueError("--result must be a JSON object")
        row = collection_state.record_result(job_name, {
            "item": item, "collection": collection, "ark": ark, "result": data,
        })
        utils.result({"status": "success", "row": row})
    except json.JSONDecodeError as e:
        utils.error(f"--result isn't valid JSON: {e}")
    except _ERRORS as e:
        utils.error(str(e))


@app.command("item-ark")
def item_ark(
    item: Annotated[str, typer.Option(help="Item bronze folder path.")],
):
    """Print the ARK last recorded for an item folder (null if none)."""
    try:
        utils.result({"status": "success", "item": collection_state.item_name(item), "ark": collection_state.item_ark(item)})
    except _ERRORS as e:
        utils.error(str(e))


@app.command("rollups-due")
def rollups_due():
    """List collections whose items have results newer than their last rollup
    and nothing still queued."""
    try:
        utils.result({"status": "success", "collections": collection_state.rollups_due()})
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def rollup(
    collection_key: Annotated[str, typer.Option(
        "--collection-key", help="Collection key, e.g. bronze/preservica/haeger.",
    )],
):
    """Recompute a collection's item count and failed items, and record them as
    its collection_rollup_job result."""
    try:
        summary = collection_state.rollup(collection_key)
        utils.log(
            f"{summary['collection']}: {summary['item_count']} item(s), {summary['succeeded']} succeeded, "
            f"{summary['failed']} failed, {summary['never_run']} not run"
        )
        utils.result({"status": "success", **summary})
    except _ERRORS as e:
        utils.error(str(e))
