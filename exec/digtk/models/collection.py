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
  Preservica's own Entity API responses — no schema.org or other reinterpretation;
  descriptive metadata is kept as raw XML, verbatim, see digtk.lib.preservica.xml).
  `preservica_tree()` rebuilds bronze's tree/ (human-readable, slugified-title-keyed)
  purely from what's already in bronze — no network calls. Media files are
  byte-identical copies of by-ref/'s (fixity sidecars excluded), and each
  fragment's raw XML is converted to a plain metadata.json (just its Content,
  attribute keys stripped — see digtk.lib.preservica.parse_metadata_content()).
  `local_to_bronze_tree()` is a debug/dev helper that materializes a local
  directory straight into tree/ only (no by-ref/, since there's no source-system
  ref to key it by) — it shares its tree-building logic with preservica_tree()
  via the private helpers below.
"""
import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from digtk.lib import db, fs, preservica
from digtk.lib.config import CONFIG

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
    :raises ValueError: If `root` is invalid, a required opt is missing, or (bronze
        only) `collection` collides with a different active registered collection
        — see register_collection().
    :raises FileNotFoundError: If `source` does not exist.
    :raises subprocess.CalledProcessError: If the cask cp invocation exits non-zero.
    """
    if not os.path.exists(source):
        raise FileNotFoundError(source)

    dest = dest_path(root, collection, opts)
    if root == "bronze":
        # Workspace is scratch/staging, not a tracked pipeline collection — only
        # bronze collections are registered, per the same 'always register on
        # add/import' rule preservica_import() follows below.
        register_collection(collection, opts.get("group"), "manual")

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


# --- Collection registry (Postgres) -----------------------------------------
#
# Tracks which collections argonath knows about (source, name, workflow tags)
# so Dagster's sensors know what to watch and what to do when a source changes
# — see docs/preservica-identifiers.md and dagster/lib/sensors.py's
# collection_partition_sync_sensor / preservica_auto_sync_sensor. Built on
# digtk.lib.db (generic Postgres helper); tables live in the 'argonath' schema
# (digtk.lib.db.SCHEMA) — see digtk/sql/collection_registry.sql for the schema
# itself, and init_pg() below to apply it.

_SCHEMA_SQL_PATH = Path(__file__).parent.parent / "sql" / "collection_registry.sql"


def init_pg(opts={}):
    """Apply collection_registry.sql (schema + tables) to argonath's Postgres
    database — safe to run repeatedly (every statement is CREATE ... IF NOT
    EXISTS). See `digtk collection init-pg`.

    :param dict opts: 'conn' (injectable psycopg2 connection, for tests/reuse).
    """
    db.run_script(_SCHEMA_SQL_PATH.read_text(), opts)


def _parse_workflow_tags(value):
    """Parse a Preservica 'argonath' identifier's value into a normalized tag set.

    Any tag implies 'sync' (e.g. 'ai,public' behaves identically to
    'sync,ai,public') — a collection only reaches this parser once an
    'argonath' identifier is already present, so 'sync' is always included.

    :param str value: Raw identifier value, e.g. 'sync', 'ai,public'.
    :returns: Normalized, sorted tag list, always including 'sync'.
    :rtype: list[str]
    """
    tags = {t.strip() for t in (value or "").split(",") if t.strip()}
    tags.add("sync")
    return sorted(tags)


def register_collection(name, group, source_type, identifiers=(), opts={}):
    """Upsert a collection_registry row (+ its collection_identifiers) — argonath's
    durable record that a collection is tracked, for Dagster's sensors to read
    (dagster/lib/sensors.py's collection_partition_sync_sensor and
    preservica_auto_sync_sensor).

    Called unconditionally by both preservica_import() and the CLI's
    `collection add` path — registration never depends on whether an 'argonath'
    identifier is present; that identifier only governs auto-sensor discovery of
    brand-new folders and optional-stage gating (see
    docs/preservica-identifiers.md), not whether an import/add gets tracked.

    Refuses (raises ValueError) rather than auto-disambiguating when `name`
    collides with a different, already-registered active collection — the
    argonath collection name must equal the source folder's own name 1:1,
    so the fix is renaming the source folder (or choosing a different name for
    a manual disk collection), never silently picking a new slug here. A
    same-identity re-registration (the exact same Preservica ref re-imported,
    or the exact same manual name+group re-added) is treated as a normal
    update, not a collision.

    :param str name: Collection name — must equal the source folder's own
        (slugified) name; used for both bronze paths and Dagster's collections
        partition key.
    :param str group: Bronze group namespace.
    :param str source_type: 'manual' (disk) or 'preservica'.
    :param list[dict] identifiers: [{'type', 'value'}, ...] — every known
        identifier for this collection (Preservica ref as 'preservica_ref', ARK,
        LocalID, the raw 'argonath' tag, etc.), stored verbatim in
        collection_identifiers. Workflow tags are derived from whichever
        identifier has type == 'argonath'.
    :param dict opts: 'conn' (injectable psycopg2 connection, for tests/reuse —
        see digtk.lib.db).
    :returns: {'id': int, 'name': str, 'workflow_tags': list[str]}.
    :rtype: dict
    :raises ValueError: If `name` collides with a different active registration.
    """
    tags = []
    incoming_ref = None
    for identifier in identifiers:
        if identifier.get("type") == "argonath" and identifier.get("value"):
            tags = _parse_workflow_tags(identifier["value"])
        if identifier.get("type") == "preservica_ref" and identifier.get("value"):
            incoming_ref = identifier["value"]

    existing = db.query(
        "SELECT id, group_name, source_type FROM argonath.collection_registry "
        "WHERE name = %s AND status = 'active'",
        (name,), opts,
    )

    collection_id = None
    if existing:
        row = existing[0]
        same_collection = False
        if source_type == "preservica" and row["source_type"] == "preservica" and incoming_ref:
            stored = db.query(
                "SELECT id_value FROM argonath.collection_identifiers "
                "WHERE collection_id = %s AND id_type = 'preservica_ref'",
                (row["id"],), opts,
            )
            same_collection = bool(stored) and stored[0]["id_value"] == incoming_ref
        elif source_type == "manual" and row["source_type"] == "manual" and row["group_name"] == group:
            same_collection = True

        if not same_collection:
            raise ValueError(
                f"Collection name '{name}' is already registered (source_type="
                f"{row['source_type']!r}, group={row['group_name']!r}) — rename the "
                "source folder (or choose a different name) rather than reusing an "
                "active collection's name."
            )
        collection_id = row["id"]

    if collection_id is None:
        inserted = db.execute(
            "INSERT INTO argonath.collection_registry (name, group_name, source_type, workflow_tags, last_synced_at) "
            "VALUES (%s, %s, %s, %s, now()) RETURNING id",
            (name, group, source_type, tags), {**opts, "returning": True},
        )
        collection_id = inserted[0]["id"]
    else:
        db.execute(
            "UPDATE argonath.collection_registry SET source_type = %s, workflow_tags = %s, "
            "last_synced_at = now(), updated_at = now() WHERE id = %s",
            (source_type, tags, collection_id), opts,
        )

    db.execute(
        "DELETE FROM argonath.collection_identifiers WHERE collection_id = %s", (collection_id,), opts,
    )
    for identifier in identifiers:
        if identifier.get("type") and identifier.get("value"):
            db.execute(
                "INSERT INTO argonath.collection_identifiers (collection_id, id_type, id_value) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (collection_id, identifier["type"], identifier["value"]), opts,
            )

    return {"id": collection_id, "name": name, "workflow_tags": tags}


def get_registration(id_type, id_value, opts={}):
    """Look up a registered collection via any tracked identifier.

    Used by preservica_auto_sync_sensor to resolve a changed Preservica entity
    ref to its owning registered collection.

    :param str id_type: Identifier type, e.g. 'preservica_ref'.
    :param str id_value: Identifier value to look up.
    :param dict opts: 'conn' (injectable psycopg2 connection, for tests/reuse).
    :returns: {'id', 'name', 'group_name', 'source_type', 'workflow_tags', 'status'}, or None.
    :rtype: dict | None
    """
    rows = db.query(
        "SELECT r.id, r.name, r.group_name, r.source_type, r.workflow_tags, r.status "
        "FROM argonath.collection_registry r "
        "JOIN argonath.collection_identifiers i ON i.collection_id = r.id "
        "WHERE i.id_type = %s AND i.id_value = %s AND r.status = 'active'",
        (id_type, id_value), opts,
    )
    return rows[0] if rows else None


def list_active_collections(source_type=None, opts={}):
    """List every active registered collection, optionally filtered by source type.

    Used by both new Dagster sensors — collection_partition_sync_sensor (to
    ensure every active collection has a Dagster 'collections' partition) and
    preservica_auto_sync_sensor (to scope which Preservica collections to watch).

    :param str|None source_type: 'manual' or 'preservica', or None for all.
    :param dict opts: 'conn' (injectable psycopg2 connection, for tests/reuse).
    :returns: [{'id', 'name', 'group_name', 'source_type', 'workflow_tags'}, ...].
    :rtype: list[dict]
    """
    sql = (
        "SELECT id, name, group_name, source_type, workflow_tags FROM argonath.collection_registry "
        "WHERE status = 'active'"
    )
    params = ()
    if source_type:
        sql += " AND source_type = %s"
        params = (source_type,)
    return db.query(sql, params, opts)


def _get_active_registration_by_name(name, group, opts={}):
    """Look up an active registered collection by its own name + group pair —
    used by preservica_sync_entities(), whose caller (the CLI's `sync`
    command, driven by Dagster's reconcile_collection_items) only knows
    collection/group, not a registry id or Preservica ref (contrast
    get_registration(), which looks up by an arbitrary tracked identifier).

    :param str name: Collection name.
    :param str group: Bronze group namespace.
    :param dict opts: 'conn' (injectable psycopg2 connection, for tests/reuse).
    :returns: {'id', 'name', 'group_name', 'source_type', 'workflow_tags'}.
    :rtype: dict
    :raises ValueError: If no active collection is registered under name+group.
    """
    rows = db.query(
        "SELECT id, name, group_name, source_type, workflow_tags FROM argonath.collection_registry "
        "WHERE name = %s AND group_name = %s AND status = 'active'",
        (name, group), opts,
    )
    if not rows:
        raise ValueError(f"No active registered collection '{group}/{name}' — run `digtk preservica import` first.")
    return rows[0]


def _touch_last_synced(collection_id, opts={}):
    """Update a collection_registry row's last_synced_at only, without
    touching its identifiers — unlike register_collection() (which always
    deletes+re-inserts every identifier row from its `identifiers` param, so
    reusing it here with an empty list would silently wipe the collection's
    real identifiers). Used by preservica_sync_entities() after a targeted
    sync completes.

    :param int collection_id: collection_registry.id.
    :param dict opts: 'conn' (injectable psycopg2 connection, for tests/reuse).
    """
    db.execute(
        "UPDATE argonath.collection_registry SET last_synced_at = now() WHERE id = %s",
        (collection_id,), opts,
    )


def _parent_ref_of(client, ref, data_type):
    """Best-effort lookup of an entity's parent structural object ref, via its
    own EntityResponse.

    NOTE: unlike client.py's other EntityResponse field reads (Title, etc.),
    this field name has not been confirmed against a live Preservica tenant —
    verify it before relying on this in production (see
    find_changed_preservica_collections()'s caller, preservica_auto_sync_sensor).

    :param PreservicaClient client: Client instance.
    :param str ref: Entity ref.
    :param str data_type: 'structural-objects' or 'information-objects'.
    :returns: Parent ref, or None if absent/unresolvable.
    :rtype: str | None
    """
    try:
        detail = client.get_entity(data_type, ref)
    except Exception:
        return None
    key = "StructuralObject" if data_type == "structural-objects" else "InformationObject"
    return (detail.get(key) or {}).get("Parent")


def _find_owning_collection(client, ref, entity_type):
    """Walk up an entity's parent chain to find the nearest registered
    Preservica collection — for a change reported inside a subfolder/item
    rather than at a collection's own root ref.

    :param PreservicaClient client: Client instance.
    :param str ref: The changed entity's own ref.
    :param str entity_type: 'SO' or 'IO' — content objects have no
        parent-walkable entity API here, so a CO-type change is not resolved
        this way (its containing IO change is expected to separately appear
        in the same updated-since response).
    :returns: {'registration': dict, 'root_ref': str} for the nearest
        registered ancestor, or None if none is found.
    :rtype: dict | None
    """
    if entity_type not in ("SO", "IO"):
        return None
    current_ref = ref
    current_type = "structural-objects" if entity_type == "SO" else "information-objects"
    seen = set()
    while current_ref and current_ref not in seen:
        seen.add(current_ref)
        parent_ref = _parent_ref_of(client, current_ref, current_type)
        if not parent_ref:
            return None
        registration = get_registration("preservica_ref", parent_ref)
        if registration:
            return {"registration": registration, "root_ref": parent_ref}
        current_ref, current_type = parent_ref, "structural-objects"
    return None


def find_changed_preservica_collections(since, opts={}):
    """Find every registered Preservica collection with any change reported by
    Preservica since `since` — the basis for the auto-sync sensor (see
    dagster/lib/sensors.py's preservica_auto_sync_sensor).

    Also auto-registers a brand-new folder the first time its own 'argonath'
    identifier is seen — a folder with no 'argonath' identifier is never
    auto-registered, so it's simply absent from the result, per the "sensor
    ignores an unlabeled collection" rule. Once registered (by this or by a
    manual `digtk preservica import`), a collection is always checked here
    going forward, regardless of its workflow tags.

    Each affected collection's own originally-reported changed entities are
    preserved verbatim in its 'changed' list (deduped by (ref, type)) — even
    when the entity was resolved to its owning collection via
    _find_owning_collection()'s parent-walk-up, the entity's own ref/type is
    kept, never the walked-up parent. This is what lets
    preservica_auto_sync_sensor drive a targeted sync of just what changed
    (see preservica_sync_entities()) instead of a full collection re-walk.

    :param str since: ISO-8601 timestamp/date, passed straight to
        PreservicaClient.get_entities_updated_since().
    :param dict opts: 'client' (injectable PreservicaClient, defaults to one
        built from digtk.lib.auth.resolve_preservica()).
    :returns: {'collections': [{'group', 'name', 'root_ref',
        'changed': [{'ref', 'type'}, ...]}, ...]} — deduped, one entry per
        affected registered collection.
    :rtype: dict
    """
    client = opts.get("client") or preservica.build_client()
    changed = client.get_entities_updated_since(since)

    affected = {}
    for entity in changed:
        ref, entity_type = entity["ref"], entity["type"]
        if entity_type not in ("SO", "IO"):
            continue  # CO changes surface separately via their containing IO

        registration = get_registration("preservica_ref", ref)
        root_ref = ref

        if registration is None:
            found = _find_owning_collection(client, ref, entity_type)
            if found:
                registration, root_ref = found["registration"], found["root_ref"]

        if registration is None and entity_type == "SO":
            identifiers = client.get_identifiers("structural-objects", ref)
            if any(i.get("type") == "argonath" and i.get("value") for i in identifiers):
                title = (client.get_entity("structural-objects", ref).get("StructuralObject") or {}).get("Title")
                name = _slugify(title or ref) or ref
                registered = register_collection(
                    name, "preservica", "preservica",
                    [*identifiers, {"type": "preservica_ref", "value": ref}],
                )
                registration = {
                    "id": registered["id"], "name": registered["name"],
                    "group_name": "preservica", "source_type": "preservica",
                }
                root_ref = ref

        if registration and registration["source_type"] == "preservica":
            bucket = affected.setdefault(registration["id"], {
                "group": registration["group_name"], "name": registration["name"],
                "root_ref": root_ref, "changed": [],
            })
            pair = {"ref": ref, "type": entity_type}
            if pair not in bucket["changed"]:
                bucket["changed"].append(pair)

    return {"collections": list(affected.values())}


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


_TREE_LINK_METADATA = "preservica_tree rebuild"


def _link_tree_source(root, dest_rel, source_rel):
    """Record CaskFS lineage from a tree/ file just written back to the by-ref/
    file it was copied or derived from. A no-op when `root` is a local
    dest_root override (dev/testing without touching CaskFS — see
    _bronze_collection_root()), since fs.add_derivative_link() only accepts
    'cask:' paths.

    :param str root: fs-compatible bronze 'latest' root.
    :param str dest_rel: tree/ file just written, relative to `root`.
    :param str source_rel: by-ref/ file it was copied/derived from, relative to `root`.
    """
    if not root.startswith("cask:"):
        return
    fs.add_derivative_link(
        f"{root}/{dest_rel}",
        f"{root}/{source_rel}",
        {"metadata": _TREE_LINK_METADATA},
    )


def _copy_bronze_dir(root, src_rel, dest_rel):
    """Recursively copy every file under one bronze directory to another,
    preserving relative structure — both located under the same fs-compatible
    `root`. Used to materialize a tree/ item's binary files as byte-identical
    copies of by-ref/'s media/ (dedupes for free once both are in content-
    addressed CaskFS). Fixity sidecars ('*.fixity.json') are not copied — they
    describe by-ref/'s own copy and would just be dead weight in tree/. Each
    copied file gets a CaskFS lineage link back to its by-ref/ source (see
    _link_tree_source()).

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
        elif entry.name.endswith(".fixity.json"):
            continue
        else:
            fs.write(
                f"{root}/{dest_rel}/{entry.name}",
                src=f"{root}/{src_rel}/{entry.name}",
                opts={"replace": True},
            )
            _link_tree_source(root, f"{dest_rel}/{entry.name}", f"{src_rel}/{entry.name}")


# --- Preservica import (phase 1: by-ref) -------------------------------------

def _record_error(ctx, ref, stage, message):
    """Record a per-entity/per-file error and immediately surface it via
    ctx['on_progress'] — a full walk can take many minutes, so errors need to be
    visible as they happen rather than only in the manifest returned at the end.

    :param dict ctx: Walk context — see _walk_preservica_folder().
    :param str ref: Entity ref the error occurred on.
    :param str stage: Short stage name (e.g. 'entity', 'download').
    :param str message: Error message.
    """
    ctx["manifest"]["errors"].append({"ref": ref, "stage": stage, "message": message})
    ctx["on_progress"](f"Error [{stage}] {ref}: {message}")


def _record_warning(ctx, ref, stage, message):
    """Record a per-entity warning and immediately surface it via
    ctx['on_progress'] (see _record_error()).

    :param dict ctx: Walk context — see _walk_preservica_folder().
    :param str ref: Entity ref the warning applies to.
    :param str stage: Short stage name.
    :param str message: Warning message.
    """
    ctx["manifest"]["warnings"].append({"ref": ref, "stage": stage, "message": message})
    ctx["on_progress"](f"Warning [{stage}] {ref}: {message}")


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
        _record_error(ctx, ref, "identifiers", str(e))
        return []


def _fetch_and_write_metadata(client, entity, root, entity_dir, ctx):
    """Fetch and write one entity's descriptive metadata fragments into by-ref/ as
    raw XML, verbatim — one file per fragment under 'metadata/', 0-indexed
    (metadata/0.xml, metadata/1.xml, ...). No parsing happens at import time;
    that's deferred to preservica_tree() (see digtk.lib.preservica.parse_metadata_content),
    which converts a fragment's raw XML into tree/'s metadata.json.

    :param PreservicaClient client: Client instance.
    :param dict entity: {'ref', 'metadataFragmentRefs', ...} — see _walk_preservica_folder()/_walk_preservica_item().
    :param str root: fs-compatible bronze 'latest' root.
    :param str entity_dir: 'by-ref/<ref>', relative to `root`.
    :param dict ctx: Walk context — see _walk_preservica_folder().
    """
    fragment_refs = entity.get("metadataFragmentRefs") or []
    urls = [f.get("url") for f in fragment_refs]
    ctx["on_progress"](f"  {len(fragment_refs)} metadata fragment(s) for {entity['ref']}: {urls}")
    try:
        raw_fragments = [client.get_metadata_fragment(f["url"]) for f in fragment_refs]
    except Exception as e:
        _record_error(ctx, entity["ref"], "metadata", str(e))
        return
    for index, raw_xml in enumerate(raw_fragments):
        ctx["on_progress"](f"  metadata/{index}.xml: {len(raw_xml)} byte(s) fetched from {urls[index]}")
        fs.write(f"{root}/{entity_dir}/metadata/{index}.xml", data=raw_xml.encode(), opts={"replace": True})


def _fixity_unchanged(root, media_rel, fixity):
    """Whether `media_rel` is already present in bronze with a fixity sidecar
    matching Preservica's current `fixity` for that file — i.e. it's safe to
    skip re-downloading (see PreservicaClient.get_bitstream_detail()'s docstring,
    which is exactly what makes this cheap: Preservica already computes and
    stores the checksum, so no local hashing is needed).

    :param str root: fs-compatible bronze 'latest' root.
    :param str media_rel: Media file path, relative to `root`.
    :param dict|None fixity: {'algorithm', 'value'} from Preservica for this file.
    :returns: True if the existing file + sidecar match `fixity`.
    :rtype: bool
    """
    if not fixity or not fixity.get("value"):
        return False
    if not fs.exists(f"{root}/{media_rel}"):
        return False
    existing = _read_bronze_json(root, f"{media_rel}.fixity.json")
    if not existing:
        return False
    return (
        (existing.get("algorithm") or "").lower() == (fixity.get("algorithm") or "").lower()
        and existing.get("value") == fixity.get("value")
    )


def _walk_preservica_item(client, io_ref, root, ctx):
    """Leaf-fetch one Preservica information object into by-ref/: entity detail,
    identifiers, metadata, resolved download chain, the actual file bytes (fetched
    concurrently, bounded by ctx['concurrency']), and a best-effort thumbnail.

    :param PreservicaClient client: Client instance.
    :param str io_ref: Information object ref.
    :param str root: fs-compatible bronze 'latest' root.
    :param dict ctx: 'manifest' ({'items', 'errors', 'warnings'}), 'concurrency',
        'on_progress' (callable(str), see preservica_import()), 'skip_unchanged'
        (bool — skip re-downloading a file whose Preservica fixity matches an
        already-written local copy, see _fixity_unchanged()).
    :returns: {'entity': dict, 'identifiers': list[dict]}, or None if the
        entity fetch itself failed (error already recorded in ctx) — used by
        preservica_sync_entities() to resolve the synced item's key.
    :rtype: dict | None
    """
    entity_dir = f"by-ref/{io_ref}"
    try:
        raw = client.get_entity("information-objects", io_ref, {"expand_structure": True})
        entity = {
            "ref": io_ref,
            "title": (raw.get("InformationObject") or {}).get("Title"),
            "description": (raw.get("InformationObject") or {}).get("Description"),
            "type": "IO",
            "metadataFragmentRefs": client.list_metadata_fragments(raw),
        }
        _write_bronze_json(root, f"{entity_dir}/entity.json", entity)
    except Exception as e:
        _record_error(ctx, io_ref, "entity", str(e))
        return None

    ctx["on_progress"](f"item  {io_ref}  {entity['title'] or '(untitled)'}")
    ctx["manifest"]["items"].append({"ref": io_ref, "type": "IO", "title": entity["title"]})

    identifiers = _fetch_and_write_identifiers(client, "information-objects", io_ref, root, entity_dir, ctx)
    _fetch_and_write_metadata(client, entity, root, entity_dir, ctx)

    try:
        representations = client.resolve_all_content(io_ref, raw)
        _write_bronze_json(root, f"{entity_dir}/representations.json", representations)
    except Exception as e:
        _record_error(ctx, io_ref, "resolveAllContent", str(e))
        representations = []

    def _download_one(file):
        media_rel = f"{entity_dir}/media/{file['representationType']}/{file['filename']}"
        if ctx["skip_unchanged"] and _fixity_unchanged(root, media_rel, file.get("fixity")):
            ctx["on_progress"](f"  skip unchanged {file['filename']}")
            return
        try:
            data = client.download_file(file["downloadUrl"])
            fs.write(f"{root}/{media_rel}", data=data, opts={"replace": True})
            if file.get("fixity"):
                _write_bronze_json(root, f"{media_rel}.fixity.json", file["fixity"])
        except Exception as e:
            _record_error(ctx, io_ref, "download", f"{file.get('filename')}: {e}")

    if representations:
        ctx["on_progress"](f"  downloading {len(representations)} file(s) for {io_ref}")
        with ThreadPoolExecutor(max_workers=ctx["concurrency"]) as executor:
            list(executor.map(_download_one, representations))

        thumb = client.get_thumbnail(io_ref)
        if thumb:
            fs.write(f"{root}/{entity_dir}/media/thumbnail", data=thumb, opts={"replace": True})

    if not any(_is_ark(i.get("value") or "") for i in identifiers):
        _record_warning(ctx, io_ref, "identifiers", "no ARK-shaped identifier found")

    return {"entity": entity, "identifiers": identifiers}


def _sync_folder_self(client, so_ref, root, ctx, raw=None):
    """Fetch+write one structural object's own entity.json/identifiers.json/
    metadata — no children, no recursion. Shared by the full recursive walk
    (_walk_preservica_folder()) and the targeted shallow sync
    (preservica_sync_entities()), which must never recurse into subfolders or
    re-walk existing children just because their parent changed.

    :param PreservicaClient client: Client instance.
    :param str so_ref: Structural object ref.
    :param str root: fs-compatible bronze 'latest' root.
    :param dict ctx: Walk context — see _walk_preservica_folder().
    :param dict|None raw: Already-fetched EntityResponse for `so_ref`, if the
        caller needed it anyway — see _walk_preservica_folder()'s docstring.
    :returns: {'entity': dict, 'identifiers': list[dict]}, or None if the
        entity fetch itself failed (error already recorded in ctx).
    :rtype: dict | None
    """
    entity_dir = f"by-ref/{so_ref}"
    try:
        raw = raw if raw is not None else client.get_entity("structural-objects", so_ref)
        entity = {
            "ref": so_ref,
            "title": (raw.get("StructuralObject") or {}).get("Title"),
            "description": (raw.get("StructuralObject") or {}).get("Description"),
            "type": "SO",
            "metadataFragmentRefs": client.list_metadata_fragments(raw),
        }
        _write_bronze_json(root, f"{entity_dir}/entity.json", entity)
    except Exception as e:
        _record_error(ctx, so_ref, "entity", str(e))
        return None

    ctx["on_progress"](f"folder  {so_ref}  {entity['title'] or '(untitled)'}")
    ctx["manifest"]["items"].append({"ref": so_ref, "type": "SO", "title": entity["title"]})

    identifiers = _fetch_and_write_identifiers(client, "structural-objects", so_ref, root, entity_dir, ctx)
    _fetch_and_write_metadata(client, entity, root, entity_dir, ctx)
    return {"entity": entity, "identifiers": identifiers}


def _walk_preservica_folder(client, so_ref, root, ctx, raw=None):
    """Recursively walk one Preservica structural object into by-ref/: entity
    detail, identifiers, metadata (via _sync_folder_self()), and children
    list, then recurse into sub-folders (bounded by ctx['max_depth']) and
    leaf-fetch items.

    :param PreservicaClient client: Client instance.
    :param str so_ref: Structural object ref.
    :param str root: fs-compatible bronze 'latest' root.
    :param dict ctx: 'max_depth', 'depth', 'manifest' ({'items', 'errors', 'warnings'}),
        'concurrency', 'on_progress' (callable(str), see preservica_import()).
    :param dict|None raw: Already-fetched EntityResponse for `so_ref`, if the
        caller needed it anyway (see preservica_import()'s collection-name
        defaulting, which fetches the root entity to slugify its title) — avoids
        fetching the same entity twice. Only meaningful for the initial call;
        recursive calls into children always fetch fresh.
    """
    if _sync_folder_self(client, so_ref, root, ctx, raw=raw) is None:
        return

    entity_dir = f"by-ref/{so_ref}"
    try:
        children = client.get_children(so_ref)
        _write_bronze_json(root, f"{entity_dir}/children.json", children)
    except Exception as e:
        _record_error(ctx, so_ref, "children", str(e))
        children = []

    for child in children:
        if child["type"] == "SO":
            if ctx["depth"] + 1 <= ctx["max_depth"]:
                _walk_preservica_folder(client, child["ref"], root, {**ctx, "depth": ctx["depth"] + 1})
        elif child["type"] == "IO":
            _walk_preservica_item(client, child["ref"], root, ctx)


def preservica_import(ref, collection=None, group="preservica", opts={}):
    """Recursively walk a Preservica collection into bronze's by-ref/ layout — a
    near-verbatim, ref-keyed relocation of Preservica's own Entity API responses
    (entity.json, identifiers.json, metadata/<n>.xml, children.json,
    representations.json, media/<RepresentationType>/<file>(+.fixity.json)). No
    schema.org or other reinterpretation happens here — descriptive metadata is
    kept as raw XML, verbatim; conversion to JSON happens later, when building
    tree/ (see preservica_tree() / digtk.lib.preservica.parse_metadata_content()).

    Every run re-walks the whole subtree from `ref` and re-fetches/overwrites
    every entity's JSON (entity/identifiers/metadata/children/representations) in
    by-ref/ — there is no incremental diffing of those in this version (a natural
    follow-up if re-walking a large collection's structure on every run becomes
    too slow). File downloads are the one exception: with `opts['skip_unchanged']`
    (opt-in, default off) a file whose Preservica-reported fixity already matches
    an existing local copy is left alone instead of re-downloaded — see
    _fixity_unchanged(). Per-entity/per-file errors are recorded in the returned
    manifest rather than aborting the whole walk, so one bad item doesn't block
    the rest of the collection.

    :param str ref: Root Preservica structural object ref to walk.
    :param str|None collection: Collection name. Defaults to a slugified version
        of the root entity's own Preservica title (mirrors preservica-etl's
        resolveSlug()) — this fetches the root entity to read its title, so no
        extra request is made once the walk itself reaches it.
    :param str group: Bronze group namespace.
    :param dict opts: 'max_depth' (int, default unlimited — maximum folder
        recursion depth), 'dest_root' (str, optional — see
        _bronze_collection_root()), 'concurrency' (int, default
        CONFIG['preservica']['concurrency'] — max concurrent file downloads per
        item), 'client' (digtk.lib.preservica.PreservicaClient, injectable for
        tests — defaults to one built from digtk.lib.auth.resolve_preservica()),
        'on_progress' (callable(str), optional — invoked with a human-readable
        line as each folder/item is walked; defaults to a no-op, so callers that
        don't care about live progress see no behavior change), 'skip_unchanged'
        (bool, default False — opt-in fixity check to skip re-downloading files
        that haven't changed since the last import; see _fixity_unchanged()).
    :returns: {'collection': str (the resolved name), 'items': [{'ref', 'type',
        'title'}, ...], 'errors': [...], 'warnings': [...]}.
    :rtype: dict
    :raises ValueError: If Preservica credentials cannot be resolved and no
        `opts['client']` was given, or `collection` collides with a different
        active registered collection — see register_collection().
    """
    client = opts.get("client") or preservica.build_client()
    max_depth = opts.get("max_depth")
    max_depth = float("inf") if max_depth is None else max_depth
    on_progress = opts.get("on_progress") or (lambda message: None)
    skip_unchanged = bool(opts.get("skip_unchanged"))

    root_raw = None
    if not collection:
        root_raw = client.get_entity("structural-objects", ref)
        title = (root_raw.get("StructuralObject") or {}).get("Title")
        collection = _slugify(title or ref) or ref

    # Registration happens unconditionally, labeled or not (an unlabeled folder
    # is still tracked — the 'argonath' identifier, if present among
    # root_identifiers, only governs the auto-sync sensor's discovery of
    # brand-new folders and optional-stage gating; see register_collection()).
    root_identifiers = client.get_identifiers("structural-objects", ref)
    register_collection(
        collection, group, "preservica",
        [*root_identifiers, {"type": "preservica_ref", "value": ref}],
    )

    root = _bronze_latest_root(collection, group, opts)
    on_progress(f"Importing '{collection}' from ref {ref} into {root}")

    manifest = {"collection": collection, "items": [], "errors": [], "warnings": []}
    ctx = {
        "max_depth": max_depth,
        "depth": 0,
        "manifest": manifest,
        "concurrency": opts.get("concurrency", CONFIG["preservica"]["concurrency"]),
        "on_progress": on_progress,
        "skip_unchanged": skip_unchanged,
    }
    _walk_preservica_folder(client, ref, root, ctx, raw=root_raw)

    on_progress(
        f"Walked {len(manifest['items'])} entities "
        f"({len(manifest['errors'])} error(s), {len(manifest['warnings'])} warning(s))"
    )

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


def preservica_sync_entities(collection, group, entities, opts={}):
    """Sync only the specific Preservica entities that changed for one
    already-registered collection — never a full recursive re-walk of the
    collection (see preservica_import() for that). This is the targeted
    counterpart driven by dagster/lib/assets.py's reconcile_collection_items
    when triggered by preservica_auto_sync_sensor's per-collection 'changed'
    list (see find_changed_preservica_collections()).

    An IO entity is leaf-fetched via _walk_preservica_item() (entity,
    identifiers, metadata, files) and classified 'added' if its by-ref/
    entity.json didn't already exist before this call, else 'changed'.

    An SO entity has only its own entity/identifiers/metadata refreshed (via
    _sync_folder_self()) plus its children.json re-fetched and diffed against
    the previously-stored version, to catch newly-added/removed *immediate*
    children — an existing subfolder is never recursed into, and an existing,
    unchanged direct child item is never re-fetched just because its parent
    changed. A newly-appeared child IO gets a full leaf-fetch (reported in
    'added'). A newly-appeared child SO gets only its own entity/identifiers/
    metadata written (no children.json fetch, no cascade — that subfolder's
    own contents are discovered later, either when Preservica itself reports
    it as changed, or as its individual items surface independently in a
    later updated-since batch) and is not itself partition-reportable
    (folders aren't Dagster 'items' partitions). A child IO that disappears
    from children.json has its key reported in 'removed'; its bronze files
    are left in place (never deleted), matching preservica_import()'s
    existing never-delete-stale-bronze behavior. A removed child SO is
    logged only — no partition action, since folders were never partitioned.

    :param str collection: Registered collection name.
    :param str group: Bronze group namespace.
    :param list[dict] entities: [{'ref', 'type'}, ...] changed entities
        belonging to this collection — 'type' 'SO' or 'IO'; any other type is
        skipped with a recorded warning. Duplicate (ref, type) pairs are
        synced only once.
    :param dict opts: 'client' (injectable digtk.lib.preservica.PreservicaClient,
        defaults to one built from digtk.lib.auth.resolve_preservica()),
        'dest_root' (str, optional — see _bronze_collection_root()),
        'concurrency' (int, default CONFIG['preservica']['concurrency']),
        'on_progress' (callable(str), optional), 'skip_unchanged' (bool,
        default False — see preservica_import()'s).
    :returns: {'added': [...], 'removed': [...], 'changed': [...],
        'errors': [...], 'warnings': [...]} — added/removed/changed are ARK
        (or fallback key, see _resolve_identifier_key()) lists for IO
        entities only, in the shape dagster/lib/assets.py's
        reconcile_collection_items expects.
    :rtype: dict
    :raises ValueError: If collection/group isn't an active registered
        Preservica collection, or Preservica credentials can't be resolved
        and no opts['client'] was given.
    """
    client = opts.get("client") or preservica.build_client()
    registration = _get_active_registration_by_name(collection, group, opts)
    root = _bronze_latest_root(collection, group, opts)

    manifest = {"items": [], "errors": [], "warnings": []}
    ctx = {
        "manifest": manifest,
        "concurrency": opts.get("concurrency", CONFIG["preservica"]["concurrency"]),
        "on_progress": opts.get("on_progress") or (lambda message: None),
        "skip_unchanged": bool(opts.get("skip_unchanged")),
    }

    added, removed, changed_out = [], [], []

    def _sync_io(io_ref):
        existed = fs.exists(f"{root}/by-ref/{io_ref}/entity.json")
        result = _walk_preservica_item(client, io_ref, root, ctx)
        if result is None:
            return
        key = _resolve_identifier_key(result["identifiers"], io_ref)["key"]
        (changed_out if existed else added).append(key)

    def _sync_so(so_ref):
        before_children = _read_bronze_json(root, f"by-ref/{so_ref}/children.json", [])
        if _sync_folder_self(client, so_ref, root, ctx) is None:
            return

        try:
            after_children = client.get_children(so_ref)
            _write_bronze_json(root, f"by-ref/{so_ref}/children.json", after_children)
        except Exception as e:
            _record_error(ctx, so_ref, "children", str(e))
            return

        before_by_ref = {c["ref"]: c for c in before_children}
        after_by_ref = {c["ref"]: c for c in after_children}

        for ref, child in after_by_ref.items():
            if ref in before_by_ref:
                continue
            if child["type"] == "IO":
                _sync_io(ref)
            elif child["type"] == "SO":
                _sync_folder_self(client, ref, root, ctx)

        for ref, child in before_by_ref.items():
            if ref in after_by_ref:
                continue
            if child["type"] == "IO":
                identifiers = _read_bronze_json(root, f"by-ref/{ref}/identifiers.json", [])
                removed.append(_resolve_identifier_key(identifiers, ref)["key"])
            else:
                ctx["on_progress"](f"folder  {ref}  removed from parent {so_ref} (bronze left in place)")

    seen = set()
    for entity in entities:
        pair = (entity["ref"], entity["type"])
        if pair in seen:
            continue
        seen.add(pair)
        ref, entity_type = pair
        if entity_type == "IO":
            _sync_io(ref)
        elif entity_type == "SO":
            _sync_so(ref)
        else:
            _record_warning(ctx, ref, "sync", f"unsupported entity type {entity_type!r} — skipped")

    _touch_last_synced(registration["id"], opts)

    return {
        "added": added, "removed": removed, "changed": changed_out,
        "errors": manifest["errors"], "warnings": manifest["warnings"],
    }


# --- Bronze tree layout (phase 2: Preservica by-ref -> tree; phase 3: local disk -> tree) --

def _copy_metadata_to_tree(root, entity_dir, dest_rel):
    """Build one entity's tree/ metadata.json: its own Preservica entity
    properties (ref/title/description/type — everything in by-ref/'s
    entity.json except the internal metadataFragmentRefs list) under a
    'preservica' key, merged with its descriptive metadata fragment's Content
    payload (e.g. {'dc': {...}}, '@_' attribute keys stripped — see
    digtk.lib.preservica.parse_metadata_content()) if it has one. A no-op
    (nothing written) only if the entity has neither — in practice every
    entity has its own by-ref/entity.json, so metadata.json is written for
    every tree/ entry, with or without a descriptive fragment.

    Every argonath Preservica collection so far has at most one descriptive
    metadata fragment per entity — more than one is surfaced loudly as a
    ValueError rather than silently guessing a merge shape for tree/. The
    written metadata.json gets a CaskFS lineage link back to each by-ref/
    source it was derived from (see _link_tree_source()).

    :param str root: fs-compatible bronze 'latest' root.
    :param str entity_dir: 'by-ref/<ref>', relative to `root`.
    :param str dest_rel: Destination tree/ directory, relative to `root`.
    :raises ValueError: If the entity has more than one metadata fragment.
    """
    content = {}
    sources = []

    entity_rel = f"{entity_dir}/entity.json"
    entity = _read_bronze_json(root, entity_rel)
    if entity:
        content["preservica"] = {k: v for k, v in entity.items() if k != "metadataFragmentRefs"}
        sources.append(entity_rel)

    metadata_dir = f"{entity_dir}/metadata"
    if fs.exists(f"{root}/{metadata_dir}"):
        fragments = sorted(fs.ls(f"{root}/{metadata_dir}"), key=lambda entry: entry.name)
        if len(fragments) > 1:
            raise ValueError(
                f"{entity_dir} has {len(fragments)} metadata fragments — preservica_tree() "
                "only supports a single fragment per entity"
            )
        if fragments:
            fragment_rel = f"{metadata_dir}/{fragments[0].name}"
            raw_xml = fs.read(f"{root}/{fragment_rel}").decode()
            content.update(preservica.parse_metadata_content(raw_xml))
            sources.append(fragment_rel)

    if not content:
        return
    _write_bronze_json(root, f"{dest_rel}/metadata.json", content)
    for source_rel in sources:
        _link_tree_source(root, f"{dest_rel}/metadata.json", source_rel)


def _build_tree_from_by_ref(so_ref, root, dest_rel):
    """Recursively rebuild one folder's worth of tree/ entries from by-ref/'s
    already-current children.json — always a full rebuild (see
    preservica_tree()), never an incremental patch, so a renamed or removed item
    never leaves a stale directory behind. Each item/sub-folder's own
    identifiers.json is copied in verbatim (Preservica's raw identifier list,
    unchanged) as the only identity-carrying file — the exact same bytes by-ref/
    already has, so it dedups for free in CaskFS. It also gets a CaskFS lineage
    link back to that by-ref/ source (see _link_tree_source()).

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
        identifiers_rel = f"by-ref/{child['ref']}/identifiers.json"
        child_identifiers = _read_bronze_json(root, identifiers_rel, [])
        key = _resolve_identifier_key(child_identifiers, child["ref"])["key"]
        slug = _unique_tree_slug(child["title"], key, used_slugs)
        child_dest_rel = f"{dest_rel}/{slug}"

        if child["type"] == "SO":
            if child_identifiers:
                _write_bronze_json(root, f"{child_dest_rel}/identifiers.json", child_identifiers)
                _link_tree_source(root, f"{child_dest_rel}/identifiers.json", identifiers_rel)
            _copy_metadata_to_tree(root, f"by-ref/{child['ref']}", child_dest_rel)
            count += 1
            count += _build_tree_from_by_ref(child["ref"], root, child_dest_rel)
        elif child["type"] == "IO":
            _copy_bronze_dir(root, f"by-ref/{child['ref']}/media", child_dest_rel)
            _write_bronze_json(root, f"{child_dest_rel}/identifiers.json", child_identifiers)
            if child_identifiers:
                _link_tree_source(root, f"{child_dest_rel}/identifiers.json", identifiers_rel)
            _copy_metadata_to_tree(root, f"by-ref/{child['ref']}", child_dest_rel)
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

    Also writes a tree/metadata.json for the collection's own root folder
    (its 'preservica' entity properties plus its own descriptive metadata
    fragment, if it has one — same shape as every other tree/ entry's
    metadata.json, see _copy_metadata_to_tree()) — the root folder itself
    isn't otherwise represented anywhere under tree/, only its children are.

    :param str collection: Collection name.
    :param str group: Bronze group namespace.
    :param dict opts: 'dest_root' (str, optional — see _bronze_collection_root()).
    :returns: Number of tree entries written (the root's own metadata.json
        isn't counted, matching that it was never a tree/ entry before).
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

    _copy_metadata_to_tree(root, f"by-ref/{root_so_ref}", "tree")
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
