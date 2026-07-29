"""
Shared utilities: database connection, subprocess exec() helper, and manifest writer.

The exec() helper runs digtk CLI commands, streams output to Dagster logs,
and returns the last stdout line parsed as JSON — matching the aggie-experts pattern.
"""
import os
import sys
import json
import datetime
import subprocess
import signal
import atexit

import dagster as dg
import psycopg2


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

conn = psycopg2.connect(
    host=os.getenv("DAGSTER_POSTGRES_HOST", "localhost"),
    database=os.getenv("DAGSTER_POSTGRES_DB", "dagster"),
    user=os.getenv("DAGSTER_POSTGRES_USER", "postgres"),
    password=os.getenv("DAGSTER_POSTGRES_PASSWORD", "postgres"),
)


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

def exec(cmd, opts={}):
    """Run a digtk CLI command, stream output to stdout, and return the last line as JSON.

    The digtk CLI prints progress lines to stdout and emits a JSON object as its
    final line. This function captures that stream so all output appears in
    Dagster's run logs, then returns the parsed JSON result.

    :param list cmd: Command and arguments (e.g. ['digtk', 'ingest', 'file', path]).
    :param dict opts: Options: check (bool, default True), no_json_parse (bool).
    :returns: Parsed JSON result dict, or the raw last line string if no_json_parse=True.
    :rtype: dict | str
    :raises subprocess.CalledProcessError: If the command exits non-zero and check=True.
    """
    check = opts.get("check", True)
    no_json_parse = opts.get("no_json_parse", False)

    print(f"Executing: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    def _terminate():
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except Exception:
                try:
                    process.terminate()
                except Exception:
                    pass

    atexit.register(_terminate)

    try:
        last_line = ""
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            last_line = line

        rc = process.wait()
        if check and rc != 0:
            raise subprocess.CalledProcessError(rc, cmd)

        if no_json_parse:
            return last_line.strip()

        try:
            return json.loads(last_line)
        except json.JSONDecodeError:
            return {"error": "failed to parse last stdout line as JSON", "raw": last_line.strip()}
    finally:
        atexit.unregister(_terminate)
        if process.stdout:
            process.stdout.close()


# ---------------------------------------------------------------------------
# Manifest writer
# ---------------------------------------------------------------------------

def write_manifest(path, data, opts={}):
    """Write a manifest.json to a process directory in CaskFS.

    Manifests record run metadata (commands, params, timestamps, outputs)
    that persists beyond Dagster's run retention window.

    :param str path: CaskFS path for the manifest.json file.
    :param dict data: Manifest payload.
    :param dict opts: Additional options passed to the CaskFS adapter.
    """
    manifest = {
        "written_at": datetime.datetime.utcnow().isoformat() + "Z",
        **data,
    }
    # CaskFS write implementation added in phase 0.2
    raise NotImplementedError("phase 0.2")
