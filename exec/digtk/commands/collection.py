"""
digtk collection — manage CaskFS collections.

Subcommands:
  add / sync — upload a local file or directory into a bronze or workspace collection
               (aliases for the same command — see digtk.models.collection). A
               bronze add always registers the collection (see
               digtk.models.collection.register_collection).
  add-tree   — materialize a local directory into a bronze tree/ layout only, for
               local dev/testing without a source system (see
               digtk.models.collection.local_to_bronze_tree). Source-agnostic —
               unlike `digtk preservica import`/`tree`, --group has no default here.
  list       — list registered collections (see digtk.models.collection.register_collection) —
               used by Dagster's collection_partition_sync_sensor to keep its
               'collections' dynamic partition in sync with the registry.
  init-pg    — apply the collection registry's Postgres schema (see
               digtk.models.collection.init_pg / collection_registry.sql). Safe
               to run repeatedly.
"""
import subprocess
from enum import Enum
from typing import Annotated

import psycopg2
import typer

from digtk.lib import db, utils
from digtk.models import collection as collection_model

app = typer.Typer(help="Manage CaskFS collections.", no_args_is_help=True)


class Root(str, Enum):
    """Destination root for a new collection — the only two roots collections may be added under."""
    bronze = "bronze"
    workspace = "workspace"


@app.command("add")
@app.command("sync")
def add(
    source: Annotated[str, typer.Argument(help="Local file or directory to upload.")],
    root: Annotated[Root, typer.Option(help="Destination root.")],
    collection: Annotated[str, typer.Option(help="Collection name.")],
    group: Annotated[str | None, typer.Option(
        help="Group namespace. Required when --root=bronze.",
    )] = None,
    time: Annotated[str | None, typer.Option(
        help="Bronze time-folder name, or the CURRENT_TIME/CURRENT_DATE sentinels. "
        "Defaults to 'latest'. Bronze only.",
    )] = None,
    username: Annotated[str | None, typer.Option(
        help="Workspace username. Defaults to the current OS user. Workspace only.",
    )] = None,
    env: Annotated[str | None, typer.Option(
        help="cask CLI environment to target, passed as `cask cp -e`.",
    )] = None,
    replace: Annotated[bool, typer.Option(help="Replace files that already exist.")] = False,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Show what would be copied without copying.",
    )] = False,
    yes: Annotated[bool, typer.Option(
        "--yes", "-y", help="Skip cask cp's confirmation prompt (default: prompt).",
    )] = False,
):
    """Upload a local file or directory into a bronze or workspace collection via `cask cp`."""
    opts = {
        "group": group,
        "time": time,
        "username": username,
        "env": env,
        "replace": replace,
        "dry_run": dry_run,
        "yes": yes,
    }

    try:
        dest = collection_model.dest_path(root.value, collection, opts)
        utils.log(f"Copying {source} -> cask:{dest}")
        collection_model.add(source, root.value, collection, opts)
    except (ValueError, FileNotFoundError) as e:
        utils.error(str(e))
    except subprocess.CalledProcessError as e:
        utils.error(f"cask cp exited with status {e.returncode}")

    utils.result({"status": "success", "source": source, "dest": f"cask:{dest}"})


@app.command("add-tree")
def add_tree(
    source: Annotated[str, typer.Argument(
        help="Local directory to materialize — its immediate subdirectories become tree/ items.",
    )],
    collection: Annotated[str, typer.Option(help="Collection name.")],
    group: Annotated[str, typer.Option(
        help="Bronze group namespace. Required — this command is source-agnostic, so "
        "there's no default to assume (unlike `digtk preservica import`/`tree`).",
    )],
    dest_root: Annotated[str | None, typer.Option(
        "--dest-root", help="Override the bronze root (e.g. a local directory for dev/"
        "testing instead of real CaskFS). Defaults to cask:/bronze/<group>/<collection>.",
    )] = None,
):
    """Materialize a local directory into a bronze tree/ layout only (debug/dev helper —
    no by-ref/, since there's no source-system ref to key it by; see
    digtk.models.collection.local_to_bronze_tree)."""
    opts = {"dest_root": dest_root}
    try:
        count = collection_model.local_to_bronze_tree(source, collection, group, opts)
    except FileNotFoundError as e:
        utils.error(str(e))

    utils.result({"status": "success", "source": source, "collection": collection, "items": count})


class SourceType(str, Enum):
    """Registry source_type filter for `collection list`."""
    manual = "manual"
    preservica = "preservica"


@app.command("list")
def list_registered(
    source_type: Annotated[SourceType | None, typer.Option(
        "--source-type", help="Filter to only 'manual' (disk) or 'preservica' collections.",
    )] = None,
):
    """List every active registered collection — see
    digtk.models.collection.register_collection()/list_active_collections()."""
    try:
        collections = collection_model.list_active_collections(source_type.value if source_type else None)
    except ValueError as e:
        utils.error(str(e))

    for c in collections:
        tags = ",".join(c["workflow_tags"]) or "(none)"
        utils.log(f"{c['group_name']}/{c['name']}  [{c['source_type']}]  tags={tags}")
    if not collections:
        utils.log("(no registered collections)")

    utils.result({"status": "success", "collections": collections})


@app.command("init-pg")
def init_pg():
    """Apply the collection registry's Postgres schema (creates the 'argonath'
    schema and its collection_registry/collection_identifiers tables if they
    don't already exist — see digtk.models.collection.init_pg()). Safe to run
    repeatedly."""
    try:
        collection_model.init_pg()
    except ValueError as e:
        utils.error(str(e))
    except psycopg2.OperationalError as e:
        utils.error(f"Could not connect to Postgres: {e}")

    utils.log(f"Applied collection registry schema to Postgres schema '{db.SCHEMA}'.")
    utils.result({"status": "success", "schema": db.SCHEMA})
