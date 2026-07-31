"""
CaskFS connection/credential resolution for the digtk CLI.

Resolution precedence: CASK_API_URL/CASK_API_KEY environment variables override the
stored credentials file (~/.digtk/credentials.json, written by `digtk auth login`).
There is no CLI flag for these — passing a bearer token as a plain CLI argument would
leak it into shell history and process listings.
"""
import json
import os
import stat
from pathlib import Path

from digtk.lib.config import CONFIG

CREDENTIALS_PATH = Path(CONFIG["auth"]["credentials_path"])

API_URL_ENV = CONFIG["auth"]["api_url_env"]
API_KEY_ENV = CONFIG["auth"]["api_key_env"]


def load_stored():
    """Read the stored credentials file.

    :returns: Stored credentials dict, or {} if no credentials file exists.
    :rtype: dict
    """
    if not CREDENTIALS_PATH.exists():
        return {}
    return json.loads(CREDENTIALS_PATH.read_text())


def save_stored(data):
    """Write the credentials file with owner-only permissions.

    :param dict data: Credentials to store (e.g. {'api_url':, 'token':, 'user':}).
    """
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(json.dumps(data))
    CREDENTIALS_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)


def clear_stored():
    """Delete the stored credentials file, if present."""
    CREDENTIALS_PATH.unlink(missing_ok=True)


def resolve():
    """Resolve the active CaskFS connection: env vars override the stored credentials file.

    :returns: Dict with keys 'api_url', 'token', 'user'. Any key may be None if unresolved.
    :rtype: dict
    """
    stored = load_stored()
    return {
        "api_url": os.getenv(API_URL_ENV) or stored.get("api_url"),
        "token": os.getenv(API_KEY_ENV) or stored.get("token"),
        "user": stored.get("user"),
    }
