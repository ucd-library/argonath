"""
digtk public-dc audio — audio derivative commands.

Subcommands:
  probe        — report ffprobe metadata (duration, codec, bitrate)
  transcode    — convert source to another audio format
  waveform     — generate a waveform image
  derivatives  — generate the full derivative set (transcode + waveform + a WebVTT
                 caption via local Whisper ASR); the others above are deterministic
                 single-recipe commands for ad-hoc/manual use.
"""
from typing import Annotated

import typer

from digtk.lib import utils
from digtk.lib.config import (
    CONFIG,
    AudioTranscodeBitrateOption,
    AudioTranscodeFormatOption,
    AudioWaveformHeightOption,
    AudioWaveformWidthOption,
    CaptionsLanguageOption,
    CaptionsModelOption,
)
from digtk.models.digital_website import audio

app = typer.Typer(help="Audio derivative commands.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


@app.command()
def probe(ark: Annotated[str, typer.Argument(help="Item ARK identifier.")]):
    """Report ffprobe metadata for every source audio file."""
    try:
        utils.result(audio.probe(ark))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def transcode(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    format: AudioTranscodeFormatOption = CONFIG["audio"]["transcode_format"],
    bitrate: AudioTranscodeBitrateOption = CONFIG["audio"]["transcode_bitrate"],
):
    """Convert source audio to the specified format."""
    try:
        utils.log(f"Transcoding {ark} to {format} @ {bitrate}")
        utils.result(audio.transcode(ark, opts={"format": format, "bitrate": bitrate}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def waveform(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    width: AudioWaveformWidthOption = CONFIG["audio"]["waveform_width"],
    height: AudioWaveformHeightOption = CONFIG["audio"]["waveform_height"],
):
    """Generate a waveform image."""
    try:
        utils.log(f"Generating waveform for {ark} ({width}x{height})")
        utils.result(audio.waveform(ark, opts={"width": width, "height": height}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def derivatives(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    format: AudioTranscodeFormatOption = CONFIG["audio"]["transcode_format"],
    language: CaptionsLanguageOption = CONFIG["captions"]["language"],
    model: CaptionsModelOption = CONFIG["captions"]["model"],
):
    """Generate the full derivative set: transcode + waveform + WebVTT caption
    (local Whisper ASR — machine-generated and unreviewed)."""
    try:
        utils.log(f"Generating derivatives for {ark}")
        result = audio.derivatives(ark, opts={"format": format, "language": language, "model": model})
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))
