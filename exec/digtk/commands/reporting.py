"""
digtk reporting -- admin commands for the reporting Postgres schema that
digtk.lib.reporting.track() writes to.

Subcommands:
  init-db  -- create/upgrade the reporting schema (idempotent)
  clean    -- prune old reporting.command/.error rows
"""
from typing import Annotated

import typer

from digtk.lib import reporting, utils

app = typer.Typer(help="Reporting schema admin commands.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


@app.command("init-db")
def init_db():
    """Create the reporting schema/tables/indexes/view if they don't already
    exist. Safe to re-run -- every statement is idempotent."""
    try:
        utils.log("Ensuring reporting schema exists")
        reporting.ensure_schema()
    except _ERRORS as e:
        utils.error(str(e))

    utils.result({"status": "success"})


@app.command()
def clean(
    days: Annotated[int, typer.Option(help="Delete commands started more than this many days ago.")] = 30,
    yes: Annotated[bool, typer.Option(
        "--yes", "-y", help="Actually delete. Without this, only reports how many rows would be deleted.",
    )] = False,
):
    """Prune old reporting.command rows (and their cascaded reporting.error
    rows) so the schema doesn't grow unbounded. Dry run unless --yes is given."""
    try:
        utils.log(f"{'Deleting' if yes else 'Counting'} commands older than {days} day(s)")
        result = reporting.clean(days, opts={"yes": yes})
        utils.log(f"{'Deleted' if yes else 'Would delete'} {result['deleted']} command(s)")
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))
