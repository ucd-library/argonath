"""
Entry point for the `digtk` media processing CLI.

Subcommands mirror the pipeline phases:
  digtk auth          — manage the CaskFS connection (login/whoami/logout)
  digtk config        — manage persistent digtk settings (set/show)
  digtk ai            — ad hoc Samwise AI queries, for testing (chat/embed)
  digtk showcase      — commands specific to the public Digital Collections website:
                          image (thumbnails, conversion, deskew, OCR prep),
                          pdf (per-page thumbnails, conversion, deskew, OCR prep),
                          video (HLS stream, captions via local Whisper),
                          audio (transcode, waveform, captions via local Whisper),
                          es (index items into the showcase website's Elasticsearch index)
  digtk ocr           — Tesseract word geometry + Qwen vision transcript
  digtk metadata      — AI and human metadata generation
  digtk accessibility — accessibility metadata (AI alt text, PDF/UA-1 tagging)
  digtk split-spreads — detect + cut scanned two-page spreads (corrective, not a
                          showcase display asset — see digtk.models.split)
  digtk ingest        — ingest files into CaskFS
  digtk collection    — manage CaskFS collections (add/sync/add-tree)
  digtk search        — LlamaIndex RAG indexing and querying
  digtk preservica  — pull a collection out of Preservica into bronze (import/tree)
  digtk reporting     — admin commands for the reporting Postgres schema
                          (see digtk.lib.reporting)
  digtk state         — collection_state queue and job results, used by Dagster
                          (see digtk.models.collection_state)
  digtk workspace     — stage local folders into CaskFS workspace, run ocr/
                          accessibility/split-spreads on them, pull outputs,
                          promote them to silver (see digtk.models.workspace)

Every command finishes with a result — --output controls whether that also
includes a structured JSON line (see digtk.lib.utils's module docstring):
'auto' (default) only when stdout isn't a terminal, 'text' never, 'json' always.
"""
import sys
from enum import Enum
from typing import Annotated

import typer

from digtk.commands import (
    accessibility, ai, auth, collection, config, ocr, metadata, ingest, preservica, reporting, showcase, search, split,
    state, workspace,
)
from digtk.lib import utils
from digtk.lib.banner import print_banner

app = typer.Typer(
    name="digtk",
    help="Digital collections media processing CLI",
    no_args_is_help=True,
)

app.add_typer(auth.app,          name="auth")
app.add_typer(config.app,        name="config")
app.add_typer(ai.app,            name="ai")
app.add_typer(showcase.app,      name="showcase")
app.add_typer(ocr.app,           name="ocr")
app.add_typer(metadata.app,      name="metadata")
app.add_typer(accessibility.app, name="accessibility")
app.add_typer(ingest.app,     name="ingest")
app.add_typer(preservica.app, name="preservica")
app.add_typer(split.app,         name="split-spreads")
app.add_typer(ingest.app,        name="ingest")
app.add_typer(collection.app,    name="collection")
app.add_typer(search.app,        name="search")
app.add_typer(reporting.app,     name="reporting")
app.add_typer(state.app,         name="state")
app.add_typer(workspace.app,     name="workspace")

class OutputMode(str, Enum):
    """CLI-wide --output mode, applying to every command's final result line."""
    auto = "auto"
    text = "text"
    json = "json"


@app.callback()
def main_callback(
    output: Annotated[OutputMode, typer.Option(
        "--output", help="'auto' (default): print the structured JSON result line only "
        "when stdout isn't a terminal (piped, redirected, or run by Dagster). 'text': "
        "never print it, just the human-readable progress lines. 'json': always print it.",
    )] = OutputMode.auto,
):
    """Digital collections media processing CLI."""
    utils.set_output_mode(None if output == OutputMode.auto else output.value)


def main():
    """Console-script entry point.

    Prints the DIGTK banner before help output (bare invocation or --help
    anywhere in argv) — never before a real subcommand run, since Click's
    --help/no_args_is_help handling exits before app()'s own callback body
    would run, and the banner must never land in front of the last-line
    JSON result a real command emits.
    """
    if len(sys.argv) == 1 or "--help" in sys.argv:
        print_banner()
    app()


if __name__ == "__main__":
    main()
