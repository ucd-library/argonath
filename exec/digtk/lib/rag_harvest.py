"""
digtk.lib.rag_harvest — RAG ingest pipeline for harvested web content.

Built on digtk.lib.http_harvest (fetch/extract) and digtk.lib.ai (chunk + embed):
  list_urls()    — resolve a sitemap (or sitemap index) URL to every page URL,
                   optionally filtered by an include/exclude regex
  harvest_page() — fetch one page, extract its content, chunk + embed it

Wired to the CLI as `digtk harvest urls`/`digtk harvest page` — see digtk.commands.harvest.
"""
import re

from digtk.lib import ai, http_harvest


def list_urls(sitemap_url, opts={}):
    """Resolve a sitemap (or sitemap index) URL to every page URL it lists.

    :param str sitemap_url: Sitemap or sitemap index URL.
    :param dict opts: 'include_pattern'/'exclude_pattern' (str regex, applied to
        each URL after sitemap resolution — 'include_pattern' keeps only matches,
        'exclude_pattern' drops matches; both may be given together).
        'timeout'/'user_agent'/'transport' — see digtk.lib.http_harvest.fetch().
    :returns: Page URLs, in document order, de-duplicated.
    :rtype: list[str]
    :raises digtk.lib.http_harvest.HarvestError: If any sitemap in the tree fails
        to fetch or parse.
    :raises re.error: If 'include_pattern'/'exclude_pattern' is not valid regex.
    """
    urls = http_harvest.sitemap_urls(sitemap_url, opts)

    include = opts.get("include_pattern")
    if include:
        urls = [url for url in urls if re.search(include, url)]

    exclude = opts.get("exclude_pattern")
    if exclude:
        urls = [url for url in urls if not re.search(exclude, url)]

    return urls


def harvest_page(url, opts={}):
    """Fetch a page, extract its content, and chunk + embed it for RAG ingest.

    :param str url: Page URL to harvest.
    :param dict opts: 'tags' — see digtk.lib.http_harvest.extract_content().
        'chunk_size'/'buffer'/'model' — see digtk.lib.ai.chunk_text()/chunk_and_embed().
        'timeout'/'user_agent'/'transport' — see digtk.lib.http_harvest.fetch().
    :returns: {"url": source URL, "model": embedding model used, "chunks": [see
        digtk.lib.ai.chunk_and_embed()]}. "chunks" is [] if `url` had no content
        matching 'tags'.
    :rtype: dict
    :raises digtk.lib.http_harvest.HarvestError: If the page fails to fetch.
    """
    html = http_harvest.fetch_page(url, opts)
    text = http_harvest.extract_content(html, opts)
    result = ai.chunk_and_embed(text=text, opts=opts)
    return {"url": url, **result}
