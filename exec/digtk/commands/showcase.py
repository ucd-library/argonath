"""
digtk showcase — commands specific to the public Digital Collections website
(as opposed to preservation-only processing).

Subcommands:
  image      — image derivative commands
  pdf        — PDF derivative commands
  video      — video derivative commands
  audio      — audio derivative commands
  es         — index items into the showcase website's Elasticsearch index
  formats    — report which media-type formats an item's bronze binaries span
  finalize   — write an item's item-level outputs after one-file-at-a-time derivatives
  build-item — assemble and write the item's complete, canonical gold item.jsonld.json
  build-collection — write the collection's gold collection.jsonld.json
  clean-item — delete a removed bronze item's silver and gold directories
  init-db    — apply dc-showcase's init SQL to its database
  init-cask  — seed dc-showcase's gold files into CaskFS
"""
from typing import Annotated, List

import psycopg2
import typer

from digtk.commands import audio, es, image, pdf, video
from digtk.lib import items, reporting, utils
from digtk.lib.config import (
    CONFIG, ReportingCollectionOption, ReportingJobIdOption, ShowcaseGoldProjectOption, TargetArgument,
)
from digtk.models.digital_website import finalize as finalize_model
from digtk.models.digital_website import collection_index, collection_record, item_record
from digtk.models.digital_website import pipeline, showcase_cask, showcase_db

app = typer.Typer(help="Public Digital Collections website commands.", no_args_is_help=True)

app.add_typer(image.app, name="image")
app.add_typer(pdf.app,   name="pdf")
app.add_typer(video.app, name="video")
app.add_typer(audio.app, name="audio")
app.add_typer(es.app,    name="es")

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


@app.command()
def formats(
    target: TargetArgument,
    files: Annotated[bool, typer.Option(
        "--files", help="Also list the item's binaries per format (full bronze paths), and "
                        "the item's own bronze directory.",
    )] = False,
):
    """Report which media-type formats (image/pdf/video/audio) an item's bronze
    binaries span, without generating anything — used by
    dagster/lib/pipeline_fanout.py and dagster/lib/pipeline_item.py to decide
    which `digtk showcase <format> derivatives` steps apply to an item (with
    --files, one step per image/PDF). `target` may be an ARK or a bronze item
    folder; the item is reported by its path, never its ARK."""
    try:
        item = items.resolve_target(target)
        item_dir = f"{items.BRONZE_PREFIX}{item.group}/{item.collection}/{item.time}/{item.item_path}".rstrip("/")
        found = sorted(pipeline.detect_formats(item.bronze_binary_paths))
        result = {"item": items.item_id(item), "formats": found}
        if files:
            result["files"] = {
                fmt: pipeline.filter_binaries_by_format(item.bronze_binary_paths, fmt) for fmt in found
            }
            result["dir"] = f"cask:{item_dir}"
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def finalize(
    target: TargetArgument,
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Write an item's item-level outputs (an image item's ocr/manifest.json)
    from its already-published per-file derivatives — run after `digtk showcase image|pdf derivatives --file ...`
    has run for every one of the item's images/PDFs. Fails without writing
    anything if any file's derivatives are missing. `target` may be an ARK or
    a bronze item folder."""
    try:
        item = items.resolve_target(target)
        label = items.item_id(item)
        utils.log(f"Finalizing derivatives for {label}")
        with reporting.track(reporting_job_id, stage="finalize", item_id=label, collection=reporting_collection):
            result = finalize_model.finalize(item)
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))


@app.command("build-item")
def build_item(
    target: TargetArgument,
    project: ShowcaseGoldProjectOption = CONFIG["showcase"]["gold_project"],
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Assemble and promote the item's complete, canonical item.jsonld.json to
    gold: identifiers, whatever descriptive metadata bronze already resolved,
    and a freshly rebuilt associatedMedia/hasPart structure read from silver's
    published derivatives and bronze's preservation originals. Run after
    `digtk showcase finalize` (and any video/audio derivatives()) have
    published this item's media. Unlike every other showcase step, this one
    requires the item to have an ARK and fails if it doesn't — the canonical
    public record needs a stable identifier. `target` may be an ARK or a
    bronze item folder."""
    try:
        item = items.resolve_target(target)
        label = items.item_id(item)
        utils.log(f"Building item record for {label} (project={project})")
        with reporting.track(reporting_job_id, stage="build-item", item_id=label, collection=reporting_collection):
            result = item_record.build(item, opts={"project": project})
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))


@app.command("build-collection")
def build_collection(
    key: Annotated[str, typer.Argument(help="Collection key: [bronze/]<group>/<collection>.")],
    project: ShowcaseGoldProjectOption = CONFIG["showcase"]["gold_project"],
):
    """Build the collection's gold collection.jsonld.json from its bronze
    tree root metadata and identifiers. Fails if the collection has no ARK."""
    try:
        group, name = collection_record.parse_key(key)
        utils.log(f"Building collection record for {group}/{name} (project={project})")
        utils.result(collection_record.build(name, group, opts={"project": project}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command("clean-item")
def clean_item(
    folders: Annotated[List[str], typer.Argument(help="Bronze item folder(s) no longer in bronze.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="List what would be removed.")] = False,
):
    """Delete the silver directory and every gold project's directory of items
    removed from bronze. A folder still in bronze is skipped."""
    try:
        result = collection_index.remove_item_outputs(folders, {"dry_run": dry_run})
        files = sum(r["files"] for r in result["removed"])
        utils.log(f"{'Would remove' if dry_run else 'Removed'} {len(result['removed'])} director(ies), {files} file(s)")
        utils.result({"status": "success", **result})
    except _ERRORS as e:
        utils.error(str(e))


@app.command("init-db")
def init_db(
    init_dir: Annotated[str | None, typer.Option(
        "--init-dir", help="Directory of dc-showcase's init .sql files (default: showcase_db.init_dir).",
    )] = None,
):
    """Apply dc-showcase's init SQL to its database, in filename order. The
    SQL is dc-showcase's own, mounted from its repo. Safe to run repeatedly."""
    try:
        files = showcase_db.init_db({"init_dir": init_dir})
        utils.log(f"Applied {len(files)} dc-showcase init file(s)")
        utils.result({"status": "success", "files": files})
    except (*_ERRORS, psycopg2.Error) as e:
        utils.error(str(e))


@app.command("init-cask")
def init_cask(
    init_dir: Annotated[str | None, typer.Option(
        "--init-dir", help="Directory of dc-showcase's gold seed files (default: showcase_cask.init_dir).",
    )] = None,
    project: ShowcaseGoldProjectOption = CONFIG["showcase"]["gold_project"],
):
    """Copy dc-showcase's gold seed files (hero banner and browse by images)
    into the gold project. Existing files are skipped, so it's safe to run
    repeatedly."""
    try:
        result = showcase_cask.init_cask({"init_dir": init_dir, "project": project})
        utils.log(f"Added {len(result['added'])} file(s), skipped {len(result['skipped'])} existing (project={project})")
        utils.result({"status": "success", **result})
    except _ERRORS as e:
        utils.error(str(e))
