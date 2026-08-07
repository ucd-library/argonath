"""
digtk.lib.http_harvest — generic HTTP page/sitemap harvesting helpers.

Primitives, composed by digtk.lib.rag_harvest and digtk.models.rag into the RAG
ingest pipeline:
  sitemap_urls()          — read a sitemap (or sitemap index) URL, return every page URL
  fetch_page()            — GET a page, return its decoded HTML
  fetch_page_conditional()  — GET a page, skipping the body if an ETag confirms no change
  extract_content()       — pull text out of predefined tags/selectors, HTML stripped

Uses httpx for requests (same choice as digtk.lib.fs.cask) and BeautifulSoup with
the stdlib 'html.parser' backend, so no new parser dependency (e.g. lxml) is needed.
"""
import re
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

from digtk.lib.config import CONFIG

_clients = {}


class HarvestError(RuntimeError):
    """An HTTP or XML failure while fetching or parsing a page or sitemap.

    :param str url: URL that failed.
    :param str reason: Human-readable failure reason.
    """

    def __init__(self, url, reason):
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason


def _get_client(opts={}):
    """Create and cache an httpx.Client per distinct (timeout, user_agent, transport).

    :param dict opts: 'timeout' (float seconds, default CONFIG["harvest"]["timeout_s"]),
        'user_agent' (str, default CONFIG["harvest"]["user_agent"]), 'transport'
        (httpx.BaseTransport override, for tests).
    :returns: Configured client.
    :rtype: httpx.Client
    """
    timeout = opts.get("timeout", CONFIG["harvest"]["timeout_s"])
    user_agent = opts.get("user_agent", CONFIG["harvest"]["user_agent"])
    transport = opts.get("transport")

    key = (timeout, user_agent, transport)
    if key not in _clients:
        _clients[key] = httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent}, transport=transport,
        )
    return _clients[key]


def _get(url, opts, headers=None):
    """Issue a GET request and return the raw response, wrapping transport failures.

    :param str url: URL to fetch.
    :param dict opts: 'timeout'/'user_agent'/'transport' — see _get_client().
    :param dict headers: Additional request headers.
    :returns: Raw response (any status code — callers decide what's an error).
    :rtype: httpx.Response
    :raises HarvestError: On a transport failure (connection/DNS/TLS/etc.).
    """
    client = _get_client(opts)
    try:
        return client.get(url, headers=headers or {})
    except httpx.TransportError as e:
        raise HarvestError(url, str(e)) from e


def fetch(url, opts={}):
    """GET a URL and return its raw response body.

    :param str url: URL to fetch.
    :param dict opts: 'timeout'/'user_agent'/'transport' — see _get_client().
    :returns: Response body.
    :rtype: bytes
    :raises HarvestError: On a transport failure or non-2xx response.
    """
    response = _get(url, opts)
    if response.status_code >= 400:
        raise HarvestError(url, f"HTTP {response.status_code}")
    return response.content


def _local_name(tag):
    """Strip an ElementTree tag's XML namespace, e.g. '{http://...}urlset' -> 'urlset'.

    :param str tag: Raw ElementTree tag name.
    :returns: Tag's local name.
    :rtype: str
    """
    return tag.rsplit("}", 1)[-1]


def _parse_sitemap(xml_bytes, url):
    """Parse one sitemap document into child sitemap URLs and/or page URLs.

    Namespace-agnostic: matches on local tag name only, since sitemaps are
    conventionally namespaced (xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    but some in the wild omit it.

    :param bytes xml_bytes: Raw sitemap XML.
    :param str url: Source URL, for error context.
    :returns: (child_sitemap_urls, page_urls) — a 'sitemapindex' document populates
        the first list (to be fetched and parsed in turn); a 'urlset' document
        populates the second. Exactly one list is ever non-empty.
    :rtype: tuple[list[str], list[str]]
    :raises HarvestError: If the XML is malformed.
    """
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as e:
        raise HarvestError(url, f"invalid sitemap XML: {e}") from e

    root_name = _local_name(root.tag)
    child_name = "sitemap" if root_name == "sitemapindex" else "url"
    locs = [
        loc.text.strip()
        for entry in root
        if _local_name(entry.tag) == child_name
        for loc in entry
        if _local_name(loc.tag) == "loc" and loc.text
    ]
    return (locs, []) if root_name == "sitemapindex" else ([], locs)


def sitemap_urls(url, opts={}):
    """Read a sitemap (or sitemap index) and return every page URL it lists.

    Sitemap index documents are followed recursively. Each sitemap URL is fetched
    at most once, so a self- or cross-referencing sitemap tree can't recurse forever.

    :param str url: Sitemap or sitemap index URL.
    :param dict opts: 'timeout'/'user_agent'/'transport' — see fetch().
    :returns: Page URLs, in document order, de-duplicated.
    :rtype: list[str]
    :raises HarvestError: If any sitemap in the tree fails to fetch or parse.
    """
    seen_sitemaps = set()
    seen_pages = set()
    pages = []
    queue = [url]

    while queue:
        current = queue.pop(0)
        if current in seen_sitemaps:
            continue
        seen_sitemaps.add(current)

        child_sitemaps, page_urls = _parse_sitemap(fetch(current, opts), current)
        queue.extend(child_sitemaps)
        for page_url in page_urls:
            if page_url not in seen_pages:
                seen_pages.add(page_url)
                pages.append(page_url)

    return pages


def fetch_page(url, opts={}):
    """Fetch a page and decode it to HTML text.

    :param str url: Page URL to fetch.
    :param dict opts: 'timeout'/'user_agent'/'transport' — see fetch().
    :returns: Decoded HTML.
    :rtype: str
    :raises HarvestError: On a transport failure or non-2xx response.
    """
    return fetch(url, opts).decode("utf-8", errors="replace")


def fetch_page_conditional(url, opts={}):
    """Fetch a page, skipping the body if its ETag confirms nothing has changed.

    Sends `opts['etag']` as an If-None-Match header — a single request either way:
    if the server confirms no change, it responds 304 with no body; otherwise it
    responds 200 with the body already in hand, so there's never a need for a
    separate follow-up GET.

    :param str url: Page URL to fetch.
    :param dict opts: 'etag' (str, optional prior ETag — omit for an unconditional
        fetch). 'timeout'/'user_agent'/'transport' — see fetch().
    :returns: {"not_modified": True, "etag": the etag passed in, "html": None,
        "raw": None, "content_type": None} if the server confirmed no change
        (HTTP 304); otherwise {"not_modified": False, "etag": the response's
        current ETag header (None if absent), "html": decoded response body,
        "raw": undecoded response body bytes, "content_type": the response's
        Content-Type header (None if absent)}.
    :rtype: dict
    :raises HarvestError: On a transport failure or a non-2xx/304 response.
    """
    headers = {"If-None-Match": opts["etag"]} if opts.get("etag") else None
    response = _get(url, opts, headers)

    if response.status_code == 304:
        return {
            "not_modified": True, "etag": opts.get("etag"),
            "html": None, "raw": None, "content_type": None,
        }
    if response.status_code >= 400:
        raise HarvestError(url, f"HTTP {response.status_code}")

    return {
        "not_modified": False,
        "etag": response.headers.get("etag"),
        "html": response.content.decode("utf-8", errors="replace"),
        "raw": response.content,
        "content_type": response.headers.get("content-type"),
    }


def resolve_tags(opts):
    """Resolve the 'tags' opt to a list of selector strings.

    :param dict opts: May contain 'tags' as a list[str] or a comma-separated str;
        falls back to CONFIG["harvest"]["content_tags"] (also comma-separated) if
        omitted.
    :returns: Non-empty selector strings, whitespace-stripped.
    :rtype: list[str]
    """
    tags = opts.get("tags") or CONFIG["harvest"]["content_tags"]
    if isinstance(tags, str):
        tags = tags.split(",")
    return [tag.strip() for tag in tags if tag.strip()]


def _normalize_whitespace(text):
    """Collapse runs of horizontal whitespace and excess blank lines.

    :param str text: Text to normalize.
    :returns: Normalized text with leading/trailing whitespace stripped.
    :rtype: str
    """
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_content(html, opts={}):
    """Extract text content from predefined tags/selectors, with all HTML stripped.

    <script>/<style>/<noscript> elements are dropped before extraction so their
    contents never leak into the returned text. Matched elements are joined in
    document order, separated by a blank line.

    :param str html: Raw HTML.
    :param dict opts: 'tags' — see resolve_tags().
    :returns: Extracted, whitespace-normalized plain text. Empty string if no
        selector in 'tags' matches anything in `html`.
    :rtype: str
    """
    tags = resolve_tags(opts)
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup.select("script, style, noscript"):
        junk.decompose()

    matches = soup.select(", ".join(tags))
    if not matches:
        return ""

    text = "\n\n".join(match.get_text(separator=" ", strip=True) for match in matches)
    return _normalize_whitespace(text)
