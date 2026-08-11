"""
digtk video — video derivative commands.

Subcommands:
  probe        — report ffprobe metadata (duration, codecs, dimensions)
  hls          — generate the HLS adaptive-bitrate stream
  poster       — extract a poster-frame JPEG
  derivatives  — generate the full derivative set (HLS stream + poster + a WebVTT
                 caption via local Whisper ASR); the others above are deterministic
                 single-recipe commands for ad-hoc/manual use.
"""
from typing import Annotated

import typer

from digtk.lib import utils
from digtk.lib.config import (
    CONFIG,
    CaptionsLanguageOption,
    CaptionsModelOption,
    VideoPosterTimestampOption,
    VideoPosterWidthOption,
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
def poster(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    timestamp: VideoPosterTimestampOption = CONFIG["video"]["poster_timestamp_s"],
    width: VideoPosterWidthOption = CONFIG["video"]["poster_width"],
):
    """Extract a poster-frame JPEG."""
    try:
        utils.log(f"Extracting poster for {ark} at {timestamp}s")
        utils.result(video.poster(ark, opts={"timestamp_s": timestamp, "width": width}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def derivatives(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    resolutions: VideoResolutionsOption = CONFIG["video"]["resolutions"],
    width: VideoPosterWidthOption = CONFIG["video"]["poster_width"],
    language: CaptionsLanguageOption = CONFIG["captions"]["language"],
    model: CaptionsModelOption = CONFIG["captions"]["model"],
):
    """Generate the full derivative set: HLS stream + poster frame + WebVTT caption
    (local Whisper ASR — machine-generated and unreviewed)."""
    try:
        utils.log(f"Generating derivatives for {ark} at resolutions {resolutions}")
        result = video.derivatives(ark, opts={
            "resolutions": resolutions, "width": width, "language": language, "model": model,
        })
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))
