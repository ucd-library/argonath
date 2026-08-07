import httpx
import pytest

from digtk.lib import http_harvest
from digtk.lib.http_harvest import HarvestError


@pytest.fixture(autouse=True)
def isolate_http_harvest_module():
    """Reset http_harvest's client cache before each test, so tests don't leak
    cached httpx.Client instances (and their transports) across tests."""
    http_harvest._clients = {}


def client_for(handler):
    return {"transport": httpx.MockTransport(handler)}


SITEMAP_NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def urlset_xml(urls, namespaced=True):
    ns = f" {SITEMAP_NS}" if namespaced else ""
    entries = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0"?><urlset{ns}>{entries}</urlset>'.encode()


def sitemapindex_xml(sitemap_urls):
    entries = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in sitemap_urls)
    return f'<?xml version="1.0"?><sitemapindex {SITEMAP_NS}>{entries}</sitemapindex>'.encode()


# --- fetch -------------------------------------------------------------------


def test_fetch_returns_body_bytes():
    def handler(request):
        return httpx.Response(200, content=b"hello")

    assert http_harvest.fetch("http://x.test/a", client_for(handler)) == b"hello"


def test_fetch_sends_user_agent_header():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, content=b"ok")

    opts = {**client_for(handler), "user_agent": "custom-agent/1.0"}
    http_harvest.fetch("http://x.test/a", opts)
    assert seen["ua"] == "custom-agent/1.0"


def test_fetch_raises_harvest_error_on_http_error_status():
    def handler(request):
        return httpx.Response(404, content=b"not found")

    with pytest.raises(HarvestError, match="HTTP 404"):
        http_harvest.fetch("http://x.test/missing", client_for(handler))


def test_fetch_raises_harvest_error_on_transport_error():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(HarvestError, match="boom"):
        http_harvest.fetch("http://x.test/a", client_for(handler))


# --- sitemap_urls --------------------------------------------------------------


def test_sitemap_urls_parses_urlset():
    def handler(request):
        return httpx.Response(200, content=urlset_xml(["http://x.test/a", "http://x.test/b"]))

    urls = http_harvest.sitemap_urls("http://x.test/sitemap.xml", client_for(handler))
    assert urls == ["http://x.test/a", "http://x.test/b"]


def test_sitemap_urls_handles_non_namespaced_urlset():
    def handler(request):
        return httpx.Response(200, content=urlset_xml(["http://x.test/a"], namespaced=False))

    urls = http_harvest.sitemap_urls("http://x.test/sitemap.xml", client_for(handler))
    assert urls == ["http://x.test/a"]


def test_sitemap_urls_follows_sitemap_index_recursively():
    def handler(request):
        url = str(request.url)
        if url == "http://x.test/sitemap-index.xml":
            return httpx.Response(200, content=sitemapindex_xml([
                "http://x.test/sitemap-1.xml", "http://x.test/sitemap-2.xml",
            ]))
        if url == "http://x.test/sitemap-1.xml":
            return httpx.Response(200, content=urlset_xml(["http://x.test/a"]))
        if url == "http://x.test/sitemap-2.xml":
            return httpx.Response(200, content=urlset_xml(["http://x.test/b"]))
        raise AssertionError(f"unexpected fetch: {url}")

    urls = http_harvest.sitemap_urls("http://x.test/sitemap-index.xml", client_for(handler))
    assert urls == ["http://x.test/a", "http://x.test/b"]


def test_sitemap_urls_dedupes_pages_and_never_refetches_a_sitemap_twice():
    fetch_count = {"n": 0}

    def handler(request):
        url = str(request.url)
        if url == "http://x.test/sitemap-index.xml":
            return httpx.Response(200, content=sitemapindex_xml([
                "http://x.test/sitemap-1.xml", "http://x.test/sitemap-1.xml",
            ]))
        fetch_count["n"] += 1
        return httpx.Response(200, content=urlset_xml(["http://x.test/a", "http://x.test/a"]))

    urls = http_harvest.sitemap_urls("http://x.test/sitemap-index.xml", client_for(handler))
    assert urls == ["http://x.test/a"]
    assert fetch_count["n"] == 1


def test_sitemap_urls_raises_harvest_error_on_malformed_xml():
    def handler(request):
        return httpx.Response(200, content=b"not xml")

    with pytest.raises(HarvestError, match="invalid sitemap XML"):
        http_harvest.sitemap_urls("http://x.test/sitemap.xml", client_for(handler))


# --- fetch_page ----------------------------------------------------------------


def test_fetch_page_decodes_utf8_html():
    def handler(request):
        return httpx.Response(200, content="<p>café</p>".encode("utf-8"))

    html = http_harvest.fetch_page("http://x.test/page", client_for(handler))
    assert html == "<p>café</p>"


# --- fetch_page_conditional ------------------------------------------------------


def test_fetch_page_conditional_sends_if_none_match_from_etag_opt():
    seen = {}

    def handler(request):
        seen["if_none_match"] = request.headers.get("if-none-match")
        return httpx.Response(200, content=b"<p>hi</p>", headers={"etag": '"new"'})

    opts = {**client_for(handler), "etag": '"old"'}
    http_harvest.fetch_page_conditional("http://x.test/page", opts)
    assert seen["if_none_match"] == '"old"'


def test_fetch_page_conditional_omits_header_when_no_etag_opt():
    seen = {}

    def handler(request):
        seen["if_none_match"] = request.headers.get("if-none-match")
        return httpx.Response(200, content=b"<p>hi</p>")

    http_harvest.fetch_page_conditional("http://x.test/page", client_for(handler))
    assert seen["if_none_match"] is None


def test_fetch_page_conditional_returns_not_modified_on_304():
    def handler(request):
        return httpx.Response(304)

    opts = {**client_for(handler), "etag": '"old"'}
    result = http_harvest.fetch_page_conditional("http://x.test/page", opts)
    assert result == {
        "not_modified": True, "etag": '"old"', "html": None, "raw": None, "content_type": None,
    }


def test_fetch_page_conditional_returns_new_etag_and_html_on_200():
    def handler(request):
        return httpx.Response(200, content=b"<p>hi</p>", headers={"etag": '"new"'})

    opts = {**client_for(handler), "etag": '"old"'}
    result = http_harvest.fetch_page_conditional("http://x.test/page", opts)
    assert result == {
        "not_modified": False, "etag": '"new"', "html": "<p>hi</p>",
        "raw": b"<p>hi</p>", "content_type": None,
    }


def test_fetch_page_conditional_etag_is_none_when_response_has_no_etag_header():
    def handler(request):
        return httpx.Response(200, content=b"<p>hi</p>")

    result = http_harvest.fetch_page_conditional("http://x.test/page", client_for(handler))
    assert result["etag"] is None


def test_fetch_page_conditional_returns_content_type_header():
    def handler(request):
        return httpx.Response(200, content=b"<p>hi</p>", headers={"content-type": "text/html; charset=utf-8"})

    result = http_harvest.fetch_page_conditional("http://x.test/page", client_for(handler))
    assert result["content_type"] == "text/html; charset=utf-8"
    assert result["raw"] == b"<p>hi</p>"


def test_fetch_page_conditional_raises_harvest_error_on_http_error_status():
    def handler(request):
        return httpx.Response(500, content=b"boom")

    with pytest.raises(HarvestError, match="HTTP 500"):
        http_harvest.fetch_page_conditional("http://x.test/page", client_for(handler))


# --- extract_content -------------------------------------------------------------


def test_extract_content_pulls_text_from_default_tags():
    html = "<html><body><nav>skip</nav><article>Hello <b>world</b></article></body></html>"
    assert http_harvest.extract_content(html) == "Hello world"


def test_extract_content_uses_opts_tags_list():
    html = "<div class='body'>content</div><article>other</article>"
    text = http_harvest.extract_content(html, {"tags": [".body"]})
    assert text == "content"


def test_extract_content_uses_opts_tags_comma_string():
    html = "<div id='main'>content</div>"
    text = http_harvest.extract_content(html, {"tags": "#main"})
    assert text == "content"


def test_extract_content_strips_script_and_style():
    html = "<article>Text<script>evil()</script><style>.a{}</style></article>"
    assert http_harvest.extract_content(html) == "Text"


def test_extract_content_joins_multiple_matches_with_blank_line():
    html = "<article>First</article><main>Second</main>"
    assert http_harvest.extract_content(html) == "First\n\nSecond"


def test_extract_content_normalizes_whitespace():
    html = "<article>a    b\n\n\n\nc</article>"
    assert http_harvest.extract_content(html) == "a b\n\nc"


def test_extract_content_returns_empty_string_when_no_tag_matches():
    html = "<div>only a div</div>"
    assert http_harvest.extract_content(html) == ""
