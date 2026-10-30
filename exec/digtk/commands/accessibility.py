"""
digtk accessibility — accessibility metadata commands.

Subcommands:
  alt-text  — generate AI alt text for an item's image(s) via Samwise vision
  tag-pdf   — assemble a searchable, PDF/UA-1-tagged PDF for an item's PDF binary(ies)
  build-pdf — build an item's page images into one searchable, tagged PDF
  figures   — detect and describe the figures on every page, for review
"""
import subprocess
from typing import Annotated

import typer

from digtk.lib import utils
from digtk.lib.config import (
    CONFIG,
    AltMaxCharsOption,
    AltWantDescriptionOption,
    AltWantLongOption,
    OcrEnhanceOption,
    EmbedFontsOption,
    FiguresOption,
    GeometryOption,
    SkipExistingOption,
    TagModeOption,
    ReportingCollectionOption,
    ReportingJobIdOption,
    TargetArgument,
    WorkspaceFolderOption,
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
    want_long: AltWantLongOption = CONFIG["alt"]["want_long"],
    max_alt_chars: AltMaxCharsOption = CONFIG["alt"]["max_chars"],
    folder: WorkspaceFolderOption = None,
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
    — in silver for an ARK, or the workspace folder's derivatives/ for a
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
                "want_long": want_long,
                "max_alt_chars": max_alt_chars,
            },
            "folder": folder, "username": user,
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
    paragraphs: Annotated[bool, typer.Option(
        help="Tag each layout text block as one paragraph instead of one per line (needs --tables).",
    )] = False,
    reuse_ocr: Annotated[bool, typer.Option(
        help="Use `digtk ocr tesseract`'s published word boxes when they were made with "
             "the same image settings, instead of running Tesseract again.",
    )] = True,
    correct: Annotated[bool, typer.Option(
        help="Fix the text layer's spelling from the vision transcript (see `digtk ocr "
             "correct`). Needs `digtk ocr tesseract` output with a transcript.",
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
    ocr_enhance: OcrEnhanceOption = None,
    skip_existing: SkipExistingOption = False,
    figures: FiguresOption = False,
    geometry: GeometryOption = "tesseract",
    mode: TagModeOption = "auto",
    embed_fonts: EmbedFontsOption = True,
    folder: WorkspaceFolderOption = None,
    user: WorkspaceUserOption = None,
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Assemble a searchable, PDF/UA-1-tagged PDF for every PDF binary in an item.

    Born-digital PDFs are tagged in place (see --mode); scans are rebuilt.
    Reuses `digtk ocr tesseract`'s word boxes when they match, else runs
    Tesseract itself. Alt text and, with --tables, a vision layout are used
    if present. Writes tagged.pdf, searchable.pdf and text.txt under
    .../accessibility/<pdf-filename>/ — in silver for an ARK, or the
    workspace folder's derivatives/.
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
                "paragraphs": paragraphs,
                "reuse_ocr": reuse_ocr,
                "correct": correct,
                "validate": validate,
                "min_conf": min_conf,
                "enhance": ocr_enhance,
                "skip_existing": skip_existing,
                "figures": figures,
                "geometry": geometry,
                "mode": mode,
                "embed_fonts": embed_fonts,
            },
            "folder": folder, "username": user,
            "reporting_job_id": reporting_job_id, "reporting_collection": reporting_collection,
        }))
    except _ERRORS as e:
        utils.error(str(e))


@app.command(name="build-pdf")
def build_pdf(
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
    paragraphs: Annotated[bool, typer.Option(
        help="Tag each layout text block as one paragraph instead of one per line (needs --tables).",
    )] = False,
    reuse_ocr: Annotated[bool, typer.Option(
        help="Use `digtk ocr tesseract`'s published word boxes when they were made with "
             "the same image settings, instead of running Tesseract again.",
    )] = True,
    correct: Annotated[bool, typer.Option(
        help="Fix the text layer's spelling from the vision transcript (see `digtk ocr "
             "correct`). Needs `digtk ocr tesseract` output with a transcript.",
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
    ocr_enhance: OcrEnhanceOption = None,
    skip_existing: SkipExistingOption = False,
    figures: FiguresOption = False,
    geometry: GeometryOption = "tesseract",
    folder: WorkspaceFolderOption = None,
    user: WorkspaceUserOption = None,
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Build an item's page images into one searchable, PDF/UA-1-tagged PDF.

    Pages are the item's images in natural filename order (page_2 before
    page_10), tagged the same way as `tag-pdf`. Writes tagged.pdf,
    searchable.pdf and text.txt under .../accessibility/document/.
    """
    try:
        utils.log(f"Building a PDF from the images in {target}")
        utils.result(workspace.apply_to_targets(target, accessibility.build_pdf, opts={
            "stage": "pdf-build",
            "fn_opts": {
                "lang": lang,
                "strict": strict,
                "lookahead": lookahead,
                "tables": tables,
                "paragraphs": paragraphs,
                "reuse_ocr": reuse_ocr,
                "correct": correct,
                "validate": validate,
                "min_conf": min_conf,
                "enhance": ocr_enhance,
                "skip_existing": skip_existing,
                "figures": figures,
                "geometry": geometry,
            },
            "folder": folder, "username": user,
            "reporting_job_id": reporting_job_id, "reporting_collection": reporting_collection,
        }))
    except _ERRORS as e:
        utils.error(str(e))


@app.command()
def figures(
    target: TargetArgument,
    collection_context: Annotated[str, typer.Option(
        help="Collection-specific context for the alt-text prompt.",
    )] = "",
    transcribe_text: Annotated[bool, typer.Option(
        help="Quote short, significant, legible text inside figures.",
    )] = True,
    max_alt_chars: AltMaxCharsOption = CONFIG["alt"]["max_chars"],
    folder: WorkspaceFolderOption = None,
    user: WorkspaceUserOption = None,
    reporting_job_id: ReportingJobIdOption = None,
    reporting_collection: ReportingCollectionOption = None,
):
    """Detect and describe the figures on every image and PDF page of an item.

    One Samwise call finds a page's figures, then one per informative figure
    writes its alt text (plus a long description for charts, diagrams and
    maps). Writes figures.json and crops per page, and adds new pages to
    .../accessibility/<file>/figure-review.csv. Edit that CSV (alt text,
    decorative, or reject/skip in flag) and upload it back; `tag-pdf
    --figures` and `build-pdf --figures` use the reviewed rows.
    """
    try:
        utils.log(f"Detecting figures for {target}")
        utils.result(workspace.apply_to_targets(target, accessibility.detect_figures, opts={
            "stage": "figures",
            "fn_opts": {
                "collection_context": collection_context,
                "transcribe_text": transcribe_text,
                "max_alt_chars": max_alt_chars,
            },
            "folder": folder, "username": user,
            "reporting_job_id": reporting_job_id, "reporting_collection": reporting_collection,
        }))
    except _ERRORS as e:
        utils.error(str(e))
