"""
digtk public-dc video — video derivative commands.

Subcommands:
  probe        — report ffprobe metadata (duration, codecs, dimensions)
  hls          — generate the HLS adaptive-bitrate stream
  derivatives  — generate the full derivative set (HLS stream + a WebVTT caption via
                 local Whisper ASR); `hls` above is a deterministic single-recipe
                 command for ad-hoc/manual use. A video item's thumbnail, if any,
                 comes from a companion image bronze binary processed independently
                 by `digtk public-dc image derivatives` — not generated here.
"""
from typing import Annotated

import typer

from digtk.lib import utils
from digtk.lib.config import (
    CONFIG,
    CaptionsLanguageOption,
    CaptionsModelOption,
    GoldAppOption,
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
    app: GoldAppOption = CONFIG["gold"]["app"],
):
    """Generate the HLS adaptive-bitrate stream."""
    try:
        utils.log(f"Generating HLS stream for {ark} at resolutions {resolutions}")
        utils.result(video.hls(ark, opts={"resolutions": resolutions, "app": app}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def derivatives(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    resolutions: VideoResolutionsOption = CONFIG["video"]["resolutions"],
    language: CaptionsLanguageOption = CONFIG["captions"]["language"],
    model: CaptionsModelOption = CONFIG["captions"]["model"],
    app: GoldAppOption = CONFIG["gold"]["app"],
):
    """Generate the full derivative set: HLS stream + WebVTT caption (local Whisper
    ASR — machine-generated and unreviewed)."""
    try:
        utils.log(f"Generating derivatives for {ark} at resolutions {resolutions}")
        result = video.derivatives(ark, opts={
            "resolutions": resolutions, "language": language, "model": model, "app": app,
        })
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))
