import pytest

from digtk.models import alt_utils


# --- build_alt_prompt ---------------------------------------------------------

def test_prompt_includes_metadata_and_context():
    p = alt_utils.build_alt_prompt(
        {"title": "Wine menu", "description": "A signed menu", "date": "1973",
         "creator": "Society", "page": (2, 14)},
        opts={"collection_context": "Wine collection papers"})
    assert "Wine collection papers" in p
    assert "Wine menu" in p
    assert "page 2 of 14" in p
    assert "at most 150 characters" in p


def test_prompt_respects_people_and_transcribe_toggles():
    on = alt_utils.build_alt_prompt({}, opts={})
    off = alt_utils.build_alt_prompt(
        {}, opts={"name_people": False, "transcribe_text": False})
    assert "Name people identified" in on and "quote it exactly" in on
    assert "Do not name individuals" in off and "Do not transcribe" in off


def test_prompt_description_ask_toggle():
    assert '"description" to null' in alt_utils.build_alt_prompt({}, opts={})
    assert "catalog description" in alt_utils.build_alt_prompt(
        {}, opts={"want_description": True})


def test_prompt_long_description_toggle():
    assert '"long_description" to null' in alt_utils.build_alt_prompt({}, opts={})
    p = alt_utils.build_alt_prompt({}, opts={"want_long": True})
    assert "information-dense" in p
    assert '"long_description": "..." or null' in p


# --- parse_model_json ---------------------------------------------------------

def test_parse_plain_json():
    assert alt_utils.parse_model_json('{"alt": "A menu."}') == {"alt": "A menu."}


def test_parse_fenced_json():
    raw = 'Sure! Here you go:\n```json\n{"alt": "A menu.", "description": null}\n```'
    assert alt_utils.parse_model_json(raw)["alt"] == "A menu."


def test_parse_json_with_surrounding_prose():
    raw = 'The answer is {"alt": "A menu."} as requested.'
    assert alt_utils.parse_model_json(raw)["alt"] == "A menu."


def test_parse_no_json_raises():
    with pytest.raises(ValueError, match="no JSON object"):
        alt_utils.parse_model_json("I cannot describe this image.")


def test_parse_broken_json_raises():
    with pytest.raises(ValueError, match="invalid JSON"):
        alt_utils.parse_model_json('{"alt": missing quotes}')


# --- validate_alt -------------------------------------------------------------

def test_validate_clean_alt():
    assert alt_utils.validate_alt("Six wine glasses on a table.") == []


def test_validate_empty():
    assert alt_utils.validate_alt("   ") == ["EMPTY"]


def test_validate_banned_openers():
    for bad in ("Image of a menu.", "A photograph showing six glasses.",
                "photo of the binder", "Scan of a page"):
        assert "BANNED_OPENER" in alt_utils.validate_alt(bad), bad


def test_validate_length_grace_margin():
    assert alt_utils.validate_alt("x" * 165) == []          # within 150 + 20
    assert alt_utils.validate_alt("x" * 171) == ["TOO_LONG"]


# --- generate_alt (mocked model) ----------------------------------------------

def test_generate_alt_happy_path(monkeypatch):
    calls = []

    def fake_chat(message, opts={}):
        calls.append((message, opts))
        return '{"alt": "Six wine glasses on a table.", "description": null}'

    monkeypatch.setattr(alt_utils.ai, "chat_completion", fake_chat)
    alt, desc, _long, flags = alt_utils.generate_alt("page.jpg", {"title": "Menu"})
    assert alt == "Six wine glasses on a table."
    assert desc is None
    assert flags == []
    assert calls[0][1]["image"] == "page.jpg"


def test_generate_alt_returns_description_when_asked(monkeypatch):
    monkeypatch.setattr(
        alt_utils.ai, "chat_completion",
        lambda m, o={}: '{"alt": "A menu.", "description": "A 1973 dinner menu."}')
    alt, desc, _long, flags = alt_utils.generate_alt(
        "p.jpg", {}, opts={"want_description": True})
    assert desc == "A 1973 dinner menu."


def test_generate_alt_retries_long_answer(monkeypatch):
    answers = iter([
        '{"alt": "' + "x" * 200 + '", "description": null}',
        '{"alt": "Short version.", "description": null}',
    ])
    monkeypatch.setattr(alt_utils.ai, "chat_completion", lambda m, o={}: next(answers))
    alt, desc, _long, flags = alt_utils.generate_alt("p.jpg", {})
    assert alt == "Short version."
    assert flags == []


def test_generate_alt_keeps_long_answer_when_retry_fails(monkeypatch):
    answers = iter([
        '{"alt": "' + "x" * 200 + '", "description": null}',
        "no json here",
    ])
    monkeypatch.setattr(alt_utils.ai, "chat_completion", lambda m, o={}: next(answers))
    alt, desc, _long, flags = alt_utils.generate_alt("p.jpg", {})
    assert len(alt) == 200
    assert flags == ["TOO_LONG"]


def test_generate_alt_unparseable_output(monkeypatch):
    calls, warnings = [], []

    def fake_chat(message, opts={}):
        calls.append(message)
        return "I refuse."

    monkeypatch.setattr(alt_utils.ai, "chat_completion", fake_chat)
    monkeypatch.setattr(alt_utils.utils, "warn", warnings.append)
    alt, desc, _long, flags = alt_utils.generate_alt("p.jpg", {})
    assert (alt, desc) == ("", None)
    assert flags == ["ERROR:unparseable-model-output"]
    assert len(calls) == 2  # original + one JSON retry
    assert len(warnings) == 1 and "I refuse." in warnings[0]


def test_generate_alt_retries_unparseable_output(monkeypatch):
    calls = []
    answers = iter([
        '{"alt": "A sign reading "Open"."}',  # unescaped quotes
        '{"alt": "A sign reading \\"Open\\"."}',
    ])

    def fake_chat(message, opts={}):
        calls.append((message, opts))
        return next(answers)

    monkeypatch.setattr(alt_utils.ai, "chat_completion", fake_chat)
    alt, _desc, _long, flags = alt_utils.generate_alt("p.jpg", {})
    assert alt == 'A sign reading "Open".'
    assert flags == []
    assert "not valid JSON" in calls[1][0]
    assert calls[1][1]["image"] == "p.jpg"


def test_generate_alt_returns_long_description_when_asked(monkeypatch):
    monkeypatch.setattr(
        alt_utils.ai, "chat_completion",
        lambda m, o={}: '{"alt": "A map of Davis.", "description": null, '
                        '"long_description": "  Streets run north-south.  "}')
    alt, desc, long_desc, flags = alt_utils.generate_alt("p.jpg", {}, opts={"want_long": True})
    assert long_desc == "Streets run north-south."
    assert flags == []


def test_generate_alt_blank_long_description_is_none(monkeypatch):
    monkeypatch.setattr(
        alt_utils.ai, "chat_completion",
        lambda m, o={}: '{"alt": "A photo.", "long_description": "  "}')
    _alt, _desc, long_desc, _flags = alt_utils.generate_alt("p.jpg", {})
    assert long_desc is None


def test_generate_alt_forwards_chat_params(monkeypatch):
    seen = {}

    def fake_chat(message, opts={}):
        seen.update(opts)
        return '{"alt": "A menu."}'

    monkeypatch.setattr(alt_utils.ai, "chat_completion", fake_chat)
    alt_utils.generate_alt("p.jpg", {}, opts={"max_tokens": 2000, "temperature": 0.0, "model": "m"})
    assert seen == {"image": "p.jpg", "max_tokens": 2000, "temperature": 0.0, "model": "m"}
