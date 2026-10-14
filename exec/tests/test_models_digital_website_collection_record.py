import json

import pytest

from digtk.models.digital_website import collection_record, item_record

ARK = "ark:/87287/d7root"
ROOT = "cask:/bronze/g/c/latest"
METADATA = {
    "preservica": {"ref": "abc", "title": "Preservica title", "description": "Preservica description"},
    "dc": {"title": "Haeger Collection", "description": "Photos.", "subject": ["Aerial photographs"]},
}
IDENTIFIERS = [{"type": "ARK", "value": ARK}, {"type": "LocalID", "value": "MC-165"}]


@pytest.fixture
def bronze(monkeypatch):
    """A fake CaskFS holding the collection root's tree files."""
    files = {
        f"{ROOT}/tree/metadata.json": METADATA,
        f"{ROOT}/tree/identifiers.json": IDENTIFIERS,
    }
    monkeypatch.setattr(collection_record.fs, "exists", lambda path: path in files)
    monkeypatch.setattr(collection_record.fs, "read", lambda path: json.dumps(files[path]).encode())
    return files


@pytest.fixture
def writes(monkeypatch):
    calls = []
    monkeypatch.setattr(collection_record.derivative, "write", lambda **kwargs: calls.append(kwargs))
    return calls


def test_parse_key_accepts_bronze_and_bare_keys():
    assert collection_record.parse_key("bronze/preservica/haeger") == ("preservica", "haeger")
    assert collection_record.parse_key("preservica/haeger") == ("preservica", "haeger")
    assert collection_record.parse_key("cask:/bronze/preservica/haeger") == ("preservica", "haeger")


def test_parse_key_rejects_other_shapes():
    with pytest.raises(ValueError):
        collection_record.parse_key("haeger")


def test_build_maps_dublin_core_onto_the_root(bronze, writes):
    doc = collection_record.build("c", "g", {"project": "digital-dev"})
    root = collection_record.root_node(doc)

    assert doc["@id"] == ARK
    assert root["@id"] == item_record.COLLECTION_URI_PREFIX + ARK
    assert root["@type"] == ["schema:Collection"]
    assert (root["name"], root["description"]) == ("Haeger Collection", "Photos.")
    assert root["about"] == [{"@id": "Aerial photographs"}]
    assert [i["value"] for i in root["identifier"]] == [ARK, "MC-165"]


def test_build_has_no_has_part(bronze, writes):
    root = collection_record.build("c", "g")["@graph"][0]
    assert "hasPart" not in root


def test_build_falls_back_to_preservica_title_and_description(bronze, writes):
    bronze[f"{ROOT}/tree/metadata.json"] = {"preservica": METADATA["preservica"]}
    root = collection_record.build("c", "g")["@graph"][0]
    assert (root["name"], root["description"]) == ("Preservica title", "Preservica description")


def test_build_writes_to_gold_with_bronze_lineage(bronze, writes):
    collection_record.build("c", "g", {"project": "digital-dev"})

    assert len(writes) == 1
    call = writes[0]
    assert (call["type"], call["collection"], call["rel_path"]) == ("gold", "c", "collection.jsonld.json")
    assert call["opts"] == {"app": "digital-dev", "replace": True}
    assert call["source_files"] == [f"{ROOT}/tree/metadata.json", f"{ROOT}/tree/identifiers.json"]


def test_build_requires_a_collection_ark(bronze, writes):
    del bronze[f"{ROOT}/tree/identifiers.json"]
    with pytest.raises(LookupError):
        collection_record.build("c", "g")
    assert writes == []


def test_record_path_is_under_the_gold_collection():
    path = collection_record.record_path("c", {"project": "digital-dev"})
    assert path == "cask:/gold/digital-dev/c/collection.jsonld.json"
