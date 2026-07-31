import base64
from types import SimpleNamespace

import pytest

from digtk.lib import ai
from digtk.lib.config import CONFIG


@pytest.fixture(autouse=True)
def isolate_ai_module(monkeypatch):
    """Reset ai's client cache and clear the Samwise API key env var before each test,
    so tests don't leak cached clients across tests or depend on the host environment."""
    monkeypatch.setattr(ai, "_clients", {})
    monkeypatch.delenv(CONFIG["samwise"]["api_key_env"], raising=False)


def make_chat_response(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def make_embed_response(vectors):
    return SimpleNamespace(data=[SimpleNamespace(embedding=v) for v in vectors])


class FakeClient:
    """Minimal stand-in for an openai.OpenAI client, used to test chat_completion()/embed()
    without hitting the real Samwise API."""

    def __init__(self, chat_response=None, embed_response=None):
        self.chat_calls = []
        self.embed_calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat))
        self.embeddings = SimpleNamespace(create=self._create_embed)
        self._chat_response = chat_response
        self._embed_response = embed_response

    def _create_chat(self, **kwargs):
        self.chat_calls.append(kwargs)
        return self._chat_response

    def _create_embed(self, **kwargs):
        self.embed_calls.append(kwargs)
        return self._embed_response


# --- get_client ---------------------------------------------------------------


def test_get_client_raises_when_no_api_key():
    with pytest.raises(RuntimeError, match=CONFIG["samwise"]["api_key_env"]):
        ai.get_client()


def test_get_client_uses_env_api_key(monkeypatch):
    monkeypatch.setenv(CONFIG["samwise"]["api_key_env"], "env-token")
    client = ai.get_client()
    assert client.api_key == "env-token"


def test_get_client_opts_api_key_overrides_env(monkeypatch):
    monkeypatch.setenv(CONFIG["samwise"]["api_key_env"], "env-token")
    client = ai.get_client({"api_key": "opt-token"})
    assert client.api_key == "opt-token"


def test_get_client_default_base_url():
    client = ai.get_client({"api_key": "tok"})
    assert str(client.base_url).rstrip("/") == CONFIG["samwise"]["api_url"].rstrip("/")


def test_get_client_base_url_override():
    client = ai.get_client({"api_key": "tok", "base_url": "http://custom.example/api"})
    assert str(client.base_url).rstrip("/") == "http://custom.example/api"


def test_get_client_caches_by_base_url():
    c1 = ai.get_client({"api_key": "tok", "base_url": "http://a.example"})
    c2 = ai.get_client({"api_key": "tok", "base_url": "http://a.example"})
    assert c1 is c2


def test_get_client_different_base_urls_are_not_cached_together():
    c1 = ai.get_client({"api_key": "tok", "base_url": "http://a.example"})
    c2 = ai.get_client({"api_key": "tok", "base_url": "http://b.example"})
    assert c1 is not c2


def test_get_client_cache_hit_skips_api_key_check():
    # A base_url already cached is returned as-is — the api_key check is only
    # reached on a cache miss, so a later call with no key available still succeeds.
    first = ai.get_client({"api_key": "tok", "base_url": "http://a.example"})
    second = ai.get_client({"base_url": "http://a.example"})
    assert second is first


# --- chat_completion -----------------------------------------------------------


def test_chat_completion_wraps_plain_string_message(monkeypatch):
    fake = FakeClient(chat_response=make_chat_response("hello back"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    result = ai.chat_completion("hi")

    assert result == "hello back"
    assert fake.chat_calls[0]["model"] == ai.LLM_MODEL
    assert fake.chat_calls[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_chat_completion_model_override(monkeypatch):
    fake = FakeClient(chat_response=make_chat_response("x"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    ai.chat_completion("hi", {"model": "custom-model"})

    assert fake.chat_calls[0]["model"] == "custom-model"


def test_chat_completion_reads_image_path_and_builds_data_url(monkeypatch):
    fake = FakeClient(chat_response=make_chat_response("x"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)
    monkeypatch.setattr(ai.fs, "read", lambda path: b"raw-bytes")

    ai.chat_completion("describe this", {"image": "photo.jpg"})

    sent = fake.chat_calls[0]["messages"][0]
    assert sent["role"] == "user"
    assert sent["content"][0] == {"type": "text", "text": "describe this"}
    expected_b64 = base64.b64encode(b"raw-bytes").decode("utf-8")
    assert sent["content"][1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{expected_b64}"},
    }


def test_chat_completion_image_path_uses_extension_for_mimetype(monkeypatch):
    fake = FakeClient(chat_response=make_chat_response("x"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)
    monkeypatch.setattr(ai.fs, "read", lambda path: b"raw-bytes")

    ai.chat_completion("describe this", {"image": "photo.png"})

    url = fake.chat_calls[0]["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_chat_completion_image_path_reads_via_fs_module(monkeypatch):
    fake = FakeClient(chat_response=make_chat_response("x"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)
    reads = []
    monkeypatch.setattr(ai.fs, "read", lambda path: reads.append(path) or b"raw-bytes")

    ai.chat_completion("describe this", {"image": "cask:/bronze/g/c/item/photo.jpg"})

    assert reads == ["cask:/bronze/g/c/item/photo.jpg"]


def test_chat_completion_rejects_image_path_with_unknown_extension(monkeypatch):
    fake = FakeClient(chat_response=make_chat_response("x"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    with pytest.raises(ValueError, match="MIME type"):
        ai.chat_completion("describe this", {"image": "photo.xyz"})


def test_chat_completion_accepts_prebuilt_image_object(monkeypatch):
    fake = FakeClient(chat_response=make_chat_response("x"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    image = ai.Image(data=b"raw-bytes", mimetype="image/webp")
    ai.chat_completion("describe this", {"image": image})

    url = fake.chat_calls[0]["messages"][0]["content"][1]["image_url"]["url"]
    expected_b64 = base64.b64encode(b"raw-bytes").decode("utf-8")
    assert url == f"data:image/webp;base64,{expected_b64}"


def test_chat_completion_passes_through_prebuilt_message_list(monkeypatch):
    fake = FakeClient(chat_response=make_chat_response("x"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    ai.chat_completion(messages)

    assert fake.chat_calls[0]["messages"] == messages


def test_chat_completion_full_response_opt(monkeypatch):
    response = make_chat_response("hello back")
    fake = FakeClient(chat_response=response)
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    result = ai.chat_completion("hi", {"full_response": True})

    assert result is response


# --- embed ----------------------------------------------------------------------


def test_embed_returns_vectors(monkeypatch):
    fake = FakeClient(embed_response=make_embed_response([[0.1, 0.2], [0.3, 0.4]]))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    vectors = ai.embed(["a", "b"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert fake.embed_calls[0]["model"] == ai.EMBED_MODEL
    assert fake.embed_calls[0]["input"] == ["a", "b"]


def test_embed_model_override(monkeypatch):
    fake = FakeClient(embed_response=make_embed_response([[0.1]]))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    ai.embed(["a"], {"model": "custom-embed"})

    assert fake.embed_calls[0]["model"] == "custom-embed"


def test_embed_full_response_opt(monkeypatch):
    response = make_embed_response([[0.1]])
    fake = FakeClient(embed_response=response)
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    result = ai.embed(["a"], {"full_response": True})

    assert result is response


# --- chunk_text -------------------------------------------------------------------


def test_chunk_text_tiles_without_gaps_or_overlap():
    text = "0123456789"
    chunks = ai.chunk_text(text, {"chunk_size": 4, "buffer": 0})

    assert [(c["start"], c["end"]) for c in chunks] == [(0, 4), (4, 8), (8, 10)]
    assert [c["text"] for c in chunks] == ["0123", "4567", "89"]


def test_chunk_text_adds_buffer_before_and_after():
    text = "0123456789"
    chunks = ai.chunk_text(text, {"chunk_size": 4, "buffer": 2})

    # Core spans (start/end) are unaffected by buffer; only `text`/buffer_* widen.
    assert [(c["start"], c["end"]) for c in chunks] == [(0, 4), (4, 8), (8, 10)]
    assert chunks[0] == {"text": "012345", "start": 0, "end": 4, "buffer_start": 0, "buffer_end": 6}
    assert chunks[1] == {"text": "23456789", "start": 4, "end": 8, "buffer_start": 2, "buffer_end": 10}
    assert chunks[2] == {"text": "6789", "start": 8, "end": 10, "buffer_start": 6, "buffer_end": 10}


def test_chunk_text_clamps_buffer_at_string_bounds():
    text = "01234"
    chunks = ai.chunk_text(text, {"chunk_size": 5, "buffer": 100})

    assert chunks == [{"text": "01234", "start": 0, "end": 5, "buffer_start": 0, "buffer_end": 5}]


def test_chunk_text_empty_string_returns_no_chunks():
    assert ai.chunk_text("", {"chunk_size": 4, "buffer": 0}) == []


def test_chunk_text_uses_config_defaults():
    chunks = ai.chunk_text("x" * (CONFIG["search"]["chunk_size"] + 1))
    assert chunks[0]["end"] == CONFIG["search"]["chunk_size"]


def test_chunk_text_rejects_non_positive_chunk_size():
    with pytest.raises(ValueError, match="chunk_size"):
        ai.chunk_text("hello", {"chunk_size": 0})


def test_chunk_text_rejects_negative_buffer():
    with pytest.raises(ValueError, match="buffer"):
        ai.chunk_text("hello", {"chunk_size": 4, "buffer": -1})


# --- chunk_and_embed --------------------------------------------------------------


def test_chunk_and_embed_requires_exactly_one_of_text_or_path():
    with pytest.raises(ValueError, match="exactly one"):
        ai.chunk_and_embed()
    with pytest.raises(ValueError, match="exactly one"):
        ai.chunk_and_embed(text="hi", path="somewhere.txt")


def test_chunk_and_embed_embeds_each_chunk(monkeypatch):
    fake = FakeClient(embed_response=make_embed_response([[0.1], [0.2], [0.3]]))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    result = ai.chunk_and_embed(text="0123456789", opts={"chunk_size": 4, "buffer": 0})

    assert result["model"] == CONFIG["search"]["embed_model"]
    assert [c["vector"] for c in result["chunks"]] == [[0.1], [0.2], [0.3]]
    assert fake.embed_calls[0]["input"] == ["0123", "4567", "89"]
    assert fake.embed_calls[0]["model"] == CONFIG["search"]["embed_model"]


def test_chunk_and_embed_model_override(monkeypatch):
    fake = FakeClient(embed_response=make_embed_response([[0.1]]))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    ai.chunk_and_embed(text="hi", opts={"model": "custom-embed"})

    assert fake.embed_calls[0]["model"] == "custom-embed"


def test_chunk_and_embed_reads_path_via_fs_module(monkeypatch):
    fake = FakeClient(embed_response=make_embed_response([[0.1]]))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)
    monkeypatch.setattr(ai.fs, "read", lambda path: "hello".encode("utf-8"))

    result = ai.chunk_and_embed(path="cask:/bronze/g/c/item/ocr.txt")

    assert fake.embed_calls[0]["input"] == ["hello"]
    assert len(result["chunks"]) == 1


def test_chunk_and_embed_with_empty_text_makes_no_embed_call(monkeypatch):
    fake = FakeClient(embed_response=make_embed_response([]))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    result = ai.chunk_and_embed(text="")

    assert result["chunks"] == []
    assert fake.embed_calls == []
