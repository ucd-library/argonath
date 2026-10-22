import json

import pytest

from digtk.lib import showcase_index
from digtk.lib.items import ItemLocation
from digtk.models.digital_website import collection_index

KEY = "bronze/g/c"
COLLECTION_ARK = "ark:/87287/d7root"
COLLECTION_ID = f"https://digital.ucdavis.edu/collection/{COLLECTION_ARK}"
FOLDERS = ["/bronze/g/c/latest/tree/a", "/bronze/g/c/latest/tree/b", "/bronze/g/c/latest/tree/noark"]
ARKS = {FOLDERS[0]: "ark:/87287/a", FOLDERS[1]: "ark:/87287/b"}


def _id(ark):
    return showcase_index.item_es_id(ark)


@pytest.fixture
def bronze(monkeypatch):
    """Fake bronze: three item folders, two with ARKs."""
    def fake_ark_for_path(path):
        if path in ARKS:
            return ARKS[path]
        raise LookupError(f"MISSING ARK: {path}")
    monkeypatch.setattr(collection_index.items, "ark_for_path", fake_ark_for_path)

    def fake_gold(location, opts={}):
        folder = f"/bronze/{location.group}/{location.collection}/{location.time}/{location.item_path}"
        if folder in ARKS:
            return {"@id": ARKS[folder]}
        raise FileNotFoundError(folder)
    monkeypatch.setattr(collection_index.item_record, "read", fake_gold)
    monkeypatch.setattr(collection_index, "gold_item_folders", lambda key, opts={}: list(FOLDERS))
    monkeypatch.setattr(collection_index.collection_model, "get_ark", lambda name, group: COLLECTION_ARK)
    monkeypatch.setattr(collection_index.collection_model, "list_item_folders", lambda name, opts={}: list(FOLDERS))
    monkeypatch.setattr(
        collection_index.collection_model, "list_item_arks", lambda name, opts={}: sorted(ARKS.values()),
    )


@pytest.fixture
def es(monkeypatch):
    """Fake showcase_index writes; records every call."""
    calls = {"index_items": [], "delete_items": [], "index_collection": [], "indexed_ids": []}

    def fake_index_items(arks, opts={}):
        calls["index_items"].append(list(arks))
        return {"indexed": list(arks), "failed": [], "pages": 0}
    monkeypatch.setattr(collection_index.showcase_index, "index_items", fake_index_items)
    monkeypatch.setattr(
        collection_index.showcase_index, "delete_items",
        lambda ids, opts={}: calls["delete_items"].append(list(ids)) or {"items": len(ids), "pages": 0, "errors": []},
    )
    monkeypatch.setattr(
        collection_index.showcase_index, "list_collection_item_ids", lambda cid, opts={}: list(calls["indexed_ids"]),
    )
    monkeypatch.setattr(
        collection_index.showcase_index, "index_collection",
        lambda name, opts={}: calls["index_collection"].append(name) or {"status": "indexed"},
    )
    return calls


def test_collection_es_id_uses_the_bronze_ark(bronze):
    assert collection_index.collection_es_id(KEY) == COLLECTION_ID


def test_collection_es_id_falls_back_to_the_gold_record(monkeypatch):
    def no_ark(name, group):
        raise LookupError("MISSING ARK")
    monkeypatch.setattr(collection_index.collection_model, "get_ark", no_ark)
    monkeypatch.setattr(collection_index.collection_record, "read", lambda name, opts={}: {
        "@id": COLLECTION_ARK, "@graph": [{"@id": COLLECTION_ID}],
    })

    assert collection_index.collection_es_id(KEY) == COLLECTION_ID


def test_collection_es_id_raises_without_bronze_or_gold(monkeypatch):
    def no_ark(name, group):
        raise LookupError("MISSING ARK")

    def no_record(name, opts={}):
        raise FileNotFoundError(name)
    monkeypatch.setattr(collection_index.collection_model, "get_ark", no_ark)
    monkeypatch.setattr(collection_index.collection_record, "read", no_record)

    with pytest.raises(LookupError):
        collection_index.collection_es_id(KEY)


def test_reindex_items_skips_folders_without_an_ark(bronze, es):
    result = collection_index.reindex_items(FOLDERS)

    assert es["index_items"] == [["ark:/87287/a", "ark:/87287/b"]]
    assert result["skipped"] == [FOLDERS[2]]
    assert result["indexed"] == [
        {"folder": FOLDERS[0], "ark": "ark:/87287/a"}, {"folder": FOLDERS[1], "ark": "ark:/87287/b"},
    ]


def test_reindex_items_only_rebuilds_when_asked(monkeypatch, bronze, es):
    builds = []
    monkeypatch.setattr(collection_index.item_record, "build", lambda target, opts={}: builds.append(target))

    collection_index.reindex_items(FOLDERS[:1])
    assert builds == []

    collection_index.reindex_items(FOLDERS[:1], {"rebuild": True})
    assert builds == [FOLDERS[0]]


def test_reindex_items_reports_a_failed_rebuild_and_indexes_the_rest(monkeypatch, bronze, es):
    def fake_build(target, opts={}):
        if target == FOLDERS[0]:
            raise LookupError("collection has no ark")
    monkeypatch.setattr(collection_index.item_record, "build", fake_build)

    result = collection_index.reindex_items(FOLDERS[:2], {"rebuild": True})

    assert es["index_items"] == [["ark:/87287/b"]]
    assert result["failed"] == [
        {"folder": FOLDERS[0], "ark": "ark:/87287/a", "error": "build-item: collection has no ark"},
    ]


def test_reindex_items_maps_index_failures_back_to_folders(monkeypatch, bronze):
    monkeypatch.setattr(collection_index.showcase_index, "index_items", lambda arks, opts={}: {
        "indexed": ["ark:/87287/a"], "failed": [{"ark": "ark:/87287/b", "error": "no gold"}], "pages": 2,
    })

    result = collection_index.reindex_items(FOLDERS[:2])

    assert result["failed"] == [{"folder": FOLDERS[1], "ark": "ark:/87287/b", "error": "no gold"}]
    assert result["pages"] == 2


def test_prune_deletes_only_items_missing_from_bronze(bronze, es):
    es["indexed_ids"] = [_id("ark:/87287/a"), _id("ark:/87287/b"), _id("ark:/87287/gone")]

    result = collection_index.prune_collection(KEY)

    assert es["delete_items"] == [[_id("ark:/87287/gone")]]
    assert result["orphans"] == [_id("ark:/87287/gone")]
    assert result["items"] == 1


def test_prune_does_nothing_without_orphans(bronze, es):
    es["indexed_ids"] = [_id("ark:/87287/a")]

    assert collection_index.prune_collection(KEY)["orphans"] == []
    assert es["delete_items"] == []


def test_prune_dry_run_deletes_nothing(bronze, es):
    es["indexed_ids"] = [_id("ark:/87287/a"), _id("ark:/87287/b"), _id("ark:/87287/gone")]

    result = collection_index.prune_collection(KEY, {"dry_run": True})

    assert result["orphans"] == [_id("ark:/87287/gone")]
    assert es["delete_items"] == []


def test_prune_refuses_when_bronze_lists_no_items(monkeypatch, bronze, es):
    monkeypatch.setattr(collection_index.collection_model, "list_item_arks", lambda name, opts={}: [])
    es["indexed_ids"] = [_id("ark:/87287/a")]

    with pytest.raises(RuntimeError, match="bronze lists no items"):
        collection_index.prune_collection(KEY)
    assert es["delete_items"] == []


def test_prune_refuses_to_delete_most_of_the_index(bronze, es):
    es["indexed_ids"] = [_id("ark:/87287/a")] + [_id(f"ark:/87287/gone{i}") for i in range(3)]

    with pytest.raises(RuntimeError, match="more than 50%"):
        collection_index.prune_collection(KEY)

    collection_index.prune_collection(KEY, {"force": True})
    assert len(es["delete_items"][0]) == 3


def test_reindex_collection_batches_prunes_and_refreshes_the_collection(bronze, es):
    es["indexed_ids"] = [_id("ark:/87287/a"), _id("ark:/87287/b"), _id("ark:/87287/gone")]

    result = collection_index.reindex_collection(KEY, {"batch_size": 1})

    assert es["index_items"] == [["ark:/87287/a"], ["ark:/87287/b"], []]
    assert result["indexed"] == 2
    assert result["skipped"] == [FOLDERS[2]]
    assert result["prune"]["orphans"] == [_id("ark:/87287/gone")]
    assert es["index_collection"] == ["c"]


def test_reindex_collection_skips_the_prune_after_a_failure(monkeypatch, bronze, es):
    monkeypatch.setattr(collection_index.showcase_index, "index_items", lambda arks, opts={}: {
        "indexed": [], "failed": [{"ark": a, "error": "boom"} for a in arks], "pages": 0,
    })

    result = collection_index.reindex_collection(KEY)

    assert result["prune"] == {"skipped": "2 item(s) failed"}
    assert es["delete_items"] == []
    assert es["index_collection"] == ["c"]


def test_reindex_collection_without_prune(bronze, es):
    result = collection_index.reindex_collection(KEY, {"prune": False})

    assert result["prune"] == {"skipped": "disabled"}


def test_delete_collection_uses_the_collection_es_id(monkeypatch, bronze):
    calls = []
    monkeypatch.setattr(
        collection_index.showcase_index, "delete_collection",
        lambda cid, opts={}: calls.append(cid) or {"collection": True, "items": 2, "pages": 1, "errors": []},
    )

    result = collection_index.delete_collection(KEY)

    assert calls == [COLLECTION_ID]
    assert result == {
        "collection": KEY, "esId": COLLECTION_ID, "deleted": True, "items": 2, "pages": 1, "errors": [],
    }


def test_delete_items_reports_each_collection(monkeypatch, es):
    def fake_gold_location(ark, opts={}):
        if ark == "ark:/87287/gone":
            raise FileNotFoundError(ark)
        location = ItemLocation(ark=ark, group="g", collection="c", time="latest", item_path="tree/a", bronze_binary_paths=[])
        return location, {"@id": ark}
    monkeypatch.setattr(collection_index.item_record, "gold_location", fake_gold_location)

    def no_bronze(ark, opts={}):
        raise AssertionError("read bronze")
    monkeypatch.setattr(collection_index.items, "resolve", no_bronze)

    result = collection_index.delete_items(["ark:/87287/a", "ark:/87287/gone"])

    assert es["delete_items"] == [[_id("ark:/87287/a"), _id("ark:/87287/gone")]]
    assert [d["collection"] for d in result["deleted"]] == [KEY, None]
    assert result["collections"] == [KEY]


class _Entry:
    def __init__(self, name, path, kind):
        self.name, self.path, self.kind = name, path, kind


@pytest.fixture
def outputs(monkeypatch):
    """Fake CaskFS holding one item's silver and gold directories."""
    tree = {
        "cask:/gold": [_Entry("digital-dev", "/gold/digital-dev", "directory"), _Entry("arda", "/gold/arda", "directory")],
        "cask:/silver/g/c/latest/tree/gone": [
            _Entry("public-frontend-derivatives", "/silver/g/c/latest/tree/gone/public-frontend-derivatives", "directory"),
            _Entry("ocr.json", "/silver/g/c/latest/tree/gone/ocr.json", "file"),
        ],
        "cask:/silver/g/c/latest/tree/gone/public-frontend-derivatives": [
            _Entry("a.jpg", "/silver/g/c/latest/tree/gone/public-frontend-derivatives/a.jpg", "file"),
            _Entry("b.jpg", "/silver/g/c/latest/tree/gone/public-frontend-derivatives/b.jpg", "file"),
        ],
        "cask:/gold/digital-dev/c/tree/gone": [_Entry("item.jsonld.json", "/gold/digital-dev/c/tree/gone/item.jsonld.json", "file")],
    }
    state = {"exists": set(tree) - {"cask:/gold"}, "deleted": []}
    monkeypatch.setattr(collection_index.fs, "ls", lambda path, opts={}: tree[path])
    monkeypatch.setattr(collection_index.fs, "exists", lambda path: path in state["exists"])
    monkeypatch.setattr(
        collection_index.fs, "delete", lambda path, opts={}: state["deleted"].append((path, opts)),
    )
    return state


def test_item_output_dirs_covers_silver_and_every_gold_project(outputs):
    assert collection_index.item_output_dirs("/bronze/g/c/latest/tree/gone") == [
        "cask:/silver/g/c/latest/tree/gone",
        "cask:/gold/arda/c/tree/gone",
        "cask:/gold/digital-dev/c/tree/gone",
    ]


def test_item_output_dirs_refuses_a_collection_root():
    with pytest.raises(ValueError):
        collection_index.item_output_dirs("/bronze/g/c/latest")


def test_remove_item_outputs_deletes_and_reports_what_existed(outputs):
    result = collection_index.remove_item_outputs(["/bronze/g/c/latest/tree/gone"])

    assert outputs["deleted"] == [
        ("cask:/silver/g/c/latest/tree/gone", {"directory": True}),
        ("cask:/gold/digital-dev/c/tree/gone", {"directory": True}),
    ]
    assert result["removed"] == [
        {"folder": "/bronze/g/c/latest/tree/gone", "path": "cask:/silver/g/c/latest/tree/gone", "files": 3},
        {"folder": "/bronze/g/c/latest/tree/gone", "path": "cask:/gold/digital-dev/c/tree/gone", "files": 1},
    ]


def test_remove_item_outputs_dry_run_deletes_nothing(outputs):
    result = collection_index.remove_item_outputs(["/bronze/g/c/latest/tree/gone"], {"dry_run": True})

    assert outputs["deleted"] == []
    assert len(result["removed"]) == 2
    assert result["dryRun"] is True


def test_remove_item_outputs_keeps_an_item_back_in_bronze(outputs):
    outputs["exists"].add("cask:/bronze/g/c/latest/tree/gone")

    result = collection_index.remove_item_outputs(["/bronze/g/c/latest/tree/gone"])

    assert result["kept"] == ["/bronze/g/c/latest/tree/gone"]
    assert outputs["deleted"] == []


def test_reindex_items_without_rebuild_never_reads_bronze(monkeypatch, bronze, es):
    def no_bronze(path):
        raise AssertionError(f"read bronze: {path}")
    monkeypatch.setattr(collection_index.items, "ark_for_path", no_bronze)

    result = collection_index.reindex_items(FOLDERS)

    assert es["index_items"] == [["ark:/87287/a", "ark:/87287/b"]]
    assert result["skipped"] == [FOLDERS[2]]


def test_reindex_collection_lists_bronze_only_with_rebuild(monkeypatch, bronze, es):
    listed = []
    monkeypatch.setattr(
        collection_index.collection_model, "list_item_folders",
        lambda name, opts={}: listed.append("bronze") or list(FOLDERS),
    )
    monkeypatch.setattr(collection_index, "gold_item_folders", lambda key, opts={}: listed.append("gold") or list(FOLDERS))
    monkeypatch.setattr(collection_index.item_record, "build", lambda target, opts={}: None)

    collection_index.reindex_collection(KEY, {"prune": False})
    collection_index.reindex_collection(KEY, {"prune": False, "rebuild": True})

    assert listed == ["gold", "bronze"]


def test_gold_item_folders_walks_gold_for_records(monkeypatch):
    tree = {
        "cask:/gold/digital-dev/c": [
            _Entry("collection.jsonld.json", "/gold/digital-dev/c/collection.jsonld.json", "file"),
            _Entry("tree", "/gold/digital-dev/c/tree", "directory"),
        ],
        "cask:/gold/digital-dev/c/tree": [
            _Entry("a", "/gold/digital-dev/c/tree/a", "directory"),
            _Entry("b", "/gold/digital-dev/c/tree/b", "directory"),
        ],
        "cask:/gold/digital-dev/c/tree/a": [
            _Entry("item.jsonld.json", "/gold/digital-dev/c/tree/a/item.jsonld.json", "file"),
            _Entry("manifest.json", "/gold/digital-dev/c/tree/a/manifest.json", "file"),
        ],
        "cask:/gold/digital-dev/c/tree/b": [
            _Entry("item.jsonld.json", "/gold/digital-dev/c/tree/b/item.jsonld.json", "file"),
        ],
    }
    manifests = {"cask:/gold/digital-dev/c/tree/a/manifest.json": {"bronze": "/bronze/g/c/latest/tree/a"}}
    monkeypatch.setattr(collection_index.fs, "ls", lambda path, opts={}: tree[path])
    monkeypatch.setattr(collection_index.fs, "exists", lambda path: path in manifests)
    monkeypatch.setattr(collection_index.fs, "read", lambda path: json.dumps(manifests[path]).encode())

    assert collection_index.gold_item_folders(KEY, {"project": "digital-dev"}) == ["/bronze/g/c/latest/tree/a"]


def test_gold_item_folders_without_a_gold_collection(monkeypatch):
    def missing(path, opts={}):
        raise FileNotFoundError(path)
    monkeypatch.setattr(collection_index.fs, "ls", missing)

    assert collection_index.gold_item_folders(KEY) == []
