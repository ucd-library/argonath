"""
digtk accessibility — accessibility metadata commands.

Subcommands:
  alt-text  — generate AI alt text for an item's image(s) via Samwise vision
  tag-pdf   — assemble a searchable, PDF/UA-1-tagged PDF for an item's PDF binary(ies)
"""
from typing import Annotated

import typer

from digtk.lib import utils
from digtk.lib.config import CONFIG, AltMaxCharsOption, AltWantDescriptionOption
from digtk.models import accessibility

app = typer.Typer(help="Accessibility metadata commands.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


@app.command(name="alt-text")
def alt_text(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    collection_context: Annotated[str, typer.Option(
        help="Collection-specific context for the prompt (e.g. what this collection is).",
    )] = "",
    name_people: Annotated[bool, typer.Option(
        help="Name people the metadata identifies as clearly the subject.",
    )] = True,
    transcribe_text: Annotated[bool, typer.Option(
        help="Quote short, significant, legible text visible in the image.",
    )] = True,
    want_description: AltWantDescriptionOption = CONFIG["alt"]["want_description"],
    max_alt_chars: AltMaxCharsOption = CONFIG["alt"]["max_chars"],
):
    """Generate ADA/WCAG alt text for every image binary, and every page of
    every PDF binary, in an item.

    Requires `digtk public-dc image derivatives` (image binaries) and/or
    `digtk public-dc pdf derivatives` (PDF binaries) to have already run for
    this item — reads back their published "large" display JPEGs rather than
    regenerating/rasterizing anything itself. Writes
    silver/.../accessibility/*-alt-text.json per image, or
    silver/.../accessibility/<pdf-filename>/<page>/alt-text.json per PDF
    page, with CaskFS lineage to the display image and the bronze sidecar.
    """
    try:
        utils.log(f"Generating alt text for {ark}")
        utils.result(accessibility.generate_alt_text(ark, opts={
            "collection_context": collection_context,
            "name_people": name_people,
            "transcribe_text": transcribe_text,
            "want_description": want_description,
            "max_alt_chars": max_alt_chars,
        }))
    except _ERRORS as e:
        utils.error(str(e))


@app.command(name="tag-pdf")
def tag_pdf(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    lang: Annotated[str, typer.Option(help="Document language for tagging (BCP-47, e.g. en-US).")] = "en-US",
    strict: Annotated[bool, typer.Option(
        help="Demote lines the layout doesn't account for (scanner junk) to "
             "untagged artifacts instead of paragraphs.",
    )] = False,
    lookahead: Annotated[int, typer.Option(
        help="How far a layout block may match past the tagger's cursor; raise "
             "for pages whose table sits below more than this many lines of prose.",
    )] = CONFIG["accessibility"]["tag_lookahead"],
    tables: Annotated[bool, typer.Option(
        help="Vision layout pass per page: tag data tables as Table/TR/TD "
             "(one extra Samwise call per page, cached; blocks that don't "
             "align fall back to paragraphs).",
    )] = False,
    validate: Annotated[bool, typer.Option(
        help="Run veraPDF on the tagged PDF and include the compliance result.",
    )] = True,
):
    """Assemble a searchable, PDF/UA-1-tagged PDF for every PDF binary in an item.

    Requires `digtk public-dc pdf derivatives` to have already run for this
    item — reads back its published per-page OCR-prep images rather than
    rasterizing anything itself, then re-runs Tesseract against them for word
    geometry. Alt text (from `digtk accessibility alt-text`) and, with
    --tables, a vision-generated layout are read back and used if present,
    but neither is required. Writes
    silver/.../accessibility/<pdf-filename>/tagged.pdf, with CaskFS lineage
    to the bronze PDF, the OCR-prep images used, and any alt-text/layout
    derivatives read.
    """
    try:
        utils.log(f"Tagging PDF(s) for {ark}")
        utils.result(accessibility.tag_pdf(ark, opts={
            "lang": lang,
            "strict": strict,
            "lookahead": lookahead,
            "tables": tables,
            "validate": validate,
        }))
    except _ERRORS as e:
        utils.error(str(e))
