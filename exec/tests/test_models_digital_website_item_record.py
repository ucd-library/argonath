import json

import pytest

from digtk.lib.items import ItemLocation
from digtk.models.digital_website import item_record

ARK = "ark:/87287/d7abc1234"

ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
    sidecar_path="cask:/bronze/g/c/latest/item/item.jsonld.json",
)

NO_ARK_ITEM = ItemLocation(
    ark=None, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
)

NO_SIDECAR_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
    sidecar_path=None,
)

MULTI_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="box-1/folder-3",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/box-1/folder-3/photo-a.tiff",
        "cask:/bronze/g/c/latest/box-1/folder-3/photo-b.tiff",
    ],
    sidecar_path=None,
)

SILVER_PATH = "cask:/silver/g/c/latest/item/item.jsonld.json"
MULTI_SILVER_PATH = "cask:/silver/g/c/latest/box-1/folder-3/item.jsonld.json"


@pytest.fixture
def fake_derivative(monkeypatch):
    calls = []

    def fake_write(**kwargs):
        calls.append(kwargs)
        dest = item_record.derivative.dest_path(
            type=kwargs["type"], collection=kwargs["collection"],
            rel_path=kwargs["rel_path"], opts=kwargs.get("opts", {}),
        )
        return {"path": f"cask:{dest}", "links": []}

    monkeypatch.setattr(item_record.derivative, "write", fake_write)
    return calls


def test_build_raises_for_ark_less_item(monkeypatch, fake_derivative):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_ARK_ITEM)
    with pytest.raises(LookupError, match="no ark"):
        item_record.build(NO_ARK_ITEM)


def test_build_adopts_bronze_sidecar_descriptive_fields(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: ITEM)
    bronze_doc = {
        "@context": {"@vocab": "https://schema.org/"}, "@id": ARK, "ark": ARK,
        "name": "Quarter Mile", "description": "A scan",
        "identifier": [{"@type": "PropertyValue", "propertyID": "ARK", "value": ARK}],
    }

    def fake_exists(path):
        return path in (ITEM.sidecar_path, SILVER_PATH)

    def fake_read(path):
        if path == ITEM.sidecar_path:
            return json.dumps(bronze_doc)
        return json.dumps({})

    monkeypatch.setattr(item_record.fs, "exists", fake_exists)
    monkeypatch.setattr(item_record.fs, "read", fake_read)

    doc = item_record.build(ITEM)

    assert doc["name"] == "Quarter Mile"
    assert doc["description"] == "A scan"
    assert doc["@id"] == ARK
    assert doc["ark"] == ARK
    assert doc["associatedMedia"] == []
    assert "hasPart" not in doc
    assert fake_derivative[0]["type"] == "silver"
    assert fake_derivative[0]["rel_path"] == "item/item.jsonld.json"


def test_build_minimal_doc_from_identifiers_json_when_no_bronze_sidecar(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_SIDECAR_ITEM)
    identifiers_path = "cask:/bronze/g/c/latest/item/identifiers.json"

    def fake_exists(path):
        return path == identifiers_path

    def fake_read(path):
        return json.dumps([
            {"type": "LocalID", "value": "abc123"},
            {"type": None, "value": ARK},
        ])

    monkeypatch.setattr(item_record.fs, "exists", fake_exists)
    monkeypatch.setattr(item_record.fs, "read", fake_read)

    doc = item_record.build(NO_SIDECAR_ITEM)

    assert doc["@id"] == ARK
    assert doc["name"] == "item"
    assert {"@type": "PropertyValue", "propertyID": "LocalID", "value": "abc123"} in doc["identifier"]
    assert {"@type": "PropertyValue", "propertyID": "ARK", "value": ARK} in doc["identifier"]
    assert doc["associatedMedia"] == []


def test_build_flat_associated_media_for_single_file_item(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: ITEM)
    existing_silver = {
        "@id": ARK, "ark": ARK,
        "associatedMedia": [{
            "@type": "ImageObject",
            "contentUrl": "cask://silver/g/c/latest/item/public-frontend-derivatives/images/photo.tiff/images/small.jpg",
            "argonath:sourceGroup": "image",
        }],
    }

    def fake_exists(path):
        return path in (ITEM.sidecar_path, SILVER_PATH)

    def fake_read(path):
        if path == ITEM.sidecar_path:
            return json.dumps({"@id": ARK, "ark": ARK, "name": "Photo"})
        return json.dumps(existing_silver)

    monkeypatch.setattr(item_record.fs, "exists", fake_exists)
    monkeypatch.setattr(item_record.fs, "read", fake_read)

    doc = item_record.build(ITEM)

    assert "hasPart" not in doc
    assert len(doc["associatedMedia"]) == 1
    assert doc["associatedMedia"][0]["isBasedOn"] == "cask://bronze/g/c/latest/item/photo.tiff"


def test_build_has_part_for_multi_file_item(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: MULTI_ITEM)
    existing_silver = {
        "@id": ARK, "ark": ARK,
        "associatedMedia": [
            {
                "@type": "ImageObject",
                "contentUrl": "cask://silver/g/c/latest/box-1/folder-3/public-frontend-derivatives/images/photo-a.tiff/images/small.jpg",
                "argonath:sourceGroup": "image",
            },
            {
                "@type": "ImageObject",
                "contentUrl": "cask://silver/g/c/latest/box-1/folder-3/public-frontend-derivatives/images/photo-b.tiff/images/small.jpg",
                "argonath:sourceGroup": "image",
            },
        ],
    }
    identifiers_path = "cask:/bronze/g/c/latest/box-1/folder-3/identifiers.json"

    def fake_exists(path):
        return path in (MULTI_SILVER_PATH,)

    def fake_read(path):
        assert path == MULTI_SILVER_PATH
        return json.dumps(existing_silver)

    monkeypatch.setattr(item_record.fs, "exists", fake_exists)
    monkeypatch.setattr(item_record.fs, "read", fake_read)

    doc = item_record.build(MULTI_ITEM)

    assert doc["associatedMedia"] == []
    assert len(doc["hasPart"]) == 2
    assert [p["position"] for p in doc["hasPart"]] == [1, 2]
    assert doc["hasPart"][0]["associatedMedia"][0]["isBasedOn"] == "cask://bronze/g/c/latest/box-1/folder-3/photo-a.tiff"
    assert doc["hasPart"][1]["associatedMedia"][0]["isBasedOn"] == "cask://bronze/g/c/latest/box-1/folder-3/photo-b.tiff"
    assert all(p["argonath:sourceGroup"] == "image" for p in doc["hasPart"])
