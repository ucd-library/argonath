"""
Item resolution: ARK or bronze path -> CaskFS item location.

An item's bronze folder path (e.g.
'bronze/preservica/haeger/latest/tree/quarter-mile') is argonath's canonical
identifier — it always exists. ARK, Preservica ref, and local ID are sidecars:
useful downstream (silver/gold products, external addressing) when present,
but never required to resolve or process an item. resolve_path() resolves an
item directly from its bronze folder, with no ARK lookup at all; resolve()
resolves from an ARK instead, via CaskFS's Layer-3 `/cask/find` search
(digtk.lib.fs.find_by_subject) — useful when that's the only identifier in
hand (e.g. a caller addressing an item by ARK alone) — through a bronze
'<binary>.jsonld.json' sidecar, or the item's gold item.jsonld.json record
once one has been built (see digtk.models.digital_website.item_record). Both
return the same
ItemLocation shape, so every phase (derivatives, OCR, metadata) resolves items
the same way regardless of which identifier it started from.

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
import re
from dataclasses import dataclass

from digtk.lib import dc_mapping, derivative, fs

BRONZE_PREFIX = "/bronze/"
GOLD_PREFIX = "/gold/"
SIDECAR_SUFFIX = ".jsonld.json"
# An item's canonical record, written only to gold (see
# digtk.models.digital_website.item_record) — never to bronze or silver.
ITEM_RECORD_FILENAME = f"item{SIDECAR_SUFFIX}"
WORKSPACE_PREFIX = "/workspace/"
# Mirrors digtk.models.collection's _is_ark() — this module already has source-
# convention awareness for identifiers.json/tree (see ark_for_path()), so knowing
# an ark's shape here too is the same category of knowledge, not new layering.
_ARK_RE = re.compile(r"^ark:/")
# Fixed subdirectories under a workspace collection: staged source binaries go in
# source/, every derivative written for them goes in derivatives/ (mirroring
# source/'s item paths), so outputs never mix with the inputs they came from.
WORKSPACE_SOURCE_DIR = "source"
WORKSPACE_DERIVATIVES_DIR = "derivatives"
# CaskFS's /dir endpoint pages directories+files together (server default 100);
# a staged folder of scans can easily exceed that, so workspace listings ask for
# one large page instead.
_WORKSPACE_LS_OPTS = {"limit": 10000}


@dataclass(frozen=True)
class ItemLocation:
    """An item's internal directory structure is arbitrary per source (see
    docs/cask-conventions.md's Bronze Layout) and can hold more than one binary —
    e.g. an archival box/folder containing several photographs. All binaries
    sharing an ARK are treated as one item.

    :param str|None ark: The item's ARK identifier, if it has one. A sidecar
        identifier, not a resolution requirement — None for a workspace item
        (never has one yet) and for a bronze item resolved by path
        (resolve_path()) whose folder has no ARK.
    :param str group: Group namespace segment of the bronze path.
    :param str collection: Collection name segment.
    :param str time: Time-folder segment (e.g. 'latest', a date, a year-week).
    :param str item_path: Item's internal path relative to the collection/time root
        (e.g. 'box-1/folder-3'), possibly '' if binaries sit directly under time/.
    :param list[str] bronze_binary_paths: CaskFS paths ('cask:/...') to the item's one
        or more source binaries, sorted — bronze binaries for a resolve()'d/
        resolve_path()'d item, workspace source/ binaries for a resolve_workspace()'d one.
    :param str sidecar_path: CaskFS path ('cask:/....jsonld.json') to the item's
        bronze sidecar — bronze_binary_paths[0] + '.jsonld.json', for an item
        ingested with one (see digtk.commands.ingest). None for a workspace
        item, a bronze tree/ item (its descriptive metadata is its folder's
        metadata.json — see metadata_for()), and test fixtures built by hand.
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


_NON_BINARY_FILENAMES = {"metadata.json", "identifiers.json", "children.json", "entity.json", "representations.json"}


def _list_item_binaries(group, collection, time, item_path):
    """List every real binary file under an item's directory tree.

    Used for a folder-addressed item (resolve_path(), and resolve() through a
    gold item record) — e.g. Preservica's tree/<slug>/ layout, where an
    item's actual binaries sit one level deeper under Access/Preservation
    representation-type subdirectories.

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


def _resolve_gold_record(ark, record_path):
    """Resolve an ARK through its gold item record — the item's bronze folder
    comes from the gold manifest.json written alongside it (gold has no time
    dimension, so the record's own path can't say which bronze snapshot it
    was built from).

    :param str ark: ARK identifier.
    :param str record_path: '/gold/.../item.jsonld.json' match for `ark`.
    :returns: Resolved item location.
    :rtype: ItemLocation
    :raises LookupError: If the gold manifest is missing or names no bronze folder.
    """
    manifest_path = f"cask:{record_path.rsplit('/', 1)[0]}/manifest.json"
    if not fs.exists(manifest_path):
        raise LookupError(f"no manifest.json next to {record_path} — cannot find {ark}'s bronze folder")
    bronze = json.loads(fs.read(manifest_path)).get("bronze")
    if not bronze:
        raise LookupError(f"{manifest_path} names no bronze folder for {ark}")
    group, collection, time, item_path = _parse_bronze_folder(bronze)
    return ItemLocation(
        ark=ark, group=group, collection=collection, time=time, item_path=item_path,
        bronze_binary_paths=_list_item_binaries(group, collection, time, item_path),
    )


def resolve(ark, opts={}):
    """Resolve an ARK to its bronze item location (one or more binaries).

    Finds every bronze '<binary>.jsonld.json' sidecar whose `@id` is the ARK —
    one for a single-binary item, more than one when the item is a multi-image
    box/folder — and derives each binary path by stripping the sidecar's
    '.jsonld.json' suffix.

    An item with no bronze sidecar (e.g. a Preservica tree/<slug>/ item) is
    found through its gold item.jsonld.json record instead (see
    _resolve_gold_record()), so it only resolves by ARK once that record has
    been built — address it by its bronze folder (resolve_path()) before then.

    :param str ark: ARK identifier, e.g. 'ark:/87287/d7abc1234'.
    :param dict opts: Forwarded to fs.find_by_subject() (e.g. 'predicate' override).
    :returns: Resolved item location.
    :rtype: ItemLocation
    :raises LookupError: If neither a bronze sidecar nor a gold record matches
        the ARK, or matching binaries don't all share the same
        group/collection/time/item_path (a data-integrity problem — binaries
        sharing an ARK should live in the same item location).
    """
    matches = fs.find_by_subject(ark, opts)
    sidecar_paths = sorted(
        m for m in matches
        if m.startswith(BRONZE_PREFIX) and m.endswith(SIDECAR_SUFFIX)
        and m.rsplit("/", 1)[-1] != ITEM_RECORD_FILENAME
    )

    if not sidecar_paths:
        records = sorted(m for m in matches if m.startswith(GOLD_PREFIX) and m.endswith(f"/{ITEM_RECORD_FILENAME}"))
        if len(records) > 1:
            raise LookupError(f"Expected one gold item record for {ark}, found {len(records)}: {records}")
        if records:
            return _resolve_gold_record(ark, records[0])
        raise LookupError(f"Expected a bronze sidecar or gold item record for {ark}, found neither")

    binary_paths = [p[: -len(SIDECAR_SUFFIX)] for p in sidecar_paths]
    parsed = [_parse_bronze_path(p) for p in binary_paths]
    locations = {(group, collection, time, item_path) for group, collection, time, item_path, _ in parsed}

    if len(locations) > 1:
        raise LookupError(
            f"Binaries for {ark} are not all in the same item location: {sorted(locations)}"
        )

    group, collection, time, item_path = locations.pop()
    return ItemLocation(
        ark=ark,
        group=group,
        collection=collection,
        time=time,
        item_path=item_path,
        bronze_binary_paths=[f"cask:{p}" for p in binary_paths],
        sidecar_path=f"cask:{sidecar_paths[0]}",
    )


def normalize_bronze_path(path):
    """A bronze path in the canonical '/bronze/...' form, with no 'cask:' prefix
    or trailing slash — accepts 'cask:/bronze/...', '/bronze/...' or 'bronze/...'.

    :param str path: Bronze path in any of the accepted forms.
    :returns: e.g. '/bronze/preservica/haeger/latest/tree/quarter-mile'.
    :rtype: str
    :raises ValueError: If the path isn't under /bronze/, or is missing its
        group/collection/time segments.
    """
    raw = path[len("cask:"):] if path.startswith("cask:") else path
    raw = "/" + raw.strip().strip("/")
    if not raw.startswith(BRONZE_PREFIX):
        raise ValueError(f"not a bronze path: {path}")
    if len(raw[len(BRONZE_PREFIX):].split("/")) < 3:
        raise ValueError(f"bronze path needs at least /bronze/[group]/[collection]/[time]: {path}")
    return raw


def ark_for_path(path):
    """Look up the ARK of the item whose folder is `path`, from its
    'identifiers.json' (written for every Preservica tree item — see
    digtk.models.collection._write_tree_child()).

    Lets a caller address an item by its bronze folder (e.g. Dagster's
    process_item_job, keyed by the tree/ path rather than the ARK) and still
    know its ARK.

    :param str path: Item folder, e.g. 'bronze/preservica/haeger/latest/tree/quarter-mile'
        (any form normalize_bronze_path() accepts).
    :returns: The item's ARK, e.g. 'ark:/87287/d7abc1234'.
    :rtype: str
    :raises ValueError: If `path` isn't a bronze path.
    :raises LookupError: If identifiers.json is missing or carries no ARK.
    """
    folder = normalize_bronze_path(path)
    identifiers_path = f"cask:{folder}/identifiers.json"
    if fs.exists(identifiers_path):
        for entry in json.loads(fs.read(identifiers_path)):
            value = entry.get("value") or ""
            if _ARK_RE.match(value):
                return value

    raise LookupError(f"MISSING ARK: no ark in {folder}/identifiers.json")


def _parse_bronze_folder(path):
    """Split a bronze item folder into its group/collection/time/item_path components
    (no filename — unlike _parse_bronze_path(), which expects one).

    :param str path: Any form normalize_bronze_path() accepts.
    :returns: (group, collection, time, item_path).
    :rtype: tuple
    :raises ValueError: If path doesn't have at least group/collection/time segments.
    """
    segments = normalize_bronze_path(path)[len(BRONZE_PREFIX):].split("/")
    if len(segments) < 3:
        raise ValueError(f"bronze path needs at least /bronze/[group]/[collection]/[time]: {path}")
    group, collection, time = segments[0], segments[1], segments[2]
    return group, collection, time, "/".join(segments[3:])


def resolve_path(path, opts={}):
    """Resolve a bronze item folder directly to its item location — no ARK lookup.

    The item's ARK is attached if one happens to resolve (via ark_for_path()), but
    resolution itself depends only on the path — unlike resolve(), this never
    requires an ARK to find the item at all.

    :param str path: Bronze item folder, e.g.
        'bronze/preservica/haeger/latest/tree/quarter-mile' (any form
        normalize_bronze_path() accepts).
    :param dict opts: Unused; kept for signature symmetry with resolve().
    :returns: Resolved item location (ark=None if the folder has no resolvable ARK).
    :rtype: ItemLocation
    :raises ValueError: If path isn't a bronze path.
    :raises LookupError: If the folder has no binaries under it.
    """
    group, collection, time, item_path = _parse_bronze_folder(path)
    bronze_binary_paths = _list_item_binaries(group, collection, time, item_path)
    if not bronze_binary_paths:
        raise LookupError(f"no binaries found under {normalize_bronze_path(path)}")
    try:
        ark = ark_for_path(path)
    except LookupError:
        ark = None
    return ItemLocation(
        ark=ark, group=group, collection=collection, time=time, item_path=item_path,
        bronze_binary_paths=bronze_binary_paths,
    )


def metadata(ark, opts={}):
    """Read an item's existing bronze descriptive metadata (see metadata_for()).

    :param str ark: ARK identifier, e.g. 'ark:/87287/d7abc1234'.
    :param dict opts: Forwarded to resolve().
    :returns: See metadata_for().
    :rtype: dict
    :raises LookupError: If the ARK doesn't resolve (see resolve()).
    """
    return metadata_for(resolve(ark, opts))


def metadata_for(item):
    """Read an already-resolved item's descriptive metadata from bronze.

    Same mapping as metadata(), without a second resolve() round-trip. Reads
    the item's bronze sidecar when it has one (bronze is immutable/append-only
    and every binary sharing an ARK was ingested from the same source record,
    so this one sidecar is authoritative for item-level fields); otherwise the
    Dublin Core in its bronze folder's metadata.json (a Preservica tree/ item —
    see digtk.lib.dc_mapping). A workspace item has neither, so every field is
    None for it — callers fall back to filename-derived values (e.g. a PDF
    title) on their own.

    :param ItemLocation item: Resolved item location.
    :returns: {"title": str|None, "description": str|None, "date": str|None,
        "creator": the raw creator value|None}.
    :rtype: dict
    """
    if item.sidecar_path:
        doc = json.loads(fs.read(item.sidecar_path))
        return {
            "title": doc.get("name"),
            "description": doc.get("description"),
            "date": doc.get("dateCreated"),
            "creator": doc.get("creator"),
        }
    empty = {"title": None, "description": None, "date": None, "creator": None}
    if item.dest_type != "silver":
        return empty
    folder = f"{BRONZE_PREFIX}{item.group}/{item.collection}/{item.time}/{item.item_path}".rstrip("/")
    metadata_path = f"cask:{folder}/metadata.json"
    if not fs.exists(metadata_path):
        return empty
    content = json.loads(fs.read(metadata_path))
    dc = dc_mapping.extract_dublin_core(content)
    return {
        "title": dc["title"] or (content.get("preservica") or {}).get("title"),
        "description": dc["description"],
        "date": dc["date"],
        "creator": dc["creator"],
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
    """Resolve an ARK, a bronze item folder, a workspace item path, or an
    already-resolved location.

    The single entry point the root-level tools (ocr, accessibility,
    split-spreads, showcase derivatives) resolve their input through. A local
    folder isn't accepted here — staging one into workspace first is
    digtk.models.workspace's job.

    :param str|ItemLocation target: 'ark:/...', a bronze item folder (any form
        normalize_bronze_path() accepts — see resolve_path()),
        'cask:/workspace/...', or an ItemLocation (returned unchanged).
    :param dict opts: Forwarded to resolve() for an ARK.
    :rtype: ItemLocation
    :raises ValueError: If `target` is none of the above.
    :raises LookupError: See resolve()/resolve_path()/resolve_workspace().
    """
    if isinstance(target, ItemLocation):
        return target
    if target.startswith("ark:/"):
        return resolve(target, opts)
    if target.startswith(f"cask:{WORKSPACE_PREFIX}"):
        return resolve_workspace(target)
    try:
        return resolve_path(target)
    except ValueError:
        pass
    raise ValueError(f"expected an ARK, a bronze path, or a cask:/workspace/... path, got {target!r}")


def item_id(item):
    """Identifier for an item in derivative outputs, results and logs — always
    its path, never its ARK, so every derivative step labels an item the same
    way whether or not it has one (the ARK only matters from the gold item
    record onward — see digtk.models.digital_website.item_record).

    :param ItemLocation item: Resolved item location.
    :returns: Its workspace source directory path, or its bronze folder path.
    :rtype: str
    """
    if item.dest_type == "workspace":
        root = workspace_source_root(item.group, item.collection)
        return f"{root}/{item.item_path}" if item.item_path else root
    return f"{BRONZE_PREFIX}{item.group}/{item.collection}/{item.time}/{item.item_path}".rstrip("/")


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
