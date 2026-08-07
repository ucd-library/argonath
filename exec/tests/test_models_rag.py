import array
import hashlib
import json
from datetime import date

import pytest

from digtk.lib import ai, fs, http_harvest
from digtk.models import rag


def hash_of(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeStore(dict):
    """A dict that also tracks recorded CaskFS derivative-link calls, so existing
    subscript-based assertions (fake_fs[path]) keep working unchanged."""
    def __init__(self):
        super().__init__()
        self.links = []


@pytest.fixture
def fake_fs(monkeypatch):
    """Stub out digtk.lib.fs so rag.py's logic is tested without a real filesystem/CaskFS."""
    store = FakeStore()

    def fake_exists(path):
        return path in store

    def fake_read(path, opts={}):
        return store[path]

    def fake_write(path, data=None, src=None, opts={}):
        store[path] = data
        return {"filepath": path}

    def fake_add_derivative_link(path, source_path, opts={}):
        store.links.append((path, source_path))
        return {"from_filepath": path, "to_filepath": source_path}

    monkeypatch.setattr(fs, "exists", fake_exists)
    monkeypatch.setattr(fs, "read", fake_read)
    monkeypatch.setattr(fs, "write", fake_write)
    monkeypatch.setattr(fs, "add_derivative_link", fake_add_derivative_link)
    return store


def stub_conditional_fetch_raw(monkeypatch, **overrides):
    response = {
        "not_modified": False, "etag": '"a"', "html": "<article>hello world</article>",
        "raw": b"<article>hello world</article>", "content_type": "text/html",
    }
    response.update(overrides)
    monkeypatch.setattr(http_harvest, "fetch_page_conditional", lambda url, opts: response)
    return response


def stub_conditional_fetch(monkeypatch, response):
    monkeypatch.setattr(http_harvest, "fetch_page_conditional", lambda url, opts: response)


def stub_extract(monkeypatch, text):
    monkeypatch.setattr(http_harvest, "extract_content", lambda html, opts: text)


def stub_embed(monkeypatch, vector=None):
    monkeypatch.setattr(ai, "embed", lambda texts, opts={}: [vector or [0.1, 0.2] for _ in texts])


# --- no dest: always a full harvest, no skip logic --------------------------------


def test_harvest_without_dest_always_fetches_and_embeds(monkeypatch, fake_fs):
    stub_conditional_fetch(monkeypatch, {"not_modified": False, "etag": '"a"', "html": "<article>hi</article>"})
    stub_extract(monkeypatch, "hi")
    stub_embed(monkeypatch)

    result = rag.harvest("http://x.test/page")

    assert result["url"] == "http://x.test/page"
    assert result["etag"] == '"a"'
    assert result["content_hash"] == hash_of("hi")
    assert result["chunks"][0]["text"] == "hi"
    assert result["skipped"] is False
    assert result["skip_reason"] is None
    assert result["saved"] is False
    assert result["dest"] is None
    assert not fake_fs


def test_harvest_without_dest_sends_no_etag_even_if_a_file_exists_elsewhere(monkeypatch, fake_fs):
    seen = {}

    def fake_conditional(url, opts):
        seen["etag"] = opts.get("etag")
        return {"not_modified": False, "etag": None, "html": "<article>hi</article>"}

    monkeypatch.setattr(http_harvest, "fetch_page_conditional", fake_conditional)
    stub_extract(monkeypatch, "hi")
    stub_embed(monkeypatch)

    rag.harvest("http://x.test/page")
    assert seen["etag"] is None


# --- dest given, no existing file: full harvest + save ----------------------------


def test_harvest_with_dest_and_no_prior_file_fetches_embeds_and_saves(monkeypatch, fake_fs):
    stub_conditional_fetch(monkeypatch, {"not_modified": False, "etag": '"a"', "html": "<article>hi</article>"})
    stub_extract(monkeypatch, "hi")
    stub_embed(monkeypatch)

    result = rag.harvest("http://x.test/page", {"dest": "cask:/silver/rag/page.json"})

    assert result["saved"] is True
    assert result["dest"] == "cask:/silver/rag/page.json"
    stored = json.loads(fake_fs["cask:/silver/rag/page.json"])
    assert stored["url"] == "http://x.test/page"
    assert stored["content_hash"] == hash_of("hi")
    assert "skipped" not in stored
    assert "saved" not in stored


# --- dest given, prior file exists: skip paths ------------------------------------


def test_harvest_skips_on_etag_match_without_extracting_or_embedding(monkeypatch, fake_fs):
    dest = "cask:/silver/rag/page.json"
    fake_fs[dest] = json.dumps({
        "url": "http://x.test/page", "etag": '"old"', "content_hash": hash_of("old text"),
        "model": "qwen3-embed:4b", "chunks": [{"text": "old text"}],
    }).encode("utf-8")

    seen = {}

    def fake_conditional(url, opts):
        seen["etag"] = opts.get("etag")
        return {"not_modified": True, "etag": opts.get("etag"), "html": None}

    monkeypatch.setattr(http_harvest, "fetch_page_conditional", fake_conditional)

    def fail_extract(html, opts):
        raise AssertionError("extract_content() should not be called on an ETag match")

    monkeypatch.setattr(http_harvest, "extract_content", fail_extract)

    def fail_embed(texts, opts={}):
        raise AssertionError("embed() should not be called on an ETag match")

    monkeypatch.setattr(ai, "embed", fail_embed)

    result = rag.harvest("http://x.test/page", {"dest": dest})

    assert seen["etag"] == '"old"'
    assert result["skipped"] is True
    assert result["skip_reason"] == "etag"
    assert result["saved"] is False
    assert result["chunks"] == [{"text": "old text"}]
    assert fake_fs[dest] == json.dumps({
        "url": "http://x.test/page", "etag": '"old"', "content_hash": hash_of("old text"),
        "model": "qwen3-embed:4b", "chunks": [{"text": "old text"}],
    }).encode("utf-8")


def test_harvest_skips_on_content_hash_match_without_embedding(monkeypatch, fake_fs):
    dest = "cask:/silver/rag/page.json"
    fake_fs[dest] = json.dumps({
        "url": "http://x.test/page", "etag": '"old"', "content_hash": hash_of("same text"),
        "model": "qwen3-embed:4b", "chunks": [{"text": "same text"}],
    }).encode("utf-8")

    stub_conditional_fetch(monkeypatch, {"not_modified": False, "etag": '"new"', "html": "<article>same text</article>"})
    stub_extract(monkeypatch, "same text")

    def fail_embed(texts, opts={}):
        raise AssertionError("embed() should not be called on a content-hash match")

    monkeypatch.setattr(ai, "embed", fail_embed)

    result = rag.harvest("http://x.test/page", {"dest": dest})

    assert result["skipped"] is True
    assert result["skip_reason"] == "content_hash"
    assert result["saved"] is False
    # the (possibly-changed) ETag is still reported back, even though nothing was saved
    assert result["etag"] == '"new"'
    assert result["chunks"] == [{"text": "same text"}]


def test_harvest_reembeds_and_resaves_when_content_hash_differs(monkeypatch, fake_fs):
    dest = "cask:/silver/rag/page.json"
    fake_fs[dest] = json.dumps({
        "url": "http://x.test/page", "etag": '"old"', "content_hash": hash_of("old text"),
        "model": "qwen3-embed:4b", "chunks": [{"text": "old text"}],
    }).encode("utf-8")

    stub_conditional_fetch(monkeypatch, {"not_modified": False, "etag": '"new"', "html": "<article>new text</article>"})
    stub_extract(monkeypatch, "new text")
    stub_embed(monkeypatch)

    result = rag.harvest("http://x.test/page", {"dest": dest})

    assert result["skipped"] is False
    assert result["saved"] is True
    assert result["content_hash"] == hash_of("new text")
    stored = json.loads(fake_fs[dest])
    assert stored["content_hash"] == hash_of("new text")
    assert stored["chunks"][0]["text"] == "new text"


def test_harvest_treats_malformed_prior_file_as_no_prior(monkeypatch, fake_fs):
    dest = "cask:/silver/rag/page.json"
    fake_fs[dest] = b"not json"

    seen = {}

    def fake_conditional(url, opts):
        seen["etag"] = opts.get("etag")
        return {"not_modified": False, "etag": '"a"', "html": "<article>hi</article>"}

    monkeypatch.setattr(http_harvest, "fetch_page_conditional", fake_conditional)
    stub_extract(monkeypatch, "hi")
    stub_embed(monkeypatch)

    result = rag.harvest("http://x.test/page", {"dest": dest})

    assert seen["etag"] is None
    assert result["skipped"] is False
    assert result["saved"] is True


# --- structured (bronze/silver) output mode ----------------------------------------

TODAY = date.today().isoformat()


def test_harvest_fs_mode_writes_bronze_and_silver_layout(monkeypatch, fake_fs):
    stub_conditional_fetch_raw(monkeypatch)
    stub_extract(monkeypatch, "hello world")
    stub_embed(monkeypatch, vector=[0.5, -0.25])

    result = rag.harvest("http://example.com/foo/bar", {"output": "fs"})

    bronze_dir = f"bronze/rag/example.com/{TODAY}"
    silver_dir = f"silver/rag/example.com/{TODAY}"

    assert result["output"] == "fs"
    assert result["group"] == "rag"
    assert result["skipped"] is False
    assert result["saved"] is True
    assert result["bronze"] == {"raw": f"{bronze_dir}/foo/bar", "meta": f"{bronze_dir}/foo/bar.json"}
    assert result["silver"] == [{"json": f"{silver_dir}/foo/bar-0.json", "bin": f"{silver_dir}/foo/bar-0.bin"}]

    assert fake_fs[f"{bronze_dir}/foo/bar"] == b"<article>hello world</article>"
    bronze_meta = json.loads(fake_fs[f"{bronze_dir}/foo/bar.json"])
    assert bronze_meta["url"] == "http://example.com/foo/bar"
    assert bronze_meta["etag"] == '"a"'
    assert bronze_meta["content_type"] == "text/html"
    assert bronze_meta["raw_content_hash"] == hashlib.sha256(b"<article>hello world</article>").hexdigest()
    assert bronze_meta["content_hash"] == hash_of("hello world")
    assert bronze_meta["chunk_count"] == 1

    chunk_meta = json.loads(fake_fs[f"{silver_dir}/foo/bar-0.json"])
    assert chunk_meta["url"] == "http://example.com/foo/bar"
    assert chunk_meta["text"] == "hello world"
    assert chunk_meta["content_hash"] == hash_of("hello world")
    assert chunk_meta["chunk_size"] == 1000
    assert chunk_meta["chunk_buffer"] == 200
    assert chunk_meta["model"] == "qwen3-embed:4b"
    assert chunk_meta["tags"] == ["article", "main"]
    assert "vector" not in chunk_meta

    assert fake_fs[f"{silver_dir}/foo/bar-0.bin"] == array.array("f", [0.5, -0.25]).tobytes()
    assert fake_fs.links == []


def test_harvest_cask_mode_writes_bronze_directly_and_silver_with_lineage(monkeypatch, fake_fs):
    stub_conditional_fetch_raw(monkeypatch)
    stub_extract(monkeypatch, "hello world")
    stub_embed(monkeypatch, vector=[0.5, -0.25])

    result = rag.harvest("http://example.com/foo/bar", {"output": "cask"})

    bronze_raw = f"cask:/bronze/rag/example.com/{TODAY}/foo/bar"
    bronze_meta = f"cask:/bronze/rag/example.com/{TODAY}/foo/bar.json"
    silver_json = f"cask:/silver/rag/example.com/{TODAY}/foo/bar-0.json"
    silver_bin = f"cask:/silver/rag/example.com/{TODAY}/foo/bar-0.bin"

    assert result["bronze"] == {"raw": bronze_raw, "meta": bronze_meta}
    assert result["silver"] == [{"json": silver_json, "bin": silver_bin}]
    assert fake_fs[bronze_raw] == b"<article>hello world</article>"
    assert json.loads(fake_fs[silver_json])["text"] == "hello world"
    assert fake_fs[silver_bin] == array.array("f", [0.5, -0.25]).tobytes()
    assert set(fake_fs.links) == {(silver_json, bronze_raw), (silver_bin, bronze_raw)}


def test_harvest_structured_uses_custom_group(monkeypatch, fake_fs):
    stub_conditional_fetch_raw(monkeypatch)
    stub_extract(monkeypatch, "hello world")
    stub_embed(monkeypatch)

    result = rag.harvest("http://example.com/foo", {"output": "fs", "group": "website-harvests"})

    assert result["group"] == "website-harvests"
    assert result["bronze"]["raw"] == f"bronze/website-harvests/example.com/{TODAY}/foo"


def test_harvest_structured_homepage_uses_index_html(monkeypatch, fake_fs):
    stub_conditional_fetch_raw(monkeypatch)
    stub_extract(monkeypatch, "home")
    stub_embed(monkeypatch)

    result = rag.harvest("http://example.com/", {"output": "fs"})
    assert result["bronze"]["raw"] == f"bronze/rag/example.com/{TODAY}/index.html"

    result = rag.harvest("http://example.com", {"output": "fs"})
    assert result["bronze"]["raw"] == f"bronze/rag/example.com/{TODAY}/index.html"


def test_harvest_structured_strips_trailing_slash_from_path(monkeypatch, fake_fs):
    stub_conditional_fetch_raw(monkeypatch)
    stub_extract(monkeypatch, "content")
    stub_embed(monkeypatch)

    result = rag.harvest("http://example.com/foo/bar/", {"output": "fs"})
    assert result["bronze"]["raw"] == f"bronze/rag/example.com/{TODAY}/foo/bar"


def test_harvest_structured_skips_on_etag_match_without_extracting_or_embedding(monkeypatch, fake_fs):
    bronze_meta_path = f"bronze/rag/example.com/{TODAY}/foo.json"
    fake_fs[bronze_meta_path] = json.dumps({
        "url": "http://example.com/foo", "etag": '"old"', "content_hash": hash_of("old text"),
        "model": "qwen3-embed:4b", "chunk_count": 1,
    }).encode("utf-8")

    seen = {}

    def fake_conditional(url, opts):
        seen["etag"] = opts.get("etag")
        return {"not_modified": True, "etag": opts.get("etag"), "html": None, "raw": None, "content_type": None}

    monkeypatch.setattr(http_harvest, "fetch_page_conditional", fake_conditional)

    def fail_extract(html, opts):
        raise AssertionError("extract_content() should not be called on an ETag match")

    monkeypatch.setattr(http_harvest, "extract_content", fail_extract)

    def fail_embed(texts, opts={}):
        raise AssertionError("embed() should not be called on an ETag match")

    monkeypatch.setattr(ai, "embed", fail_embed)

    result = rag.harvest("http://example.com/foo", {"output": "fs"})

    assert seen["etag"] == '"old"'
    assert result["skipped"] is True
    assert result["skip_reason"] == "etag"
    assert result["saved"] is False
    assert result["chunks"] is None
    assert result["silver"] == [{
        "json": f"silver/rag/example.com/{TODAY}/foo-0.json",
        "bin": f"silver/rag/example.com/{TODAY}/foo-0.bin",
    }]
    # bronze is untouched — still exactly the prior metadata
    assert json.loads(fake_fs[bronze_meta_path])["etag"] == '"old"'


def test_harvest_structured_skips_on_content_hash_match_without_embedding(monkeypatch, fake_fs):
    bronze_meta_path = f"bronze/rag/example.com/{TODAY}/foo.json"
    fake_fs[bronze_meta_path] = json.dumps({
        "url": "http://example.com/foo", "etag": '"old"', "content_hash": hash_of("same text"),
        "model": "qwen3-embed:4b", "chunk_count": 1,
    }).encode("utf-8")

    stub_conditional_fetch_raw(monkeypatch, etag='"new"', html="<article>same text</article>")
    stub_extract(monkeypatch, "same text")

    def fail_embed(texts, opts={}):
        raise AssertionError("embed() should not be called on a content-hash match")

    monkeypatch.setattr(ai, "embed", fail_embed)

    result = rag.harvest("http://example.com/foo", {"output": "fs"})

    assert result["skipped"] is True
    assert result["skip_reason"] == "content_hash"
    assert result["saved"] is False
    assert result["etag"] == '"new"'
    assert f"bronze/rag/example.com/{TODAY}/foo" not in fake_fs


def test_harvest_structured_skips_in_cask_mode_reconstructs_cask_silver_paths(monkeypatch, fake_fs):
    bronze_meta_path = f"cask:/bronze/rag/example.com/{TODAY}/foo.json"
    fake_fs[bronze_meta_path] = json.dumps({
        "url": "http://example.com/foo", "etag": '"old"', "content_hash": hash_of("old text"),
        "model": "qwen3-embed:4b", "chunk_count": 2,
    }).encode("utf-8")

    def fake_conditional(url, opts):
        return {"not_modified": True, "etag": opts.get("etag"), "html": None, "raw": None, "content_type": None}

    monkeypatch.setattr(http_harvest, "fetch_page_conditional", fake_conditional)

    result = rag.harvest("http://example.com/foo", {"output": "cask"})

    assert result["skipped"] is True
    assert result["silver"] == [
        {"json": f"cask:/silver/rag/example.com/{TODAY}/foo-0.json", "bin": f"cask:/silver/rag/example.com/{TODAY}/foo-0.bin"},
        {"json": f"cask:/silver/rag/example.com/{TODAY}/foo-1.json", "bin": f"cask:/silver/rag/example.com/{TODAY}/foo-1.bin"},
    ]
