"""Tests for digtk.models.collection_registry.

The path/key helpers are plain unit tests. Everything that issues SQL runs
against a real Postgres (the queue and rollup logic is set-based SQL, which a
fake can't meaningfully check), so those tests need DIGTK_TEST_PG_DSN set to a
scratch database, e.g.

    docker run -d --rm --name cs-test -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
    DIGTK_TEST_PG_DSN=postgresql://postgres:test@localhost:55432/postgres pytest tests/test_models_collection_registry.py

and are skipped otherwise. Each test gets freshly created schemas, dropped afterwards.
"""
import os

import psycopg2
import pytest

from digtk.lib import db
from digtk.models import collection as collection_model
from digtk.models import collection_registry

DSN = os.environ.get("DIGTK_TEST_PG_DSN")
KEY = "bronze/preservica/haeger"
ITEM_A = f"{KEY}/latest/tree/a"
ITEM_B = f"{KEY}/latest/tree/b"

needs_pg = pytest.mark.skipif(not DSN, reason="DIGTK_TEST_PG_DSN not set")


# --- path helpers ------------------------------------------------------------

def test_item_name_normalizes_every_accepted_form():
    for path in (f"cask:/{ITEM_A}/", f"/{ITEM_A}", ITEM_A):
        assert collection_registry.item_name(path) == ITEM_A


def test_item_name_rejects_non_bronze_paths():
    with pytest.raises(ValueError):
        collection_registry.item_name("silver/preservica/haeger/latest/tree/a")


def test_collection_key_takes_first_three_segments():
    assert collection_registry.collection_key(f"cask:/{ITEM_A}") == KEY
    assert collection_registry.collection_key(KEY) == KEY


def test_collection_key_rejects_short_or_non_bronze_paths():
    for path in ("bronze/preservica", "silver/a/b/c"):
        with pytest.raises(ValueError):
            collection_registry.collection_key(path)


def test_init_pg_runs_the_schema_sql_file(monkeypatch):
    seen = {}
    monkeypatch.setattr(collection_registry.db, "run_script", lambda sql, opts={}: seen.update(sql=sql))
    collection_registry.init_pg()
    assert "CREATE SCHEMA IF NOT EXISTS collection_registry" in seen["sql"]
    assert "upsert_job_result" in seen["sql"]


def test_record_result_requires_exactly_one_target():
    with pytest.raises(ValueError):
        collection_registry.record_result("process_item_job", {"result": {}})
    with pytest.raises(ValueError):
        collection_registry.record_result("process_item_job", {"item": ITEM_A, "collection": KEY})


# --- against Postgres --------------------------------------------------------

@pytest.fixture
def pg(monkeypatch):
    """A fresh collection_registry schema on DIGTK_TEST_PG_DSN; every connection
    the model opens goes there."""
    def connect():
        return psycopg2.connect(DSN, options=f"-c search_path={db.SCHEMA},public")
    monkeypatch.setattr(db, "connect", connect)

    conn = connect()
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS collection_registry CASCADE;")
    conn.commit()
    collection_registry.init_pg()
    collection_registry.init_pg()  # idempotent
    yield conn
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS collection_registry CASCADE;")
    conn.commit()
    conn.close()


@pytest.fixture
def folders(monkeypatch):
    """Set the item folders list_item_folders() reports, and each one's ark (as
    resolved via items.ark_for_path()), as {path: ark|None}. A None value means
    the item has no ark, same as items.ark_for_path() raising LookupError."""
    current = {}

    def list_item_folders(collection, opts={}):
        return [f"/{path}" for path in sorted(current)]

    def ark_for_path(path):
        key = path.lstrip("/")
        ark = current.get(key)
        if not ark:
            raise LookupError(f"MISSING ARK: {path}")
        return ark

    monkeypatch.setattr(collection_model, "list_item_folders", list_item_folders)
    monkeypatch.setattr(collection_registry.items, "ark_for_path", ark_for_path)
    return current


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
    conn.commit()
    return rows


@needs_pg
def test_enqueue_collection_tracks_links_and_queues_every_item(pg, folders):
    folders.update({ITEM_A: "ark:/1/a", ITEM_B: "ark:/1/b"})

    result = collection_registry.enqueue_collection(KEY, {"batch": "run-1"})

    assert result == {"collection": KEY, "items": 2, "added": 2, "enqueued": 2}
    assert _rows(pg, "SELECT name, source_type FROM collection_registry.collection") == [(KEY, "unknown")]
    assert collection_registry.item_ark(ITEM_B) == "ark:/1/b"
    rows = collection_registry.pending("process_item_job", 10)
    assert [r["item"] for r in rows] == [ITEM_A, ITEM_B]
    assert rows[0]["config"] == {"item": ITEM_A, "trigger": "collection", "batch": "run-1"}


@needs_pg
def test_enqueue_collection_force_is_recorded_on_every_row(pg, folders):
    folders.update({ITEM_A: "ark:/1/a", ITEM_B: "ark:/1/b"})

    collection_registry.enqueue_collection(KEY, {"batch": "run-1", "force": True})

    rows = collection_registry.pending("process_item_job", 10)
    assert [r["config"]["force"] for r in rows] == [True, True]


@needs_pg
def test_enqueue_collection_still_queues_items_with_no_ark(pg, folders):
    # Not every collection's items carry an ark -- a missing one shouldn't
    # drop the item from the queue, just leave item_ark() with nothing to report.
    folders.update({ITEM_A: "ark:/1/a", ITEM_B: None})

    result = collection_registry.enqueue_collection(KEY)

    assert result == {"collection": KEY, "items": 2, "added": 2, "enqueued": 2}
    assert collection_registry.item_ark(ITEM_A) == "ark:/1/a"
    assert collection_registry.item_ark(ITEM_B) is None
    rows = collection_registry.pending("process_item_job", 10)
    assert [r["item"] for r in rows] == [ITEM_A, ITEM_B]


@needs_pg
def test_enqueue_items_queues_just_those_items(pg, folders):
    folders.update({ITEM_A: "ark:/1/a", ITEM_B: "ark:/1/b"})

    result = collection_registry.enqueue_items([f"/{ITEM_B}", f"cask:/{ITEM_B}"], {"trigger": "cask-audit"})

    assert result == {"items": 1, "enqueued": 1, "collections": [KEY]}
    rows = collection_registry.pending("process_item_job", 10)
    assert [r["item"] for r in rows] == [ITEM_B]
    assert rows[0]["config"] == {"item": ITEM_B, "trigger": "cask-audit"}
    assert collection_registry.item_ark(ITEM_B) == "ark:/1/b"

    collection_registry.enqueue_items([ITEM_B])
    assert len(collection_registry.pending("process_item_job", 10)) == 1


@needs_pg
def test_enqueue_collection_twice_replaces_pending_rows(pg, folders):
    folders.update({ITEM_A: "ark:/1/a"})
    collection_registry.enqueue_collection(KEY)
    second = collection_registry.enqueue_collection(KEY)

    assert second["added"] == 0
    assert len(collection_registry.pending("process_item_job", 10)) == 1


@needs_pg
def test_enqueue_collection_leaves_items_gone_from_bronze_alone(pg, folders):
    folders.update({ITEM_A: "ark:/1/a", ITEM_B: "ark:/1/b"})
    collection_registry.enqueue_collection(KEY)
    collection_registry.ack([r["id"] for r in collection_registry.pending("process_item_job", 10)])

    del folders[ITEM_B]
    result = collection_registry.enqueue_collection(KEY)

    assert result["items"] == 1
    assert [r["item"] for r in collection_registry.pending("process_item_job", 10)] == [ITEM_A]
    linked = _rows(pg, "SELECT i.name FROM collection_registry.collection_item ci "
                       "JOIN collection_registry.item i ON i.id = ci.item_id ORDER BY 1")
    assert linked == [(ITEM_A,), (ITEM_B,)]


@needs_pg
def test_enqueue_collection_ignores_folders_outside_the_collection_key(pg, folders):
    folders.update({ITEM_A: "ark:/1/a", "bronze/other-group/haeger/latest/tree/x": "ark:/1/x"})
    assert collection_registry.enqueue_collection(KEY)["items"] == 1


@needs_pg
def test_enqueue_reuses_the_registered_collection_row(pg, folders):
    collection_model.register_collection("haeger", "preservica", "preservica", [{"type": "preservica_ref", "value": "ref-1"}])
    folders.update({ITEM_A: "ark:/1/a"})

    collection_registry.enqueue_collection(KEY)

    assert _rows(pg, "SELECT name, slug, group_name, source_type FROM collection_registry.collection") == [
        (KEY, "haeger", "preservica", "preservica"),
    ]
    idents = _rows(pg, "SELECT id_type, id_value FROM collection_registry.collection_identifiers")
    assert idents == [("preservica_ref", "ref-1")]


@needs_pg
def test_register_promotes_an_unknown_row(pg, folders):
    folders.update({ITEM_A: "ark:/1/a"})
    collection_registry.enqueue_collection(KEY)
    assert collection_model.list_active_collections() == []

    collection_model.register_collection("haeger", "preservica", "manual")

    assert _rows(pg, "SELECT count(*), max(source_type) FROM collection_registry.collection") == [(1, "manual")]
    assert [c["name"] for c in collection_model.list_active_collections()] == ["haeger"]


@needs_pg
def test_collection_and_item_ids_are_uuids_referenced_by_the_queue(pg, folders):
    folders.update({ITEM_A: "ark:/1/a"})
    collection_registry.enqueue_collection(KEY)

    types = dict(_rows(pg, """
        SELECT table_name || '.' || column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'collection_registry'
          AND column_name IN ('id', 'collection_id', 'item_id')
          AND table_name IN ('collection', 'item', 'job_queue', 'job_result')
    """))
    assert types == {
        "collection.id": "uuid", "item.id": "uuid",
        "job_queue.id": "integer", "job_queue.collection_id": "uuid", "job_queue.item_id": "uuid",
        "job_result.id": "integer", "job_result.collection_id": "uuid", "job_result.item_id": "uuid",
    }
    assert _rows(pg, """
        SELECT i.name FROM collection_registry.job_queue q
        JOIN collection_registry.item i ON i.id = q.item_id
    """) == [(ITEM_A,)]


@needs_pg
def test_ack_removes_only_given_rows(pg, folders):
    folders.update({ITEM_A: "ark:/1/a", ITEM_B: "ark:/1/b"})
    collection_registry.enqueue_collection(KEY)
    first = collection_registry.pending("process_item_job", 1)

    assert collection_registry.ack([first[0]["id"]]) == 1
    assert collection_registry.ack([first[0]["id"]]) == 0
    assert [r["item"] for r in collection_registry.pending("process_item_job", 10)] == [ITEM_B]


@needs_pg
def test_record_result_tracks_an_untracked_item_and_keeps_one_row(pg):
    collection_registry.record_result("process_item_job", {"item": ITEM_A, "ark": "ark:/1/a", "result": {"status": "failed"}})
    row = collection_registry.record_result("process_item_job", {"item": ITEM_A, "result": {"status": "success"}})

    assert row["result"] == {"status": "success"}
    assert _rows(pg, "SELECT count(*) FROM collection_registry.job_result") == [(1,)]
    assert collection_registry.item_ark(ITEM_A) == "ark:/1/a"
    assert _rows(pg, "SELECT count(*) FROM collection_registry.collection_item") == [(1,)]


@needs_pg
def test_rollups_due_waits_for_the_queue_then_rollup_clears_it(pg, folders):
    folders.update({ITEM_A: "ark:/1/a", ITEM_B: "ark:/1/b"})
    collection_registry.enqueue_collection(KEY)
    collection_registry.record_result("process_item_job", {"item": ITEM_A, "result": {"status": "success"}})

    assert collection_registry.rollups_due() == []  # items still queued

    collection_registry.ack([r["id"] for r in collection_registry.pending("process_item_job", 10)])
    collection_registry.record_result("process_item_job", {
        "item": ITEM_B, "result": {"status": "failed", "error": "boom", "run_id": "r2"},
    })
    due = collection_registry.rollups_due()
    assert [d["collection"] for d in due] == [KEY]

    summary = collection_registry.rollup(KEY)
    assert summary["item_count"] == 2
    assert (summary["succeeded"], summary["failed"], summary["never_run"]) == (1, 1, 0)
    assert summary["failed_items"] == [{"item": ITEM_B, "error": "boom", "run_id": "r2"}]
    assert collection_registry.rollups_due() == []

    collection_registry.record_result("process_item_job", {"item": ITEM_B, "result": {"status": "success"}})
    assert [d["collection"] for d in collection_registry.rollups_due()] == [KEY]


@needs_pg
def test_rollup_raises_for_an_untracked_collection(pg):
    with pytest.raises(LookupError):
        collection_registry.rollup(KEY)
