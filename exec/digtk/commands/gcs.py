"""
digtk public-dc gcs -- sync already-published derivatives into the legacy DAMS/fin GCS
bucket layout.

Subcommands:
  sync  -- sync one item's derivatives (images/PDFs/videos) into GCS
"""
from typing import Annotated

import typer

from digtk.lib import utils
from digtk.lib.config import CONFIG, GcsBucketOption
from digtk.lib import gcs_sync

app = typer.Typer(help="GCS derivative sync commands.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


@app.command()
def sync(
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    bucket: GcsBucketOption = CONFIG["gcs"]["bucket"],
):
    """Sync an item's already-published derivatives into GCS, DAMS-shaped.

    Auto-detects which media types (image/PDF/video) the item actually has
    and syncs each; a media type the item has none of is simply omitted from
    the result, not an error.
    """
    try:
        utils.log(f"Syncing {ark} to GCS (bucket={bucket})")
        result = gcs_sync.sync_item(ark, opts={"bucket": bucket})
        for key in ("images", "pdfs", "videos"):
            if key in result:
                utils.log(f"  {key}: {', '.join(result[key])}")
        for fmt, reason in result.get("skipped", {}).items():
            utils.log(f"  skipped {fmt}: {reason}")
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))
