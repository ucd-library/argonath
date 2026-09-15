"""
digtk showcase — commands specific to the public Digital Collections website
(as opposed to preservation-only processing).

Subcommands:
  image  — image derivative commands
  pdf    — PDF derivative commands
  video  — video derivative commands
  audio  — audio derivative commands
  gcs    — sync derivatives into the legacy DAMS/fin GCS bucket layout
  es     — index items into the DAMS website's Elasticsearch index
"""
import typer

from digtk.commands import audio, es, gcs, image, pdf, video

app = typer.Typer(help="Public Digital Collections website commands.", no_args_is_help=True)

app.add_typer(image.app, name="image")
app.add_typer(pdf.app,   name="pdf")
app.add_typer(video.app, name="video")
app.add_typer(audio.app, name="audio")
app.add_typer(gcs.app,   name="gcs")
app.add_typer(es.app,    name="es")
