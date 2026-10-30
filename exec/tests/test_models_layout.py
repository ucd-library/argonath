import json

from digtk.lib import ai
from digtk.models import layout


def test_page_layout_returns_blocks(monkeypatch):
    monkeypatch.setattr(ai, "chat_completion", lambda message, opts={}: json.dumps({
        "blocks": [{"type": "heading", "text": "Title"}, {"type": "text", "text": "Body"}],
    }))

    result = layout.page_layout("cask:/silver/g/c/latest/item/ocr/page-000/prep.jpg")

    assert result == {
        "version": layout.LAYOUT_VERSION,
        "blocks": [{"type": "heading", "text": "Title"}, {"type": "text", "text": "Body"}],
    }


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
    calls, warnings = [], []
    monkeypatch.setattr(ai, "chat_completion",
                        lambda message, opts={}: calls.append(message) or "I cannot read this page.")
    monkeypatch.setattr(layout.utils, "warn", warnings.append)

    assert layout.page_layout("local/page.jpg") is None
    assert len(calls) == 2  # original + one retry
    assert len(warnings) == 1 and "I cannot read this page." in warnings[0]


def test_page_layout_retries_unusable_output(monkeypatch):
    calls = []
    answers = iter(["```json\n[\n  [],\n  []\n]\n```", json.dumps({"blocks": [{"type": "text", "text": "Hi"}]})])

    def fake_chat(message, opts={}):
        calls.append((message, opts))
        return next(answers)

    monkeypatch.setattr(ai, "chat_completion", fake_chat)

    result = layout.page_layout("local/page.jpg")

    assert result["blocks"] == [{"type": "text", "text": "Hi"}]
    assert "not usable" in calls[1][0]
    assert calls[1][1] == {"image": "local/page.jpg"}


def test_page_layout_returns_none_when_blocks_missing(monkeypatch):
    monkeypatch.setattr(ai, "chat_completion", lambda message, opts={}: json.dumps({"not_blocks": []}))

    assert layout.page_layout("local/page.jpg") is None


def test_page_layout_returns_none_when_blocks_not_a_list(monkeypatch):
    calls = []
    monkeypatch.setattr(ai, "chat_completion",
                        lambda message, opts={}: calls.append(message) or json.dumps({"blocks": "oops"}))
    monkeypatch.setattr(layout.utils, "warn", lambda message: None)

    assert layout.page_layout("local/page.jpg") is None
    assert len(calls) == 2


def test_prompt_asks_for_levels_lists_lang_and_columns():
    prompt = layout._LAYOUT_PROMPT
    assert '"level": 1' in prompt
    assert '"type": "list"' in prompt
    assert '"lang"' in prompt
    assert '"columns"' in prompt


def test_page_layout_keeps_column_count_clamped(monkeypatch):
    monkeypatch.setattr(ai, "chat_completion", lambda message, opts={}: json.dumps({"columns": 9, "blocks": []}))
    assert layout.page_layout("p.jpg")["columns"] == 4


def test_page_layout_drops_unusable_column_count(monkeypatch):
    monkeypatch.setattr(ai, "chat_completion", lambda message, opts={}: json.dumps({"columns": "two", "blocks": []}))
    assert "columns" not in layout.page_layout("p.jpg")


def test_page_layout_forwards_max_tokens(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "chat_completion", lambda message, opts={}: seen.update(opts) or '{"blocks": []}')
    layout.page_layout("p.jpg", opts={"max_tokens": 4096})
    assert seen["max_tokens"] == 4096


def test_is_current():
    assert layout.is_current({"version": layout.LAYOUT_VERSION, "blocks": []})
    assert not layout.is_current({"blocks": []})
    assert not layout.is_current(None)
