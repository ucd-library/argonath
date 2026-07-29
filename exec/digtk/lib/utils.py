"""
Shared CLI utilities: JSON result output and progress logging.

All digtk commands stream progress lines to stdout and print a JSON object
as the final line. Dagster assets call exec(["digtk", ...]) and read that
last line as the structured result.
"""
import json
import sys


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
