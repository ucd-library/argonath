"""
Unified read/write/list/exists/delete interface for the local filesystem and CaskFS.

A path routes to CaskFS if it starts with 'cask:/' (see digtk.lib.fs.paths); anything
else is a local filesystem path, resolved against cwd if relative. Both backends
(digtk.lib.fs.local, digtk.lib.fs.cask.CaskBackend) implement the same duck-typed method
set and raise the same Python builtin exceptions, so calling code never needs to
branch on which backend a path resolved to.
"""
from contextlib import contextmanager

from digtk.lib import auth
from digtk.lib.fs import local as _local
from digtk.lib.fs import paths as _paths
from digtk.lib.fs.cask import CaskBackend

_CASK_WRITE_OPTS = {"partition_keys", "metadata", "bucket"}
_CASK_LS_OPTS = {"limit", "offset", "query"}
_CASK_MOVE_OPTS = {"recheck_mime_type"}

_cask_backend = None


def _get_cask_backend():
    """Lazily build the process-wide CaskBackend singleton from resolved credentials.

    :returns: Configured CaskBackend.
    :rtype: digtk.lib.fs.cask.CaskBackend
    :raises RuntimeError: If no CaskFS connection is configured.
    """
    global _cask_backend
    if _cask_backend is None:
        creds = auth.resolve()
        if not creds.get("api_url"):
            raise RuntimeError(
                "No CaskFS connection configured. Run `digtk auth login`, "
                "or set CASK_API_URL (and CASK_API_KEY)."
            )
        _cask_backend = CaskBackend(
            creds["api_url"], token=creds.get("token"), trusted_user=creds.get("trusted_user")
        )
    return _cask_backend


def _backend_for(ref):
    """
    :param digtk.lib.fs.paths.PathRef ref: Parsed path.
    :returns: digtk.lib.fs.local module, or a CaskBackend instance.
    """
    return _get_cask_backend() if ref.is_cask else _local


def _check_cask_only_opts(ref, opts, cask_only):
    """Raise if any CaskFS-only opt is used against a local path.

    :param digtk.lib.fs.paths.PathRef ref: Parsed path.
    :param dict opts: Opts passed by the caller.
    :param set cask_only: Opt names that only make sense for CaskFS.
    :raises ValueError: If ref is local and opts uses any name in cask_only.
    """
    if ref.is_local:
        used = cask_only & opts.keys()
        if used:
            raise ValueError(
                f"{sorted(used)} only apply to cask:/ paths, but '{ref.raw}' is a local path"
            )


def read(path, opts={}):
    """Read a file fully into memory, from either the local filesystem or CaskFS.

    :param str path: Path to read. A 'cask:/' prefix routes to CaskFS; anything else
        is a local filesystem path (relative paths resolve against cwd).
    :param dict opts: Unused for reads; accepted for signature symmetry with write()/ls().
    :returns: File contents.
    :rtype: bytes
    """
    ref = _paths.parse(path)
    return _backend_for(ref).read_bytes(ref.path)


@contextmanager
def read_stream(path, opts={}):
    """Read a file as a stream of byte chunks, from either the local filesystem or CaskFS.

    :param str path: Path to read (see read()).
    :param dict opts: Unused for reads; accepted for signature symmetry with write()/ls().
    :yields: Iterator of bytes chunks.
    """
    ref = _paths.parse(path)
    with _backend_for(ref).read_stream(ref.path) as chunks:
        yield chunks


def write(path, data=None, src=None, opts={}):
    """Write a file to either the local filesystem or CaskFS.

    Exactly one of `data` or `src` must be given. `data` writes bytes already in
    memory. `src` streams from another path (local or 'cask:/') without buffering
    the whole file in memory — this is how disk<->CaskFS copies work.

    :param str path: Destination path. A 'cask:/' prefix routes to CaskFS.
    :param bytes data: Content to write, if writing from memory.
    :param str src: Source path to stream from (local or 'cask:/'), if not writing from memory.
    :param dict opts: 'replace' (bool, default False — overwrite vs. fail if the destination
        exists). CaskFS-only: 'partition_keys' (list[str]), 'metadata' (dict), 'bucket' (str).
    :returns: The CaskFS file record if the destination is CaskFS, else None.
    :rtype: dict|None
    :raises ValueError: If both or neither of `data`/`src` are given, or a CaskFS-only
        opt is passed for a local destination.
    """
    if (data is None) == (src is None):
        raise ValueError("write() requires exactly one of 'data' or 'src'")

    ref = _paths.parse(path)
    _check_cask_only_opts(ref, opts, _CASK_WRITE_OPTS)
    backend = _backend_for(ref)

    if data is not None:
        return backend.write_bytes(ref.path, data, opts)

    src_ref = _paths.parse(src)
    with _backend_for(src_ref).read_stream(src_ref.path) as chunks:
        return backend.write_stream(ref.path, chunks, opts)


def ls(path, opts={}):
    """List entries in a directory, on either the local filesystem or CaskFS.

    :param str path: Directory path. A 'cask:/' prefix routes to CaskFS.
    :param dict opts: CaskFS-only: 'limit', 'offset', 'query'. Without 'limit',
        every entry is returned.
    :returns: Directory entries.
    :rtype: list[digtk.lib.fs.paths.DirEntry]
    :raises ValueError: If a CaskFS-only opt is passed for a local directory.
    """
    ref = _paths.parse(path)
    _check_cask_only_opts(ref, opts, _CASK_LS_OPTS)
    return _backend_for(ref).ls(ref.path, opts)


def exists(path):
    """Check whether a path exists, on either the local filesystem or CaskFS.

    :param str path: Path to check. A 'cask:/' prefix routes to CaskFS.
    :returns: True if the path exists.
    :rtype: bool
    """
    ref = _paths.parse(path)
    return _backend_for(ref).exists(ref.path)


def delete(path, opts={}):
    """Delete a file or directory, on either the local filesystem or CaskFS.

    :param str path: Path to delete. A 'cask:/' prefix routes to CaskFS.
    :param dict opts: 'directory' (bool) to delete a directory tree instead of a single file.
    """
    ref = _paths.parse(path)
    return _backend_for(ref).delete(ref.path, opts)


def move(path, dest_path, opts={}):
    """Move or rename a file or directory, on either the local filesystem or CaskFS.

    :param str path: Source path. A 'cask:/' prefix routes to CaskFS.
    :param str dest_path: Destination path — must resolve to the same backend as `path`.
    :param dict opts: CaskFS-only: 'recheck_mime_type' (bool).
    :returns: The CaskFS move response if the destination is CaskFS, else None.
    :rtype: dict|None
    :raises ValueError: If `path` and `dest_path` resolve to different backends,
        or a CaskFS-only opt is passed for a local move.
    :raises FileNotFoundError: If `path` does not exist.
    :raises FileExistsError: If `dest_path` already exists.
    """
    ref = _paths.parse(path)
    dest_ref = _paths.parse(dest_path)
    if ref.is_cask != dest_ref.is_cask:
        raise ValueError(f"move() requires both paths on the same backend: {path!r} -> {dest_path!r}")
    _check_cask_only_opts(ref, opts, _CASK_MOVE_OPTS)
    return _backend_for(ref).move(ref.path, dest_ref.path, opts)


def add_derivative_link(path, source_path, opts={}):
    """Record CaskFS derivative-link lineage: `path` was derived from `source_path`.

    Lineage links are a CaskFS-only concept (structural metadata, not file bytes) —
    both `path` and `source_path` must be 'cask:/' paths.

    :param str path: Derivative file path. Must be a 'cask:/' path.
    :param str source_path: Source file path. Must be a 'cask:/' path.
    :param dict opts: 'relation', 'metadata' — see CaskBackend.add_derivative_link.
    :returns: The created/updated derivative_link record.
    :rtype: dict
    :raises ValueError: If `path` or `source_path` is not a 'cask:/' path.
    """
    ref = _paths.parse(path)
    src_ref = _paths.parse(source_path)
    if ref.is_local or src_ref.is_local:
        raise ValueError("add_derivative_link() only applies to cask:/ paths")
    return _get_cask_backend().add_derivative_link(ref.path, src_ref.path, opts)


def find_by_subject(subject, opts={}):
    """Find files whose RDF graph contains a triple with the given subject.

    CaskFS-only — there's no local-filesystem equivalent of the Layer-3 RDF
    graph, so unlike every other function here this doesn't take a path to
    dispatch on. Used by digtk.lib.items to resolve an ARK to its bronze sidecar.

    :param str subject: Subject URI to search for (e.g. an ARK).
    :param dict opts: See digtk.lib.fs.cask.CaskBackend.find_by_subject.
    :returns: Matching file records.
    :rtype: list[dict]
    """
    return _get_cask_backend().find_by_subject(subject, opts)


def find_by_graph(graph, opts={}):
    """Find files whose RDF content includes the given named graph.
    CaskFS-only, same as find_by_subject().

    :param str graph: Named graph URI (e.g. an ARK).
    :param dict opts: See digtk.lib.fs.cask.CaskBackend.find_by_graph.
    :returns: Every matching file's CaskFS-absolute path.
    :rtype: list[str]
    """
    return _get_cask_backend().find_by_graph(graph, opts)


def find_by_partition_keys(partition_keys, opts={}):
    """Find files tagged with all of the given partition keys — no RDF/
    JSON-LD content required, unlike find_by_subject(). CaskFS-only, same as
    find_by_subject().

    :param list[str] partition_keys: Partition keys a file must carry (all of them).
    :param dict opts: See digtk.lib.fs.cask.CaskBackend.find_by_partition_keys.
    :returns: Every matching file's CaskFS-absolute path.
    :rtype: list[str]
    """
    return _get_cask_backend().find_by_partition_keys(partition_keys, opts)
