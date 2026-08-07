"""
digtk.models.rag — harvest a URL's content, embed it, and optionally persist the
result to the local filesystem or CaskFS.

Two independent persistence shapes, selected by `opts['output']`:

  - Unset, or a path (`opts['dest']`) — the original single-JSON-blob mode: wraps
    digtk.lib.http_harvest/digtk.lib.ai with a skip-if-unchanged fast path, keyed off
    a prior result already stored at `opts['dest']`.
  - 'fs' or 'cask' (`opts['output']`) — the medallion-layout mode (see
    docs/cask-conventions.md): writes the raw fetched response + a metadata sidecar to
    bronze (`/bronze/[group]/[host]/[date]/[website path]`), and one .bin (raw float32
    embedding vector) + .json (chunk fields, minus the vector) pair per chunk to silver
    (`/silver/[group]/[host]/[date]/[website path]-[chunk index]`). 'cask' additionally
    records CaskFS derivative-link lineage from every silver file back to the bronze
    raw response (digtk.lib.derivative); 'fs' writes cwd-relative local paths with no
    lineage (digtk.lib.fs.add_derivative_link is CaskFS-only).

Both modes share the same skip-if-unchanged strategy, cheapest-first:

  1. ETag — a conditional GET (If-None-Match: <stored etag>) lets the server say
     nothing changed (304, no body) without transferring the page at all.
  2. Content hash — if the page's bytes did change (or it has no ETag), the freshly
     extracted text is hashed and compared to what's stored; HTML churn that
     doesn't touch the extracted text (ads, timestamps, nav) still skips the
     embedding call, the most expensive step.

Either check matching skips embedding and every write entirely — existing
bronze/silver/dest content is left untouched, and its etag/content_hash keep serving
as the baseline for the next run.
"""
import array
import hashlib
import json
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from digtk.lib import ai, derivative, fs, http_harvest
from digtk.lib.config import CONFIG
from digtk.models import collection


def _hash_content(text):
    """
    :param str text: Extracted page text.
    :returns: sha256 hex digest.
    :rtype: str
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pack_vector(vector):
    """
    :param list[float] vector: Embedding vector.
    :returns: Raw float32 array bytes (native byte order) — what gets written to a
        silver chunk's .bin file.
    :rtype: bytes
    """
    return array.array("f", vector).tobytes()


def _read_json_if_exists(path):
    """Read a JSON file if it exists, tolerating malformed content as absence.

    :param str path: Path to read (local, or 'cask:/'-prefixed).
    :returns: Parsed JSON dict, or None if `path` doesn't exist or isn't valid JSON.
    :rtype: dict|None
    """
    if not fs.exists(path):
        return None
    try:
        return json.loads(fs.read(path))
    except (ValueError, UnicodeDecodeError):
        return None


def _website_rel_path(url):
    """Derive a host and a path-safe relative path from a URL, for bronze/silver layout.

    :param str url: Page URL.
    :returns: (host, rel_path) — `host` is the URL's hostname; `rel_path` is its path
        with leading/trailing slashes stripped (e.g. '/foo/bar/' -> 'foo/bar'), or
        'index.html' if the URL has no path (or only '/').
    :rtype: tuple[str, str]
    """
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    return parsed.hostname, path or "index.html"


def _bronze_root(mode, group, host, today):
    """Build the bronze directory for a harvest run.

    :param str mode: 'fs' or 'cask'.
    :param str group: Group namespace.
    :param str host: Website host.
    :param str today: ISO date (no time component).
    :returns: 'cask:/bronze/...' for mode='cask'; a cwd-relative 'bronze/...' path
        for mode='fs'.
    :rtype: str
    """
    root = collection.dest_path("bronze", host, {"group": group, "time": today})
    return f"cask:{root}" if mode == "cask" else root.lstrip("/")


def _silver_local_path(group, host, today, rel_path):
    """Build a cwd-relative silver path for mode='fs' (mode='cask' goes through
    digtk.lib.derivative.write_cask() instead, which builds its own path + lineage).

    :param str group: Group namespace.
    :param str host: Website host.
    :param str today: ISO date (no time component).
    :param str rel_path: Path relative to the collection/time directory (e.g. 'foo/bar-0.json').
    :returns: cwd-relative 'silver/...' path.
    :rtype: str
    """
    return derivative.dest_path(
        type="silver", collection=host, rel_path=rel_path, opts={"group": group, "time": today},
    ).lstrip("/")


def _reconstruct_silver_paths(mode, group, host, today, rel_path, chunk_count):
    """Rebuild the deterministic silver paths for a prior harvest, without touching disk.

    Used to report `silver` paths on a skip, since bronze metadata records how many
    chunks a prior run produced (`chunk_count`) and every silver path is derived
    purely from (mode, group, host, today, rel_path, chunk index).

    :param str mode: 'fs' or 'cask'.
    :param str group: Group namespace.
    :param str host: Website host.
    :param str today: ISO date (no time component).
    :param str rel_path: Website path (see _website_rel_path()).
    :param int chunk_count: Number of chunks the prior run produced.
    :returns: [{"json":, "bin":}, ...], one per chunk index.
    :rtype: list[dict]
    """
    paths = []
    for i in range(chunk_count):
        chunk_rel = f"{rel_path}-{i}"
        if mode == "cask":
            json_path = derivative.dest_path(
                type="silver", collection=host, rel_path=f"{chunk_rel}.json", opts={"group": group, "time": today},
            )
            bin_path = derivative.dest_path(
                type="silver", collection=host, rel_path=f"{chunk_rel}.bin", opts={"group": group, "time": today},
            )
            paths.append({"json": f"cask:{json_path}", "bin": f"cask:{bin_path}"})
        else:
            paths.append({
                "json": _silver_local_path(group, host, today, f"{chunk_rel}.json"),
                "bin": _silver_local_path(group, host, today, f"{chunk_rel}.bin"),
            })
    return paths


def _harvest_structured(url, mode, opts={}):
    """Harvest a URL into the bronze/silver medallion layout.

    See the module docstring for the path shapes and skip-if-unchanged strategy.

    :param str url: Page URL to harvest.
    :param str mode: 'fs' or 'cask'.
    :param dict opts: 'group' (str, default CONFIG["harvest"]["group"]). 'tags' — see
        digtk.lib.http_harvest.extract_content(). 'chunk_size'/'buffer'/'model' — see
        digtk.lib.ai.chunk_text()/chunk_and_embed(). 'timeout'/'user_agent' — see
        digtk.lib.http_harvest.fetch().
    :returns: {"url":, "etag":, "content_hash": extracted-text sha256 hex digest (or
        the prior run's, on a skip), "model":, "chunks": chunk dicts (with vectors),
        or None on a skip, "skipped": bool, "skip_reason": "etag"|"content_hash"|None,
        "saved": bool, "output": mode, "group":, "bronze": {"raw":, "meta":}, "silver":
        [{"json":, "bin":}, ...]}.
    :rtype: dict
    :raises digtk.lib.http_harvest.HarvestError: If the page fails to fetch.
    """
    group = opts.get("group") or CONFIG["harvest"]["group"]
    host, rel_path = _website_rel_path(url)
    today = date.today().isoformat()

    bronze_root = _bronze_root(mode, group, host, today)
    bronze_raw_dest = f"{bronze_root}/{rel_path}"
    bronze_meta_dest = f"{bronze_raw_dest}.json"

    prior = _read_json_if_exists(bronze_meta_dest)
    fetched = http_harvest.fetch_page_conditional(url, {**opts, "etag": prior.get("etag") if prior else None})

    common = {
        "output": mode, "group": group,
        "bronze": {"raw": bronze_raw_dest, "meta": bronze_meta_dest},
    }

    if fetched["not_modified"]:
        return {
            "url": url, "etag": prior.get("etag"), "content_hash": prior.get("content_hash"),
            "model": prior.get("model"), "chunks": None, "skipped": True, "skip_reason": "etag",
            "saved": False,
            "silver": _reconstruct_silver_paths(mode, group, host, today, rel_path, prior.get("chunk_count", 0)),
            **common,
        }

    text = http_harvest.extract_content(fetched["html"], opts)
    content_hash = _hash_content(text)

    if prior and prior.get("content_hash") == content_hash:
        return {
            "url": url, "etag": fetched["etag"], "content_hash": content_hash,
            "model": prior.get("model"), "chunks": None, "skipped": True, "skip_reason": "content_hash",
            "saved": False,
            "silver": _reconstruct_silver_paths(mode, group, host, today, rel_path, prior.get("chunk_count", 0)),
            **common,
        }

    embedded = ai.chunk_and_embed(text=text, opts=opts)
    model = embedded["model"]
    chunks = embedded["chunks"]
    chunk_size = opts.get("chunk_size", CONFIG["search"]["chunk_size"])
    chunk_buffer = opts.get("buffer", CONFIG["search"]["chunk_buffer"])
    tags = http_harvest.resolve_tags(opts)
    raw_content_hash = hashlib.sha256(fetched["raw"]).hexdigest()

    bronze_meta = {
        "url": url,
        "etag": fetched["etag"],
        "content_type": fetched["content_type"],
        "raw_content_hash": raw_content_hash,
        "content_hash": content_hash,
        "model": model,
        "chunk_count": len(chunks),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    raw_write_opts = {"replace": True}
    if fetched["content_type"]:
        raw_write_opts["mime_type"] = fetched["content_type"]
    fs.write(bronze_raw_dest, data=fetched["raw"], opts=raw_write_opts)
    fs.write(
        bronze_meta_dest, data=json.dumps(bronze_meta).encode("utf-8"),
        opts={"replace": True, "mime_type": "application/json"},
    )

    silver_paths = []
    for i, chunk in enumerate(chunks):
        chunk_rel = f"{rel_path}-{i}"
        chunk_meta = {k: v for k, v in chunk.items() if k != "vector"}
        chunk_meta.update({
            "url": url, "content_hash": content_hash, "chunk_size": chunk_size,
            "chunk_buffer": chunk_buffer, "model": model, "tags": tags,
        })
        chunk_json = json.dumps(chunk_meta).encode("utf-8")
        chunk_bin = _pack_vector(chunk["vector"])

        if mode == "cask":
            json_written = derivative.write_cask(
                type="silver", collection=host, rel_path=f"{chunk_rel}.json", data=chunk_json,
                source_files=[bronze_raw_dest],
                opts={"group": group, "time": today, "replace": True, "mime_type": "application/json"},
            )
            bin_written = derivative.write_cask(
                type="silver", collection=host, rel_path=f"{chunk_rel}.bin", data=chunk_bin,
                source_files=[bronze_raw_dest],
                opts={"group": group, "time": today, "replace": True, "mime_type": "application/octet-stream"},
            )
            silver_paths.append({"json": json_written["path"], "bin": bin_written["path"]})
        else:
            json_path = _silver_local_path(group, host, today, f"{chunk_rel}.json")
            bin_path = _silver_local_path(group, host, today, f"{chunk_rel}.bin")
            fs.write(json_path, data=chunk_json, opts={"replace": True})
            fs.write(bin_path, data=chunk_bin, opts={"replace": True})
            silver_paths.append({"json": json_path, "bin": bin_path})

    return {
        "url": url, "etag": fetched["etag"], "content_hash": content_hash, "model": model,
        "chunks": chunks, "skipped": False, "skip_reason": None, "saved": True,
        "silver": silver_paths,
        **common,
    }


def _harvest_json(url, opts={}):
    """Harvest a URL, embed its content, and optionally persist the result as one JSON blob.

    :param str url: Page URL to harvest.
    :param dict opts: 'dest' (str, optional — where to persist the result: local
        path or 'cask:/'-prefixed; omit to just harvest and return, no skip
        checks). 'tags' — see digtk.lib.http_harvest.extract_content().
        'chunk_size'/'buffer'/'model' — see digtk.lib.ai.chunk_text()/chunk_and_embed().
        'timeout'/'user_agent' — see digtk.lib.http_harvest.fetch().
    :returns: {"url":, "etag": current ETag or None, "content_hash": sha256 hex
        digest of the extracted text, "model": embedding model used, "chunks": [see
        digtk.lib.ai.chunk_and_embed()] — carried over from the prior result on a
        skip, "skipped": bool, "skip_reason": "etag"|"content_hash"|None, "saved":
        bool (True iff this call wrote `dest`), "dest": opts['dest'] or None}.
    :rtype: dict
    :raises digtk.lib.http_harvest.HarvestError: If the page fails to fetch.
    """
    dest = opts.get("dest")
    prior = _read_json_if_exists(dest) if dest else None

    fetched = http_harvest.fetch_page_conditional(url, {**opts, "etag": prior.get("etag") if prior else None})

    if fetched["not_modified"]:
        return {**prior, "skipped": True, "skip_reason": "etag", "saved": False, "dest": dest}

    text = http_harvest.extract_content(fetched["html"], opts)
    content_hash = _hash_content(text)

    if prior and prior.get("content_hash") == content_hash:
        return {**prior, "etag": fetched["etag"], "skipped": True, "skip_reason": "content_hash", "saved": False, "dest": dest}

    embedded = ai.chunk_and_embed(text=text, opts=opts)
    stored = {"url": url, "etag": fetched["etag"], "content_hash": content_hash, **embedded}

    saved = False
    if dest:
        fs.write(dest, data=json.dumps(stored).encode("utf-8"), opts={"replace": True})
        saved = True

    return {**stored, "skipped": False, "skip_reason": None, "saved": saved, "dest": dest}


def harvest(url, opts={}):
    """Harvest a URL, embed its content, and optionally persist the result.

    Dispatches on `opts['output']`: 'fs' or 'cask' persists into the bronze/silver
    medallion layout (see _harvest_structured()); anything else (including unset)
    falls back to the single-JSON-blob mode keyed by `opts['dest']` (see _harvest_json()).

    :param str url: Page URL to harvest.
    :param dict opts: see _harvest_structured()/_harvest_json() for the opts each
        mode reads.
    :returns: see _harvest_structured()/_harvest_json().
    :rtype: dict
    :raises digtk.lib.http_harvest.HarvestError: If the page fails to fetch.
    """
    output = opts.get("output")
    if output in ("fs", "cask"):
        return _harvest_structured(url, output, opts)
    return _harvest_json(url, opts)
