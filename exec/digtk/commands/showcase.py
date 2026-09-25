"""
digtk showcase — commands specific to the public Digital Collections website
(as opposed to preservation-only processing).

Subcommands:
  image    — image derivative commands
  pdf      — PDF derivative commands
  video    — video derivative commands
  audio    — audio derivative commands
  gcs      — sync derivatives into the legacy DAMS/fin GCS bucket layout
  es       — index items into the DAMS website's Elasticsearch index
  formats  — report which media-type formats an item's bronze binaries span
"""
from typing import Annotated

import typer

from digtk.commands import audio, es, gcs, image, pdf, video
from digtk.lib import items, utils
from digtk.models.digital_website import pipeline

app = typer.Typer(help="Public Digital Collections website commands.", no_args_is_help=True)

app.add_typer(image.app, name="image")
app.add_typer(pdf.app,   name="pdf")
app.add_typer(video.app, name="video")
app.add_typer(audio.app, name="audio")
app.add_typer(gcs.app,   name="gcs")
app.add_typer(es.app,    name="es")

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


@app.command()
def formats(ark: Annotated[str, typer.Argument(help="Item ARK identifier.")]):
    """Report which media-type formats (image/pdf/video/audio) an item's bronze
    binaries span, without generating anything — used by
    dagster/lib/pipeline_fanout.py's process_derivatives to decide which
    `digtk showcase <format> derivatives` commands apply to a given item."""
    try:
        item = items.resolve(ark)
        utils.result({"ark": ark, "formats": sorted(pipeline.detect_formats(item.bronze_binary_paths))})
    except _ERRORS as e:
        utils.error(str(e))
