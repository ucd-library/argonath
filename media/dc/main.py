"""
Entry point for the `dc` media processing CLI.

Subcommands mirror the pipeline phases:
  dc image     — image derivatives (thumbnails, conversion, deskew, OCR prep)
  dc ocr       — OCR via Tesseract or ML fallback
  dc metadata  — AI and human metadata generation
  dc ingest    — ingest files into CaskFS
  dc search    — LlamaIndex RAG indexing and querying
"""
import typer

from dc.commands import image, ocr, metadata, ingest, search

app = typer.Typer(
    name="dc",
    help="Digital collections media processing CLI.",
    no_args_is_help=True,
)

app.add_typer(image.app,    name="image")
app.add_typer(ocr.app,      name="ocr")
app.add_typer(metadata.app, name="metadata")
app.add_typer(ingest.app,   name="ingest")
app.add_typer(search.app,   name="search")


if __name__ == "__main__":
    app()
