import json

from digtk.lib import ai
from digtk.models import layout


def test_page_layout_returns_blocks(monkeypatch):
    monkeypatch.setattr(ai, "chat_completion", lambda message, opts={}: json.dumps({
        "blocks": [{"type": "heading", "text": "Title"}, {"type": "text", "text": "Body"}],
    }))

    result = layout.page_layout("cask:/silver/g/c/latest/item/ocr/page-000/prep.jpg")

    assert result == {"blocks": [{"type": "heading", "text": "Title"}, {"type": "text", "text": "Body"}]}


def test_page_layout_forwards_image_and_model_opt(monkeypatch):
    seen = {}

    def fake_chat(message, opts={}):
        seen["message"] = message
        seen["opts"] = opts
        return json.dumps({"blocks": []})

    monkeypatch.setattr(ai, "chat_completion", fake_chat)

    layout.page_layout("local/page.jpg", opts={"model": "qwen-vision"})

    assert seen["opts"] == {"image": "local/page.jpg", "model": "qwen-vision"}
    assert "layout as JSON" in seen["message"]


def test_page_layout_returns_none_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(ai, "chat_completion", lambda message, opts={}: "I cannot read this page.")

    assert layout.page_layout("local/page.jpg") is None


def test_page_layout_returns_none_when_blocks_missing(monkeypatch):
    monkeypatch.setattr(ai, "chat_completion", lambda message, opts={}: json.dumps({"not_blocks": []}))

    assert layout.page_layout("local/page.jpg") is None


def test_page_layout_returns_none_when_blocks_not_a_list(monkeypatch):
    monkeypatch.setattr(ai, "chat_completion", lambda message, opts={}: json.dumps({"blocks": "oops"}))

    assert layout.page_layout("local/page.jpg") is None
