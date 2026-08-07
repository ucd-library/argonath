"""
digtk harvest — RAG ingest from harvested web content.

Subcommands:
  urls  — resolve a sitemap (or sitemap index) to every page URL it lists
  page  — crawl one page, extract its content, and chunk + embed it for RAG ingest
          (skipping re-embedding if --output already holds an unchanged result)
"""
import re
from typing import Annotated

import typer

from digtk.lib import rag_harvest, utils
from digtk.lib.config import (
    CONFIG,
    HarvestContentTagsOption,
    HarvestGroupOption,
    HarvestTimeoutOption,
    HarvestUserAgentOption,
    SearchChunkBufferOption,
    SearchChunkSizeOption,
    SearchEmbedModelOption,
)
from digtk.lib.http_harvest import HarvestError
from digtk.models import rag as rag_model

app = typer.Typer(help="RAG ingest from harvested web content.", no_args_is_help=True)


def _request_opts(user_agent, timeout):
    """
    :param str user_agent: --user-agent value.
    :param float timeout: --timeout value.
    :returns: opts dict for http_harvest.fetch()/fetch_page().
    :rtype: dict
    """
    return {"user_agent": user_agent, "timeout": timeout}


@app.command()
def urls(
    sitemap_url: Annotated[str, typer.Argument(help="Sitemap or sitemap index URL.")],
    include: Annotated[str | None, typer.Option(
        "--include", help="Regex — keep only URLs matching this pattern.",
    )] = None,
    exclude: Annotated[str | None, typer.Option(
        "--exclude", help="Regex — drop URLs matching this pattern.",
    )] = None,
    user_agent: HarvestUserAgentOption = CONFIG["harvest"]["user_agent"],
    timeout: HarvestTimeoutOption = CONFIG["harvest"]["timeout_s"],
):
    """Resolve a sitemap (or sitemap index) to every page URL it lists."""
    opts = {**_request_opts(user_agent, timeout), "include_pattern": include, "exclude_pattern": exclude}

    try:
        found = rag_harvest.list_urls(sitemap_url, opts=opts)
    except HarvestError as e:
        utils.error(str(e))
    except re.error as e:
        utils.error(f"Invalid --include/--exclude regex: {e}")

    utils.log(f"urls: {len(found)}")
    utils.result({"status": "success", "urls": found})


@app.command()
def page(
    url: Annotated[str, typer.Argument(help="Page URL to harvest.")],
    tags: HarvestContentTagsOption = CONFIG["harvest"]["content_tags"],
    chunk_size: SearchChunkSizeOption = CONFIG["search"]["chunk_size"],
    chunk_buffer: SearchChunkBufferOption = CONFIG["search"]["chunk_buffer"],
    model: SearchEmbedModelOption = None,
    user_agent: HarvestUserAgentOption = CONFIG["harvest"]["user_agent"],
    timeout: HarvestTimeoutOption = CONFIG["harvest"]["timeout_s"],
    output: Annotated[str | None, typer.Option(
        "--output", help="'fs' writes the harvested page into a bronze/silver medallion "
        "layout on local disk (relative to cwd). 'cask' writes the same layout into "
        "CaskFS, with derivative lineage from every silver file back to its bronze "
        "source. Any other value is treated as a local path or 'cask:/'-prefixed path "
        "to persist the result as a single JSON blob there instead. Either way, skips "
        "re-embedding when the page's ETag or extracted content is unchanged. Omit to "
        "just harvest and print the result.",
    )] = None,
    group: HarvestGroupOption = CONFIG["harvest"]["group"],
):
    """Crawl one page, extract its content, and chunk + embed it for RAG ingest."""
    opts = {
        **_request_opts(user_agent, timeout),
        "tags": tags, "chunk_size": chunk_size, "buffer": chunk_buffer,
    }
    if model:
        opts["model"] = model
    if output in ("fs", "cask"):
        opts["output"] = output
        opts["group"] = group
    elif output:
        opts["dest"] = output

    try:
        harvested = rag_model.harvest(url, opts=opts)
    except HarvestError as e:
        utils.error(str(e))

    if harvested["skipped"]:
        utils.log(f"skipped ({harvested['skip_reason']} unchanged): {url}")
    else:
        utils.log(f"model: {harvested['model']}")
        utils.log(f"chunks: {len(harvested['chunks'])}")
        if harvested.get("output") in ("fs", "cask"):
            utils.log(f"bronze: {harvested['bronze']['raw']}")

    utils.result({"status": "success", **harvested})
