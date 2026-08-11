"""
Entry point for the `digtk` media processing CLI.

Subcommands mirror the pipeline phases:
  digtk auth      — manage the CaskFS connection (login/whoami/logout)
  digtk config    — manage persistent digtk settings (set/show)
  digtk ai        — ad hoc Samwise AI queries, for testing (chat/embed)
  digtk image     — image derivatives (thumbnails, conversion, deskew, OCR prep)
  digtk pdf       — PDF derivatives (per-page thumbnails, conversion, deskew, OCR prep)
  digtk video     — video derivatives (HLS stream, poster frame, captions via local Whisper)
  digtk audio     — audio derivatives (transcode, waveform, captions via local Whisper)
  digtk ocr       — OCR via Tesseract or ML fallback
  digtk metadata    — AI and human metadata generation
  digtk ingest      — ingest files into CaskFS
  digtk collection  — manage CaskFS collections (add/sync)
  digtk search      — LlamaIndex RAG indexing and querying
"""
import sys

import typer

from digtk.commands import ai, audio, auth, collection, config, image, ocr, metadata, ingest, pdf, search, video
from digtk.lib.banner import print_banner

app = typer.Typer(
    name="digtk",
    help="Digital collections media processing CLI",
    no_args_is_help=True,
)

app.add_typer(auth.app,       name="auth")
app.add_typer(config.app,     name="config")
app.add_typer(ai.app,         name="ai")
app.add_typer(image.app,      name="image")
app.add_typer(pdf.app,        name="pdf")
app.add_typer(video.app,      name="video")
app.add_typer(audio.app,      name="audio")
app.add_typer(ocr.app,        name="ocr")
app.add_typer(metadata.app,   name="metadata")
app.add_typer(ingest.app,     name="ingest")
app.add_typer(collection.app, name="collection")
app.add_typer(search.app,     name="search")


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
