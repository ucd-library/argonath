import pytest

from digtk.lib.items import ItemLocation
from digtk.models.digital_website import finalize as fz
from digtk.models.digital_website import image, pdf

ARK = "ark:/87287/d7abc1234"


def _item(*filenames):
    return ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=[f"cask:/bronze/g/c/latest/item/{f}" for f in filenames],
    )


@pytest.fixture
def finalizers(monkeypatch):
    calls = []
    monkeypatch.setattr(image, "finalize", lambda ark, opts={}: calls.append("image") or {"status": "success"})
    monkeypatch.setattr(pdf, "finalize", lambda ark, opts={}: calls.append("pdf") or {"status": "success"})
    return calls


def test_finalize_runs_each_format_present(monkeypatch, finalizers):
    monkeypatch.setattr(fz.items, "resolve_target", lambda target, opts={}: _item("a.tif", "doc.pdf", "clip.mp4"))

    result = fz.finalize(ARK)

    assert finalizers == ["image", "pdf"]
    assert set(result) == {"status", "item", "image", "pdf"}


def test_finalize_only_images(monkeypatch, finalizers):
    monkeypatch.setattr(fz.items, "resolve_target", lambda target, opts={}: _item("a.tif"))

    assert "pdf" not in fz.finalize(ARK)
    assert finalizers == ["image"]


def test_finalize_rejects_items_with_nothing_to_finalize(monkeypatch, finalizers):
    monkeypatch.setattr(fz.items, "resolve_target", lambda target, opts={}: _item("clip.mp4"))

    with pytest.raises(ValueError, match="no image or PDF binaries"):
        fz.finalize(ARK)
