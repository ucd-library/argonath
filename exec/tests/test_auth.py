import threading
import urllib.parse
import urllib.request

import pytest

from digtk.lib import auth


def _fake_browser_open(token="tok-123", user="jdoe", state_override=None, omit_token=False):
    """Build an `open_browser` stand-in that hits the CLI's own loopback callback.

    Runs the HTTP GET in a background thread — `login()` doesn't call
    `server.handle_request()` until after `open_browser()` returns, so a synchronous
    request here would deadlock waiting on a server that isn't accepting yet.
    """

    def opener(login_url):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(login_url).query)
        port = params["port"][0]
        state = state_override if state_override is not None else params["state"][0]

        query = {"state": state, "user": user}
        if not omit_token:
            query["token"] = token
        callback_url = f"http://127.0.0.1:{port}/callback?" + urllib.parse.urlencode(query)

        def hit():
            try:
                urllib.request.urlopen(callback_url, timeout=5)
            except Exception:
                pass

        threading.Thread(target=hit, daemon=True).start()

    return opener


@pytest.fixture(autouse=True)
def credentials_path(tmp_path, monkeypatch):
    path = tmp_path / ".digtk" / "credentials.json"
    monkeypatch.setattr(auth, "CREDENTIALS_PATH", path)
    monkeypatch.delenv(auth.API_URL_ENV, raising=False)
    monkeypatch.delenv(auth.API_KEY_ENV, raising=False)
    monkeypatch.delenv(auth.TRUSTED_USER_ENV, raising=False)
    return path


@pytest.fixture
def cask_environments_path(tmp_path, monkeypatch):
    path = tmp_path / ".caskfs" / "environments.json"
    monkeypatch.setenv(auth.CASKFS_ENV_FILE_ENV, str(path))
    return path


def test_resolve_with_nothing_configured():
    creds = auth.resolve()
    assert creds == {"api_url": None, "token": None, "user": None, "trusted_user": None}


def test_save_and_load_stored():
    auth.save_stored({"api_url": "https://cask.example", "token": "tok", "user": "jdoe"})
    assert auth.load_stored() == {"api_url": "https://cask.example", "token": "tok", "user": "jdoe"}


def test_save_stored_sets_owner_only_permissions(credentials_path):
    auth.save_stored({"api_url": "x", "token": "y", "user": "z"})
    mode = credentials_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_resolve_uses_stored_file():
    auth.save_stored({"api_url": "https://cask.example", "token": "tok", "user": "jdoe"})
    creds = auth.resolve()
    assert creds == {
        "api_url": "https://cask.example",
        "token": "tok",
        "user": "jdoe",
        "trusted_user": None,
    }


def test_env_vars_override_stored_file(monkeypatch):
    auth.save_stored({"api_url": "https://cask.example", "token": "tok", "user": "jdoe"})
    monkeypatch.setenv(auth.API_URL_ENV, "https://override.example")
    monkeypatch.setenv(auth.API_KEY_ENV, "override-tok")
    creds = auth.resolve()
    assert creds["api_url"] == "https://override.example"
    assert creds["token"] == "override-tok"
    assert creds["user"] == "jdoe"  # user only ever comes from the stored file


def test_resolve_trusted_user_is_env_only_with_no_stored_fallback(monkeypatch):
    monkeypatch.setenv(auth.TRUSTED_USER_ENV, "dagster-worker")
    creds = auth.resolve()
    assert creds["trusted_user"] == "dagster-worker"


def test_clear_stored_removes_file(credentials_path):
    auth.save_stored({"api_url": "x"})
    auth.clear_stored()
    assert not credentials_path.exists()
    assert auth.load_stored() == {}


def test_clear_stored_is_a_noop_when_nothing_stored():
    auth.clear_stored()


def test_login_stores_credentials_from_gateway_callback():
    creds = auth.login(gateway_url="https://gateway.example", open_browser=_fake_browser_open())

    assert creds == {"api_url": "https://gateway.example/cask/api", "token": "tok-123", "user": "jdoe"}
    assert auth.load_stored() == creds


def test_login_strips_trailing_slash_from_gateway_url():
    creds = auth.login(gateway_url="https://gateway.example/", open_browser=_fake_browser_open())
    assert creds["api_url"] == "https://gateway.example/cask/api"


def test_login_rejects_mismatched_state():
    with pytest.raises(auth.LoginError, match="state"):
        auth.login(
            gateway_url="https://gateway.example",
            open_browser=_fake_browser_open(state_override="wrong-state"),
        )
    assert auth.load_stored() == {}


def test_login_rejects_callback_with_no_token():
    with pytest.raises(auth.LoginError, match="token"):
        auth.login(
            gateway_url="https://gateway.example",
            open_browser=_fake_browser_open(omit_token=True),
        )
    assert auth.load_stored() == {}


def test_login_times_out_if_browser_never_completes():
    with pytest.raises(auth.LoginError, match="Timed out"):
        auth.login(gateway_url="https://gateway.example", open_browser=lambda url: None, timeout=0.2)
    assert auth.load_stored() == {}


def test_load_cask_environments_with_no_file(cask_environments_path):
    assert auth.load_cask_environments() == {"environments": {}, "defaultEnvironment": None}


def test_update_cask_env_rejects_unknown_environment(cask_environments_path):
    auth.save_cask_environments({"environments": {}, "defaultEnvironment": None})

    with pytest.raises(auth.LoginError, match="No CaskFS environment named 'argo-local-dev'"):
        auth.update_cask_env("argo-local-dev", "tok-123", "jdoe", "https://gateway.example/cask")


def test_update_cask_env_error_suggests_the_create_command(cask_environments_path):
    with pytest.raises(auth.LoginError, match=r"cask env set -t http -h https://gateway\.example/cask argo-local-dev"):
        auth.update_cask_env("argo-local-dev", "tok-123", "jdoe", "https://gateway.example/cask")


def test_update_cask_env_sets_token_fields_and_preserves_others(cask_environments_path):
    auth.save_cask_environments({
        "environments": {
            "argo-local-dev": {"type": "http", "host": "http://localhost:3001/cask", "clientEnv": "dev"},
            "local-dev": {"type": "direct-pg", "host": "localhost"},
        },
        "defaultEnvironment": "local-dev",
    })

    auth.update_cask_env("argo-local-dev", "tok-123", "jdoe", "https://gateway.example/cask")

    data = auth.load_cask_environments()
    assert data["environments"]["argo-local-dev"] == {
        "type": "http",
        "host": "http://localhost:3001/cask",
        "clientEnv": "dev",
        "token": "tok-123",
        "tokenUsername": "jdoe",
    }
    assert data["environments"]["local-dev"] == {"type": "direct-pg", "host": "localhost"}
    # a default was already set — updating a non-default env must not steal it
    assert data["defaultEnvironment"] == "local-dev"


def test_update_cask_env_sets_default_when_none_is_set(cask_environments_path):
    auth.save_cask_environments({
        "environments": {"argo-local-dev": {"type": "http", "host": "http://localhost:3001/cask"}},
        "defaultEnvironment": None,
    })

    auth.update_cask_env("argo-local-dev", "tok-123", "jdoe", "https://gateway.example/cask")

    assert auth.load_cask_environments()["defaultEnvironment"] == "argo-local-dev"
