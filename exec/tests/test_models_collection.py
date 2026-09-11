import json
import os
import re

import pytest

from digtk.lib import fs
from digtk.models import collection


def test_resolve_time_passes_through_literal():
    assert collection.resolve_time("2026-W29") == "2026-W29"


def test_resolve_time_none_stays_none():
    assert collection.resolve_time(None) is None


def test_resolve_time_current_date_is_iso_date():
    resolved = collection.resolve_time(collection.CURRENT_DATE)
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", resolved)


def test_resolve_time_current_time_is_iso_datetime():
    resolved = collection.resolve_time(collection.CURRENT_TIME)
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", resolved)


def test_dest_path_bronze_defaults_time_to_latest():
    path = collection.dest_path("bronze", "ucd-photos", {"group": "digital-collections"})
    assert path == "/bronze/digital-collections/ucd-photos/latest"


def test_dest_path_bronze_with_literal_time():
    path = collection.dest_path(
        "bronze", "ucd-photos", {"group": "digital-collections", "time": "2026-07-22"}
    )
    assert path == "/bronze/digital-collections/ucd-photos/2026-07-22"


def test_dest_path_bronze_with_current_date_sentinel():
    path = collection.dest_path(
        "bronze", "ucd-photos", {"group": "digital-collections", "time": collection.CURRENT_DATE}
    )
    assert re.match(r"^/bronze/digital-collections/ucd-photos/\d{4}-\d{2}-\d{2}$", path)


def test_dest_path_bronze_requires_group():
    with pytest.raises(ValueError, match="group"):
        collection.dest_path("bronze", "ucd-photos", {})


def test_dest_path_workspace_uses_given_username():
    path = collection.dest_path("workspace", "my-collection", {"username": "jrmerz"})
    assert path == "/workspace/jrmerz/my-collection"


def test_dest_path_workspace_defaults_username_from_env(monkeypatch):
    monkeypatch.setenv("USER", "jrmerz")
    path = collection.dest_path("workspace", "my-collection", {})
    assert path == "/workspace/jrmerz/my-collection"


def test_dest_path_workspace_rejects_time():
    with pytest.raises(ValueError, match="time"):
        collection.dest_path("workspace", "my-collection", {"username": "jrmerz", "time": "2026-07-22"})


def test_dest_path_rejects_unknown_root():
    with pytest.raises(ValueError, match="root"):
        collection.dest_path("gold", "my-collection", {})


def test_add_raises_if_source_missing():
    with pytest.raises(FileNotFoundError):
        collection.add("/no/such/path", "bronze", "ucd-photos", {"group": "digital-collections"})


def test_add_invokes_cask_cp_with_expected_argv(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()
    seen = {}

    def fake_run(argv, check):
        seen["argv"] = argv
        seen["check"] = check

    collection.add(
        str(source), "bronze", "ucd-photos",
        {"group": "digital-collections", "env": "prod", "run": fake_run},
    )

    assert seen["argv"] == [
        "cask", "cp", str(source), "/bronze/digital-collections/ucd-photos/latest",
        "-e", "prod",
    ]
    assert seen["check"] is True


def test_add_replace_and_dry_run_flags(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()
    seen = {}

    def fake_run(argv, check):
        seen["argv"] = argv

    collection.add(
        str(source), "workspace", "my-collection",
        {"username": "jrmerz", "replace": True, "dry_run": True, "run": fake_run},
    )

    assert seen["argv"] == [
        "cask", "cp", str(source), "/workspace/jrmerz/my-collection",
        "-x", "-d",
    ]


def test_add_defaults_to_leaving_cask_cps_confirmation_prompt_in_place(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()
    seen = {}

    def fake_run(argv, check):
        seen["argv"] = argv

    collection.add(
        str(source), "workspace", "my-collection",
        {"username": "jrmerz", "run": fake_run},
    )

    assert "-y" not in seen["argv"]


def test_add_yes_true_skips_confirmation_prompt(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()
    seen = {}

    def fake_run(argv, check):
        seen["argv"] = argv

    collection.add(
        str(source), "workspace", "my-collection",
        {"username": "jrmerz", "yes": True, "run": fake_run},
    )

    assert "-y" in seen["argv"]


# --- helpers: _slugify / _is_ark / _resolve_identifier_key / _unique_tree_slug ---

def test_slugify_lowercases_and_collapses_non_alnum():
    assert collection._slugify("Old Vines_New Lands--DC") == "old-vines-new-lands-dc"


def test_slugify_strips_leading_and_trailing_hyphens():
    assert collection._slugify("--Weird__Title__--") == "weird-title"


def test_is_ark_true_for_ark_prefixed_value():
    assert collection._is_ark("ark:/87287/d7abc123") is True


def test_is_ark_false_for_other_values():
    assert collection._is_ark("not-an-ark") is False
    assert collection._is_ark("") is False


def test_resolve_identifier_key_prefers_ark():
    result = collection._resolve_identifier_key(
        [{"type": "LocalID", "value": "loc-1"}, {"type": None, "value": "ark:/87287/d7abc123"}],
        "ref-1",
    )
    assert result == {"key": "d7abc123", "identifiers": ["ark:/87287/d7abc123"]}


def test_resolve_identifier_key_falls_back_to_local_id():
    result = collection._resolve_identifier_key([{"type": "LocalID", "value": "My Local ID"}], "ref-1")
    assert result == {"key": "my-local-id", "identifiers": ["My Local ID"]}


def test_resolve_identifier_key_falls_back_to_ref():
    result = collection._resolve_identifier_key([], "ref-1")
    assert result == {"key": "ref-1", "identifiers": ["ref-1"]}


def test_unique_tree_slug_disambiguates_collisions():
    used = set()
    first = collection._unique_tree_slug("Same Title", "key-1", used)
    second = collection._unique_tree_slug("Same Title", "key-2", used)
    assert first == "same-title"
    assert second == "same-title--key-2"


def test_unique_tree_slug_falls_back_to_untitled():
    used = set()
    assert collection._unique_tree_slug("", "", used) == "untitled"


# --- preservica_import -------------------------------------------------------

class _FakePreservicaClient:
    """Minimal fake standing in for digtk.lib.preservica.PreservicaClient, so
    preservica_import()'s own orchestration logic is tested without a real
    Preservica connection or its XML parsing (see test_preservica_client.py for
    that)."""

    def __init__(self):
        self.entities = {}          # ref -> {'title', 'type', 'fragment_refs': [...]}
        self.children = {}          # ref -> [{'ref', 'title', 'type'}]
        self.identifiers = {}       # ref -> [{'type', 'value'}]
        self.metadata_fragments = {}  # url -> {'schemaUri', 'rawXml', 'parsed'}
        self.representations = {}   # ref -> [...]
        self.files = {}             # downloadUrl -> bytes
        self.thumbnails = {}        # ref -> bytes|None
        self.raise_for = {}         # ref -> Exception, raised from get_entity

    def get_entity(self, data_type, ref, opts={}):
        if ref in self.raise_for:
            raise self.raise_for[ref]
        entity = self.entities[ref]
        key = "StructuralObject" if entity["type"] == "SO" else "InformationObject"
        return {key: {"Title": entity["title"]}, "_fragment_refs": entity.get("fragment_refs", [])}

    def list_metadata_fragments(self, entity_detail):
        return entity_detail.get("_fragment_refs", [])

    def get_identifiers(self, data_type, ref):
        return self.identifiers.get(ref, [])

    def get_metadata_fragment(self, url):
        return self.metadata_fragments[url]

    def get_children(self, ref, opts={}):
        return self.children.get(ref, [])

    def resolve_all_content(self, io_ref, entity_detail=None):
        return self.representations.get(io_ref, [])

    def download_file(self, download_url):
        return self.files[download_url]

    def get_thumbnail(self, ref):
        return self.thumbnails.get(ref)


def _read_json(path):
    return json.loads(fs.read(path))


def test_preservica_import_writes_by_ref_layout(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "My Collection", "type": "SO"}
    client.entities["io-1"] = {"title": "Item One", "type": "IO", "fragment_refs": [
        {"schemaUri": "http://oai_dc", "url": "http://x/frag1"},
    ]}
    client.metadata_fragments["http://x/frag1"] = {
        "schemaUri": "http://oai_dc", "rawXml": "<dc/>", "parsed": {"dc": {"title": "Item One"}},
    }
    client.children["root-ref"] = [{"ref": "io-1", "title": "Item One", "type": "IO"}]
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc123"}]
    client.representations["io-1"] = [{
        "representationType": "Preservation", "contentObjectRef": "co-1", "mimeGroup": "image",
        "filename": "page1.tif", "downloadUrl": "http://x/f1", "fileSize": 9,
        "fixity": {"algorithm": "SHA1", "value": "abc123"},
    }]
    client.files["http://x/f1"] = b"tiffbytes"
    client.thumbnails["io-1"] = b"thumbbytes"

    manifest = collection.preservica_import(
        "root-ref", "my-collection", "preservica",
        {"client": client, "dest_root": str(tmp_path)},
    )

    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    collection_manifest = _read_json(str(tmp_path / "bronze" / "preservica" / "my-collection" / "manifest.json"))
    assert collection_manifest == {
        "sourceType": "preservica", "timeFormat": "latest", "preservicaRootRef": "root-ref",
    }

    assert _read_json(str(root / "by-ref" / "root-ref" / "entity.json")) == {
        "ref": "root-ref", "title": "My Collection", "type": "SO", "metadataFragmentRefs": [],
    }
    assert _read_json(str(root / "by-ref" / "root-ref" / "children.json")) == [
        {"ref": "io-1", "title": "Item One", "type": "IO"},
    ]

    io_entity = _read_json(str(root / "by-ref" / "io-1" / "entity.json"))
    assert io_entity["title"] == "Item One"
    assert io_entity["type"] == "IO"
    assert _read_json(str(root / "by-ref" / "io-1" / "identifiers.json")) == client.identifiers["io-1"]
    assert _read_json(str(root / "by-ref" / "io-1" / "metadata.json")) == [client.metadata_fragments["http://x/frag1"]]
    assert _read_json(str(root / "by-ref" / "io-1" / "representations.json")) == client.representations["io-1"]

    media_path = root / "by-ref" / "io-1" / "media" / "Preservation" / "page1.tif"
    assert media_path.read_bytes() == b"tiffbytes"
    assert _read_json(str(media_path) + ".fixity.json") == {"algorithm": "SHA1", "value": "abc123"}
    assert (root / "by-ref" / "io-1" / "media" / "thumbnail").read_bytes() == b"thumbbytes"

    run_manifest = _read_json(str(root / "manifest.json"))
    assert {"ref": "root-ref", "type": "SO", "title": "My Collection"} in run_manifest["items"]
    assert {"ref": "io-1", "type": "IO", "title": "Item One"} in run_manifest["items"]
    assert run_manifest["errors"] == []
    assert run_manifest["warnings"] == []
    assert "walkedAt" in run_manifest


def test_preservica_import_warns_when_no_ark(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["io-1"] = {"title": "Item One", "type": "IO"}
    client.children["root-ref"] = [{"ref": "io-1", "title": "Item One", "type": "IO"}]
    client.identifiers["io-1"] = [{"type": "LocalID", "value": "loc-1"}]
    client.representations["io-1"] = []

    manifest = collection.preservica_import(
        "root-ref", "my-collection", "preservica",
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert manifest["warnings"] == [
        {"ref": "io-1", "stage": "identifiers", "message": "no ARK-shaped identifier found"},
    ]


def test_preservica_import_records_errors_without_aborting(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["io-2"] = {"title": "Item Two", "type": "IO"}
    client.children["root-ref"] = [
        {"ref": "io-1", "title": "Item One", "type": "IO"},
        {"ref": "io-2", "title": "Item Two", "type": "IO"},
    ]
    client.raise_for["io-1"] = RuntimeError("boom")
    client.identifiers["io-2"] = [{"type": None, "value": "ark:/87287/d7xyz"}]
    client.representations["io-2"] = []

    manifest = collection.preservica_import(
        "root-ref", "my-collection", "preservica",
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert manifest["errors"] == [{"ref": "io-1", "stage": "entity", "message": "boom"}]
    assert {"ref": "io-2", "type": "IO", "title": "Item Two"} in manifest["items"]
    assert not any(i["ref"] == "io-1" for i in manifest["items"])


def test_preservica_import_respects_max_depth(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["sub-folder"] = {"title": "Sub Folder", "type": "SO"}
    client.children["root-ref"] = [{"ref": "sub-folder", "title": "Sub Folder", "type": "SO"}]
    client.children["sub-folder"] = []

    manifest = collection.preservica_import(
        "root-ref", "my-collection", "preservica",
        {"client": client, "dest_root": str(tmp_path), "max_depth": 0},
    )

    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    assert (root / "by-ref" / "root-ref" / "entity.json").exists()
    assert not (root / "by-ref" / "sub-folder" / "entity.json").exists()
    assert not any(i["ref"] == "sub-folder" for i in manifest["items"])


def test_preservica_import_raises_without_client_or_credentials(tmp_path, monkeypatch):
    from digtk.lib import auth
    monkeypatch.setattr(auth, "resolve_preservica", lambda: {})
    with pytest.raises(ValueError, match="Preservica"):
        collection.preservica_import("root-ref", "my-collection", "preservica", {"dest_root": str(tmp_path)})


# --- preservica_tree ----------------------------------------------------------

def _import_simple_collection(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["io-1"] = {"title": "Old Vines_New Lands", "type": "IO"}
    client.children["root-ref"] = [{"ref": "io-1", "title": "Old Vines_New Lands", "type": "IO"}]
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc123"}]
    client.representations["io-1"] = [{
        "representationType": "Preservation", "contentObjectRef": "co-1", "mimeGroup": "image",
        "filename": "page1.tif", "downloadUrl": "http://x/f1", "fileSize": 9, "fixity": None,
    }]
    client.files["http://x/f1"] = b"tiffbytes"

    collection.preservica_import(
        "root-ref", "my-collection", "preservica",
        {"client": client, "dest_root": str(tmp_path)},
    )
    return tmp_path / "bronze" / "preservica" / "my-collection" / "latest"


def test_preservica_tree_builds_from_by_ref(tmp_path):
    root = _import_simple_collection(tmp_path)

    count = collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    assert count == 1
    item_dir = root / "tree" / "old-vines-new-lands"
    assert (item_dir / "Preservation" / "page1.tif").read_bytes() == b"tiffbytes"
    assert _read_json(str(item_dir / "identifiers.json")) == [{"type": None, "value": "ark:/87287/d7abc123"}]


def test_preservica_tree_is_idempotent_and_rebuilds_from_scratch(tmp_path):
    root = _import_simple_collection(tmp_path)
    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    # A stale leftover file from a hypothetical earlier run/rename should not survive a rebuild.
    stale = root / "tree" / "renamed-away" / "leftover.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")

    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    assert not stale.exists()
    assert (root / "tree" / "old-vines-new-lands" / "Preservation" / "page1.tif").exists()


def test_preservica_tree_raises_without_prior_import(tmp_path):
    with pytest.raises(FileNotFoundError):
        collection.preservica_tree("no-such-collection", "preservica", {"dest_root": str(tmp_path)})


# --- local_to_bronze_tree ------------------------------------------------------

def test_local_to_bronze_tree_materializes_directories_with_fixity(tmp_path):
    source = tmp_path / "source"
    (source / "Item One").mkdir(parents=True)
    (source / "Item One" / "photo.jpg").write_bytes(b"jpegbytes")

    dest_root = tmp_path / "dest"
    count = collection.local_to_bronze_tree(
        str(source), "my-collection", "digitization-lab", {"dest_root": str(dest_root)},
    )

    assert count == 1
    item_dir = dest_root / "bronze" / "digitization-lab" / "my-collection" / "latest" / "tree" / "item-one"
    assert (item_dir / "photo.jpg").read_bytes() == b"jpegbytes"

    import hashlib
    expected_hash = hashlib.sha256(b"jpegbytes").hexdigest()
    assert _read_json(str(item_dir / "photo.jpg.fixity.json")) == {"algorithm": "SHA256", "value": expected_hash}


def test_local_to_bronze_tree_ignores_files_directly_in_source_dir(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "stray.txt").write_text("not an item")
    (source / "Item One").mkdir()
    (source / "Item One" / "a.txt").write_text("a")

    count = collection.local_to_bronze_tree(
        str(source), "my-collection", "digitization-lab", {"dest_root": str(tmp_path / "dest")},
    )
    assert count == 1


def test_local_to_bronze_tree_raises_for_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError):
        collection.local_to_bronze_tree(str(tmp_path / "nope"), "my-collection", "group", {})
