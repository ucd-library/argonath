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


def test_set_setting_persists_preservica_tenant(home_config_path):
    config.set_setting("preservica.tenant", "mytenant", path=home_config_path)
    assert config.read_home_config(home_config_path) == {"preservica.tenant": "mytenant"}


def test_set_setting_persists_showcase_es_settings(home_config_path):
    config.set_setting("showcase.es_host", "http://es.example:9200", path=home_config_path)
    config.set_setting("showcase.es_index", "item-test", path=home_config_path)
    config.set_setting("showcase.es_page_search_index", "page-search-test", path=home_config_path)
    assert config.read_home_config(home_config_path) == {
        "showcase.es_host": "http://es.example:9200",
        "showcase.es_index": "item-test",
        "showcase.es_page_search_index": "page-search-test",
    }


def test_is_secret():
    assert config.is_secret("samwise.api_key") is True
    assert config.is_secret("samwise.api_url") is False
    assert config.is_secret("not.a.real.setting") is False


def test_is_secret_preservica_password_only():
    assert config.is_secret("preservica.password") is True
    assert config.is_secret("preservica.tenant") is False
    assert config.is_secret("preservica.username") is False
    assert config.is_secret("preservica.base_url") is False


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
    assert resolved["fs.chunk_size"] == config.CONFIG["fs"]["chunk_size"]
    assert resolved["search.top_k"] == config.CONFIG["search"]["top_k"]


def test_resolved_settings_reports_showcase_es_settings():
    resolved = config.resolved_settings()
    assert resolved["showcase.es_host"] == config.CONFIG["showcase"]["es_host"]
    assert resolved["showcase.es_index"] == config.CONFIG["showcase"]["es_index"]
    assert resolved["showcase.es_page_search_index"] == config.CONFIG["showcase"]["es_page_search_index"]


def test_showcase_es_defaults():
    """Pins the showcase ES defaults that digtk.lib.showcase_index writes to — the
    "-write" aliases of fin's item and page-search indexes."""
    showcase = config.CONFIG["showcase"]
    assert showcase["es_host"] == "http://elasticsearch:9200"
    assert showcase["es_index"] == "item-write"
    assert showcase["es_page_search_index"] == "page-search-write"


def test_image_derivatives_defaults():
    """Pins the phase 0.4 image-processing defaults ported from the reference
    ImageMagick/Tesseract pipeline (dams/services/image-utils)."""
    image = config.CONFIG["image"]
    assert image["dpi"] == 300
    assert image["deskew_threshold"] == 40
    assert image["fuzz_threshold"] == 80
    assert image["thumbnail_quality"] == 90
    assert image["ocr_prep_quality"] == 100
    assert image["ocr_prep_size"] == 2048
    assert image["deskew_working_width"] == 2048
    assert image["timeout_s"] == 120.0


def test_ocr_orientation_defaults():
    """Distinct from ocr.confidence_threshold (the ML-fallback trigger) — these
    govern the derivatives-time deskew orientation comparison."""
    ocr = config.CONFIG["ocr"]
    assert ocr["orientation_word_count_threshold"] == 100
    assert ocr["orientation_word_confidence_threshold"] == 50.0


def test_alt_defaults():
    """max_chars matches alt_utils.DEFAULT_MAX_ALT_CHARS; want_description off by
    default since it changes generate_alt()'s prompt/return shape."""
    assert config.CONFIG["alt"]["max_chars"] == 150
    assert config.CONFIG["alt"]["want_description"] is False

def test_gateway_and_login_timeout_defaults():
    """Pins the Anduin auth gateway default and browser-login timeout that
    digtk.lib.auth.login() reads via CONFIG['gateway']['url']/CONFIG['auth']['login_timeout_s']."""
    assert config.CONFIG["gateway"]["url"] == "https://argonath.library.ucdavis.edu"
    assert config.CONFIG["auth"]["login_timeout_s"] == 300.0


def test_rag_chunking_defaults():
    """Pins the common RAG chunking defaults (1000/200 chars) and the RAG-specific
    embedding model (distinct from samwise.embed_model) — regression test for the
    drift previously found between digtk.lib.ai's docstring and its actual default."""
    assert config.CONFIG["search"]["chunk_size"] == 1000
    assert config.CONFIG["search"]["chunk_buffer"] == 200
    assert config.CONFIG["search"]["embed_model"] == "qwen3-embed:4b"
    assert config.CONFIG["samwise"]["embed_model"] == "qwen3-embed:4b"
