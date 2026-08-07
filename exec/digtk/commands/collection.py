"""
digtk collection — manage CaskFS collections.

Subcommands:
  add / sync — upload a local file or directory into a bronze or workspace collection
               (aliases for the same command — see digtk.models.collection)
"""
import subprocess
from enum import Enum
from typing import Annotated

import typer

from digtk.lib import utils
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
