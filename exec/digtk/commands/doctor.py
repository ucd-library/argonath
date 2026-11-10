"""
digtk doctor — check required external CLI tools.

Subcommands:
  check — report install status for every external tool digtk's pipeline commands
          shell out to (tesseract, ImageMagick, Ghostscript, poppler, ffmpeg,
          veraPDF + its Java runtime, whisper), with a link to docs/external-tools.md
          (on GitHub) for anything missing.
"""
import typer

from digtk.lib import doctor as doctor_lib
from digtk.lib import utils

app = typer.Typer(help="Check required external CLI tools.", no_args_is_help=True)

# Summary doc (per-OS quick-install commands, one official link per tool) — see
# docs/external-tools.md. Linked here rather than only in per-tool install_urls so a
# user missing more than one tool has a single page to work from.
DOCS_URL = "https://github.com/ucd-library/argonath/blob/main/docs/external-tools.md"


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
        utils.log(f"Install guide: {DOCS_URL}")
    else:
        utils.log(f"\nAll {len(results)} required tools are available.")

    utils.result({"status": "success", "tools": results, "all_available": not missing, "docs_url": DOCS_URL})
