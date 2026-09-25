"""
digtk showcase video — video derivative commands.

Subcommands:
  probe        — report ffprobe metadata (duration, codecs, dimensions)
  hls          — generate the HLS adaptive-bitrate stream
  derivatives  — generate the full derivative set (HLS stream + a WebVTT caption via
                 local Whisper ASR); `hls` above is a deterministic single-recipe
                 command for ad-hoc/manual use. A video item's thumbnail, if any,
                 comes from a companion image bronze binary processed independently
                 by `digtk showcase image derivatives` — not generated here.
"""
from typing import Annotated

import typer

from digtk.lib import reporting, utils
from digtk.lib.config import (
    CONFIG,
    CaptionsLanguageOption,
    CaptionsModelOption,
    ReportingCollectionOption,
    ReportingJobIdOption,
    VideoResolutionsOption,
)
from digtk.models.digital_website import video

app = typer.Typer(help="Video derivative commands.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


@app.command()
def probe(ark: Annotated[str, typer.Argument(help="Item ARK identifier.")]):
    """Report ffprobe metadata for every source video."""
    try:
        utils.result(video.probe(ark))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def hls(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    resolutions: VideoResolutionsOption = CONFIG["video"]["resolutions"],
):
    """Generate the HLS adaptive-bitrate stream."""
    try:
        utils.log(f"Generating HLS stream for {ark} at resolutions {resolutions}")
        utils.result(video.hls(ark, opts={"resolutions": resolutions}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def derivatives(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    resolutions: VideoResolutionsOption = CONFIG["video"]["resolutions"],
    language: CaptionsLanguageOption = CONFIG["captions"]["language"],
    model: CaptionsModelOption = CONFIG["captions"]["model"],
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Generate the full derivative set: HLS stream + WebVTT caption (local Whisper
    ASR — machine-generated and unreviewed). One of the commands the
    collection-level fan-out job (dagster/lib/pipeline_fanout.py) calls for a
    video-bearing item — passing its own run id as --reporting-job-id, same as
    image derivatives."""
    try:
        utils.log(f"Generating derivatives for {ark} at resolutions {resolutions}")
        with reporting.track(
            reporting_job_id, stage="derivatives", item_id=ark, collection=reporting_collection,
            options={"resolutions": resolutions, "language": language, "model": model},
        ):
            result = video.derivatives(ark, opts={
                "resolutions": resolutions, "language": language, "model": model,
            })
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))
