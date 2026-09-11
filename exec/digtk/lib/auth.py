"""
Connection/credential resolution for the digtk CLI — CaskFS and Preservica.

Resolution precedence: environment variables override the stored credentials file
(~/.digtk/credentials.json). CaskFS's CASK_API_URL/CASK_API_KEY are written there by
`digtk auth login`; Preservica's PRESERVICA_TENANT/PRESERVICA_USERNAME/
PRESERVICA_PASSWORD live alongside them under a "preservica" key. There is no CLI flag
for any of these — passing a bearer token or password as a plain CLI argument would
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

PRESERVICA_TENANT_ENV = CONFIG["preservica"]["tenant_env"]
PRESERVICA_USERNAME_ENV = CONFIG["preservica"]["username_env"]
PRESERVICA_PASSWORD_ENV = CONFIG["preservica"]["password_env"]
PRESERVICA_BASE_URL_ENV = CONFIG["preservica"]["base_url_env"]


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


def resolve_preservica():
    """Resolve the active Preservica connection: env vars override the stored credentials file.

    Stored Preservica credentials live under the "preservica" key of the same
    ~/.digtk/credentials.json file CaskFS's own credentials live in (see save_stored()).

    :returns: Dict with keys 'tenant', 'username', 'password', 'base_url'. Any key may be
        None if unresolved. 'base_url' defaults to 'https://{tenant}.preservica.com' when
        a tenant is known but no explicit base_url override is set — Preservica's Starter
        tier derives the tenant from the hostname rather than a request parameter.
    :rtype: dict
    """
    stored = load_stored().get("preservica", {})
    tenant = os.getenv(PRESERVICA_TENANT_ENV) or stored.get("tenant")
    base_url = os.getenv(PRESERVICA_BASE_URL_ENV) or stored.get("base_url")
    if not base_url and tenant:
        base_url = f"https://{tenant}.preservica.com"
    return {
        "tenant": tenant,
        "username": os.getenv(PRESERVICA_USERNAME_ENV) or stored.get("username"),
        "password": os.getenv(PRESERVICA_PASSWORD_ENV) or stored.get("password"),
        "base_url": base_url,
    }
