"""
dc ingest — ingest commands.

All subcommands write into bronze/[group]/[collection]/[time]/[item] and print
a last-line JSON result of the form {"added": [...], "removed": [...], "changed": [...]}
(ARK lists), diffed against the collection's previous bronze state — see
docs/cask-conventions.md for the reconciliation contract each subcommand fulfills.

Subcommands:
  file     — ingest a single file into a CaskFS collection (digital-collection source)
  batch    — ingest all files in a directory into a CaskFS collection (digital-collection source)
  crawl    — crawl a website and ingest its pages (website source)
  db-dump  — ingest rows from a database dump (db-dump source)
"""
from typing import Annotated

import typer

app = typer.Typer(help="Ingest files into CaskFS collections.", no_args_is_help=True)


@app.command()
def file(
    path: Annotated[str, typer.Argument(help="Path to the source file.")],
    collection: Annotated[str, typer.Option(help="Collection name.")],
    group: Annotated[str, typer.Option(help="Group namespace the collection belongs to.")],
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Ingest a single file, mint an ARK, write the initial schema.org sidecar to bronze."""
    raise NotImplementedError("phase 0.3")


@app.command()
def batch(
    directory: Annotated[str, typer.Argument(help="Directory of source files to ingest.")],
    collection: Annotated[str, typer.Option(help="Collection name.")],
    group: Annotated[str, typer.Option(help="Group namespace the collection belongs to.")],
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Ingest all files in a directory as a batch. Digital collections are additive-only —
    items missing from a re-run are never treated as removed."""
    raise NotImplementedError("phase 0.3")


@app.command()
def crawl(
    url: Annotated[str, typer.Argument(help="Root URL to crawl.")],
    collection: Annotated[str, typer.Option(help="Collection name.")],
    group: Annotated[str, typer.Option(help="Group namespace the collection belongs to.")],
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Crawl a website, writing each page as a bronze item under a new time-dated snapshot.
    Pages no longer found are reported as removed for downstream pruning."""
    raise NotImplementedError("phase 0.3")


@app.command(name="db-dump")
def db_dump(
    connection: Annotated[str, typer.Argument(help="Source database connection string.")],
    collection: Annotated[str, typer.Option(help="Collection name.")],
    group: Annotated[str, typer.Option(help="Group namespace the collection belongs to.")],
    local_root: Annotated[str | None, typer.Option("--local-root")] = None,
    cask_root:  Annotated[str | None, typer.Option("--cask-root")]  = None,
):
    """Ingest rows from a database dump, writing each row as a bronze item under a new
    year-week-dated snapshot. Rows no longer present are reported as removed for downstream pruning."""
    raise NotImplementedError("phase 0.3")
