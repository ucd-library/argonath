"""
digtk showcase — commands specific to the public Digital Collections website
(as opposed to preservation-only processing).

Subcommands:
  image    — image derivative commands
  pdf      — PDF derivative commands
  video    — video derivative commands
  audio    — audio derivative commands
  es       — index items into the DAMS website's Elasticsearch index
  formats  — report which media-type formats an item's bronze binaries span
  finalize — write an item's item-level outputs after one-file-at-a-time derivatives
"""
from typing import Annotated

import typer

from digtk.commands import audio, es, image, pdf, video
from digtk.lib import items, reporting, utils
from digtk.lib.config import ReportingCollectionOption, ReportingJobIdOption, TargetArgument
from digtk.models.digital_website import finalize as finalize_model
from digtk.models.digital_website import pipeline

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
    folder; the result's "ark" is None if the item has no ARK (not an error)."""
    try:
        item = items.resolve_target(target)
        item_dir = f"{items.BRONZE_PREFIX}{item.group}/{item.collection}/{item.time}/{item.item_path}".rstrip("/")
        found = sorted(pipeline.detect_formats(item.bronze_binary_paths))
        result = {"ark": item.ark, "formats": found}
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
    """Write an item's item-level outputs (an image item's ocr/manifest.json; the
    item sidecar's associatedMedia) from its already-published per-file
    derivatives — run after `digtk showcase image|pdf derivatives --file ...`
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
