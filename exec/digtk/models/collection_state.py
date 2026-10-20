"""
collection_state — argonath's processing-state store: which collections and
items are being tracked, the work queue Dagster's sensors poll, and each job's
latest result (see exec/digtk/sql/collection_state.sql for the schema itself).

Identity:
  - a collection's `name` is its collection key, "bronze/[group]/[collection]"
    (e.g. "bronze/preservica/haeger") — the same key Dagster's 'collections'
    partitions use;
  - an item's `name` is its bronze folder path, with no leading slash (e.g.
    "bronze/preservica/haeger/latest/tree/quarter-mile") — the same form
    Dagster's process_item_job is launched with. Its ARK is an identifier
    (item_identifiers, id_type 'ark'), kept after the item leaves bronze.

Visibility: a collection is 'public' if its argonath.collection_registry row's
workflow_tags include 'public' (see collection.register_collection()), else
'private' (also for a collection that isn't registered at all); its items
inherit that value. Nothing tracks per-item access yet.

The queue: enqueue_collection() writes one job_queue row per item for
ITEM_JOB, replacing any row already pending for the same item, so the latest
request wins. Items that have left the collection's bronze tree/ are not
removed or unlinked here (their ES docs are pruned by `digtk showcase es prune`). Rows aren't claimed
here — Dagster's item_queue_sensor reads them with pending() and deletes them
with ack() one tick after submitting their runs (see
dagster/lib/pipeline_collection.py), so a crashed sensor tick never loses a row. Outcomes go to job_result via record_result() (one row per job
and target, through the schema's upsert_job_result()).

Rollup: rollups_due() finds collections with item results newer than their
last ROLLUP_JOB result and nothing left in the queue; rollup() recomputes the
collection's item count and failed items into its own ROLLUP_JOB result.

Every table name is schema-qualified — digtk.lib.db.connect() sets
search_path to the 'argonath' schema, not this one.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

from digtk.lib import db, items
from digtk.models import collection as collection_model

_SCHEMA_SQL_PATH = Path(__file__).parent.parent / "sql" / "collection_state.sql"

# job_name values this module reads and writes — they match the Dagster job names.
ITEM_JOB = "process_item_job"
ROLLUP_JOB = "collection_rollup_job"


def init_pg(opts={}):
    """Apply the collection_state schema (exec/digtk/sql/collection_state.sql).
    Safe to run repeatedly — every statement is IF NOT EXISTS / OR REPLACE.

    :param dict opts: 'conn' (injectable psycopg2 connection, for tests/reuse).
    """
    db.run_script(_SCHEMA_SQL_PATH.read_text(), opts)


def item_name(path):
    """An item's collection_state name: its bronze folder path with no 'cask:'
    prefix, leading slash or trailing slash.

    :param str path: e.g. "cask:/bronze/preservica/haeger/latest/tree/quarter-mile/".
    :returns: e.g. "bronze/preservica/haeger/latest/tree/quarter-mile".
    :rtype: str
    :raises ValueError: If the path isn't a folder under bronze/[group]/[collection]/[time].
    """
    return items.normalize_bronze_path(path).lstrip("/")


def collection_key(path):
    """The collection key ("bronze/[group]/[collection]") a bronze path lives under.

    :param str path: A collection key or any bronze path below one, in any
        form normalize_bronze_path() accepts.
    :returns: e.g. "bronze/preservica/haeger".
    :rtype: str
    :raises ValueError: If the path isn't under bronze/[group]/[collection].
    """
    raw = path[len("cask:"):] if path.startswith("cask:") else path
    segments = raw.strip().strip("/").split("/")
    if segments[0] != "bronze" or len(segments) < 3 or not all(segments[:3]):
        raise ValueError(f"not under bronze/[group]/[collection]: {path}")
    return "/".join(segments[:3])


def _iso(value):
    """A timestamp as an ISO string (JSON-safe), passing anything else through.

    :param value: datetime or anything.
    :returns: ISO 8601 string for a datetime, else value unchanged.
    """
    return value.isoformat() if isinstance(value, datetime) else value


@contextmanager
def _transaction(opts={}):
    """A dict-row cursor inside one transaction: committed when the block
    finishes, rolled back if it raises.

    :param dict opts: 'conn' (injectable psycopg2 connection — when given, the
        caller owns commit/close and this only yields a cursor on it).
    :yields: psycopg2 RealDictCursor.
    """
    conn = opts.get("conn") or db.connect()
    owned = "conn" not in opts
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        if owned:
            conn.commit()
    except Exception:
        if owned:
            conn.rollback()
        raise
    finally:
        if owned:
            conn.close()


def registry_info(key):
    """A collection's visibility and identifiers, from argonath.collection_registry.

    Looked up on its own connection (not inside a collection_state transaction)
    so a missing registry schema can't abort the caller's transaction.

    :param str key: Collection key, "bronze/[group]/[collection]".
    :returns: {"visibility": "public"|"private", "identifiers": [(id_type, id_value), ...]}
        — 'private' with no identifiers if the collection isn't registered.
    :rtype: dict
    """
    _, group, name = key.split("/")
    try:
        reg = collection_model._get_active_registration_by_name(name, group)
        idents = db.query(
            "SELECT id_type, id_value FROM argonath.collection_identifiers WHERE collection_id = %s",
            (reg["id"],),
        )
    except (ValueError, psycopg2.Error):
        return {"visibility": "private", "identifiers": []}
    tags = reg.get("workflow_tags") or []
    return {
        "visibility": "public" if "public" in tags else "private",
        "identifiers": [("registry_id", str(reg["id"]))] + [(i["id_type"], i["id_value"]) for i in idents],
    }


def _upsert_collection(cur, key, info):
    """Insert or update a collection row and add its identifiers.

    :param cur: Cursor inside the caller's transaction.
    :param str key: Collection key.
    :param dict info: registry_info()'s result.
    :returns: {"id", "visibility"}.
    :rtype: dict
    """
    cur.execute(
        "INSERT INTO collection_state.collection (name, visibility) VALUES (%s, %s) "
        "ON CONFLICT (name) DO UPDATE SET visibility = EXCLUDED.visibility, last_updated_at = now() "
        "RETURNING id, visibility",
        (key, info["visibility"]),
    )
    row = cur.fetchone()
    for id_type, id_value in info["identifiers"]:
        cur.execute(
            "INSERT INTO collection_state.collection_identifiers (collection_id, id_type, id_value) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (row["id"], id_type, id_value),
        )
    return dict(row)


def _upsert_items(cur, entries, visibility):
    """Insert or update item rows and add their ARK identifiers.

    :param cur: Cursor inside the caller's transaction.
    :param list[tuple[str, str|None]] entries: (item name, ARK or None) pairs.
    :param str visibility: 'public' or 'private' (the collection's).
    :returns: {item name: item id}.
    :rtype: dict
    """
    if not entries:
        return {}
    names = [name for name, _ in entries]
    cur.execute(
        "INSERT INTO collection_state.item (name, visibility) SELECT unnest(%s::text[]), %s "
        "ON CONFLICT (name) DO UPDATE SET visibility = EXCLUDED.visibility, last_updated_at = now() "
        "RETURNING id, name",
        (names, visibility),
    )
    ids = {row["name"]: row["id"] for row in cur.fetchall()}
    cur.execute(
        "INSERT INTO collection_state.item_identifiers (item_id, id_type, id_value) "
        "SELECT i.id, 'ark', x.ark FROM unnest(%s::text[], %s::text[]) AS x(name, ark) "
        "JOIN collection_state.item i ON i.name = x.name WHERE x.ark IS NOT NULL "
        "ON CONFLICT DO NOTHING",
        (names, [ark for _, ark in entries]),
    )
    return ids


def _link_items(cur, collection_id, item_ids):
    """Link items to a collection (collection_item), skipping existing links.

    :param cur: Cursor inside the caller's transaction.
    :param int collection_id: Collection row id.
    :param list[int] item_ids: Item row ids.
    :returns: Ids of the items that weren't linked before.
    :rtype: set[int]
    """
    if not item_ids:
        return set()
    cur.execute(
        "INSERT INTO collection_state.collection_item (collection_id, item_id) "
        "SELECT %s, unnest(%s::int[]) ON CONFLICT DO NOTHING RETURNING item_id",
        (collection_id, list(item_ids)),
    )
    return {row["item_id"] for row in cur.fetchall()}


def _enqueue(cur, job_name, requests):
    """Queue item jobs, replacing any row already pending for the same item and job.

    :param cur: Cursor inside the caller's transaction.
    :param str job_name: e.g. ITEM_JOB.
    :param list[tuple[int, dict]] requests: (item id, job config) pairs.
    :returns: How many rows were queued.
    :rtype: int
    """
    if not requests:
        return 0
    item_ids = [item_id for item_id, _ in requests]
    cur.execute(
        "DELETE FROM collection_state.job_queue WHERE job_name = %s AND item_id = ANY(%s::int[])",
        (job_name, item_ids),
    )
    cur.execute(
        "INSERT INTO collection_state.job_queue (item_id, job_name, config) "
        "SELECT x.item_id, %s, x.config::jsonb FROM unnest(%s::int[], %s::text[]) AS x(item_id, config)",
        (job_name, item_ids, [json.dumps(config) for _, config in requests]),
    )
    return len(requests)


def enqueue_collection(key, opts={}):
    """Queue an ITEM_JOB run for every item currently in a collection's bronze
    items root (see digtk.models.collection.list_item_folders()).

    Upserts the collection and its items and links new items — all in one
    transaction. A pending row for the same item is replaced, so enqueuing
    twice doesn't run an item twice. Items no longer present are left as they
    are (`digtk showcase es prune` removes their ES docs).

    :param str key: Collection key, e.g. "bronze/preservica/haeger".
    :param dict opts: 'batch' (str — recorded on each row, e.g. the launching
        Dagster run id), 'trigger' (str, default "collection"), 'conn'
        (injectable psycopg2 connection).
    :returns: {"collection", "items": int, "added": int, "enqueued": int}.
    :rtype: dict
    """
    key = collection_key(key)
    _, group, name = key.split("/")
    current = []
    for folder_path in collection_model.list_item_folders(name, {"group": group}):
        path = item_name(folder_path)
        if path.startswith(f"{key}/"):
            try:
                ark = items.ark_for_path(folder_path)
            except LookupError:
                ark = None
            current.append((path, ark))

    info = registry_info(key)
    extra = {"trigger": opts.get("trigger", "collection")}
    if opts.get("batch"):
        extra["batch"] = opts["batch"]

    with _transaction(opts) as cur:
        coll = _upsert_collection(cur, key, info)
        ids = _upsert_items(cur, current, coll["visibility"])
        added = _link_items(cur, coll["id"], ids.values())
        requests = [(ids[path], {"item": path, **extra}) for path, _ in current]
        enqueued = _enqueue(cur, ITEM_JOB, requests)

    return {"collection": key, "items": len(current), "added": len(added), "enqueued": enqueued}


def pending(job_name, limit, opts={}):
    """The oldest queued rows for a job, without removing them (see ack()).

    :param str job_name: e.g. ITEM_JOB.
    :param int limit: Most rows to return.
    :param dict opts: 'conn' (injectable psycopg2 connection).
    :returns: [{"id", "job_name", "config": dict, "item": str|None, "collection": str|None, "created_at"}].
    :rtype: list[dict]
    """
    if limit <= 0:
        return []
    rows = db.query(
        "SELECT q.id, q.job_name, q.config, i.name AS item, c.name AS collection, q.created_at "
        "FROM collection_state.job_queue q "
        "LEFT JOIN collection_state.item i ON i.id = q.item_id "
        "LEFT JOIN collection_state.collection c ON c.id = q.collection_id "
        "WHERE q.job_name = %s ORDER BY q.created_at, q.id LIMIT %s",
        (job_name, limit), opts,
    )
    return [{**row, "config": row["config"] or {}, "created_at": _iso(row["created_at"])} for row in rows]


def ack(ids, opts={}):
    """Remove queue rows whose work has been handed to Dagster.

    :param list[int] ids: job_queue ids.
    :param dict opts: 'conn' (injectable psycopg2 connection).
    :returns: How many rows were removed (already-removed ids are ignored).
    :rtype: int
    """
    if not ids:
        return 0
    rows = db.execute(
        "DELETE FROM collection_state.job_queue WHERE id = ANY(%s::int[]) RETURNING id",
        ([int(i) for i in ids],), {**opts, "returning": True},
    )
    return len(rows)


def record_result(job_name, opts={}):
    """Record a job's latest result for one item or one collection (one row per
    job and target — later results replace earlier ones).

    For an item, the item (and its collection) are created if they aren't
    tracked yet and linked, so a manual run of an item the queue never saw is
    still recorded.

    :param str job_name: e.g. ITEM_JOB.
    :param dict opts: exactly one of 'item' (bronze folder path) / 'collection'
        (collection key); 'result' (dict — stored as JSONB, e.g.
        {"status": "success"|"failed", "run_id", "ark", "error", "finished_at"}); 'ark' (str — added
        as an item identifier); 'conn' (injectable psycopg2 connection).
    :returns: The job_result row ({"id", "job_name", "collection_id", "item_id", "result", "updated_at"}).
    :rtype: dict
    :raises ValueError: If not exactly one of item/collection is given.
    """
    item, coll_key = opts.get("item"), opts.get("collection")
    if bool(item) == bool(coll_key):
        raise ValueError("give exactly one of item or collection")
    result = opts.get("result") or {}
    key = collection_key(item or coll_key)
    info = registry_info(key)

    with _transaction(opts) as cur:
        coll = _upsert_collection(cur, key, info)
        if item:
            name = item_name(item)
            item_id = _upsert_items(cur, [(name, opts.get("ark"))], coll["visibility"])[name]
            _link_items(cur, coll["id"], [item_id])
            target = (None, item_id)
        else:
            target = (coll["id"], None)
        cur.execute(
            "SELECT * FROM collection_state.upsert_job_result(%s, %s::integer, %s::integer, %s::jsonb)",
            (job_name, target[0], target[1], json.dumps(result)),
        )
        row = dict(cur.fetchone())
    return {**row, "updated_at": _iso(row["updated_at"])}


def item_ark(path, opts={}):
    """An item's most recently recorded ARK, from collection_state rather than
    its bronze identifiers.json.

    :param str path: Item bronze folder path.
    :param dict opts: 'conn' (injectable psycopg2 connection).
    :returns: The ARK, or None if none was ever recorded.
    :rtype: str|None
    """
    rows = db.query(
        "SELECT ii.id_value FROM collection_state.item_identifiers ii "
        "JOIN collection_state.item i ON i.id = ii.item_id "
        "WHERE i.name = %s AND ii.id_type = 'ark' ORDER BY ii.created_at DESC, ii.id DESC LIMIT 1",
        (item_name(path),), opts,
    )
    return rows[0]["id_value"] if rows else None


def rollups_due(opts={}):
    """Collections whose items have results newer than the collection's last
    rollup, and nothing still queued for any of their items.

    :param dict opts: 'conn' (injectable psycopg2 connection).
    :returns: [{"collection": key, "latest_result_at": ISO str}], by collection.
    :rtype: list[dict]
    """
    rows = db.query(
        "WITH latest AS ("
        "  SELECT ci.collection_id, max(r.updated_at) AS latest_result_at "
        "  FROM collection_state.collection_item ci "
        "  JOIN collection_state.job_result r ON r.item_id = ci.item_id AND r.job_name = %s "
        "  GROUP BY ci.collection_id"
        ") "
        "SELECT c.name AS collection, l.latest_result_at FROM latest l "
        "JOIN collection_state.collection c ON c.id = l.collection_id "
        "LEFT JOIN collection_state.job_result cr ON cr.collection_id = c.id AND cr.job_name = %s "
        "WHERE (cr.updated_at IS NULL OR l.latest_result_at > cr.updated_at) "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM collection_state.job_queue q "
        "  JOIN collection_state.collection_item ci2 ON ci2.item_id = q.item_id "
        "  WHERE ci2.collection_id = c.id"
        ") ORDER BY c.name",
        (ITEM_JOB, ROLLUP_JOB), opts,
    )
    return [{"collection": row["collection"], "latest_result_at": _iso(row["latest_result_at"])} for row in rows]


def rollup(key, opts={}):
    """Recompute a collection's item count and item outcomes from each linked
    item's latest ITEM_JOB result, and record them as the collection's
    ROLLUP_JOB result.

    :param str key: Collection key.
    :param dict opts: 'conn' (injectable psycopg2 connection).
    :returns: {"collection", "item_count", "succeeded", "failed", "never_run",
        "failed_items": [{"item", "error", "run_id"}], "rolled_up_at"}.
    :rtype: dict
    :raises LookupError: If the collection isn't tracked in collection_state.
    """
    key = collection_key(key)
    rows = db.query(
        "SELECT c.id AS collection_id, i.name AS item, r.result "
        "FROM collection_state.collection c "
        "LEFT JOIN collection_state.collection_item ci ON ci.collection_id = c.id "
        "LEFT JOIN collection_state.item i ON i.id = ci.item_id "
        "LEFT JOIN collection_state.job_result r ON r.item_id = i.id AND r.job_name = %s "
        "WHERE c.name = %s ORDER BY i.name",
        (ITEM_JOB, key), opts,
    )
    if not rows:
        raise LookupError(f"collection not tracked in collection_state: {key}")

    summary = {"item_count": 0, "succeeded": 0, "failed": 0, "never_run": 0, "failed_items": []}
    for row in rows:
        if row["item"] is None:
            continue
        summary["item_count"] += 1
        status = (row["result"] or {}).get("status")
        if status == "success":
            summary["succeeded"] += 1
        elif status == "failed":
            summary["failed"] += 1
            summary["failed_items"].append({
                "item": row["item"], "error": row["result"].get("error"), "run_id": row["result"].get("run_id"),
            })
        else:
            summary["never_run"] += 1

    result = {"collection": key, **summary, "rolled_up_at": datetime.now(timezone.utc).isoformat()}
    record_result(ROLLUP_JOB, {**opts, "collection": key, "result": result})
    return result
