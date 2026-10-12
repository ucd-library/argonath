import json

import pytest

from digtk.lib.items import ItemLocation
from digtk.models.digital_website import captions, item_record

ARK = "ark:/87287/d7abc1234"

TREE_ITEM = ItemLocation(
    ark=ARK, group="preservica", collection="haeger", time="latest", item_path="tree/quarter-mile",
    bronze_binary_paths=["cask:/bronze/preservica/haeger/latest/tree/quarter-mile/Preservation/photo.tiff"],
)
TREE_FOLDER = "cask:/bronze/preservica/haeger/latest/tree/quarter-mile"

SIDECAR_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
    sidecar_path="cask:/bronze/g/c/latest/item/photo.tiff.jsonld.json",
)

PLAIN_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
)

NO_ARK_ITEM = ItemLocation(
    ark=None, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
)

MULTI_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="box-1/folder-3",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/box-1/folder-3/photo-a.tiff",
        "cask:/bronze/g/c/latest/box-1/folder-3/photo-b.tiff",
    ],
)

VIDEO_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/movie.mp4"],
)


def _image_entry(item_path, filename, label="small"):
    return {
        "@type": "ImageObject",
        "contentUrl": f"cask://silver/g/c/latest/{item_path}/public-frontend-derivatives/images/{filename}/images/{label}.jpg",
    }


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


@pytest.fixture
def fake_fs(monkeypatch):
    files = {}
    monkeypatch.setattr(item_record.fs, "exists", lambda path: path in files)
    monkeypatch.setattr(item_record.fs, "read", lambda path: files[path])
    return files


@pytest.fixture
def fake_media(monkeypatch):
    media = {}
    for group in list(item_record._MEDIA_READERS):
        monkeypatch.setitem(item_record._MEDIA_READERS, group, lambda item, g=group: [dict(e) for e in media.get(g, [])])
    return media


def _written(calls, suffix):
    return [c for c in calls if c["rel_path"].endswith(suffix)]


def test_build_raises_for_ark_less_item(fake_derivative):
    with pytest.raises(LookupError, match="no ark"):
        item_record.build(NO_ARK_ITEM)


def test_build_writes_the_record_and_its_manifest_to_gold_only(fake_derivative, fake_fs, fake_media):
    item_record.build(TREE_ITEM)

    record = _written(fake_derivative, "item.jsonld.json")[0]
    assert record["type"] == "gold"
    assert record["opts"]["group"] == "preservica"
    assert record["rel_path"] == "tree/quarter-mile/item.jsonld.json"
    assert all(c["type"] == "gold" for c in fake_derivative)
    manifest = json.loads(_written(fake_derivative, "manifest.json")[0]["data"])
    assert manifest["bronze"] == "/bronze/preservica/haeger/latest/tree/quarter-mile"
    assert item_record.record_path(TREE_ITEM) == "cask:/gold/preservica/haeger/tree/quarter-mile/item.jsonld.json"


def test_build_maps_tree_metadata_json_dublin_core(fake_derivative, fake_fs, fake_media):
    fake_fs[f"{TREE_FOLDER}/metadata.json"] = json.dumps({
        "dc": {"title": "Quarter Mile", "description": "A scan"},
        "preservica": {"ref": "io-1", "title": "Quarter_Mile", "type": "IO"},
    })

    doc = item_record.build(TREE_ITEM)

    assert doc["@id"] == ARK
    assert doc["ark"] == ARK
    assert doc["name"] == "Quarter Mile"
    assert doc["description"] == "A scan"
    assert doc["identifier"] == [{"@type": "PropertyValue", "propertyID": "ARK", "value": ARK}]
    assert doc["argonath:fieldSources"] == {"name": "human", "description": "human"}


def test_build_minimal_doc_from_identifiers_json_without_a_metadata_fragment(fake_derivative, fake_fs, fake_media):
    fake_fs[f"{TREE_FOLDER}/metadata.json"] = json.dumps({"preservica": {"ref": "io-1", "title": "Quarter_Mile"}})
    fake_fs[f"{TREE_FOLDER}/identifiers.json"] = json.dumps([
        {"type": "LocalID", "value": "abc123"},
        {"type": None, "value": ARK},
    ])

    doc = item_record.build(TREE_ITEM)

    assert doc["@id"] == ARK
    assert doc["name"] == "quarter-mile"
    assert {"@type": "PropertyValue", "propertyID": "LocalID", "value": "abc123"} in doc["identifier"]
    assert {"@type": "PropertyValue", "propertyID": "ARK", "value": ARK} in doc["identifier"]
    assert doc["associatedMedia"] == []


def test_build_adopts_a_bronze_binary_sidecar(fake_derivative, fake_fs, fake_media):
    fake_fs[SIDECAR_ITEM.sidecar_path] = json.dumps({"@id": ARK, "ark": ARK, "name": "Photo", "description": "A scan"})

    doc = item_record.build(SIDECAR_ITEM)

    assert doc["name"] == "Photo"
    assert doc["description"] == "A scan"


def test_build_flat_associated_media_for_single_file_item(fake_derivative, fake_fs, fake_media):
    fake_media["image"] = [_image_entry("item", "photo.tiff")]

    doc = item_record.build(PLAIN_ITEM)

    assert "hasPart" not in doc
    assert len(doc["associatedMedia"]) == 1
    assert doc["associatedMedia"][0]["argonath:sourceGroup"] == "image"
    assert doc["associatedMedia"][0]["isBasedOn"] == "cask://bronze/g/c/latest/item/photo.tiff"


def test_build_has_part_for_multi_file_item(fake_derivative, fake_fs, fake_media):
    fake_media["image"] = [
        _image_entry("box-1/folder-3", "photo-a.tiff"),
        _image_entry("box-1/folder-3", "photo-b.tiff"),
    ]

    doc = item_record.build(MULTI_ITEM)

    assert doc["associatedMedia"] == []
    assert [p["position"] for p in doc["hasPart"]] == [1, 2]
    assert doc["hasPart"][0]["associatedMedia"][0]["isBasedOn"] == "cask://bronze/g/c/latest/box-1/folder-3/photo-a.tiff"
    assert doc["hasPart"][1]["associatedMedia"][0]["isBasedOn"] == "cask://bronze/g/c/latest/box-1/folder-3/photo-b.tiff"
    assert all(p["argonath:sourceGroup"] == "image" for p in doc["hasPart"])
    assert len(item_record.flat_media(doc)) == 2


def test_build_attaches_captions_and_flags_them_ai(fake_derivative, fake_fs, fake_media, monkeypatch):
    fake_media["video"] = [{
        "@type": "VideoObject", "identifier": "movie",
        "contentUrl": "cask://silver/g/c/latest/item/public-frontend-derivatives/movie.mp4/streaming/playlist.m3u8",
    }]
    fake_fs["cask:/silver/g/c/latest/item/captions/manifest.json"] = json.dumps({
        "params": {"language": "en"}, "outputs": ["output-en.vtt"],
    })
    monkeypatch.setattr(captions.fs, "exists", item_record.fs.exists)
    monkeypatch.setattr(captions.fs, "read", item_record.fs.read)

    doc = item_record.build(VIDEO_ITEM)

    entry = doc["associatedMedia"][0]
    assert entry["argonath:sourceGroup"] == "video"
    assert entry["caption"]["contentUrl"] == "cask://silver/g/c/latest/item/captions/output-en.vtt"
    assert doc["argonath:fieldSources"]["caption"] == "ai"
