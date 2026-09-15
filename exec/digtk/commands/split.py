"""
digtk split-spreads — detect the binding gutter in a scanned two-page spread,
then cut confirmed spreads into separate left/right page images.

Top-level, not nested under `showcase`: unlike showcase's image/pdf/video/
audio commands, this isn't producing an asset for the public website — it's
a corrective, format-agnostic-in-spirit step on an item's own image binaries
(see digtk.models.split), same reasoning that keeps `accessibility`/`ocr`
top-level rather than nested under showcase image/pdf.

Subcommands:
  detect — detect the binding gutter in every not-yet-detected image binary of an item
  apply  — cut every confirmed spread per an item's (human-reviewed) gutters.json
"""
from typing import Annotated

import typer

from digtk.lib import utils
from digtk.lib.config import CONFIG, SplitMaxPxOption
from digtk.models import split as split_model

app = typer.Typer(help="Detect and cut scanned two-page spreads.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


@app.command()
def detect(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    max_px: SplitMaxPxOption = CONFIG["split"]["max_px"],
):
    """Detect the binding gutter in every not-yet-detected image binary of this item.

    Publishes a review strip and contact sheet(s) as silver derivatives of
    this item for a human to check before running `apply`. Resumable — never
    re-detects or overwrites an entry a human has already reviewed/corrected
    in gutters.json.
    """
    try:
        utils.log(f"Detecting spreads for {ark}")
        utils.result(split_model.detect(ark, opts={"max_px": max_px}))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def apply(ark: Annotated[str, typer.Argument(help="Item ARK identifier.")]):
    """Cut every confirmed spread per this item's (human-reviewed) gutters.json.

    Requires `detect` to have already run — reads gutters.json back rather
    than re-detecting anything. Publishes cut pages as silver derivatives of
    this item (or the original image unchanged, for non-spreads) — not new
    bronze items.
    """
    try:
        utils.log(f"Applying spread splits for {ark}")
        utils.result(split_model.apply(ark))
    except _ERRORS as e:
        utils.error(str(e))
