"""
Shared CLI utilities: JSON result output and progress logging.

All digtk commands stream progress lines to stdout and print a JSON object
as the final line. Dagster assets call exec(["digtk", ...]) and read that
last line as the structured result.
"""
import json
import sys
from datetime import datetime, timezone

from digtk.lib import derivative


def log(message):
    """Print a progress message to stdout.

    :param str message: Human-readable progress line.
    """
    print(message, flush=True)


def result(data):
    """Print a structured JSON result as the final stdout line and exit 0.

    :param dict data: Result payload. Should include at minimum {"status": "success"}.
    """
    print(json.dumps(data), flush=True)
    raise SystemExit(0)


def error(message, code=1):
    """Print a structured JSON error as the final stdout line and exit non-zero.

    :param str message: Error description.
    :param int code: Exit code (default 1).
    """
    print(json.dumps({"status": "error", "error": message}), flush=True)
    raise SystemExit(code)


def write_manifest(collection, rel_path, command, params, outputs, source_files, opts={}):
    """Write a process-directory manifest.json, recording what a digtk command
    ran and produced (see docs/cask-conventions.md's manifest.json convention).

    Goes through digtk.lib.derivative.write() — the canonical way to write any
    silver pipeline output (see CLAUDE.md) — rather than hand-building the path
    and calling fs.write() directly, so the manifest gets the same lineage
    tracking as the outputs it describes.

    This is distinct from dagster.lib.utils.write_manifest, which covers a
    separate, still-unimplemented bronze/collection-level structural manifest
    (item lists + hashes) owned by ingest. This one is digtk's own
    responsibility for its process directories (e.g. silver/.../derivatives/),
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
    :param dict opts: 'group' (required), 'time' (default 'latest'), 'status'
        (default "success"), 'duration_s' (float), 'error' (str, only meaningful
        if status="error").
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

    derivative.write(
        type="silver",
        collection=collection,
        rel_path=f"{rel_path.rstrip('/')}/manifest.json",
        data=json.dumps(manifest, indent=2).encode(),
        source_files=source_files,
        opts={"group": opts.get("group"), "time": opts.get("time"), "replace": True},
    )
    return manifest
