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


def test_chat_completion_encodes_bytes_image(monkeypatch):
    fake = FakeClient(chat_response=make_chat_response("x"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    ai.chat_completion("describe this", {"image": b"raw-bytes"})

    sent = fake.chat_calls[0]["messages"][0]
    assert sent["content"] == "describe this"
    assert sent["image"] == base64.b64encode(b"raw-bytes").decode("utf-8")


def test_chat_completion_passes_through_str_image_unchanged(monkeypatch):
    fake = FakeClient(chat_response=make_chat_response("x"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    ai.chat_completion("describe this", {"image": "already-base64=="})

    assert fake.chat_calls[0]["messages"][0]["image"] == "already-base64=="


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
