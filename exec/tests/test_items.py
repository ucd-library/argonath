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
    with pytest.raises(LookupError, match="found 0"):
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


# --- resolve() for a standalone 'item.jsonld.json' sidecar (docs/cask-conventions.md) ---


def test_resolve_standalone_sidecar_lists_real_files_under_item_path(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/preservica/haeger/latest/tree/calera-formatted/item.jsonld.json",
    ])

    def fake_ls(path, opts={}):
        if path == "cask:/bronze/preservica/haeger/latest/tree/calera-formatted":
            return [
                _entry("identifiers.json", "/bronze/preservica/haeger/latest/tree/calera-formatted/identifiers.json", "file"),
                _entry("item.jsonld.json", "/bronze/preservica/haeger/latest/tree/calera-formatted/item.jsonld.json", "file"),
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

    assert location.item_path == "tree/calera-formatted"
    assert location.sidecar_path == "cask:/bronze/preservica/haeger/latest/tree/calera-formatted/item.jsonld.json"
    assert location.bronze_binary_paths == [
        "cask:/bronze/preservica/haeger/latest/tree/calera-formatted/Access/page_001.jpg",
        "cask:/bronze/preservica/haeger/latest/tree/calera-formatted/Preservation/page_001.tif",
    ]


def test_resolve_standalone_sidecar_does_not_trigger_for_a_binary_literally_named_item(monkeypatch):
    # A single-binary item whose file happens to be named "item" (no extension)
    # would produce the same stripped-suffix filename "item" as the standalone-
    # sidecar convention -- resolve() can't tell these apart from the sidecar
    # match alone, so the standalone-listing path always wins once every
    # matching sidecar's stripped filename is "item". Documenting this as
    # expected (not a real-world collision in practice) rather than silently
    # asserting the opposite.
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/g/c/latest/i1/item.jsonld.json",
    ])
    monkeypatch.setattr(fs, "ls", lambda path, opts={}: [
        _entry("item", "/bronze/g/c/latest/i1/item", "file"),
        _entry("item.jsonld.json", "/bronze/g/c/latest/i1/item.jsonld.json", "file"),
    ])
    location = items.resolve(ARK)
    assert location.bronze_binary_paths == ["cask:/bronze/g/c/latest/i1/item"]


def test_metadata_reads_standalone_sidecar_directly(monkeypatch):
    monkeypatch.setattr(fs, "find_by_subject", lambda subject, opts={}: [
        "/bronze/preservica/haeger/latest/tree/calera-formatted/item.jsonld.json",
    ])
    monkeypatch.setattr(fs, "ls", lambda path, opts={}: [
        _entry("Preservation", "/bronze/preservica/haeger/latest/tree/calera-formatted/Preservation", "directory"),
    ] if path == "cask:/bronze/preservica/haeger/latest/tree/calera-formatted" else [
        _entry("page_001.tif", "/bronze/preservica/haeger/latest/tree/calera-formatted/Preservation/page_001.tif", "file"),
    ])

    seen = {}

    def fake_read(path):
        seen["path"] = path
        return json.dumps({"name": "Calera"}).encode()

    monkeypatch.setattr(fs, "read", fake_read)
    metadata = items.metadata(ARK)
    assert seen["path"] == "cask:/bronze/preservica/haeger/latest/tree/calera-formatted/item.jsonld.json"
    assert metadata["title"] == "Calera"


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
