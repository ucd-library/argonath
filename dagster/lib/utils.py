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
import yaml


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

    The digtk CLI's default --output mode ('auto') already prints its final JSON
    result line whenever stdout isn't a terminal — true here regardless, since
    subprocess.Popen's stdout=PIPE below is never a tty — but `--output json` is
    inserted explicitly anyway so this bridge never silently depends on that
    auto-detection (see digtk.lib.utils's module docstring). This function
    captures the whole stream so all output appears in Dagster's run logs, then
    returns the parsed JSON result.

    :param list cmd: Command and arguments (e.g. ['digtk', 'ingest', 'file', path]).
    :param dict opts: Options: check (bool, default True), no_json_parse (bool).
    :returns: Parsed JSON result dict, or the raw last line string if no_json_parse=True.
    :rtype: dict | str
    :raises subprocess.CalledProcessError: If the command exits non-zero and check=True.
    """
    check = opts.get("check", True)
    no_json_parse = opts.get("no_json_parse", False)

    cmd = [cmd[0], "--output", "json"] + list(cmd[1:])
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
# Celery run config
# ---------------------------------------------------------------------------

def load_celery_run_config():
    """Load celery_config.yaml as a job's default run config.

    Without this, a run launched from Dagit with no run config falls back to
    celery_executor's built-in default broker (pyamqp://guest@localhost//)
    instead of this deployment's actual broker -- connection refused, since
    nothing is listening on localhost:5672 wherever the run worker executes.
    Every op-based (non-asset) job using celery_executor needs this passed as
    its own `config=` -- see lib/pipeline_fanout.py's process_collection_job.

    :returns: Parsed run config dict (the `execution:` block).
    :rtype: dict
    """
    path = os.path.join(os.path.dirname(__file__), "..", "celery_config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def default_run_config(op_configs, opts={}):
    """A job's default run config: the celery execution block plus each listed
    op's config filled with its Config class defaults — so the launchpad opens
    with every option (e.g. max_steps) visible, and a backfill (which never
    sees launchpad edits) runs with the same values.

    :param dict op_configs: {op name: dagster Config class}.
    :param dict opts: 'celery' (bool, default True) — include load_celery_run_config().
    :returns: Run config dict.
    :rtype: dict
    """
    config = dict(load_celery_run_config()) if opts.get("celery", True) else {}
    config["ops"] = {
        op_name: {"config": {field: getattr(cls(), field) for field in cls.model_fields}}
        for op_name, cls in op_configs.items()
    }
    return config


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
