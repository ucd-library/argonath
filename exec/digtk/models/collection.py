"""
Collection creation — add a local file or directory as a new CaskFS collection, and
build a bronze by-ref/tree layout for a collection pulled from a source system
(currently Preservica).

This module owns argonath's own business logic for what a "collection" is and how
it's structured in bronze — per CLAUDE.md's lib-vs-models split, source-specific
*clients* (e.g. digtk.lib.preservica) stay domain-agnostic; the bronze layout logic
that uses them lives here, alongside the original `add()` upload path:

- `add()` — formulates *where* a local file/directory upload goes ('bronze' or
  'workspace' — the only two roots new collections may be added under, per
  docs/cask-conventions.md) and delegates the actual transfer to the `cask cp` CLI,
  which owns its own rsync-style batch hash-check sync, progress reporting, and (by
  default) an interactive confirmation prompt — this path never touches file bytes
  itself, and only ever targets real CaskFS. Talking to the right CaskFS instance is
  the cask CLI's own concern too: `opts['env']` is passed straight through as
  `cask cp`'s `-e/--env` flag (see `cask env` in the caskfs repo), so environment
  selection follows whatever the caller has configured there rather than digtk's own
  CASK_API_URL/CASK_API_KEY (used by digtk.lib.fs.cask's HTTP backend, a separate
  connection path).

- `preservica_import()` / `preservica_tree()` / `local_to_bronze_tree()` — write
  many small files (JSON + binary media) through digtk.lib.fs instead, so a
  destination can be real CaskFS ('cask:/...') or, for local dev/testing, a plain
  local directory (see `opts['dest_root']`). `preservica_import()` walks a Preservica
  collection into bronze's by-ref/ (ref-keyed, near-verbatim relocation of
  Preservica's own Entity API responses — no schema.org or other reinterpretation,
  only one-to-one XML->JSON translation for descriptive metadata, see
  digtk.lib.preservica.xml). `preservica_tree()` rebuilds bronze's tree/ (human-
  readable, slugified-title-keyed, byte-identical to by-ref/) purely from what's
  already in bronze — no network calls. `local_to_bronze_tree()` is a debug/dev
  helper that materializes a local directory straight into tree/ only (no by-ref/,
  since there's no source-system ref to key it by) — it shares its tree-building
  logic with preservica_tree() via the private helpers below.
"""
import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from digtk.lib import auth, fs
from digtk.lib.config import CONFIG
from digtk.lib.preservica import PreservicaClient

CURRENT_TIME = "CURRENT_TIME"
CURRENT_DATE = "CURRENT_DATE"

ROOTS = ("bronze", "workspace")

_ARK_RE = re.compile(r"^ark:/")


def resolve_time(time):
    """Resolve a bronze time-folder value, expanding the CURRENT_TIME/CURRENT_DATE sentinels.

    :param str|None time: A literal time-folder name, one of the CURRENT_TIME/CURRENT_DATE
        sentinels, or None.
    :returns: Resolved time-folder name, or None if `time` was None.
    :rtype: str|None
    """
    if time == CURRENT_TIME:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if time == CURRENT_DATE:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return time


def dest_path(root, collection, opts={}):
    """Build the CaskFS-absolute destination path for a new collection.

    :param str root: 'bronze' or 'workspace' — the only roots new collections may be added under.
    :param str collection: Collection name.
    :param dict opts: 'group' (required when root='bronze'), 'time' (bronze only — literal
        time-folder name or a CURRENT_TIME/CURRENT_DATE sentinel; defaults to 'latest' if
        omitted), 'username' (workspace only, defaults to the current OS user).
    :returns: CaskFS-absolute path, e.g. '/bronze/digital-collections/ucd-photos/latest'.
    :rtype: str
    :raises ValueError: If `root` isn't 'bronze'/'workspace', a required opt is missing, or
        'time' is given for root='workspace' (workspace has no time dimension).
    """
    if root not in ROOTS:
        raise ValueError(f"root must be one of {ROOTS}, got {root!r}")

    if root == "bronze":
        group = opts.get("group")
        if not group:
            raise ValueError("opts['group'] is required when root='bronze'")
        time = resolve_time(opts.get("time")) or "latest"
        return f"/bronze/{group}/{collection}/{time}"

    if opts.get("time"):
        raise ValueError("opts['time'] does not apply when root='workspace' (no time dimension)")
    username = opts.get("username") or os.environ.get("USER") or os.environ.get("USERNAME")
    if not username:
        raise ValueError("opts['username'] is required when root='workspace' (could not detect current user)")
    return f"/workspace/{username}/{collection}"


def _build_argv(source, dest, opts):
    """Build the `cask cp` argv for uploading a collection.

    `cask cp`'s local-to-CaskFS path only checks its *source* argument for a 'cask:'
    prefix; a local source is copied to `dest-path` taken as a literal CaskFS path,
    so `dest` must NOT be 'cask:'-prefixed here (unlike a cask:->cask: or cask:->local
    cask cp invocation, which this module never issues).

    :param str source: Local path (file or directory) to upload.
    :param str dest: CaskFS-absolute destination path (no 'cask:' prefix).
    :param dict opts: 'env' (cask CLI environment name, -> -e), 'replace' (bool, -> -x),
        'dry_run' (bool, -> -d), 'yes' (bool, default False, -> -y — skip cask cp's
        confirmation prompt; by default cask cp prompts the user before copying).
    :returns: argv list suitable for subprocess.run.
    :rtype: list[str]
    """
    argv = ["cask", "cp", source, dest]
    if opts.get("env"):
        argv += ["-e", opts["env"]]
    if opts.get("replace"):
        argv.append("-x")
    if opts.get("dry_run"):
        argv.append("-d")
    if opts.get("yes"):
        argv.append("-y")
    return argv


def add(source, root, collection, opts={}):
    """Add a local file or directory to CaskFS as a new (or extended) collection.

    Formulates the destination path from `root`/`collection`/opts, then shells out to
    `cask cp` to perform the upload — `cask cp` does its own hash-based batch sync
    against what's already in CaskFS, so re-running `add()` over a source that's
    partially already uploaded only transfers what's changed.

    By default `cask cp` is left to prompt the user (on stdin) to confirm before
    copying, same as running it by hand — pass opts['yes']=True to skip that prompt
    for non-interactive/scripted use.

    :param str source: Local path (file or directory) to upload.
    :param str root: 'bronze' or 'workspace' — the only roots new collections may be added under.
    :param str collection: Collection name.
    :param dict opts: 'group' (required when root='bronze'), 'time' (bronze only, see
        dest_path()), 'username' (workspace only, see dest_path()), 'env' (cask CLI
        environment name), 'replace' (bool), 'dry_run' (bool), 'yes' (bool, default False
        — skip cask cp's confirmation prompt), 'run' (callable, defaults to
        subprocess.run — injectable for tests).
    :returns: Completed cask cp invocation.
    :rtype: subprocess.CompletedProcess
    :raises ValueError: If `root` is invalid or a required opt is missing.
    :raises FileNotFoundError: If `source` does not exist.
    :raises subprocess.CalledProcessError: If the cask cp invocation exits non-zero.
    """
    if not os.path.exists(source):
        raise FileNotFoundError(source)

    dest = dest_path(root, collection, opts)
    argv = _build_argv(source, dest, opts)
    run = opts.get("run", subprocess.run)
    return run(argv, check=True)


# --- Shared path/identifier helpers (preservica_import/tree, local_to_bronze_tree) --

def _bronze_collection_root(collection, group, opts={}):
    """Build the fs-compatible root for a bronze collection (no time segment).

    :param str collection: Collection name.
    :param str group: Bronze group namespace.
    :param dict opts: 'dest_root' (str, optional) — overrides the base prefix
        (default 'cask:', i.e. real CaskFS) with e.g. a local directory, for dev/
        testing without touching CaskFS. The 'bronze/<group>/<collection>' suffix
        is always appended, so a local override still gets the same layout CaskFS
        would have.
    :returns: fs-compatible path, e.g. 'cask:/bronze/preservica/haeger' or
        '/tmp/dev-output/bronze/preservica/haeger'.
    :rtype: str
    """
    base = opts.get("dest_root")
    base = base.rstrip("/") if base else "cask:"
    return f"{base}/bronze/{group}/{collection}"


def _bronze_latest_root(collection, group, opts={}):
    """Build the fs-compatible root for a bronze collection's 'latest' snapshot.

    There is no dated-snapshot support yet (see CLAUDE.md/docs/cask-conventions.md)
    — every write in this module targets 'latest' and a re-run updates it in place.

    :param str collection: Collection name.
    :param str group: Bronze group namespace.
    :param dict opts: See _bronze_collection_root().
    :returns: fs-compatible path, e.g. 'cask:/bronze/preservica/haeger/latest'.
    :rtype: str
    """
    return f"{_bronze_collection_root(collection, group, opts)}/latest"


def _write_bronze_json(root, rel_path, data):
    """Write a JSON-serializable value into bronze via fs.write() — one-to-one,
    no schema.org or other reinterpretation happens to `data` here.

    :param str root: fs-compatible bronze root (collection or collection/latest).
    :param str rel_path: Path relative to `root`.
    :param data: JSON-serializable value.
    """
    fs.write(f"{root}/{rel_path}", data=json.dumps(data, indent=2).encode(), opts={"replace": True})


def _read_bronze_json(root, rel_path, default=None):
    """Read a JSON file from bronze via fs.read(), or return `default` if absent.

    :param str root: fs-compatible bronze root.
    :param str rel_path: Path relative to `root`.
    :param default: Value to return if the file does not exist.
    :returns: Parsed JSON, or `default`.
    """
    path = f"{root}/{rel_path}"
    if not fs.exists(path):
        return default
    return json.loads(fs.read(path))


def _slugify(value):
    """Turn an arbitrary string into a filesystem-safe slug.

    :param str value: Raw value (e.g. an entity title).
    :returns: Lowercased, with runs of non-alphanumeric characters collapsed to a
        single hyphen and leading/trailing hyphens stripped.
    :rtype: str
    """
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _is_ark(value):
    """
    :param str value: An identifier value.
    :returns: True if `value` looks like an ARK ('ark:/...').
    :rtype: bool
    """
    return bool(_ARK_RE.match(value or ""))


def _resolve_identifier_key(identifiers, ref):
    """Resolve the file-naming key for an entity, without ever minting an ARK.

    Prefers an ARK among the entity's external identifiers; falls back to a
    slugified LocalID, then the raw Preservica ref. ARK minting is a separate,
    existing process — never done here, and a missing ARK is surfaced to the
    caller (see preservica_import()'s 'warnings'), not silently invented.

    :param list[dict] identifiers: [{'type', 'value'}, ...], e.g. from bronze's identifiers.json.
    :param str ref: The entity's Preservica ref, used as the last-resort key.
    :returns: {'key': str, 'identifiers': list[str]} — 'identifiers' is whichever
        raw values were actually found, never fabricated.
    :rtype: dict
    """
    values = [i.get("value") for i in identifiers if i.get("value")]

    ark = next((v for v in values if _is_ark(v)), None)
    if ark:
        return {"key": ark.rstrip("/").split("/")[-1], "identifiers": [ark]}

    local_id = next((i for i in identifiers if i.get("type") == "LocalID" and i.get("value")), None)
    if local_id:
        return {"key": _slugify(local_id["value"]), "identifiers": [local_id["value"]]}

    return {"key": ref, "identifiers": values or [ref]}


def _unique_tree_slug(title, key, used_slugs):
    """Pick a directory-safe slug for a tree/ entry, disambiguating collisions
    (two siblings with the same title) by appending the entity's own resolved key
    rather than an arbitrary counter — the key is already the unique join key, so
    this needs no new bookkeeping.

    :param str title: Entity title.
    :param str key: Resolved identifier key (see _resolve_identifier_key()), or
        another already-unique value for sources with no identifier concept
        (see local_to_bronze_tree()).
    :param set[str] used_slugs: Slugs already used among this entity's siblings —
        mutated in place, the returned slug is added to it.
    :returns: A slug unique among `used_slugs`.
    :rtype: str
    """
    base = _slugify(title or key)[:80].rstrip("-") or "untitled"
    candidate = base
    if candidate in used_slugs:
        candidate = f"{base}--{key}"
    used_slugs.add(candidate)
    return candidate


def _copy_bronze_dir(root, src_rel, dest_rel):
    """Recursively copy every file under one bronze directory to another,
    preserving relative structure — both located under the same fs-compatible
    `root`. Used to materialize a tree/ item's binary files as byte-identical
    copies of by-ref/'s media/ (dedupes for free once both are in content-
    addressed CaskFS).

    Paths are rebuilt from `root` rather than reused from fs.ls()'s DirEntry.path
    directly — CaskFS's backend returns that field as its own CaskFS-absolute
    path with no 'cask:' prefix (see digtk.lib.fs.cask.CaskBackend.ls()), which
    isn't directly fs-routable on its own.

    :param str root: fs-compatible bronze root common to both src and dest.
    :param str src_rel: Source directory, relative to `root`.
    :param str dest_rel: Destination directory, relative to `root`.
    """
    src_path = f"{root}/{src_rel}"
    if not fs.exists(src_path):
        return
    for entry in fs.ls(src_path):
        if entry.kind == "directory":
            _copy_bronze_dir(root, f"{src_rel}/{entry.name}", f"{dest_rel}/{entry.name}")
        else:
            fs.write(
                f"{root}/{dest_rel}/{entry.name}",
                src=f"{root}/{src_rel}/{entry.name}",
                opts={"replace": True},
            )


# --- Preservica import (phase 1: by-ref) -------------------------------------

def _build_preservica_client():
    """Build a PreservicaClient from resolved Preservica credentials.

    :returns: Configured client.
    :rtype: digtk.lib.preservica.PreservicaClient
    :raises ValueError: If no Preservica connection is configured.
    """
    creds = auth.resolve_preservica()
    if not (creds.get("base_url") and creds.get("username") and creds.get("password")):
        raise ValueError(
            "No Preservica connection configured. Set PRESERVICA_TENANT (or "
            "PRESERVICA_BASE_URL), PRESERVICA_USERNAME, and PRESERVICA_PASSWORD."
        )
    return PreservicaClient(creds["base_url"], creds["username"], creds["password"])


def _fetch_and_write_identifiers(client, data_type, ref, root, entity_dir, ctx):
    """Fetch and write one entity's external identifiers into by-ref/.

    :param PreservicaClient client: Client instance.
    :param str data_type: 'structural-objects' or 'information-objects'.
    :param str ref: Entity ref.
    :param str root: fs-compatible bronze 'latest' root.
    :param str entity_dir: 'by-ref/<ref>', relative to `root`.
    :param dict ctx: Walk context — see _walk_preservica_folder().
    :returns: The fetched identifiers, or [] if the fetch failed (error recorded in ctx).
    :rtype: list[dict]
    """
    try:
        identifiers = client.get_identifiers(data_type, ref)
        _write_bronze_json(root, f"{entity_dir}/identifiers.json", identifiers)
        return identifiers
    except Exception as e:
        ctx["manifest"]["errors"].append({"ref": ref, "stage": "identifiers", "message": str(e)})
        return []


def _fetch_and_write_metadata(client, entity, root, entity_dir, ctx):
    """Fetch and write one entity's descriptive metadata fragments into by-ref/,
    one-to-one (see digtk.lib.preservica.xml) — no schema.org reinterpretation.

    :param PreservicaClient client: Client instance.
    :param dict entity: {'ref', 'metadataFragmentRefs', ...} — see _walk_preservica_folder()/_walk_preservica_item().
    :param str root: fs-compatible bronze 'latest' root.
    :param str entity_dir: 'by-ref/<ref>', relative to `root`.
    :param dict ctx: Walk context — see _walk_preservica_folder().
    """
    try:
        resolved = [client.get_metadata_fragment(f["url"]) for f in entity.get("metadataFragmentRefs") or []]
        _write_bronze_json(root, f"{entity_dir}/metadata.json", resolved)
    except Exception as e:
        ctx["manifest"]["errors"].append({"ref": entity["ref"], "stage": "metadata", "message": str(e)})


def _walk_preservica_item(client, io_ref, root, ctx):
    """Leaf-fetch one Preservica information object into by-ref/: entity detail,
    identifiers, metadata, resolved download chain, the actual file bytes (fetched
    concurrently, bounded by ctx['concurrency']), and a best-effort thumbnail.

    :param PreservicaClient client: Client instance.
    :param str io_ref: Information object ref.
    :param str root: fs-compatible bronze 'latest' root.
    :param dict ctx: 'manifest' ({'items', 'errors', 'warnings'}), 'concurrency'.
    """
    entity_dir = f"by-ref/{io_ref}"
    try:
        raw = client.get_entity("information-objects", io_ref, {"expand_structure": True})
        entity = {
            "ref": io_ref,
            "title": (raw.get("InformationObject") or {}).get("Title"),
            "type": "IO",
            "metadataFragmentRefs": client.list_metadata_fragments(raw),
        }
        _write_bronze_json(root, f"{entity_dir}/entity.json", entity)
    except Exception as e:
        ctx["manifest"]["errors"].append({"ref": io_ref, "stage": "entity", "message": str(e)})
        return

    ctx["manifest"]["items"].append({"ref": io_ref, "type": "IO", "title": entity["title"]})

    identifiers = _fetch_and_write_identifiers(client, "information-objects", io_ref, root, entity_dir, ctx)
    _fetch_and_write_metadata(client, entity, root, entity_dir, ctx)

    try:
        representations = client.resolve_all_content(io_ref, raw)
        _write_bronze_json(root, f"{entity_dir}/representations.json", representations)
    except Exception as e:
        ctx["manifest"]["errors"].append({"ref": io_ref, "stage": "resolveAllContent", "message": str(e)})
        representations = []

    def _download_one(file):
        try:
            data = client.download_file(file["downloadUrl"])
            media_rel = f"{entity_dir}/media/{file['representationType']}/{file['filename']}"
            fs.write(f"{root}/{media_rel}", data=data, opts={"replace": True})
            if file.get("fixity"):
                _write_bronze_json(root, f"{media_rel}.fixity.json", file["fixity"])
        except Exception as e:
            ctx["manifest"]["errors"].append({"ref": io_ref, "stage": "download", "message": str(e)})

    if representations:
        with ThreadPoolExecutor(max_workers=ctx["concurrency"]) as executor:
            list(executor.map(_download_one, representations))

        thumb = client.get_thumbnail(io_ref)
        if thumb:
            fs.write(f"{root}/{entity_dir}/media/thumbnail", data=thumb, opts={"replace": True})

    if not any(_is_ark(i.get("value") or "") for i in identifiers):
        ctx["manifest"]["warnings"].append({
            "ref": io_ref, "stage": "identifiers", "message": "no ARK-shaped identifier found",
        })


def _walk_preservica_folder(client, so_ref, root, ctx):
    """Recursively walk one Preservica structural object into by-ref/: entity
    detail, identifiers, metadata, and children list, then recurse into
    sub-folders (bounded by ctx['max_depth']) and leaf-fetch items.

    :param PreservicaClient client: Client instance.
    :param str so_ref: Structural object ref.
    :param str root: fs-compatible bronze 'latest' root.
    :param dict ctx: 'max_depth', 'depth', 'manifest' ({'items', 'errors', 'warnings'}), 'concurrency'.
    """
    entity_dir = f"by-ref/{so_ref}"
    try:
        raw = client.get_entity("structural-objects", so_ref)
        entity = {
            "ref": so_ref,
            "title": (raw.get("StructuralObject") or {}).get("Title"),
            "type": "SO",
            "metadataFragmentRefs": client.list_metadata_fragments(raw),
        }
        _write_bronze_json(root, f"{entity_dir}/entity.json", entity)
    except Exception as e:
        ctx["manifest"]["errors"].append({"ref": so_ref, "stage": "entity", "message": str(e)})
        return

    ctx["manifest"]["items"].append({"ref": so_ref, "type": "SO", "title": entity["title"]})

    _fetch_and_write_identifiers(client, "structural-objects", so_ref, root, entity_dir, ctx)
    _fetch_and_write_metadata(client, entity, root, entity_dir, ctx)

    try:
        children = client.get_children(so_ref)
        _write_bronze_json(root, f"{entity_dir}/children.json", children)
    except Exception as e:
        ctx["manifest"]["errors"].append({"ref": so_ref, "stage": "children", "message": str(e)})
        children = []

    for child in children:
        if child["type"] == "SO":
            if ctx["depth"] + 1 <= ctx["max_depth"]:
                _walk_preservica_folder(client, child["ref"], root, {**ctx, "depth": ctx["depth"] + 1})
        elif child["type"] == "IO":
            _walk_preservica_item(client, child["ref"], root, ctx)


def preservica_import(ref, collection, group, opts={}):
    """Recursively walk a Preservica collection into bronze's by-ref/ layout — a
    near-verbatim, ref-keyed relocation of Preservica's own Entity API responses
    (entity.json, identifiers.json, metadata.json, children.json,
    representations.json, media/<RepresentationType>/<file>(+.fixity.json)). No
    schema.org or other reinterpretation happens here — XML descriptive metadata
    is translated to JSON one-to-one (see digtk.lib.preservica.xml).

    Every run re-walks the whole subtree from `ref` and overwrites by-ref/ in
    place: there is no local staging cache and no content-hash-based incremental
    diffing in this version (both are natural follow-ups if re-walking a large
    collection on every run becomes too slow). Per-entity/per-file errors are
    recorded in the returned manifest rather than aborting the whole walk, so one
    bad item doesn't block the rest of the collection.

    :param str ref: Root Preservica structural object ref to walk.
    :param str collection: Collection name.
    :param str group: Bronze group namespace.
    :param dict opts: 'max_depth' (int, default unlimited — maximum folder
        recursion depth), 'dest_root' (str, optional — see
        _bronze_collection_root()), 'concurrency' (int, default
        CONFIG['preservica']['concurrency'] — max concurrent file downloads per
        item), 'client' (digtk.lib.preservica.PreservicaClient, injectable for
        tests — defaults to one built from digtk.lib.auth.resolve_preservica()).
    :returns: {'items': [{'ref', 'type', 'title'}, ...], 'errors': [...], 'warnings': [...]}.
    :rtype: dict
    :raises ValueError: If Preservica credentials cannot be resolved and no
        `opts['client']` was given.
    """
    client = opts.get("client") or _build_preservica_client()
    max_depth = opts.get("max_depth")
    max_depth = float("inf") if max_depth is None else max_depth
    root = _bronze_latest_root(collection, group, opts)

    manifest = {"items": [], "errors": [], "warnings": []}
    ctx = {
        "max_depth": max_depth,
        "depth": 0,
        "manifest": manifest,
        "concurrency": opts.get("concurrency", CONFIG["preservica"]["concurrency"]),
    }
    _walk_preservica_folder(client, ref, root, ctx)

    _write_bronze_json(_bronze_collection_root(collection, group, opts), "manifest.json", {
        "sourceType": "preservica",
        "timeFormat": "latest",
        "preservicaRootRef": ref,
    })
    _write_bronze_json(root, "manifest.json", {
        "walkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "items": manifest["items"],
        "errors": manifest["errors"],
        "warnings": manifest["warnings"],
    })
    return manifest


# --- Bronze tree layout (phase 2: Preservica by-ref -> tree; phase 3: local disk -> tree) --

def _build_tree_from_by_ref(so_ref, root, dest_rel):
    """Recursively rebuild one folder's worth of tree/ entries from by-ref/'s
    already-current children.json — always a full rebuild (see
    preservica_tree()), never an incremental patch, so a renamed or removed item
    never leaves a stale directory behind. Each item/sub-folder's own
    identifiers.json is copied in verbatim (Preservica's raw identifier list,
    unchanged) as the only identity-carrying file — the exact same bytes by-ref/
    already has, so it dedups for free in CaskFS.

    :param str so_ref: Structural object ref to recurse from.
    :param str root: fs-compatible bronze 'latest' root.
    :param str dest_rel: Path (relative to `root`) to build this folder's children into.
    :returns: Number of tree entries written under `dest_rel` (recursively).
    :rtype: int
    """
    children = _read_bronze_json(root, f"by-ref/{so_ref}/children.json", [])
    used_slugs = set()
    count = 0

    for child in children:
        child_identifiers = _read_bronze_json(root, f"by-ref/{child['ref']}/identifiers.json", [])
        key = _resolve_identifier_key(child_identifiers, child["ref"])["key"]
        slug = _unique_tree_slug(child["title"], key, used_slugs)
        child_dest_rel = f"{dest_rel}/{slug}"

        if child["type"] == "SO":
            if child_identifiers:
                _write_bronze_json(root, f"{child_dest_rel}/identifiers.json", child_identifiers)
            count += 1
            count += _build_tree_from_by_ref(child["ref"], root, child_dest_rel)
        elif child["type"] == "IO":
            _copy_bronze_dir(root, f"by-ref/{child['ref']}/media", child_dest_rel)
            _write_bronze_json(root, f"{child_dest_rel}/identifiers.json", child_identifiers)
            count += 1

    return count


def preservica_tree(collection, group, opts={}):
    """Rebuild bronze's tree/ layout from a collection's already-imported by-ref/
    (see preservica_import()) — a human-readable, slugified-title-keyed
    reconstruction of the same entities, byte-identical to by-ref/ (dedupes for
    free once both live in content-addressed CaskFS). No network calls to
    Preservica are made; this only reads what's already in bronze.

    Always a full rebuild, never an incremental patch: a title (and therefore a
    tree/ path) isn't stable identity the way a Preservica ref is, so patching
    incrementally risks leaving a stale directory behind after a rename.

    :param str collection: Collection name.
    :param str group: Bronze group namespace.
    :param dict opts: 'dest_root' (str, optional — see _bronze_collection_root()).
    :returns: Number of tree entries written.
    :rtype: int
    :raises FileNotFoundError: If preservica_import() has not been run yet for
        this collection (no collection-level manifest.json / preservicaRootRef).
    """
    collection_root = _bronze_collection_root(collection, group, opts)
    collection_manifest = _read_bronze_json(collection_root, "manifest.json")
    root_so_ref = (collection_manifest or {}).get("preservicaRootRef")
    if not root_so_ref:
        raise FileNotFoundError(
            f"No preservicaRootRef at {collection_root}/manifest.json — run preservica_import() first"
        )

    root = _bronze_latest_root(collection, group, opts)
    try:
        fs.delete(f"{root}/tree", opts={"directory": True})
    except FileNotFoundError:
        pass

    return _build_tree_from_by_ref(root_so_ref, root, "tree")


def local_to_bronze_tree(source_dir, collection, group, opts={}):
    """Materialize a local directory into bronze's tree/ layout only — no by-ref/,
    since there is no source-system ref to key it by. For local dev/testing
    without a live Preservica sandbox; not a production ingest path.

    Each immediate subdirectory of `source_dir` becomes one tree/ entry
    (slugified by its own directory name), with its files copied in as-is and a
    `<file>.fixity.json` sidecar computed locally for each (SHA-256, since
    there's no source-system-reported checksum to reuse here unlike the
    Preservica-sourced path).

    :param str source_dir: Local directory to materialize — its immediate
        subdirectories become tree/ items.
    :param str collection: Collection name.
    :param str group: Bronze group namespace.
    :param dict opts: 'dest_root' (str, optional — see _bronze_collection_root()).
    :returns: Number of tree entries written.
    :rtype: int
    :raises FileNotFoundError: If `source_dir` does not exist.
    """
    if not os.path.isdir(source_dir):
        raise FileNotFoundError(source_dir)

    root = _bronze_latest_root(collection, group, opts)
    used_slugs = set()
    count = 0

    for name in sorted(os.listdir(source_dir)):
        item_path = os.path.join(source_dir, name)
        if not os.path.isdir(item_path):
            continue
        slug = _unique_tree_slug(name, name, used_slugs)
        _copy_local_dir_into_bronze(item_path, root, f"tree/{slug}")
        count += 1

    return count


def _copy_local_dir_into_bronze(local_dir, root, dest_rel):
    """Recursively copy a local directory's files into bronze, computing a
    SHA-256 fixity sidecar for each — see local_to_bronze_tree().

    :param str local_dir: Local source directory.
    :param str root: fs-compatible bronze root.
    :param str dest_rel: Destination directory, relative to `root`.
    """
    for name in sorted(os.listdir(local_dir)):
        path = os.path.join(local_dir, name)
        if os.path.isdir(path):
            _copy_local_dir_into_bronze(path, root, f"{dest_rel}/{name}")
            continue
        with open(path, "rb") as f:
            data = f.read()
        fs.write(f"{root}/{dest_rel}/{name}", data=data, opts={"replace": True})
        fixity = {"algorithm": "SHA256", "value": hashlib.sha256(data).hexdigest()}
        _write_bronze_json(root, f"{dest_rel}/{name}.fixity.json", fixity)
