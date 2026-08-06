import pytest

from digtk.lib import fs, items

ARK = "ark:/87287/d7abc1234"


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
