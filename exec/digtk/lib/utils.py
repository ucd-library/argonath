"""
Shared CLI utilities: JSON result output and progress logging.

Every digtk command streams human-readable progress lines to stdout via log(),
then finishes with result()/error(). Whether that final call *also* prints a
structured JSON line is controlled by the process-wide output mode (see
set_output_mode()):

- 'json' always prints it — this is what Dagster's exec() bridge relies on
  (dagster/lib/utils.py passes --output json explicitly when it shells out, so
  it never depends on the auto-detection below).
- 'text' never prints it, just the log() lines already printed — for a clean
  interactive CLI experience.
- The default, 'auto' (None here), prints JSON only when stdout isn't a
  terminal (piped, redirected, or a subprocess — which is also true whenever
  Dagster invokes digtk, even without --output json) and skips it in an
  interactive terminal.

main.py's root --output option calls set_output_mode() once before any
subcommand runs.
"""
import json
import sys
from datetime import datetime, timezone

from digtk.lib import derivative

_output_mode = None  # None = auto (isatty-based); 'json' or 'text' once set explicitly.


def set_output_mode(mode):
    """Set the process-wide output mode for result()/error().

    :param str|None mode: 'json', 'text', or None for auto (see module docstring).
    """
    global _output_mode
    _output_mode = mode


def _json_enabled():
    """
    :returns: Whether result()/error() should print their structured JSON line,
        under the current output mode.
    :rtype: bool
    """
    if _output_mode == "json":
        return True
    if _output_mode == "text":
        return False
    return not sys.stdout.isatty()


def log(message):
    """Print a progress message to stdout.

    :param str message: Human-readable progress line.
    """
    print(message, flush=True)


def warn(message):
    """Print a non-fatal problem to stderr — something the command worked
    around (e.g. one page it had to skip) rather than a reason to stop, so it
    stays visible even with --output json keeping stdout for the result line.

    :param str message: Human-readable warning.
    """
    print(f"Warning: {message}", file=sys.stderr, flush=True)


def result(data):
    """Print a structured JSON result as the final stdout line, if JSON output is
    enabled (see _json_enabled()), and exit 0.

    :param dict data: Result payload. Should include at minimum {"status": "success"}.
    """
    if _json_enabled():
        print(json.dumps(data), flush=True)
    raise SystemExit(0)


def error(message, code=1):
    """Report an error and exit non-zero: a structured JSON line if JSON output
    is enabled (see _json_enabled()), else a plain message to stderr.

    :param str message: Error description.
    :param int code: Exit code (default 1).
    """
    if _json_enabled():
        print(json.dumps({"status": "error", "error": message}), flush=True)
    else:
        print(f"Error: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def write_manifest(collection, rel_path, command, params, outputs, source_files, *, type="silver", opts={}):
    """Write a process-directory manifest.json, recording what a digtk command
    ran and produced (see docs/cask-conventions.md's manifest.json convention).

    Goes through digtk.lib.derivative.write() — the canonical way to write any
    silver/gold pipeline output (see CLAUDE.md) — rather than hand-building the path
    and calling fs.write() directly, so the manifest gets the same lineage
    tracking as the outputs it describes.

    This is distinct from dagster.lib.utils.write_manifest, which covers a
    separate, still-unimplemented bronze/collection-level structural manifest
    (item lists + hashes) owned by ingest. This one is digtk's own
    responsibility for its process directories (e.g. gold/.../derivatives/),
    shared across phases (derivatives, OCR, metadata) so they don't each
    duplicate the same manifest-writing logic.

    :param str collection: Collection name.
    :param str rel_path: Path of the process directory this manifest describes,
        relative to the collection/time root (e.g. "box-1/folder-3/derivatives").
    :param list[str] command: Full argv, e.g. ["digtk", "image", "derivatives", ark].
    :param dict params: Parsed/typed params used for this run.
    :param list[str] outputs: Output filenames, relative to rel_path.
    :param list[str] source_files: CaskFS paths ('cask:/'-prefixed) the described
        outputs were derived from — this manifest's own lineage.
    :param str type: 'silver' (default) or 'gold' — see digtk.lib.derivative.dest_path().
    :param dict opts: 'group'/'time' (silver — 'group' required, 'time' default
        'latest') or 'app' (gold, required) — see dest_path(). Also: 'status'
        (default "success"), 'duration_s' (float), 'error' (str, only meaningful
        if status="error"), 'extra' (dict, optional) — additional top-level keys
        merged into the manifest as-is, for callers that need richer per-output
        content than the generic 'outputs' filename list (e.g.
        digtk.models.digital_website.image's per-size/url "original"/"small"/
        "medium"/"large"/"tiled"/"ocr" manifest content — see
        docs/cask-conventions.md's manifest.json section). Generic here on
        purpose: this function stays media-type-agnostic, so the caller decides
        what "extra" means for its own outputs.
    :returns: The manifest dict that was written.
    :rtype: dict
    """
    manifest = {
        "written_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": opts.get("status", "success"),
        "command": command,
        "params": params,
        "outputs": outputs,
        "duration_s": opts.get("duration_s"),
    }
    if "error" in opts:
        manifest["error"] = opts["error"]
    if "extra" in opts:
        manifest.update(opts["extra"])

    if type == "gold":
        write_opts = {"app": opts.get("app"), "replace": True}
    else:
        write_opts = {"group": opts.get("group"), "time": opts.get("time"), "replace": True}

    derivative.write(
        type=type,
        collection=collection,
        rel_path=f"{rel_path.rstrip('/')}/manifest.json",
        data=json.dumps(manifest, indent=2).encode(),
        source_files=source_files,
        opts=write_opts,
    )
    return manifest
