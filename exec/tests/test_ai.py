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


def as_stream(response):
    """Re-shape a non-streamed chat response the way a streamed call returns it:
    an iterable of chunks whose delta carries the message's content/reasoning,
    split in two so joining is exercised."""
    message = response.choices[0].message
    chunks = []
    for field in ("content", "reasoning_content"):
        text = getattr(message, field, None)
        if text:
            mid = len(text) // 2
            for part in (text[:mid], text[mid:]):
                chunks.append(SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(**{field: part}))]))
    return chunks


def respond(kwargs, response):
    """What a client double should return for a create() call with these kwargs."""
    return as_stream(response) if kwargs.get("stream") else response


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
        return respond(kwargs, self._chat_response)

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


def test_chat_completion_strips_think_blocks(monkeypatch):
    fake = FakeClient(chat_response=make_chat_response("<think>hmm let me see</think>The answer is 4."))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    assert ai.chat_completion("2+2?") == "The answer is 4."


def test_chat_completion_falls_back_to_reasoning_content(monkeypatch):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=None, reasoning_content="<think>x</think>reasoned answer",
    ))])
    fake = FakeClient(chat_response=response)
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    assert ai.chat_completion("hi") == "reasoned answer"


def test_chat_completion_retries_and_eventually_succeeds(monkeypatch):
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class FlakyClient:
        chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: _flaky(calls, kw)))

    def _flaky(calls, kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("wedged")
        return respond(kw, make_chat_response("ok"))

    monkeypatch.setattr(ai, "get_client", lambda opts: FlakyClient())

    assert ai.chat_completion("hi") == "ok"
    assert calls["n"] == 3


def test_chat_completion_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    monkeypatch.setitem(CONFIG["samwise"], "retries", 2)

    class AlwaysFailsClient:
        chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(ConnectionError("down"))))

    monkeypatch.setattr(ai, "get_client", lambda opts: AlwaysFailsClient())

    with pytest.raises(RuntimeError, match="failed after 2 tries"):
        ai.chat_completion("hi")


def test_chat_completion_waits_out_maintenance_window(monkeypatch):
    monkeypatch.setattr(ai, "_wait_out_maintenance", lambda: seen.append("waited"))
    seen = []
    fake = FakeClient(chat_response=make_chat_response("ok"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    ai.chat_completion("hi")

    assert seen == ["waited"]


def test_chat_completion_skips_maintenance_wait_for_full_response(monkeypatch):
    def fail():
        raise AssertionError("should not wait for full_response calls")
    monkeypatch.setattr(ai, "_wait_out_maintenance", fail)
    fake = FakeClient(chat_response=make_chat_response("ok"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    ai.chat_completion("hi", {"full_response": True})


# --- streaming ---------------------------------------------------------------------


def test_chat_completion_streams_by_default_and_joins_chunks(monkeypatch):
    fake = FakeClient(chat_response=make_chat_response("<think>hmm</think>A long transcript."))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    assert ai.chat_completion("hi") == "A long transcript."
    assert fake.chat_calls[0]["stream"] is True


def test_chat_completion_streamed_falls_back_to_reasoning_deltas(monkeypatch):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=None, reasoning_content="reasoned answer",
    ))])
    monkeypatch.setattr(ai, "get_client", lambda opts: FakeClient(chat_response=response))

    assert ai.chat_completion("hi") == "reasoned answer"


def test_chat_completion_stream_setting_off_uses_a_single_request(monkeypatch):
    monkeypatch.setitem(CONFIG["samwise"], "stream", False)
    fake = FakeClient(chat_response=make_chat_response("ok"))
    monkeypatch.setattr(ai, "get_client", lambda opts: fake)

    assert ai.chat_completion("hi") == "ok"
    assert "stream" not in fake.chat_calls[0]


def test_streamed_chunks_with_no_choices_are_ignored(monkeypatch):
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello "))]),
        SimpleNamespace(choices=[]),  # e.g. a trailing usage-only chunk
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="world"))]),
    ]

    class StreamClient:
        chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: iter(chunks)))

    monkeypatch.setattr(ai, "get_client", lambda opts: StreamClient())

    assert ai.chat_completion("hi") == "Hello world"


class ClockedStream:
    """A streamed response that advances a fake monotonic clock `step_s` per chunk
    and records whether it was closed."""

    def __init__(self, clock, step_s, n):
        self.clock, self.step_s, self.n, self.closed = clock, step_s, n, False

    def __iter__(self):
        for _ in range(self.n):
            self.clock["now"] += self.step_s
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="abcd"))])

    def close(self):
        self.closed = True


def test_streamed_call_past_max_call_s_raises_without_retrying(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(ai.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(ai.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("must not retry")))
    monkeypatch.setitem(CONFIG["samwise"], "max_call_s", 100.0)
    streams = []

    def create(**kw):
        streams.append(ClockedStream(clock, step_s=40.0, n=10))
        return streams[-1]

    monkeypatch.setattr(ai, "get_client", lambda opts: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))

    with pytest.raises(ai.CallTooLong, match=r"after 100s \(12 chars received\)"):
        ai.chat_completion("hi")
    assert len(streams) == 1
    assert streams[0].closed is True


def test_long_but_steady_stream_completes_and_logs_progress(monkeypatch, capsys):
    clock = {"now": 0.0}
    monkeypatch.setattr(ai.time, "monotonic", lambda: clock["now"])
    monkeypatch.setitem(CONFIG["samwise"], "max_call_s", 900.0)
    stream = ClockedStream(clock, step_s=20.0, n=20)  # 400s total, well past the old 180s read timeout
    monkeypatch.setattr(ai, "get_client", lambda opts: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kw: stream))))

    assert ai.chat_completion("hi") == "abcd" * 20
    assert stream.closed is True
    err = capsys.readouterr().err
    assert "digtk: Samwise still responding (40s, 8 chars so far)" in err


def test_streamed_call_logs_a_full_timeline(monkeypatch, capsys):
    clock = {"now": 0.0}
    monkeypatch.setattr(ai.time, "monotonic", lambda: clock["now"])
    monkeypatch.setitem(CONFIG["samwise"], "progress_every_s", 10.0)
    stream = ClockedStream(clock, step_s=6.0, n=5)  # chunks at 6, 12, 18, 24, 30s
    monkeypatch.setattr(ai, "get_client", lambda opts: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kw: stream))))

    ai.chat_completion("hi")

    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("digtk: Samwise")]
    assert lines == [
        "digtk: Samwise first response after 6s",
        "digtk: Samwise still responding (12s, 8 chars so far)",
        "digtk: Samwise still responding (24s, 16 chars so far)",
        "digtk: Samwise done in 30s (20 chars)",
    ]


def test_first_response_log_waits_for_actual_text(monkeypatch, capsys):
    """A buffering proxy shows up as a late 'first response' — empty keep-alive
    chunks before the text must not count as the first response."""
    clock = {"now": 0.0}
    monkeypatch.setattr(ai.time, "monotonic", lambda: clock["now"])
    monkeypatch.setitem(CONFIG["samwise"], "progress_every_s", 1000.0)

    def chunks():
        for _ in range(3):
            clock["now"] += 20.0
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))])
        clock["now"] += 20.0
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="all at once"))])

    monkeypatch.setattr(ai, "get_client", lambda opts: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kw: chunks()))))

    ai.chat_completion("hi")

    err = capsys.readouterr().err
    assert "digtk: Samwise first response after 80s" in err
    assert "digtk: Samwise done in 80s (11 chars)" in err


# --- _wait_out_maintenance -------------------------------------------------------


def test_wait_out_maintenance_noop_when_unconfigured(monkeypatch):
    monkeypatch.setitem(CONFIG["samwise"], "maint_start", "")
    monkeypatch.setitem(CONFIG["samwise"], "maint_end", "")
    monkeypatch.setattr(ai.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("should not sleep")))

    ai._wait_out_maintenance()  # must return immediately, no error


def test_wait_out_maintenance_sleeps_then_exits_once_outside_window(monkeypatch):
    monkeypatch.setitem(CONFIG["samwise"], "maint_start", "23:50")
    monkeypatch.setitem(CONFIG["samwise"], "maint_end", "01:45")

    # First "now" is inside the overnight window (00:30); second is outside (02:00).
    times = iter([
        SimpleNamespace(hour=0, minute=30),
        SimpleNamespace(hour=2, minute=0),
    ])
    monkeypatch.setattr(ai.datetime, "datetime", SimpleNamespace(now=lambda: next(times)))
    sleeps = []
    monkeypatch.setattr(ai.time, "sleep", lambda s: sleeps.append(s))

    ai._wait_out_maintenance()

    assert sleeps == [60]


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
