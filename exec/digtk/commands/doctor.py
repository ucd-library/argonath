"""
digtk doctor — check required external CLI tools.

Subcommands:
  check — report install status for every external tool digtk's pipeline commands
          shell out to (tesseract, ImageMagick, Ghostscript, poppler, ffmpeg,
          veraPDF + its Java runtime, whisper), with a link to install docs for
          anything missing.
"""
import typer

from digtk.lib import doctor as doctor_lib
from digtk.lib import utils

app = typer.Typer(help="Check required external CLI tools.", no_args_is_help=True)


@app.command()
def check():
    """Report install status for every external tool digtk shells out to."""
    results = doctor_lib.check_all()
    for tool in results:
        if not tool["available"]:
            utils.log(f"[MISSING] {tool['name']}: install from {tool['install_url']}")
        elif tool["warning"]:
            utils.log(f"[OK]      {tool['name']}: {tool['path']} (warning: {tool['warning']})")
        else:
            detail = f" ({tool['version']})" if tool["version"] else ""
            utils.log(f"[OK]      {tool['name']}: {tool['path']}{detail}")

    missing = [tool["name"] for tool in results if not tool["available"]]
    if missing:
        utils.log(f"\n{len(missing)} of {len(results)} tool(s) missing: {', '.join(missing)}")
    else:
        utils.log(f"\nAll {len(results)} required tools are available.")

    utils.result({"status": "success", "tools": results, "all_available": not missing})
