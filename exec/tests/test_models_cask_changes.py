from datetime import datetime, timedelta, timezone

import pytest

from digtk.models import cask_changes

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(minutes=10)
FRESH = NOW - timedelta(minutes=1)
KEY = "bronze/preservica/haeger"
TREE = "/bronze/preservica/haeger/latest/tree"


def _event(eid, path, created=OLD, operation="file.write", resource_type="file", details=None):
    return {
        "audit_log_id": eid, "created": created, "operation": operation,
        "resource_type": resource_type, "resource_path": path, "details": details or {},
    }


@pytest.fixture
def cask(monkeypatch):
    """Fake registry, audit log, CaskFS and gold records."""
    state = {"events": [], "exists": set(), "gold": {}, "arks": set(), "reads": []}
    monkeypatch.setattr(
        cask_changes.collection_model, "list_active_collections",
        lambda source_type=None, opts={}: [{"name": "haeger", "group_name": "preservica"}],
    )
    monkeypatch.setattr(cask_changes.collection_model, "items_root", lambda name, group, opts={}: f"cask:{TREE}")
    monkeypatch.setattr(cask_changes.collection_model, "list_item_arks", lambda name, opts={}: sorted(state["arks"]))

    def read_events(after, opts={}):
        state["reads"].append((after, opts.get("prefixes")))
        return [e for e in state["events"] if e["audit_log_id"] > after]
    monkeypatch.setattr(cask_changes.cask_audit, "read_events", read_events)
    monkeypatch.setattr(cask_changes.fs, "exists", lambda path: path in state["exists"])

    def read_gold(location, opts={}):
        if location.item_path in state["gold"]:
            return {"@id": state["gold"][location.item_path]}
        raise FileNotFoundError(location.item_path)
    monkeypatch.setattr(cask_changes.item_record, "read", read_gold)
    return state


def _gold_path(item_path):
    return f"cask:/gold/digital-dev/haeger/{item_path}/item.jsonld.json"


def _changes(cursor={"after": 0, "done": {}}, **opts):
    return cask_changes.changes(cursor, {"now": NOW, **opts})


def test_target_for_maps_paths_to_items_and_collection_files():
    roots = [{"key": KEY, "items_root": TREE, "collection_files": [f"{TREE}/metadata.json", f"{TREE}/identifiers.json"]}]

    assert cask_changes.target_for(f"{TREE}/foo/Preservation/p1.tif", "file", roots) == {
        "kind": "item", "key": KEY, "folder": f"{TREE}/foo", "rel": "Preservation/p1.tif",
    }
    assert cask_changes.target_for(f"{TREE}/foo", "directory", roots)["folder"] == f"{TREE}/foo"
    assert cask_changes.target_for(f"{TREE}/metadata.json", "file", roots) == {"kind": "collection", "key": KEY}
    assert cask_changes.target_for(f"{TREE}/stray.txt", "file", roots) is None
    assert cask_changes.target_for("/bronze/preservica/haeger/latest/by-ref/x/entity.json", "file", roots) is None


def test_reads_with_items_root_and_collection_file_prefixes(cask):
    _changes()

    assert cask["reads"][0] == (0, [f"{TREE}/", f"{TREE}/identifiers.json", f"{TREE}/metadata.json"])


def test_a_new_item_is_processed(cask):
    cask["events"] = [_event(1, f"{TREE}/new", resource_type="directory"), _event(2, f"{TREE}/new/Preservation/p1.tif")]
    cask["exists"].add(f"cask:{TREE}/new")

    result = _changes()

    assert result["process"] == [f"{TREE}/new"]
    assert result["cleanup"] == {}
    assert result["cursor"] == {"after": 2, "done": {}}


def test_a_metadata_edit_is_reindexed(cask):
    cask["events"] = [_event(5, f"{TREE}/foo/metadata.json"), _event(6, f"{TREE}/foo/photo.tif.jsonld.json")]
    cask["exists"].update({f"cask:{TREE}/foo", _gold_path("tree/foo")})

    result = _changes()

    assert result["reindex"] == {KEY: [f"{TREE}/foo"]}
    assert result["process"] == []


def test_a_metadata_edit_without_a_gold_record_is_processed(cask):
    cask["events"] = [_event(5, f"{TREE}/foo/metadata.json")]
    cask["exists"].add(f"cask:{TREE}/foo")

    assert _changes()["process"] == [f"{TREE}/foo"]


def test_a_binary_change_is_processed(cask):
    cask["events"] = [_event(5, f"{TREE}/foo/metadata.json"), _event(6, f"{TREE}/foo/Preservation/p2.tif")]
    cask["exists"].update({f"cask:{TREE}/foo", _gold_path("tree/foo")})

    assert _changes()["process"] == [f"{TREE}/foo"]


def test_a_deleted_item_is_deleted_by_its_gold_ark(cask):
    cask["events"] = [
        _event(7, f"{TREE}/gone/p1.tif", operation="file.delete"),
        _event(8, f"{TREE}/gone", operation="directory.delete", resource_type="directory"),
    ]
    cask["gold"]["tree/gone"] = "ark:/87293/gone"

    result = _changes()

    assert result["delete"] == {KEY: ["ark:/87293/gone"]}
    assert result["cleanup"] == {KEY: [f"{TREE}/gone"]}


def test_a_deleted_item_never_indexed_is_skipped(cask):
    cask["events"] = [_event(7, f"{TREE}/gone", operation="directory.delete", resource_type="directory")]

    result = _changes()

    assert result["delete"] == {}
    assert result["skipped"] == [{"folder": f"{TREE}/gone", "reason": "deleted, never indexed"}]
    assert result["cleanup"] == {KEY: [f"{TREE}/gone"]}


def test_a_renamed_item_is_processed_at_its_new_path_and_not_deleted(cask):
    cask["events"] = [_event(
        9, f"{TREE}/new-name", operation="directory.move", resource_type="directory",
        details={"fromPath": f"{TREE}/old-name", "toPath": f"{TREE}/new-name"},
    )]
    cask["exists"].add(f"cask:{TREE}/new-name")
    cask["gold"]["tree/old-name"] = "ark:/87293/same"
    cask["arks"].add("ark:/87293/same")

    result = _changes()

    assert result["process"] == [f"{TREE}/new-name"]
    assert result["delete"] == {}
    assert result["skipped"] == [{"folder": f"{TREE}/old-name", "reason": "ark still in the collection"}]
    assert result["cleanup"] == {KEY: [f"{TREE}/old-name"]}


def test_a_collection_file_edit_marks_the_collection(cask):
    cask["events"] = [_event(3, f"{TREE}/metadata.json")]

    result = _changes()

    assert result["collections"] == [KEY]
    assert result["process"] == []


def test_a_recent_item_is_deferred_and_holds_the_cursor(cask):
    cask["events"] = [
        _event(10, f"{TREE}/done/metadata.json"),
        _event(11, f"{TREE}/busy/p1.tif", created=FRESH),
        _event(12, f"{TREE}/busy/p2.tif", created=FRESH),
    ]
    cask["exists"].update({f"cask:{TREE}/done", f"cask:{TREE}/busy", _gold_path("tree/done")})

    result = _changes()

    assert result["reindex"] == {KEY: [f"{TREE}/done"]}
    assert result["process"] == []
    assert result["deferred"] == 1
    assert result["cursor"] == {"after": 10, "done": {}}


def test_done_items_are_not_acted_on_twice(cask):
    cask["events"] = [_event(10, f"{TREE}/a/metadata.json"), _event(11, f"{TREE}/busy/p1.tif", created=FRESH)]
    cask["exists"].update({f"cask:{TREE}/a", f"cask:{TREE}/busy", _gold_path("tree/a")})

    first = _changes({"after": 9, "done": {}})
    second = _changes(first["cursor"])

    assert first["reindex"] == {KEY: [f"{TREE}/a"]}
    assert first["cursor"] == {"after": 10, "done": {}}
    assert second["reindex"] == {}

    # a deferred item ahead of a settled one
    cask["events"] = [_event(20, f"{TREE}/busy2/p1.tif", created=FRESH), _event(21, f"{TREE}/b/metadata.json")]
    cask["exists"].update({f"cask:{TREE}/busy2", f"cask:{TREE}/b", _gold_path("tree/b")})
    third = _changes({"after": 19, "done": {}})
    fourth = _changes(third["cursor"])

    assert third["cursor"] == {"after": 19, "done": {f"{TREE}/b": 21}}
    assert fourth["reindex"] == {}


def test_nothing_new_keeps_the_cursor(cask):
    assert _changes({"after": 50, "done": {}})["cursor"] == {"after": 50, "done": {}}


def test_start_cursor_uses_the_newest_event(monkeypatch):
    monkeypatch.setattr(cask_changes.cask_audit, "max_event_id", lambda opts={}: 77)

    assert cask_changes.start_cursor() == {"after": 77, "done": {}}
