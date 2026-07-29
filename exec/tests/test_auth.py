import pytest

from digtk.lib import auth


@pytest.fixture(autouse=True)
def credentials_path(tmp_path, monkeypatch):
    path = tmp_path / ".digtk" / "credentials.json"
    monkeypatch.setattr(auth, "CREDENTIALS_PATH", path)
    monkeypatch.delenv(auth.API_URL_ENV, raising=False)
    monkeypatch.delenv(auth.API_KEY_ENV, raising=False)
    return path


def test_resolve_with_nothing_configured():
    creds = auth.resolve()
    assert creds == {"api_url": None, "token": None, "user": None}


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
    assert creds == {"api_url": "https://cask.example", "token": "tok", "user": "jdoe"}


def test_env_vars_override_stored_file(monkeypatch):
    auth.save_stored({"api_url": "https://cask.example", "token": "tok", "user": "jdoe"})
    monkeypatch.setenv(auth.API_URL_ENV, "https://override.example")
    monkeypatch.setenv(auth.API_KEY_ENV, "override-tok")
    creds = auth.resolve()
    assert creds["api_url"] == "https://override.example"
    assert creds["token"] == "override-tok"
    assert creds["user"] == "jdoe"  # user only ever comes from the stored file


def test_clear_stored_removes_file(credentials_path):
    auth.save_stored({"api_url": "x"})
    auth.clear_stored()
    assert not credentials_path.exists()
    assert auth.load_stored() == {}


def test_clear_stored_is_a_noop_when_nothing_stored():
    auth.clear_stored()
