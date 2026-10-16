import json
from types import SimpleNamespace

import pytest

from digtk.lib import fs, items

ARK = "ark:/87287/d7abc1234"


def _entry(name, path, kind):
    return SimpleNamespace(name=name, path=path, kind=kind)


def test_resolve_single_binary(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/digital-collections/my-coll/latest/box-1/photo.tiff.jsonld.json",
    ])
    location = items.resolve(ARK)
    assert location.ark == ARK
    assert location.group == "digital-collections"
    assert location.collection == "my-coll"
    assert location.time == "latest"
    assert location.item_path == "box-1"
    assert location.bronze_binary_paths == ["cask:/bronze/digital-collections/my-coll/latest/box-1/photo.tiff"]


def test_resolve_binary_directly_under_time_has_empty_item_path(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/g/c/latest/photo.tiff.jsonld.json",
    ])
    location = items.resolve(ARK)
    assert location.item_path == ""


def test_resolve_multiple_binaries_sharing_a_location(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/a/b/latest/box-1/folder-3/photo-b.tiff.jsonld.json",
        "/bronze/a/b/latest/box-1/folder-3/photo-a.tiff.jsonld.json",
    ])
    location = items.resolve(ARK)
    assert location.group == "a"
    assert location.collection == "b"
    assert location.time == "latest"
    assert location.item_path == "box-1/folder-3"
    assert location.bronze_binary_paths == [
        "cask:/bronze/a/b/latest/box-1/folder-3/photo-a.tiff",
        "cask:/bronze/a/b/latest/box-1/folder-3/photo-b.tiff",
    ]


def test_resolve_raises_on_malformed_bronze_path_missing_segments(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/g/c.jsonld.json",
    ])
    with pytest.raises(ValueError, match="missing group/collection/time/filename"):
        items.resolve(ARK)


def test_resolve_raises_on_no_match(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [])
    with pytest.raises(LookupError, match="found neither"):
        items.resolve(ARK)


def test_resolve_raises_when_binaries_span_different_locations(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/a/b/latest/i1/photo.tiff.jsonld.json",
        "/bronze/a/b/latest/i2/photo.tiff.jsonld.json",
    ])
    with pytest.raises(LookupError, match="not all in the same item location"):
        items.resolve(ARK)


def test_resolve_ignores_non_bronze_and_non_sidecar_matches(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/silver/a/b/latest/i1/derivatives/thumb-256.jpg",
        "/bronze/a/b/latest/i1/photo.tiff",
        "/bronze/a/b/latest/i1/photo.tiff.jsonld.json",
    ])
    location = items.resolve(ARK)
    assert location.bronze_binary_paths == ["cask:/bronze/a/b/latest/i1/photo.tiff"]


def test_resolve_forwards_opts_to_find_by_subject(monkeypatch):
    seen = {}

    def fake_find(subject, opts={}):
        seen["subject"] = subject
        seen["opts"] = opts
        return ["/bronze/a/b/latest/i1/photo.tiff.jsonld.json"]

    monkeypatch.setattr(fs, "find_by_subject", fake_find)
    items.resolve(ARK, opts={"predicate": "custom"})
    assert seen["subject"] == ARK
    assert seen["opts"] == {"predicate": "custom"}


def test_resolve_sets_sidecar_path_for_single_binary(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/digital-collections/my-coll/latest/box-1/photo.tiff.jsonld.json",
    ])
    location = items.resolve(ARK)
    assert location.sidecar_path == "cask:/bronze/digital-collections/my-coll/latest/box-1/photo.tiff.jsonld.json"


# --- resolve() through a gold item record (docs/cask-conventions.md) ---


def _fake_gold(monkeypatch, files):
    monkeypatch.setattr(fs, "exists", lambda path: path in files)
    monkeypatch.setattr(fs, "read", lambda path: files[path])


def test_resolve_through_gold_record_lists_real_files_under_its_bronze_folder(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [])
    monkeypatch.setattr(fs, "find_by_graph", lambda graph, opts={}: [
        "/silver/preservica/haeger/latest/tree/calera-formatted/item.jsonld.json",
        "/gold/digital-dev/haeger/tree/calera-formatted/item.jsonld.json",
    ])
    _fake_gold(monkeypatch, {"cask:/gold/digital-dev/haeger/tree/calera-formatted/manifest.json": json.dumps({
        "bronze": "/bronze/preservica/haeger/latest/tree/calera-formatted",
    })})

    def fake_ls(path, opts={}):
        if path == "cask:/bronze/preservica/haeger/latest/tree/calera-formatted":
            return [
                _entry("identifiers.json", "/bronze/preservica/haeger/latest/tree/calera-formatted/identifiers.json", "file"),
                _entry("metadata.json", "/bronze/preservica/haeger/latest/tree/calera-formatted/metadata.json", "file"),
                _entry("Access", "/bronze/preservica/haeger/latest/tree/calera-formatted/Access", "directory"),
                _entry("Preservation", "/bronze/preservica/haeger/latest/tree/calera-formatted/Preservation", "directory"),
            ]
        if path == "cask:/bronze/preservica/haeger/latest/tree/calera-formatted/Access":
            return [_entry("page_001.jpg", "/bronze/preservica/haeger/latest/tree/calera-formatted/Access/page_001.jpg", "file")]
        if path == "cask:/bronze/preservica/haeger/latest/tree/calera-formatted/Preservation":
            return [_entry("page_001.tif", "/bronze/preservica/haeger/latest/tree/calera-formatted/Preservation/page_001.tif", "file")]
        raise AssertionError(f"unexpected ls() path: {path}")

    monkeypatch.setattr(fs, "ls", fake_ls)
    location = items.resolve(ARK)

    assert location.ark == ARK
    assert (location.group, location.collection, location.time) == ("preservica", "haeger", "latest")
    assert location.item_path == "tree/calera-formatted"
    assert location.sidecar_path is None
    assert location.bronze_binary_paths == [
        "cask:/bronze/preservica/haeger/latest/tree/calera-formatted/Access/page_001.jpg",
        "cask:/bronze/preservica/haeger/latest/tree/calera-formatted/Preservation/page_001.tif",
    ]


def test_resolve_ignores_a_stale_bronze_item_jsonld(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/g/c/latest/i1/item.jsonld.json",
    ])
    with pytest.raises(LookupError, match="found neither"):
        items.resolve(ARK)


def test_resolve_gold_record_without_a_manifest_raises(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [])
    monkeypatch.setattr(fs, "find_by_graph", lambda graph, opts={}: ["/gold/digital-dev/c/i1/item.jsonld.json"])
    _fake_gold(monkeypatch, {})
    with pytest.raises(LookupError, match="no manifest.json"):
        items.resolve(ARK)


def test_resolve_rejects_more_than_one_gold_record(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [])
    monkeypatch.setattr(fs, "find_by_graph", lambda graph, opts={}: [
        "/gold/digital-dev/c/i1/item.jsonld.json", "/gold/digital-dev/c/i2/item.jsonld.json",
    ])
    with pytest.raises(LookupError, match="Expected one gold item record"):
        items.resolve(ARK)


def test_resolve_searches_gold_records_by_named_graph(monkeypatch):
    graphs = []

    def fake_find_by_graph(graph, opts={}):
        graphs.append(graph)
        return ["/gold/digital-dev/c/i1/item.jsonld.json"]
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [])
    monkeypatch.setattr(fs, "find_by_graph", fake_find_by_graph)
    _fake_gold(monkeypatch, {"cask:/gold/digital-dev/c/i1/manifest.json": json.dumps({"bronze": "/bronze/g/c/latest/i1"})})
    monkeypatch.setattr(fs, "ls", lambda path, opts={}: [])

    assert items.resolve(ARK).item_path == "i1"
    assert graphs == [ARK]


def test_resolve_prefers_the_gold_record_under_the_requested_project(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [])
    monkeypatch.setattr(fs, "find_by_graph", lambda graph, opts={}: [
        "/gold/digital-dev/c/i1/item.jsonld.json", "/gold/digital-prod/c/i1/item.jsonld.json",
    ])
    _fake_gold(monkeypatch, {
        "cask:/gold/digital-dev/c/i1/manifest.json": json.dumps({"bronze": "/bronze/g/c/dev-snapshot/i1"}),
        "cask:/gold/digital-prod/c/i1/manifest.json": json.dumps({"bronze": "/bronze/g/c/prod-snapshot/i1"}),
    })
    monkeypatch.setattr(fs, "ls", lambda path, opts={}: [])

    assert items.resolve(ARK).time == "dev-snapshot"
    assert items.resolve(ARK, {"project": "digital-prod"}).time == "prod-snapshot"


# --- resolve_path ---------------------------------------------------------------

def test_resolve_path_lists_binaries_with_no_ark_lookup(monkeypatch):
    def no_rdf_search(subject, opts={}):
        raise AssertionError("resolve_path() must not search by subject/ark")
    monkeypatch.setattr(fs, "find_by_subject", no_rdf_search)
    monkeypatch.setattr(fs, "exists", lambda path: False)
    monkeypatch.setattr(fs, "ls", lambda path, opts={}: [
        _entry("photo.tiff", "/bronze/digital-collections/my-coll/latest/box-1/photo.tiff", "file"),
    ] if path == "cask:/bronze/digital-collections/my-coll/latest/box-1" else [])

    location = items.resolve_path("bronze/digital-collections/my-coll/latest/box-1")

    assert location.ark is None
    assert location.sidecar_path is None
    assert location.group == "digital-collections"
    assert location.collection == "my-coll"
    assert location.time == "latest"
    assert location.item_path == "box-1"
    assert location.bronze_binary_paths == ["cask:/bronze/digital-collections/my-coll/latest/box-1/photo.tiff"]


def test_resolve_path_attaches_ark_when_the_folder_has_one(monkeypatch):
    identifiers = "cask:/bronze/preservica/haeger/latest/tree/quarter-mile/identifiers.json"
    monkeypatch.setattr(fs, "ls", lambda path, opts={}: [
        _entry("identifiers.json", "/bronze/preservica/haeger/latest/tree/quarter-mile/identifiers.json", "file"),
        _entry("page_001.tif", "/bronze/preservica/haeger/latest/tree/quarter-mile/page_001.tif", "file"),
    ])
    monkeypatch.setattr(fs, "exists", lambda path: path == identifiers)
    monkeypatch.setattr(fs, "read", lambda path: json.dumps([{"type": None, "value": ARK}]))

    location = items.resolve_path("bronze/preservica/haeger/latest/tree/quarter-mile")

    assert location.ark == ARK
    assert location.sidecar_path is None
    assert location.bronze_binary_paths == ["cask:/bronze/preservica/haeger/latest/tree/quarter-mile/page_001.tif"]


def test_resolve_path_raises_when_folder_has_no_binaries(monkeypatch):
    monkeypatch.setattr(fs, "ls", lambda path, opts={}: [])
    with pytest.raises(LookupError):
        items.resolve_path("bronze/g/c/latest/empty-folder")


def test_resolve_path_rejects_non_bronze_paths():
    with pytest.raises(ValueError):
        items.resolve_path("/workspace/jdoe/box-12")


def test_metadata_for_maps_a_tree_items_metadata_json_dublin_core(monkeypatch):
    location = items.ItemLocation(
        ark=ARK, group="preservica", collection="haeger", time="latest",
        item_path="tree/calera-formatted", bronze_binary_paths=[],
    )
    metadata_path = "cask:/bronze/preservica/haeger/latest/tree/calera-formatted/metadata.json"
    monkeypatch.setattr(fs, "exists", lambda path: path == metadata_path)
    monkeypatch.setattr(fs, "read", lambda path: json.dumps({
        "dc": {"title": "Calera", "date": "1973", "creator": "Haeger"},
        "preservica": {"title": "Calera_Formatted"},
    }).encode())

    assert items.metadata_for(location) == {
        "title": "Calera", "description": None, "date": "1973", "creator": "Haeger",
    }


def test_metadata_for_falls_back_to_the_preservica_title(monkeypatch):
    location = items.ItemLocation(
        ark=None, group="preservica", collection="haeger", time="latest",
        item_path="tree/calera-formatted", bronze_binary_paths=[],
    )
    monkeypatch.setattr(fs, "exists", lambda path: True)
    monkeypatch.setattr(fs, "read", lambda path: json.dumps({"preservica": {"title": "Calera_Formatted"}}).encode())

    assert items.metadata_for(location)["title"] == "Calera_Formatted"


def test_metadata_maps_sidecar_fields(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/digital-collections/my-coll/latest/box-1/photo.tiff.jsonld.json",
    ])
    monkeypatch.setattr(fs, "read", lambda path: json.dumps({
        "name": "Wine menu", "description": "A signed menu",
        "dateCreated": "1973", "creator": "Society",
    }).encode())

    assert items.metadata(ARK) == {
        "title": "Wine menu", "description": "A signed menu",
        "date": "1973", "creator": "Society",
    }


def test_metadata_reads_only_first_binarys_sidecar(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/a/b/latest/i1/photo-a.tiff.jsonld.json",
        "/bronze/a/b/latest/i1/photo-b.tiff.jsonld.json",
    ])
    seen = {}

    def fake_read(path):
        seen["path"] = path
        return json.dumps({"name": "Box 1"}).encode()

    monkeypatch.setattr(fs, "read", fake_read)
    metadata = items.metadata(ARK)
    assert seen["path"] == "cask:/bronze/a/b/latest/i1/photo-a.tiff.jsonld.json"
    assert metadata["title"] == "Box 1"


def test_metadata_defaults_missing_fields_to_none(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/a/b/latest/i1/photo.tiff.jsonld.json",
    ])
    monkeypatch.setattr(fs, "read", lambda path: json.dumps({}).encode())

    assert items.metadata(ARK) == {
        "title": None, "description": None, "date": None, "creator": None,
    }


# --- metadata_for -------------------------------------------------------------

def test_metadata_for_returns_all_none_for_a_workspace_item(monkeypatch):
    def fake_read(path):
        raise AssertionError(f"unexpected fs.read({path})")
    monkeypatch.setattr(fs, "read", fake_read)
    location = items.ItemLocation(
        ark=None, group="jdoe", collection="box-12", time=None, item_path="",
        bronze_binary_paths=["cask:/workspace/jdoe/box-12/source/photo.tiff"],
        dest_type="workspace", dest_prefix="derivatives",
    )
    assert items.metadata_for(location) == {"title": None, "description": None, "date": None, "creator": None}


def test_metadata_for_reads_the_items_own_sidecar(monkeypatch):
    seen = []
    monkeypatch.setattr(fs, "read", lambda path: seen.append(path) or json.dumps({"name": "Menu"}).encode())
    location = items.ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="i",
        bronze_binary_paths=["cask:/bronze/g/c/latest/i/photo.tiff"],
        sidecar_path="cask:/bronze/g/c/latest/i/photo.tiff.jsonld.json",
    )
    assert items.metadata_for(location)["title"] == "Menu"
    assert seen == ["cask:/bronze/g/c/latest/i/photo.tiff.jsonld.json"]


# --- workspace items -----------------------------------------------------------

def _fake_workspace_ls(tree):
    """fs.ls stub over {dir: [(name, kind), ...]} keyed by 'cask:/...' directory."""
    def fake_ls(path, opts={}):
        raw = path[len("cask:"):]
        return [_entry(name, f"{raw}/{name}", kind) for name, kind in tree.get(path, [])]
    return fake_ls


def test_workspace_source_root():
    assert items.workspace_source_root("jdoe", "box-12") == "cask:/workspace/jdoe/box-12/source"


def test_resolve_workspace_collection_root_means_its_source_dir(monkeypatch):
    monkeypatch.setattr(fs, "ls", _fake_workspace_ls({
        "cask:/workspace/jdoe/box-12/source": [
            ("b.tiff", "file"), ("a.tiff", "file"), ("folder-1", "directory"),
            ("a.tiff.jsonld.json", "file"),
        ],
    }))
    location = items.resolve_workspace("cask:/workspace/jdoe/box-12")
    assert location.ark is None
    assert location.group == "jdoe"
    assert location.collection == "box-12"
    assert location.time is None
    assert location.item_path == ""
    assert location.sidecar_path is None
    assert location.dest_type == "workspace"
    assert location.dest_prefix == "derivatives"
    # Only files directly in the directory — not subdirectories or sidecars.
    assert location.bronze_binary_paths == [
        "cask:/workspace/jdoe/box-12/source/a.tiff",
        "cask:/workspace/jdoe/box-12/source/b.tiff",
    ]


def test_resolve_workspace_subdirectory_sets_item_path(monkeypatch):
    monkeypatch.setattr(fs, "ls", _fake_workspace_ls({
        "cask:/workspace/jdoe/box-12/source/folder-1/sub": [("page.jpg", "file")],
    }))
    location = items.resolve_workspace("cask:/workspace/jdoe/box-12/source/folder-1/sub")
    assert location.item_path == "folder-1/sub"
    assert location.bronze_binary_paths == ["cask:/workspace/jdoe/box-12/source/folder-1/sub/page.jpg"]


def test_resolve_workspace_raises_when_directory_has_no_files(monkeypatch):
    monkeypatch.setattr(fs, "ls", _fake_workspace_ls({
        "cask:/workspace/jdoe/box-12/source": [("folder-1", "directory")],
    }))
    with pytest.raises(LookupError, match="no files found"):
        items.resolve_workspace("cask:/workspace/jdoe/box-12")


@pytest.mark.parametrize("path", [
    "cask:/bronze/g/c/latest/photo.tiff",
    "cask:/workspace/jdoe",
    "cask:/workspace/jdoe/box-12/derivatives/text",
])
def test_resolve_workspace_rejects_paths_outside_a_collections_source_dir(path):
    with pytest.raises(ValueError):
        items.resolve_workspace(path)


def test_list_workspace_items_finds_every_directory_holding_files(monkeypatch):
    monkeypatch.setattr(fs, "ls", _fake_workspace_ls({
        "cask:/workspace/jdoe/box-12/source": [("cover.jpg", "file"), ("folder-1", "directory"), ("empty", "directory")],
        "cask:/workspace/jdoe/box-12/source/folder-1": [("p1.tiff", "file"), ("sub", "directory")],
        "cask:/workspace/jdoe/box-12/source/folder-1/sub": [("p2.tiff", "file")],
        "cask:/workspace/jdoe/box-12/source/empty": [("only.jsonld.json", "file")],
    }))
    assert items.list_workspace_items("cask:/workspace/jdoe/box-12") == [
        "cask:/workspace/jdoe/box-12/source",
        "cask:/workspace/jdoe/box-12/source/folder-1",
        "cask:/workspace/jdoe/box-12/source/folder-1/sub",
    ]


def test_list_workspace_items_starts_from_a_subdirectory(monkeypatch):
    monkeypatch.setattr(fs, "ls", _fake_workspace_ls({
        "cask:/workspace/jdoe/box-12/source/folder-1": [("p1.tiff", "file")],
    }))
    assert items.list_workspace_items("cask:/workspace/jdoe/box-12/source/folder-1") == [
        "cask:/workspace/jdoe/box-12/source/folder-1",
    ]


# --- resolve_target / item_id ---------------------------------------------------

def test_resolve_target_dispatches_by_prefix(monkeypatch):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: ("bronze", ark))
    monkeypatch.setattr(items, "resolve_workspace", lambda path: ("workspace", path))
    monkeypatch.setattr(items, "resolve_path", lambda path, opts={}: ("path", path))
    assert items.resolve_target(ARK) == ("bronze", ARK)
    assert items.resolve_target("cask:/workspace/jdoe/box-12") == ("workspace", "cask:/workspace/jdoe/box-12")
    assert items.resolve_target("bronze/g/c/latest/x") == ("path", "bronze/g/c/latest/x")


def test_resolve_target_passes_a_location_through():
    location = items.ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="", bronze_binary_paths=[],
    )
    assert items.resolve_target(location) is location


@pytest.mark.parametrize("target", ["./scans/box-12", "/tmp/box-12"])
def test_resolve_target_rejects_anything_else(target):
    with pytest.raises(ValueError, match="expected an ARK"):
        items.resolve_target(target)


def test_item_id_is_always_a_path_never_the_ark():
    bronze = items.ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="i", bronze_binary_paths=[],
    )
    workspace = items.ItemLocation(
        ark=None, group="jdoe", collection="box-12", time=None, item_path="folder-1",
        bronze_binary_paths=[], dest_type="workspace", dest_prefix="derivatives",
    )
    assert items.item_id(bronze) == "/bronze/g/c/latest/i"
    assert items.item_id(workspace) == "cask:/workspace/jdoe/box-12/source/folder-1"


# --- output destination ---------------------------------------------------------

SILVER_LOCATION = items.ItemLocation(
    ark=ARK, group="g", collection="c", time="2026-01", item_path="box-1",
    bronze_binary_paths=["cask:/bronze/g/c/2026-01/box-1/photo.tiff"],
)
WORKSPACE_LOCATION = items.ItemLocation(
    ark=None, group="jdoe", collection="box-12", time=None, item_path="folder-1",
    bronze_binary_paths=["cask:/workspace/jdoe/box-12/source/folder-1/photo.tiff"],
    dest_type="workspace", dest_prefix="derivatives",
)


def test_dest_rel_prefixes_item_path_and_workspace_derivatives_dir():
    assert items.dest_rel(SILVER_LOCATION, "text", "ocr.hocr") == "box-1/text/ocr.hocr"
    assert items.dest_rel(WORKSPACE_LOCATION, "text", "ocr.hocr") == "derivatives/folder-1/text/ocr.hocr"


def test_dest_rel_skips_an_empty_item_path():
    location = items.ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="", bronze_binary_paths=[],
    )
    assert items.dest_rel(location, "split-spreads", "gutters.json") == "split-spreads/gutters.json"


def test_dest_opts_has_no_time_for_workspace():
    assert items.dest_opts(SILVER_LOCATION) == {"group": "g", "time": "2026-01"}
    assert items.dest_opts(WORKSPACE_LOCATION) == {"group": "jdoe"}


def test_dest_dir_builds_the_full_cask_directory():
    assert items.dest_dir(SILVER_LOCATION, "split-spreads") == "cask:/silver/g/c/2026-01/box-1/split-spreads"
    assert items.dest_dir(WORKSPACE_LOCATION, "split-spreads") == (
        "cask:/workspace/jdoe/box-12/derivatives/folder-1/split-spreads"
    )


def test_workspace_listings_request_one_large_page(monkeypatch):
    seen = []

    def fake_ls(path, opts={}):
        seen.append(opts)
        return [_entry("a.tiff", "/workspace/jdoe/box-12/source/a.tiff", "file")]
    monkeypatch.setattr(fs, "ls", fake_ls)

    items.resolve_workspace("cask:/workspace/jdoe/box-12")
    items.list_workspace_items("cask:/workspace/jdoe/box-12")

    assert seen == [{"limit": 10000}, {"limit": 10000}]


def test_resolve_tree_item_without_media_has_no_binaries(monkeypatch):
    """A tree/ item whose media were removed resolves with an empty binary list
    (no fallback to by-ref/) — callers report it as missing media."""
    tree = "/bronze/preservica/haeger/latest/tree/quarter-mile"
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [])
    monkeypatch.setattr(fs, "find_by_graph", lambda graph, opts={}: [
        "/gold/digital-dev/haeger/tree/quarter-mile/item.jsonld.json",
    ])
    _fake_gold(monkeypatch, {"cask:/gold/digital-dev/haeger/tree/quarter-mile/manifest.json": json.dumps({"bronze": tree})})
    listing = {tree: [
        _entry("metadata.json", f"{tree}/metadata.json", "file"),
        _entry("identifiers.json", f"{tree}/identifiers.json", "file"),
    ]}

    def fake_ls(path, opts={}):
        raw = path[len("cask:"):]
        if raw not in listing:
            raise AssertionError(f"unexpected ls() path: {path}")
        return listing[raw]
    monkeypatch.setattr(fs, "ls", fake_ls)

    location = items.resolve(ARK)

    assert location.bronze_binary_paths == []
    assert location.item_path == "tree/quarter-mile"


@pytest.mark.parametrize("path", [
    "bronze/preservica/haeger/latest/tree/quarter-mile",
    "/bronze/preservica/haeger/latest/tree/quarter-mile/",
    "cask:/bronze/preservica/haeger/latest/tree/quarter-mile",
])
def test_normalize_bronze_path_accepts_every_form(path):
    assert items.normalize_bronze_path(path) == "/bronze/preservica/haeger/latest/tree/quarter-mile"


@pytest.mark.parametrize("path", ["workspace/jdoe/box-12", "bronze/preservica/haeger"])
def test_normalize_bronze_path_rejects_non_item_paths(path):
    with pytest.raises(ValueError):
        items.normalize_bronze_path(path)


def test_ark_for_path_missing_identifiers_json_is_missing_ark(monkeypatch):
    monkeypatch.setattr(fs, "exists", lambda path: False)

    with pytest.raises(LookupError, match="MISSING ARK"):
        items.ark_for_path("bronze/preservica/haeger/latest/tree/quarter-mile")


def test_ark_for_path_reads_identifiers_json(monkeypatch):
    identifiers = "cask:/bronze/preservica/haeger/latest/tree/quarter-mile/identifiers.json"
    monkeypatch.setattr(fs, "exists", lambda path: path == identifiers)
    monkeypatch.setattr(fs, "read", lambda path: json.dumps([
        {"type": "LocalID", "value": "abc123"},
        {"type": None, "value": ARK},
    ]))

    assert items.ark_for_path("bronze/preservica/haeger/latest/tree/quarter-mile") == ARK


def test_ark_for_path_raises_when_identifiers_json_has_no_ark_either(monkeypatch):
    identifiers = "cask:/bronze/preservica/haeger/latest/tree/quarter-mile/identifiers.json"
    monkeypatch.setattr(fs, "exists", lambda path: path == identifiers)
    monkeypatch.setattr(fs, "read", lambda path: json.dumps([{"type": "LocalID", "value": "abc123"}]))

    with pytest.raises(LookupError, match="MISSING ARK"):
        items.ark_for_path("bronze/preservica/haeger/latest/tree/quarter-mile")
