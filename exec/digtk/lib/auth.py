"""
Connection/credential resolution for the digtk CLI — CaskFS and Preservica.

CaskFS's connection (CASK_API_URL/CASK_API_KEY) resolves from environment variables
overriding a dedicated stored credentials file (~/.digtk/credentials.json), written by
the browser-based `digtk auth login` flow below — CaskFS's token is OIDC-issued and
has no home-config-settable dotpath.

Preservica has no such separate flow: tenant/username/base_url are plain
digtk.lib.config dotpath settings (`preservica.tenant`, etc.), resolved through
CONFIG with the same default -> home config file -> env var precedence as everything
else, settable via `digtk config set preservica.<name> <value>`. 'password' is marked
secret in CONFIG's SETTINGS registry (hidden-input prompt, resolved only via env var —
including one seeded from the home config file — never surfaced as a CONFIG value) —
see digtk.lib.config's module docstring for why a secret never becomes a CLI flag or a
value that could get logged (shell history, process listings).

`login()` implements the browser-based OIDC flow against the Anduin auth gateway
(project-anduin's auth-gateway service — see its controllers/auth.js). The gateway
holds the Keycloak client secret and does the actual token exchange; what it hands
back to the CLI is its own opaque session id, not a JWT. The gateway resolves that
session id against its session store on every subsequent request (the same way it
would resolve a browser's session cookie), so the CLI never parses or verifies the
token itself — it is just a bearer credential scoped to the gateway. CaskFS itself
never sees it; the gateway injects a verified x-anduin-user header when proxying.

CASK_TRUSTED_USER is a different, tokenless path for callers that run behind the
same trusted perimeter as CaskFS with no gateway hop in between (e.g. Dagster,
which shells out to this CLI from within the same worker container/network and
has no browser available to run the OIDC flow above). Setting it makes digtk send
the x-anduin-user header itself, directly, the same header the gateway would
otherwise have injected after resolving a session — so it only makes sense where
CaskFS is deployed to trust that header on requests arriving over that trusted
path. It resolves purely from the environment; there's no stored-credentials-file
equivalent, since it identifies a service account for a headless deployment, not
something the interactive login flow produces.
"""
import http.server
import json
import os
import secrets
import stat
import urllib.parse
import webbrowser
from pathlib import Path

from digtk.lib.config import CONFIG

CREDENTIALS_PATH = Path(CONFIG["auth"]["credentials_path"])

API_URL_ENV = CONFIG["auth"]["api_url_env"]
API_KEY_ENV = CONFIG["auth"]["api_key_env"]
TRUSTED_USER_ENV = "CASK_TRUSTED_USER"

GATEWAY_URL = CONFIG["gateway"]["url"]
LOGIN_TIMEOUT_S = CONFIG["auth"]["login_timeout_s"]

# Same env var name the caskfs CLI itself resolves (src/lib/environment.js), so an
# override applies consistently to both tools.
CASKFS_ENV_FILE_ENV = "CASKFS_ENV_FILE"

PRESERVICA_PASSWORD_ENV = CONFIG["preservica"]["password_env"]


class LoginError(RuntimeError):
    """Raised when the browser-based OIDC login flow fails or times out."""


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

    :returns: Dict with keys 'api_url', 'token', 'user', 'trusted_user'. Any key may be
        None if unresolved. 'trusted_user' is env-only (see CASK_TRUSTED_USER in this
        module's docstring) — it has no stored-credentials-file fallback.
    :rtype: dict
    """
    stored = load_stored()
    return {
        "api_url": os.getenv(API_URL_ENV) or stored.get("api_url"),
        "token": os.getenv(API_KEY_ENV) or stored.get("token"),
        "user": stored.get("user"),
        "trusted_user": os.getenv(TRUSTED_USER_ENV),
    }


def _callback_handler_class(expected_state, result):
    """Build a one-shot BaseHTTPRequestHandler that captures the gateway's redirect.

    :param str expected_state: The state value generated for this login attempt; a
        callback request must echo it back, or its token is rejected as forged/stale.
    :param dict result: Mutable dict the handler writes 'token'/'user'/'error' into.
    :returns: A BaseHTTPRequestHandler subclass bound to the given state/result.
    :rtype: type
    """

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            state = query.get("state", [None])[0]
            token = query.get("token", [None])[0]

            if state != expected_state:
                result["error"] = "Login callback had a missing or mismatched state; ignoring it."
                body = "Login failed (state mismatch). You may close this window."
            elif not token:
                result["error"] = "Login callback did not include a token."
                body = "Login failed (no token received). You may close this window."
            else:
                result["token"] = token
                result["user"] = query.get("user", [None])[0]
                body = "Login successful. You may close this window and return to your terminal."

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<html><body><p>{body}</p></body></html>".encode())

        def log_message(self, *args):
            pass  # suppress BaseHTTPRequestHandler's default request logging to stderr

    return CallbackHandler


def login(gateway_url=None, timeout=None, open_browser=webbrowser.open):
    """Log in via the Anduin auth gateway's browser-based OIDC flow and store the result.

    Starts a one-request HTTP server on an ephemeral loopback port, opens the gateway's
    login page in the user's browser, and waits for the gateway to redirect the browser
    back to that port with a session token once Keycloak login succeeds (see this
    module's docstring for what that token is and isn't).

    :param str gateway_url: Anduin auth gateway base URL. Defaults to CONFIG['gateway']['url'].
    :param float timeout: Seconds to wait for the browser flow to complete. Defaults to
        CONFIG['auth']['login_timeout_s'].
    :param callable open_browser: Function used to open the login URL; overridable for testing.
    :returns: The stored credentials dict ({'api_url':, 'token':, 'user':}).
    :rtype: dict
    :raises LoginError: If the login flow times out, is cancelled, or the gateway reports an error.
    """
    gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
    timeout = timeout if timeout is not None else LOGIN_TIMEOUT_S

    state = secrets.token_urlsafe(24)
    result = {}
    server = http.server.HTTPServer(("127.0.0.1", 0), _callback_handler_class(state, result))
    server.timeout = timeout

    try:
        port = server.server_address[1]
        login_url = gateway_url + "/auth/login?" + urllib.parse.urlencode({
            "headless": "true",
            "port": port,
            "state": state,
        })

        open_browser(login_url)
        server.handle_request()
    finally:
        server.server_close()

    if result.get("error"):
        raise LoginError(result["error"])
    if not result.get("token"):
        raise LoginError("Timed out waiting for browser login to complete.")

    creds = {
        "api_url": gateway_url + "/cask/api",
        "token": result["token"],
        "user": result.get("user"),
    }
    save_stored(creds)
    return creds


def _cask_environments_path():
    """
    :returns: Path to the caskfs CLI's environments.json — CASKFS_ENV_FILE if set,
        else ~/.caskfs/environments.json (the same resolution caskfs itself uses).
    :rtype: Path
    """
    override = os.getenv(CASKFS_ENV_FILE_ENV)
    return Path(override) if override else Path.home() / ".caskfs" / "environments.json"


def load_cask_environments(path=None):
    """Read the caskfs CLI's environments.json.

    :param Path path: File to read. Defaults to `_cask_environments_path()`.
    :returns: {'environments': {name: {...}}, 'defaultEnvironment': name-or-None}.
        Matches caskfs's own in-memory default when the file doesn't exist yet.
    :rtype: dict
    """
    path = path or _cask_environments_path()
    if not path.exists():
        return {"environments": {}, "defaultEnvironment": None}
    return json.loads(path.read_text())


def save_cask_environments(data, path=None):
    """Write the caskfs CLI's environments.json, creating its parent directory if needed.

    :param dict data: Full {'environments':, 'defaultEnvironment':} document to write.
    :param Path path: File to write. Defaults to `_cask_environments_path()`.
    """
    path = path or _cask_environments_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def update_cask_env(env_name, token, user, api_url, path=None):
    """Write a login token into an existing caskfs CLI environment.

    Mirrors what `cask auth login` itself does (caskfs's src/bin/cask-auth.js and
    src/lib/environment.js's `saveEnv`): read the environment's existing config,
    overwrite only the token fields, write it back — so unrelated fields (host, path,
    ...) survive. Only updates an environment that already exists; it deliberately
    doesn't create one, since digtk doesn't know what type/host a new entry should be
    beyond the http endpoint it already talked to.

    :param str env_name: Name of an existing environment in environments.json.
    :param str token: Bearer token to store as that environment's 'token'.
    :param str user: Username to store as that environment's 'tokenUsername'.
    :param str api_url: This digtk login's CaskFS URL — used only to suggest the
        `cask env set` command if `env_name` doesn't exist yet.
    :param Path path: environments.json path override, for testing.
    :raises LoginError: If `env_name` isn't a key in environments.json.
    """
    path = path or _cask_environments_path()
    data = load_cask_environments(path)
    environments = data.setdefault("environments", {})

    if env_name not in environments:
        raise LoginError(
            f"No CaskFS environment named '{env_name}' in {path}. Create it first with:\n"
            f"  cask env set -t http -h {api_url} {env_name}"
        )

    env_config = dict(environments[env_name])
    env_config["token"] = token
    env_config["tokenUsername"] = user
    environments[env_name] = env_config

    if not data.get("defaultEnvironment"):
        data["defaultEnvironment"] = env_name

    save_cask_environments(data, path)


def resolve_preservica():
    """Resolve the active Preservica connection from digtk's central CONFIG.

    tenant/username/base_url are plain CONFIG['preservica'] values (see
    digtk.lib.config — default -> home config file -> env var, settable via
    `digtk config set preservica.<name> <value>`). password is resolved straight
    from its env var (PRESERVICA_PASSWORD_ENV) rather than CONFIG, since it's
    marked secret and CONFIG never stores a secret's resolved value — but that env
    var is itself seeded from the home config file the same way every other
    setting's is, so `digtk config set preservica.password` (hidden-input prompt)
    still works.

    :returns: Dict with keys 'tenant', 'username', 'password', 'base_url'. Any key
        may be None if unresolved. 'base_url' defaults to
        'https://{tenant}.preservica.com' when a tenant is known but no explicit
        base_url override is set — Preservica's Starter tier derives the tenant
        from the hostname rather than a request parameter.
    :rtype: dict
    """
    return {
        "tenant": CONFIG["preservica"]["tenant"] or None,
        "username": CONFIG["preservica"]["username"] or None,
        "password": os.getenv(PRESERVICA_PASSWORD_ENV) or None,
        "base_url": CONFIG["preservica"]["base_url"] or None,
    }
