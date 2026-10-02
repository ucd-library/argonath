"""
digtk accessibility — accessibility metadata commands.

Subcommands:
  alt-text  — generate AI alt text for an item's image(s) via Samwise vision
  tag-pdf   — assemble a searchable, PDF/UA-1-tagged PDF for an item's PDF binary(ies)
"""
import subprocess
from typing import Annotated

import typer

from digtk.lib import utils
from digtk.lib.config import (
    CONFIG,
    AltMaxCharsOption,
    AltWantDescriptionOption,
    ReportingCollectionOption,
    ReportingJobIdOption,
    TargetArgument,
    WorkspaceCollectionOption,
    WorkspaceUserOption,
)
from digtk.models import accessibility, workspace

app = typer.Typer(help="Accessibility metadata commands.", no_args_is_help=True)

# CalledProcessError: `cask cp` failing while staging a local folder target.
_ERRORS = (LookupError, OSError, RuntimeError, ValueError, subprocess.CalledProcessError)


@app.command(name="alt-text")
def alt_text(
    target: TargetArgument,
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
    collection: WorkspaceCollectionOption = None,
    user: WorkspaceUserOption = None,
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Generate ADA/WCAG alt text for every image binary, and every page of
    every PDF binary, in an item.

    Builds its own resized image per image/PDF page from the source binary
    — no `digtk showcase` derivatives needed. Writes
    .../accessibility/*-alt-text.json per image, or
    .../accessibility/<pdf-filename>/<page>/alt-text.json per PDF page, with
    CaskFS lineage to the source binary (and the bronze sidecar, for an ARK)
    — in silver for an ARK, or the workspace collection's derivatives/ for a
    workspace path or local folder.
    """
    try:
        utils.log(f"Generating alt text for {target}")
        utils.result(workspace.apply_to_targets(target, accessibility.generate_alt_text, opts={
            "stage": "alt-text",
            "fn_opts": {
                "collection_context": collection_context,
                "name_people": name_people,
                "transcribe_text": transcribe_text,
                "want_description": want_description,
                "max_alt_chars": max_alt_chars,
            },
            "collection": collection, "username": user,
            "reporting_job_id": reporting_job_id, "reporting_collection": reporting_collection,
        }))
    except _ERRORS as e:
        utils.error(str(e))


@app.command(name="tag-pdf")
def tag_pdf(
    target: TargetArgument,
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
    min_conf: Annotated[float, typer.Option(
        "--min-conf",
        help="Drop hOCR words below this Tesseract confidence (0-100) from "
             "the searchable-PDF text layer, instead of baking OCR noise "
             "(e.g. over handwriting/marginalia) into it. Default: no filtering.",
    )] = CONFIG["accessibility"]["min_word_confidence"],
    collection: WorkspaceCollectionOption = None,
    user: WorkspaceUserOption = None,
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Assemble a searchable, PDF/UA-1-tagged PDF for every PDF binary in an item.

    Rasterizes each page and builds its own OCR-prep image from the source
    PDF — no `digtk showcase` derivatives needed — then runs Tesseract for
    word geometry (--min-conf drops words below that confidence from the
    resulting searchable text layer). Alt text (from `digtk accessibility
    alt-text`) and, with --tables, a vision-generated layout are read back
    and used if present, but neither is required. Writes
    .../accessibility/<pdf-filename>/tagged.pdf, with CaskFS lineage to the
    source PDF and any alt-text/layout derivatives read — in silver for an
    ARK, or the workspace collection's derivatives/ for a workspace path or
    local folder.
    """
    try:
        utils.log(f"Tagging PDF(s) for {target}")
        utils.result(workspace.apply_to_targets(target, accessibility.tag_pdf, opts={
            "stage": "tag-pdf",
            "fn_opts": {
                "lang": lang,
                "strict": strict,
                "lookahead": lookahead,
                "tables": tables,
                "validate": validate,
                "min_conf": min_conf,
            },
            "collection": collection, "username": user,
            "reporting_job_id": reporting_job_id, "reporting_collection": reporting_collection,
        }))
    except _ERRORS as e:
        utils.error(str(e))
