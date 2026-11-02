import pytest

from digtk.models.digital_website import showcase_cask


@pytest.fixture
def init_dir(tmp_path):
    """A seed dir with a hero-banner.json, one image and a dotfile."""
    hero = tmp_path / "hero-banner"
    hero.mkdir(parents=True)
    (tmp_path / "hero-banner.json").write_text("[]")
    (hero / "a.jpg").write_bytes(b"jpg")
    (hero / ".DS_Store").write_bytes(b"x")
    return tmp_path


@pytest.fixture
def cask(monkeypatch):
    """Fake CaskFS: existing paths, plus every write as (dest, src)."""
    state = {"existing": set(), "writes": []}
    monkeypatch.setattr(showcase_cask.fs, "exists", lambda path: path in state["existing"])
    monkeypatch.setattr(showcase_cask.fs, "write", lambda path, src=None: state["writes"].append((path, src)))
    return state


def test_init_cask_copies_files_into_the_gold_project(init_dir, cask):
    result = showcase_cask.init_cask({"init_dir": str(init_dir), "project": "digital-dev"})

    assert sorted(result["added"]) == [
        "cask:/gold/digital-dev/hero-banner.json",
        "cask:/gold/digital-dev/hero-banner/a.jpg",
    ]
    assert result["skipped"] == []
    assert (
        "cask:/gold/digital-dev/hero-banner/a.jpg",
        str(init_dir / "hero-banner" / "a.jpg"),
    ) in cask["writes"]


def test_init_cask_skips_files_already_in_cask(init_dir, cask):
    cask["existing"].add("cask:/gold/digital-prod/hero-banner.json")

    result = showcase_cask.init_cask({"init_dir": str(init_dir), "project": "digital-prod"})

    assert result["skipped"] == ["cask:/gold/digital-prod/hero-banner.json"]
    assert [w[0] for w in cask["writes"]] == ["cask:/gold/digital-prod/hero-banner/a.jpg"]


def test_init_cask_defaults_to_the_configured_dir_and_project(init_dir, cask, monkeypatch):
    monkeypatch.setitem(showcase_cask.CONFIG["showcase_cask"], "init_dir", str(init_dir))
    monkeypatch.setitem(showcase_cask.CONFIG["showcase"], "gold_project", "digital-x")

    result = showcase_cask.init_cask()

    assert "cask:/gold/digital-x/hero-banner.json" in result["added"]


def test_init_cask_requires_files(tmp_path, cask):
    with pytest.raises(FileNotFoundError):
        showcase_cask.init_cask({"init_dir": str(tmp_path)})
