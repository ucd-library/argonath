import pytest

from digtk.lib import ai, http_harvest, rag_harvest


# --- list_urls -----------------------------------------------------------------


def test_list_urls_passes_through_sitemap_urls(monkeypatch):
    monkeypatch.setattr(http_harvest, "sitemap_urls", lambda url, opts: ["http://x.test/a", "http://x.test/b"])
    assert rag_harvest.list_urls("http://x.test/sitemap.xml") == ["http://x.test/a", "http://x.test/b"]


def test_list_urls_applies_include_pattern(monkeypatch):
    monkeypatch.setattr(
        http_harvest, "sitemap_urls",
        lambda url, opts: ["http://x.test/blog/a", "http://x.test/about"],
    )
    urls = rag_harvest.list_urls("http://x.test/sitemap.xml", {"include_pattern": r"/blog/"})
    assert urls == ["http://x.test/blog/a"]


def test_list_urls_applies_exclude_pattern(monkeypatch):
    monkeypatch.setattr(
        http_harvest, "sitemap_urls",
        lambda url, opts: ["http://x.test/blog/a", "http://x.test/tag/foo"],
    )
    urls = rag_harvest.list_urls("http://x.test/sitemap.xml", {"exclude_pattern": r"/tag/"})
    assert urls == ["http://x.test/blog/a"]


def test_list_urls_applies_both_patterns_together(monkeypatch):
    monkeypatch.setattr(
        http_harvest, "sitemap_urls",
        lambda url, opts: ["http://x.test/blog/a", "http://x.test/blog/tag/foo", "http://x.test/about"],
    )
    urls = rag_harvest.list_urls(
        "http://x.test/sitemap.xml", {"include_pattern": r"/blog/", "exclude_pattern": r"/tag/"},
    )
    assert urls == ["http://x.test/blog/a"]


def test_list_urls_raises_on_invalid_regex(monkeypatch):
    monkeypatch.setattr(http_harvest, "sitemap_urls", lambda url, opts: ["http://x.test/a"])
    with pytest.raises(Exception):
        rag_harvest.list_urls("http://x.test/sitemap.xml", {"include_pattern": "("})


# --- harvest_page ----------------------------------------------------------------


def test_harvest_page_fetches_extracts_and_embeds(monkeypatch):
    monkeypatch.setattr(http_harvest, "fetch_page", lambda url, opts: "<html>raw</html>")
    monkeypatch.setattr(http_harvest, "extract_content", lambda html, opts: "hello world")
    monkeypatch.setattr(ai, "embed", lambda texts, opts={}: [[0.1, 0.2] for _ in texts])

    result = rag_harvest.harvest_page("http://x.test/page")

    assert result["url"] == "http://x.test/page"
    assert result["chunks"][0]["text"] == "hello world"
    assert result["chunks"][0]["vector"] == [0.1, 0.2]


def test_harvest_page_passes_opts_through_to_fetch_and_extract(monkeypatch):
    seen = {}

    def fake_fetch_page(url, opts):
        seen["fetch_opts"] = opts
        return "<html></html>"

    def fake_extract_content(html, opts):
        seen["extract_opts"] = opts
        return ""

    monkeypatch.setattr(http_harvest, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(http_harvest, "extract_content", fake_extract_content)

    opts = {"tags": "article", "timeout": 5, "user_agent": "test-agent"}
    rag_harvest.harvest_page("http://x.test/page", opts)

    assert seen["fetch_opts"] is opts
    assert seen["extract_opts"] is opts


def test_harvest_page_returns_no_chunks_and_skips_embed_when_no_content_extracted(monkeypatch):
    monkeypatch.setattr(http_harvest, "fetch_page", lambda url, opts: "<html></html>")
    monkeypatch.setattr(http_harvest, "extract_content", lambda html, opts: "")

    def fail_embed(texts, opts={}):
        raise AssertionError("embed() should not be called when there is no content")

    monkeypatch.setattr(ai, "embed", fail_embed)

    result = rag_harvest.harvest_page("http://x.test/empty")
    assert result["url"] == "http://x.test/empty"
    assert result["chunks"] == []
    assert result["model"]
