import json

import pytest

from digtk.lib.preservica.local_client import LocalPreservicaClient
from digtk.models import collection
from tests.test_models_collection import _FakeRegistry


@pytest.fixture
def fake_registry(monkeypatch):
    """preservica_import() calls register_collection() unconditionally, which
    would otherwise open a real Postgres connection — see
    test_models_collection.py's own identically-shaped autouse fixture; not
    reused as autouse here since only the one end-to-end test below touches
    the registry at all."""
    registry = _FakeRegistry()
    monkeypatch.setattr(collection.db, "query", registry.query)
    monkeypatch.setattr(collection.db, "execute", registry.execute)
    return registry


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _make_item(base, ref, title, description=None, identifiers=None, media=None, fragments=None):
    """Build one {ref}/ entity directory in the shape LocalPreservicaClient expects."""
    entity_dir = base / ref
    _write_json(entity_dir / "entity.json", {"title": title, "description": description, "type": "IO"})
    _write_json(entity_dir / "identifiers.json", identifiers or [])
    if fragments is not None:
        _write_json(entity_dir / "metadata.json", [
            {"schemaUri": "http://www.openarchives.org/OAI/2.0/oai_dc/", "rawXml": xml}
            for xml in fragments
        ])
    for rep_type, filename, data in (media or []):
        media_path = entity_dir / "media" / rep_type / filename
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(data)
    return entity_dir


def _write_fixity_sidecar(base, ref, rep_type, filename):
    path = base / ref / "media" / rep_type / f"{filename}.fixity.json"
    _write_json(path, {"algorithm": "SHA1", "value": "deadbeef"})


def _make_folder(base, ref, title, children):
    entity_dir = base / ref
    _write_json(entity_dir / "entity.json", {"title": title, "description": None, "type": "SO"})
    _write_json(entity_dir / "children.json", children)
    return entity_dir


# --- LocalPreservicaClient unit tests ----------------------------------------


def test_get_entity_wraps_information_object(tmp_path):
    _make_item(tmp_path, "io-1", "Item One", description="A description")
    client = LocalPreservicaClient(str(tmp_path))

    detail = client.get_entity("information-objects", "io-1")

    assert detail["InformationObject"] == {"Title": "Item One", "Description": "A description"}
    assert detail["_local_ref"] == "io-1"


def test_get_entity_wraps_structural_object(tmp_path):
    _make_folder(tmp_path, "so-1", "Folder One", [])
    client = LocalPreservicaClient(str(tmp_path))

    detail = client.get_entity("structural-objects", "so-1")

    assert detail["StructuralObject"] == {"Title": "Folder One", "Description": None}


def test_get_entity_missing_ref_returns_empty_titles(tmp_path):
    client = LocalPreservicaClient(str(tmp_path))
    detail = client.get_entity("information-objects", "nope")
    assert detail["InformationObject"] == {"Title": None, "Description": None}


def test_list_and_read_metadata_fragments_round_trip(tmp_path):
    _make_item(tmp_path, "io-1", "Item One", fragments=["<dc><title>A</title></dc>", "<dc><title>B</title></dc>"])
    client = LocalPreservicaClient(str(tmp_path))

    detail = client.get_entity("information-objects", "io-1")
    fragments = client.list_metadata_fragments(detail)

    assert len(fragments) == 2
    assert client.get_metadata_fragment(fragments[0]["url"]) == "<dc><title>A</title></dc>"
    assert client.get_metadata_fragment(fragments[1]["url"]) == "<dc><title>B</title></dc>"


def test_list_metadata_fragments_empty_when_no_metadata_json(tmp_path):
    _make_item(tmp_path, "io-1", "Item One")
    client = LocalPreservicaClient(str(tmp_path))
    detail = client.get_entity("information-objects", "io-1")
    assert client.list_metadata_fragments(detail) == []


def test_get_identifiers_reads_identifiers_json(tmp_path):
    _make_item(tmp_path, "io-1", "Item One", identifiers=[{"type": "ARK", "value": "ark:/1/x"}])
    client = LocalPreservicaClient(str(tmp_path))
    assert client.get_identifiers("information-objects", "io-1") == [{"type": "ARK", "value": "ark:/1/x"}]


def test_get_identifiers_missing_file_returns_empty_list(tmp_path):
    client = LocalPreservicaClient(str(tmp_path))
    assert client.get_identifiers("information-objects", "nope") == []


def test_get_children_reads_children_json(tmp_path):
    _make_folder(tmp_path, "so-1", "Folder One", [{"ref": "io-1", "type": "IO"}])
    client = LocalPreservicaClient(str(tmp_path))
    assert client.get_children("so-1") == [{"ref": "io-1", "type": "IO"}]


def test_resolve_all_content_derives_representations_from_media_dirs(tmp_path):
    _make_item(tmp_path, "io-1", "Item One", media=[
        ("Preservation", "page_001.tif", b"tiffbytes"),
        ("Access", "page_001.jpg", b"jpegbytes"),
    ])
    client = LocalPreservicaClient(str(tmp_path))

    representations = client.resolve_all_content("io-1")

    by_type = {(r["representationType"], r["filename"]): r for r in representations}
    assert set(by_type) == {("Access", "page_001.jpg"), ("Preservation", "page_001.tif")}
    for rep in representations:
        assert rep["fixity"] is None
        assert rep["downloadUrl"].endswith(f"media/{rep['representationType']}/{rep['filename']}")


def test_resolve_all_content_ignores_fixity_json_sidecars(tmp_path):
    _make_item(tmp_path, "io-1", "Item One", media=[("Preservation", "page_001.tif", b"tiffbytes")])
    _write_fixity_sidecar(tmp_path, "io-1", "Preservation", "page_001.tif")
    client = LocalPreservicaClient(str(tmp_path))

    representations = client.resolve_all_content("io-1")

    assert len(representations) == 1
    assert representations[0]["filename"] == "page_001.tif"


def test_resolve_all_content_empty_when_no_media_dir(tmp_path):
    _make_item(tmp_path, "io-1", "Item One")
    client = LocalPreservicaClient(str(tmp_path))
    assert client.resolve_all_content("io-1") == []


def test_download_file_reads_local_path(tmp_path):
    _make_item(tmp_path, "io-1", "Item One", media=[("Preservation", "page_001.tif", b"tiffbytes")])
    client = LocalPreservicaClient(str(tmp_path))
    [rep] = client.resolve_all_content("io-1")
    assert client.download_file(rep["downloadUrl"]) == b"tiffbytes"


def test_get_thumbnail_always_none(tmp_path):
    client = LocalPreservicaClient(str(tmp_path))
    assert client.get_thumbnail("io-1") is None


# --- End-to-end via collection.preservica_import() ---------------------------


def test_preservica_import_with_local_client_writes_by_ref_layout(tmp_path, fake_registry):
    source_dir = tmp_path / "source"
    _make_folder(source_dir, "root-ref", "My Collection", [{"ref": "io-1", "type": "IO"}])
    _make_item(
        source_dir, "io-1", "Item One",
        identifiers=[{"type": "ARK", "value": "ark:/87287/d7abc123"}],
        fragments=["<dc><title>Item One</title></dc>"],
        media=[("Preservation", "page1.tif", b"tiffbytes")],
    )

    dest_root = tmp_path / "dest"
    client = LocalPreservicaClient(str(source_dir))
    manifest = collection.preservica_import(
        "root-ref", "my-collection", "preservica",
        {"client": client, "dest_root": str(dest_root)},
    )

    root = dest_root / "bronze" / "preservica" / "my-collection" / "latest"
    io_entity = json.loads((root / "by-ref" / "io-1" / "entity.json").read_text())
    assert io_entity["title"] == "Item One"
    assert io_entity["type"] == "IO"

    assert (root / "by-ref" / "io-1" / "media" / "Preservation" / "page1.tif").read_bytes() == b"tiffbytes"
    assert (root / "by-ref" / "io-1" / "metadata" / "0.xml").read_text() == "<dc><title>Item One</title></dc>"
    # preservica_import() re-derives its own by-ref/metadata/0.xml (numbered
    # raw-fragment convention) regardless of the source's own metadata.json
    # array shape -- this proves the two conventions don't need to match.

    assert manifest["errors"] == []
    assert {"ref": "io-1", "type": "IO", "title": "Item One"} in manifest["items"]
