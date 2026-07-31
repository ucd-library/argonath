import os

import pytest

from digtk.lib import config


@pytest.fixture
def home_config_path(tmp_path):
    return tmp_path / ".digtk" / "config.json"


@pytest.fixture(autouse=True)
def clean_seeded_env(monkeypatch):
    """Every SETTINGS env var starts unset, regardless of what the real shell exports."""
    for setting in config.SETTINGS:
        monkeypatch.delenv(setting["env"], raising=False)


def test_read_home_config_with_no_file(home_config_path):
    assert config.read_home_config(home_config_path) == {}


def test_write_and_read_home_config_round_trip(home_config_path):
    config.write_home_config(home_config_path, {"samwise.api_url": "https://example.com"})
    assert config.read_home_config(home_config_path) == {"samwise.api_url": "https://example.com"}


def test_write_home_config_sets_owner_only_permissions(home_config_path):
    config.write_home_config(home_config_path, {"samwise.api_key": "sk-secret"})
    mode = home_config_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_read_home_config_raises_on_invalid_json(home_config_path):
    home_config_path.parent.mkdir(parents=True)
    home_config_path.write_text("not json")
    with pytest.raises(ValueError, match="Invalid JSON"):
        config.read_home_config(home_config_path)


def test_set_setting_rejects_unknown_dotpath(home_config_path):
    with pytest.raises(ValueError, match="Unknown setting"):
        config.set_setting("not.a.real.setting", "x", path=home_config_path)


def test_set_setting_rejects_value_failing_cast(home_config_path):
    with pytest.raises(ValueError, match="Invalid value"):
        config.set_setting("search.top_k", "not-an-int", path=home_config_path)


def test_set_setting_persists_value(home_config_path):
    config.set_setting("samwise.api_url", "https://samwise.example", path=home_config_path)
    assert config.read_home_config(home_config_path) == {"samwise.api_url": "https://samwise.example"}


def test_set_setting_preserves_other_keys(home_config_path):
    config.set_setting("samwise.api_url", "https://samwise.example", path=home_config_path)
    config.set_setting("search.top_k", "20", path=home_config_path)
    assert config.read_home_config(home_config_path) == {
        "samwise.api_url": "https://samwise.example",
        "search.top_k": "20",
    }


def test_is_secret():
    assert config.is_secret("samwise.api_key") is True
    assert config.is_secret("samwise.api_url") is False
    assert config.is_secret("not.a.real.setting") is False


def test_seed_env_from_home_config_sets_unset_vars(home_config_path, monkeypatch):
    config.write_home_config(home_config_path, {"samwise.api_url": "https://from-file.example"})
    config._seed_env_from_home_config(home_config_path, config.SETTINGS)
    assert os.environ[config.SAMWISE_API_URL_ENV] == "https://from-file.example"


def test_seed_env_from_home_config_does_not_override_existing_env(home_config_path, monkeypatch):
    monkeypatch.setenv(config.SAMWISE_API_URL_ENV, "https://from-env.example")
    config.write_home_config(home_config_path, {"samwise.api_url": "https://from-file.example"})
    config._seed_env_from_home_config(home_config_path, config.SETTINGS)
    assert os.environ[config.SAMWISE_API_URL_ENV] == "https://from-env.example"


def test_seed_env_from_home_config_warns_on_unknown_key(home_config_path, capsys):
    config.write_home_config(home_config_path, {"not.a.real.setting": "x"})
    config._seed_env_from_home_config(home_config_path, config.SETTINGS)
    assert "not.a.real.setting" in capsys.readouterr().err


def test_seed_env_from_home_config_with_no_file_is_a_noop(tmp_path):
    config._seed_env_from_home_config(tmp_path / "missing.json", config.SETTINGS)


def test_resolved_settings_reports_secret_not_set():
    resolved = config.resolved_settings()
    assert resolved["samwise.api_key"] == "(not set)"


def test_resolved_settings_reports_secret_set(monkeypatch):
    monkeypatch.setenv(config.SAMWISE_API_KEY_ENV, "sk-secret")
    resolved = config.resolved_settings()
    assert resolved["samwise.api_key"] == "***set***"
    assert "sk-secret" not in resolved.values()


def test_resolved_settings_reports_non_secret_config_values():
    resolved = config.resolved_settings()
    assert resolved["ark.naan"] == config.CONFIG["ark"]["naan"]
    assert resolved["search.top_k"] == config.CONFIG["search"]["top_k"]


def test_rag_chunking_defaults():
    """Pins the common RAG chunking defaults (1000/200 chars) and the RAG-specific
    embedding model (distinct from samwise.embed_model) — regression test for the
    drift previously found between digtk.lib.ai's docstring and its actual default."""
    assert config.CONFIG["search"]["chunk_size"] == 1000
    assert config.CONFIG["search"]["chunk_buffer"] == 200
    assert config.CONFIG["search"]["embed_model"] == "qwen3-embed:4b"
    assert config.CONFIG["samwise"]["embed_model"] == "qwen3-embed:4b"
