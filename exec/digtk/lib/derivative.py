"""
digtk.lib.derivative — write silver/gold/workspace derivative files into CaskFS.

A thin wrapper around digtk.lib.fs: builds the CaskFS destination path for a
derivative artifact (silver mirrors bronze's [group]/[collection]/[time]/ shape;
gold is the same [group]/[collection]/ minus the time dimension — current-state
only, see docs/cask-conventions.md's Gold Layout;
workspace is a per-user [group]/[collection]/ view — same 'group' opt as silver,
just no time dimension, since 'group' there is really the user's username — see
docs/cask-conventions.md), writes its bytes, then records CaskFS derivative-link
lineage back to every file it was derived from (digtk.lib.fs.add_derivative_link).
Lineage is structural metadata kept separate from the write itself, so a derivative
can never be written without at least one recorded source.

write() is the single entry point devs call. It's a railroad switch on `path`:
omit `path` and pass type/collection/rel_path/source_files for a real CaskFS
derivative write (dispatches to write_cask()); pass `path` with `local=True` for
an unchecked local-disk debug write (dispatches to write_local()). `local=True` is
required to take the local branch — a bare `path` with no `local=True` is treated
as caller error rather than silently skipping every CaskFS check, since the two
branches have very different guarantees (write_cask() enforces source_files and
records lineage; write_local() does neither).

All functions here take keyword-only arguments.
"""
from digtk.lib import fs

TYPES = ("silver", "gold", "workspace")


def _clean_rel_path(rel_path):
    """Validate and normalize a caller-supplied path relative to a collection[/time] root.

    :param str rel_path: Path relative to the collection (or collection/time) directory.
    :returns: Normalized relative path with leading/trailing slashes stripped.
    :rtype: str
    :raises ValueError: If `rel_path` is empty or contains a '..' segment.
    """
    if not rel_path:
        raise ValueError("rel_path is required")
    cleaned = rel_path.strip("/")
    if not cleaned or any(part == ".." for part in cleaned.split("/")):
        raise ValueError(f"rel_path must not be empty or contain '..' segments, got {rel_path!r}")
    return cleaned


def dest_path(*, type, collection, rel_path, opts={}):
    """Build the CaskFS-absolute destination path for a derivative file.

    :param str type: 'silver', 'gold', or 'workspace'.
    :param str collection: Collection name.
    :param str rel_path: Path relative to the collection[/time] directory (e.g.
        'item-1/derivatives/thumb-256.jpg'). Supplied by the caller rather than
        inferred, since a derivative can be produced from more than one source.
    :param dict opts: 'group' (required for every type — for 'workspace' this is
        the user's username, plumbed in by the caller), 'time' (silver only,
        defaults to 'latest' if omitted — gold and workspace have no time dimension).
    :returns: CaskFS-absolute path, e.g.
        '/silver/digital-collections/ucd-photos/latest/item-1/derivatives/thumb-256.jpg'.
    :rtype: str
    :raises ValueError: If `type` isn't 'silver'/'gold'/'workspace', `collection`/
        `rel_path` is missing or invalid, 'group' is missing, or 'time' is given
        for gold/workspace.
    """
    if type not in TYPES:
        raise ValueError(f"type must be one of {TYPES}, got {type!r}")
    if not collection:
        raise ValueError("collection is required")
    rel_path = _clean_rel_path(rel_path)

    group = opts.get("group")
    if not group:
        raise ValueError(f"opts['group'] is required when type={type!r}")

    if type in ("gold", "workspace"):
        if opts.get("time"):
            raise ValueError(f"opts['time'] does not apply when type={type!r} (no time dimension)")
        return f"/{type}/{group}/{collection}/{rel_path}"

    time = opts.get("time") or "latest"
    return f"/silver/{group}/{collection}/{time}/{rel_path}"


def write_cask(*, type, collection, rel_path, data, source_files, opts={}):
    """Write a derivative file to CaskFS and record its lineage back to each source file.

    Builds the destination path (see dest_path()), verifies every entry in
    `source_files` already exists in CaskFS, writes `data`, then calls
    fs.add_derivative_link() once per source so the file's full provenance is
    recorded — a derivative is never written with zero recorded sources.

    Note: the file write and each lineage link are separate CaskFS API calls. If a
    link call fails partway through `source_files`, the derivative file and any
    earlier links have already been committed — this is not rolled back.

    :param str type: 'silver', 'gold', or 'workspace'.
    :param str collection: Collection name.
    :param str rel_path: Path relative to the collection[/time] directory — see dest_path().
    :param bytes data: Derivative file content.
    :param list[str] source_files: CaskFS paths ('cask:/'-prefixed) this derivative was
        produced from. Must be non-empty — a derivative always has at least one source.
    :param dict opts: 'group'/'time' — see dest_path(). 'mime_type' (str, guessed
        from `rel_path` if omitted). 'replace' (bool, default False — overwrite vs. fail
        if the destination already exists). 'link_relation' (str, optional — forwarded to
        fs.add_derivative_link() as 'relation' for every source link; defaults server-side
        to schema:source if omitted). 'link_metadata' (str, optional — forwarded as
        'metadata' on every source link; CaskFS stores this as an opaque, unqueried text
        field — see caskfs's docs/structural-metadata.md — so pass a json.dumps()-encoded
        string for structured content, not a dict).
    :returns: dict with 'path' (the cask:/ destination written) and 'links' (the
        derivative_link record per source file, in `source_files` order).
    :rtype: dict
    :raises ValueError: If `data` is None, `source_files` is empty/missing, any entry
        in `source_files` isn't a 'cask:/' path, the destination lists itself as one
        of its own sources, or a dest_path() check fails.
    :raises FileNotFoundError: If any entry in `source_files` does not exist in CaskFS.
    """
    if data is None:
        raise ValueError("data is required")
    if not source_files:
        raise ValueError(
            "source_files is required and must be non-empty — "
            "a derivative cannot be written without at least one source"
        )
    for source in source_files:
        if not source.startswith("cask:/"):
            raise ValueError(f"source_files must be 'cask:/'-prefixed CaskFS paths, got {source!r}")

    dest = dest_path(type=type, collection=collection, rel_path=rel_path, opts=opts)
    dest_cask = f"cask:{dest}"

    if dest_cask in source_files:
        raise ValueError("a derivative file cannot list itself as one of its own source_files")

    for source in source_files:
        if not fs.exists(source):
            raise FileNotFoundError(source)

    write_opts = {"replace": opts.get("replace", False)}
    if opts.get("mime_type"):
        write_opts["mime_type"] = opts["mime_type"]
    fs.write(dest_cask, data=data, opts=write_opts)

    link_opts = {}
    if opts.get("link_relation"):
        link_opts["relation"] = opts["link_relation"]
    if opts.get("link_metadata"):
        link_opts["metadata"] = opts["link_metadata"]
    links = [fs.add_derivative_link(dest_cask, source, link_opts) for source in source_files]

    return {"path": dest_cask, "links": links}


def write_local(*, path, data):
    """Write derivative bytes straight to local disk, bypassing CaskFS entirely.

    For local debugging/testing only: no destination path is built from
    type/collection/rel_path, no source_files are required, and no derivative-link
    lineage is recorded — the write is untracked. Existing files at `path` are
    overwritten. Prefer calling write(path=..., data=..., local=True) over calling
    this directly, so the intentional-local-write guard in write() still applies.

    :param str path: Local filesystem destination path (must not be 'cask:/'-prefixed).
    :param bytes data: Derivative file content.
    :raises ValueError: If `path` is a 'cask:/' path — use write_cask() for CaskFS
        destinations.
    """
    if path.startswith("cask:/"):
        raise ValueError("write_local() is for local paths only — use write_cask() for cask:/ destinations")
    fs.write(path, data=data, opts={"replace": True})


def write(
    *, path=None, local=False, type=None, collection=None, rel_path=None,
    data, source_files=None, opts={},
):
    """Write a derivative file — the single entry point for both CaskFS and local writes.

    Railroad switch on `path`/`local`:

    - Omit `path` (and `local`) to write a real CaskFS derivative: `type`,
      `collection`, `rel_path`, and `source_files` are required, and this dispatches
      to write_cask() (destination path built for you, lineage recorded to every
      source).
    - Pass `path` together with `local=True` for an unchecked local-disk debug write:
      dispatches to write_local(). No source_files, no path building, no lineage.

    `local=True` must be given explicitly to take the local branch — a bare `path`
    with `local` left False is rejected rather than silently downgrading what may
    have been intended as a real CaskFS write into an unchecked, untracked local one.

    :param str path: Local filesystem destination path. Only valid together with
        `local=True`; omit for a CaskFS write.
    :param bool local: Must be True to use `path` for a local debug write. Default False.
    :param str type: 'silver', 'gold', or 'workspace'. CaskFS write only — see write_cask().
    :param str collection: Collection name. CaskFS write only.
    :param str rel_path: Path relative to the collection[/time] directory. CaskFS write only.
    :param bytes data: File content (required for both branches).
    :param list[str] source_files: CaskFS paths this derivative was produced from.
        CaskFS write only — see write_cask().
    :param dict opts: CaskFS write only — see write_cask()/dest_path().
    :returns: write_cask()'s result dict for a CaskFS write; None for a local write.
    :rtype: dict|None
    :raises ValueError: If `path` is given without `local=True` (or vice versa), or
        if the chosen branch's own required arguments are missing/invalid.
    :raises FileNotFoundError: If any entry in `source_files` does not exist in CaskFS
        (CaskFS write only).
    """
    if path is not None or local:
        if path is None:
            raise ValueError("write(local=True) requires path")
        if not local:
            raise ValueError(
                "write(path=...) requires local=True to confirm this is an intentional "
                "local-disk debug write — CaskFS writes never take a raw path, they take "
                "type/collection/rel_path"
            )
        if type is not None or collection is not None or source_files is not None:
            raise ValueError("write(local=True) does not accept type/collection/source_files")
        write_local(path=path, data=data)
        return None

    if type is None or collection is None or rel_path is None:
        raise ValueError(
            "write() requires type/collection/rel_path/source_files for a CaskFS write, "
            "or path/local=True for a local debug write"
        )
    return write_cask(
        type=type, collection=collection, rel_path=rel_path,
        data=data, source_files=source_files, opts=opts,
    )
