"""
ARK -> CaskFS bronze location resolution.

Item paths in CaskFS are arbitrary (see docs/cask-conventions.md) — the ARK is
never derived from a path, only from a sidecar's `@id`/`ark` property. This
module is the single place that turns an ARK into a bronze location, via
CaskFS's Layer-3 `/cask/find` search (digtk.lib.fs.find_by_subject), so every
phase (derivatives, OCR, metadata) resolves items the same way.

Resolves to group/collection/time/item_path components (not a pre-built path
string) because that's what digtk.lib.derivative.write()'s
type/collection/rel_path/opts signature — the canonical way to write silver
outputs — needs to build the matching silver destination.

digtk.lib.fs.find_by_subject() (via CaskBackend.find_by_subject()) already
handles /cask/find's paginated {"totalCount", "results": [{"filepath": ...}]}
response shape and hands back a flat list of path strings — this module never
deals with pagination or the response envelope directly. CaskFS's API is
still under active development; if its shape changes again, that's the layer
to fix, not here.

Also resolves CaskFS workspace paths (resolve_workspace()): items staged under
/workspace/[username]/[collection]/source/ before they have an ARK or a bronze
copy (see digtk.models.workspace, which owns staging local folders there). A
workspace item's outputs go to /workspace/[username]/[collection]/derivatives/
instead of silver — every ItemLocation carries its own destination
(dest_type/dest_prefix, see dest_rel()/dest_opts()/dest_dir()), so the root-level
tools write wherever their item came from without branching on it themselves.
"""
import json
from dataclasses import dataclass

from digtk.lib import derivative, fs

BRONZE_PREFIX = "/bronze/"
SIDECAR_SUFFIX = ".jsonld.json"
WORKSPACE_PREFIX = "/workspace/"
# Fixed subdirectories under a workspace collection: staged source binaries go in
# source/, every derivative written for them goes in derivatives/ (mirroring
# source/'s item paths), so outputs never mix with the inputs they came from.
WORKSPACE_SOURCE_DIR = "source"
WORKSPACE_DERIVATIVES_DIR = "derivatives"
# CaskFS's /dir endpoint pages directories+files together (server default 100);
# a staged folder of scans can easily exceed that, so workspace listings ask for
# one large page instead — same workaround digtk.lib.gcs_sync uses.
_WORKSPACE_LS_OPTS = {"limit": 10000}


@dataclass(frozen=True)
class ItemLocation:
    """An item's internal directory structure is arbitrary per source (see
    docs/cask-conventions.md's Bronze Layout) and can hold more than one binary —
    e.g. an archival box/folder containing several photographs. All binaries
    sharing an ARK are treated as one item.

    :param str ark: The item's ARK identifier.
    :param str group: Group namespace segment of the bronze path.
    :param str collection: Collection name segment.
    :param str time: Time-folder segment (e.g. 'latest', a date, a year-week).
    :param str item_path: Item's internal path relative to the collection/time root
        (e.g. 'box-1/folder-3'), possibly '' if binaries sit directly under time/.
    :param list[str] bronze_binary_paths: CaskFS paths ('cask:/...') to the item's one
        or more source binaries, sorted — bronze binaries for a resolve()'d item,
        workspace source/ binaries for a resolve_workspace()'d one.
    :param str sidecar_path: CaskFS path ('cask:/....jsonld.json') to the item's
        canonical bronze sidecar. Usually bronze_binary_paths[0] + '.jsonld.json',
        but not for a standalone 'item.jsonld.json' sidecar (see resolve()) — a
        fixed name describing every binary under item_path, not one co-located
        binary of the same name — so callers that need the sidecar itself
        should read this field rather than reconstructing it from
        bronze_binary_paths. None for a workspace item (no sidecar yet) and for
        test fixtures built by hand; resolve() always sets a real value.
    :param str dest_type: digtk.lib.derivative type this item's outputs are
        written as — 'silver' (default, a bronze item) or 'workspace'.
    :param str dest_prefix: Path segment placed before item_path in every
        output rel_path — '' for silver, WORKSPACE_DERIVATIVES_DIR for workspace.
    """
    ark: str
    group: str
    collection: str
    time: str
    item_path: str
    bronze_binary_paths: list
    sidecar_path: str = None
    dest_type: str = "silver"
    dest_prefix: str = ""


def _parse_bronze_path(path):
    """Split a bronze-absolute path into its group/collection/time/item_path/filename components.

    :param str path: e.g. '/bronze/digital-collections/my-coll/latest/box-1/folder-3/photo.tiff'.
    :returns: (group, collection, time, item_path, filename) — item_path is '' if the
        binary sits directly under the time/ folder.
    :rtype: tuple
    :raises ValueError: If path doesn't have at least group/collection/time/filename segments.
    """
    segments = path[len(BRONZE_PREFIX):].split("/")
    if len(segments) < 4:
        raise ValueError(f"Bronze path missing group/collection/time/filename segments: {path}")
    group, collection, time = segments[0], segments[1], segments[2]
    filename = segments[-1]
    item_path = "/".join(segments[3:-1])
    return group, collection, time, item_path, filename


_STANDALONE_SIDECAR_FILENAME = "item"
_NON_BINARY_FILENAMES = {"metadata.json", "identifiers.json", "children.json", "entity.json", "representations.json"}


def _list_item_binaries(group, collection, time, item_path):
    """List every real binary file under an item's directory tree.

    Used for the standalone-sidecar case in resolve() (a fixed-name
    'item.jsonld.json' describing every binary under item_path, not one
    co-located binary sharing its name) — e.g. Preservica's tree/<slug>/
    layout, where an item's actual binaries sit one level deeper under
    Access/Preservation representation-type subdirectories, not flat
    alongside the sidecar the way the single-binary convention assumes.

    :param str group: Bronze group namespace.
    :param str collection: Bronze collection name.
    :param str time: Time-folder segment.
    :param str item_path: Item's internal path relative to the collection/time root.
    :returns: Bronze binary paths ('cask:/...'), sorted.
    :rtype: list[str]
    """
    root = f"{BRONZE_PREFIX}{group}/{collection}/{time}/{item_path}"
    binaries = []
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in fs.ls(f"cask:{current}"):
            if entry.kind == "directory":
                stack.append(entry.path)
            elif _is_binary(entry.name):
                binaries.append(entry.path)
    return sorted(f"cask:{p}" for p in binaries)


def _is_binary(filename):
    """Whether a listed file is a real source binary rather than a sidecar or
    source-system bookkeeping file.

    :param str filename: File name (no directory).
    :rtype: bool
    """
    return not filename.endswith(SIDECAR_SUFFIX) and filename not in _NON_BINARY_FILENAMES


def resolve(ark, opts={}):
    """Resolve an ARK to its bronze item location (one or more binaries).

    Finds every bronze sidecar file whose `@id` is the ARK — one for a
    single-binary item, more than one when the item is a multi-image box/folder —
    and derives each binary path by stripping the sidecar's '.jsonld.json' suffix.

    Exception: a sidecar literally named 'item.jsonld.json' (docs/cask-
    conventions.md's `<item>.jsonld.json` domain-sidecar convention, used e.g.
    by Preservica's tree/<slug>/ layout) describes every binary under its own
    directory, not one binary of the same name — stripping the suffix there
    would point at a file that doesn't exist. In that case every real file
    under item_path is listed instead (see _list_item_binaries()).

    :param str ark: ARK identifier, e.g. 'ark:/87287/d7abc1234'.
    :param dict opts: Forwarded to fs.find_by_subject() (e.g. 'predicate' override).
    :returns: Resolved item location.
    :rtype: ItemLocation
    :raises LookupError: If no bronze sidecar matches the ARK, or matching binaries
        don't all share the same group/collection/time/item_path (a data-integrity
        problem — binaries sharing an ARK should live in the same item location).
    """
    matches = fs.find_by_subject(ark, opts)
    sidecar_paths = sorted(
        m for m in matches
        if m.startswith(BRONZE_PREFIX) and m.endswith(SIDECAR_SUFFIX)
    )

    if not sidecar_paths:
        raise LookupError(f"Expected exactly one bronze sidecar for {ark}, found 0")

    binary_paths = [p[: -len(SIDECAR_SUFFIX)] for p in sidecar_paths]
    parsed = [_parse_bronze_path(p) for p in binary_paths]
    locations = {(group, collection, time, item_path) for group, collection, time, item_path, _ in parsed}

    if len(locations) > 1:
        raise LookupError(
            f"Binaries for {ark} are not all in the same item location: {sorted(locations)}"
        )

    group, collection, time, item_path = locations.pop()
    filenames = {filename for *_, filename in parsed}

    if filenames == {_STANDALONE_SIDECAR_FILENAME}:
        bronze_binary_paths = _list_item_binaries(group, collection, time, item_path)
    else:
        bronze_binary_paths = [f"cask:{p}" for p in binary_paths]

    return ItemLocation(
        ark=ark,
        group=group,
        collection=collection,
        time=time,
        item_path=item_path,
        bronze_binary_paths=bronze_binary_paths,
        sidecar_path=f"cask:{sidecar_paths[0]}",
    )


def metadata(ark, opts={}):
    """Read an item's existing bronze descriptive metadata.

    Reads the item's own bronze sidecar (location.sidecar_path) — bronze is
    immutable/append-only and every binary sharing an ARK was ingested from
    the same source record, so this one sidecar is treated as authoritative
    for item-level fields across this codebase.

    :param str ark: ARK identifier, e.g. 'ark:/87287/d7abc1234'.
    :param dict opts: Forwarded to resolve().
    :returns: See metadata_for().
    :rtype: dict
    :raises LookupError: If no bronze sidecar matches the ARK (see resolve()).
    """
    return metadata_for(resolve(ark, opts))


def metadata_for(item):
    """Read an already-resolved item's descriptive metadata from its sidecar.

    Same mapping as metadata(), without a second resolve() round-trip. A
    workspace item has no sidecar yet, so every field is None for it — callers
    fall back to filename-derived values (e.g. a PDF title) on their own.

    :param ItemLocation item: Resolved item location.
    :returns: {"title": str|None, "description": str|None, "date": str|None,
        "creator": the sidecar's raw 'creator' value|None} — mapped from the
        sidecar's schema.org 'name'/'description'/'dateCreated'/'creator' keys.
    :rtype: dict
    """
    if not item.sidecar_path:
        return {"title": None, "description": None, "date": None, "creator": None}
    doc = json.loads(fs.read(item.sidecar_path))
    return {
        "title": doc.get("name"),
        "description": doc.get("description"),
        "date": doc.get("dateCreated"),
        "creator": doc.get("creator"),
    }


# --- Workspace items ---------------------------------------------------------

def workspace_source_root(username, collection):
    """CaskFS path of a workspace collection's source/ directory.

    :param str username: Workspace owner.
    :param str collection: Workspace collection name.
    :returns: e.g. 'cask:/workspace/jdoe/box-12/source'.
    :rtype: str
    """
    return f"cask:{WORKSPACE_PREFIX}{username}/{collection}/{WORKSPACE_SOURCE_DIR}"


def _parse_workspace_path(path):
    """Split a workspace path into username/collection/item_path.

    Accepts the collection root itself ('/workspace/u/c', taken to mean its
    source/ directory) or anything under its source/ directory.

    :param str path: 'cask:/workspace/...' or '/workspace/...'.
    :returns: (username, collection, item_path) — item_path is '' for source/ itself.
    :rtype: tuple
    :raises ValueError: If the path isn't under /workspace/[user]/[collection],
        or points inside the collection but outside source/.
    """
    raw = path[len("cask:"):] if path.startswith("cask:") else path
    if not raw.startswith(WORKSPACE_PREFIX):
        raise ValueError(f"not a workspace path: {path}")
    segments = [s for s in raw[len(WORKSPACE_PREFIX):].split("/") if s]
    if len(segments) < 2:
        raise ValueError(f"workspace path needs at least /workspace/[username]/[collection]: {path}")
    username, collection, rest = segments[0], segments[1], segments[2:]
    if rest and rest[0] != WORKSPACE_SOURCE_DIR:
        raise ValueError(
            f"workspace path must be the collection itself or under its "
            f"{WORKSPACE_SOURCE_DIR}/ directory: {path}"
        )
    return username, collection, "/".join(rest[1:])


def resolve_workspace(path):
    """Resolve a workspace source/ directory to an item location.

    A workspace item is every binary sitting directly in one directory (not
    recursively) — a flat folder of scans is one item; a nested box/folder
    layout is one item per folder that actually holds files (see
    list_workspace_items()).

    :param str path: e.g. 'cask:/workspace/jdoe/box-12' or
        'cask:/workspace/jdoe/box-12/source/folder-3'.
    :returns: Item location with ark=None, time=None, no sidecar, and a
        workspace destination under the collection's derivatives/ directory.
    :rtype: ItemLocation
    :raises ValueError: See _parse_workspace_path().
    :raises LookupError: If the directory has no binaries directly inside it.
    """
    username, collection, item_path = _parse_workspace_path(path)
    root = workspace_source_root(username, collection)
    item_dir = f"{root}/{item_path}" if item_path else root
    binaries = sorted(
        f"cask:{entry.path}" for entry in fs.ls(item_dir, _WORKSPACE_LS_OPTS)
        if entry.kind != "directory" and _is_binary(entry.name)
    )
    if not binaries:
        raise LookupError(f"no files found directly in {item_dir}")
    return ItemLocation(
        ark=None, group=username, collection=collection, time=None,
        item_path=item_path, bronze_binary_paths=binaries,
        dest_type="workspace", dest_prefix=WORKSPACE_DERIVATIVES_DIR,
    )


def list_workspace_items(path):
    """List every item directory under a workspace path.

    :param str path: A workspace collection root or any directory under its
        source/ (see resolve_workspace()).
    :returns: 'cask:/workspace/...' directories (including `path` itself) that
        directly contain at least one binary, sorted — each one resolve_workspace()-able.
    :rtype: list[str]
    :raises ValueError: See _parse_workspace_path().
    """
    username, collection, item_path = _parse_workspace_path(path)
    root = workspace_source_root(username, collection)
    start = f"{root}/{item_path}" if item_path else root
    found = []
    stack = [start]
    while stack:
        current = stack.pop()
        has_binary = False
        for entry in fs.ls(current, _WORKSPACE_LS_OPTS):
            if entry.kind == "directory":
                stack.append(f"cask:{entry.path}")
            elif _is_binary(entry.name):
                has_binary = True
        if has_binary:
            found.append(current)
    return sorted(found)


def resolve_target(target, opts={}):
    """Resolve an ARK, a workspace item path, or an already-resolved location.

    The single entry point the root-level tools (ocr, accessibility,
    split-spreads) resolve their input through. A local folder isn't accepted
    here — staging one into workspace first is digtk.models.workspace's job.

    :param str|ItemLocation target: 'ark:/...', 'cask:/workspace/...', or an
        ItemLocation (returned unchanged).
    :param dict opts: Forwarded to resolve() for an ARK.
    :rtype: ItemLocation
    :raises ValueError: If `target` is none of the above.
    :raises LookupError: See resolve()/resolve_workspace().
    """
    if isinstance(target, ItemLocation):
        return target
    if target.startswith("ark:/"):
        return resolve(target, opts)
    if target.startswith(f"cask:{WORKSPACE_PREFIX}"):
        return resolve_workspace(target)
    raise ValueError(f"expected an ARK or a cask:/workspace/... path, got {target!r}")


def item_id(item):
    """Human-readable identifier for an item, for results and logs.

    :param ItemLocation item: Resolved item location.
    :returns: The ARK, or for a workspace item its source directory path.
    :rtype: str
    """
    if item.ark:
        return item.ark
    root = workspace_source_root(item.group, item.collection)
    return f"{root}/{item.item_path}" if item.item_path else root


# --- Output destination ------------------------------------------------------

def dest_rel(item, *parts):
    """rel_path (relative to the destination collection[/time] root) for one of
    this item's outputs — the item_path-prefixing every derivative.write() call
    in the root-level tools needs.

    :param ItemLocation item: Resolved item location.
    :param str parts: Path segments to join after the item's own location.
    :returns: e.g. 'box-1/text/ocr.hocr' (silver) or
        'derivatives/folder-3/text/ocr.hocr' (workspace).
    :rtype: str
    """
    return "/".join(p for p in (item.dest_prefix, item.item_path, *parts) if p)


def dest_opts(item):
    """digtk.lib.derivative.dest_path()/write() opts for this item's destination.

    :param ItemLocation item: Resolved item location.
    :returns: {'group', 'time'} for silver, {'group'} (the username) for workspace.
    :rtype: dict
    """
    if item.dest_type == "workspace":
        return {"group": item.group}
    return {"group": item.group, "time": item.time}


def dest_dir(item, subdir):
    """CaskFS directory an output subdir of this item lives in.

    :param ItemLocation item: Resolved item location.
    :param str subdir: e.g. 'split-spreads' or 'accessibility/document.pdf/1'.
    :returns: 'cask:/...' directory path.
    :rtype: str
    """
    path = derivative.dest_path(
        type=item.dest_type, collection=item.collection,
        rel_path=dest_rel(item, subdir), opts=dest_opts(item),
    )
    return f"cask:{path}"
