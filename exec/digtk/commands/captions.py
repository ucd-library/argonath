"""
digtk captions — caption/transcript generation for video and audio items.

Subcommands:
  transcribe   — transcribe every source video/audio file to a WebVTT caption file
                 via local Whisper ASR. Machine-generated and unreviewed — see
                 digtk.models.digital_website.captions.
"""
from typing import Annotated

import typer

from digtk.lib import utils
from digtk.lib.config import CONFIG, CaptionsLanguageOption, CaptionsModelOption
from digtk.models.digital_website import captions

app = typer.Typer(help="Caption/transcript generation commands.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


@app.command()
def transcribe(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    language: CaptionsLanguageOption = CONFIG["captions"]["language"],
    model: CaptionsModelOption = CONFIG["captions"]["model"],
):
    """Transcribe every source video/audio file to a WebVTT caption file."""
    try:
        utils.log(f"Transcribing {ark} (language={language}, model={model})")
        utils.result(captions.transcribe(ark, opts={"language": language, "model": model}))
    except _ERRORS as e:
        utils.error(str(e))
