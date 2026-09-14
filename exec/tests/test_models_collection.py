import json
import os
import re

import pytest

from digtk.lib import fs
from digtk.models import collection


class _FakeRegistry:
    """In-memory stand-in for collection_registry + collection_identifiers,
    driven by monkeypatching digtk.lib.db.query/execute — see
    collection.register_collection()/get_registration()/list_active_collections().
    Pattern-matches on SQL text rather than implementing a real query engine,
    since collection.py only ever issues a handful of fixed statement shapes."""

    def __init__(self):
        self.rows = {}
        self.identifiers = {}
        self._next_id = 1

    def query(self, sql, params=(), opts={}):
        if "JOIN argonath.collection_identifiers" in sql:
            id_type, id_value = params
            for cid, idents in self.identifiers.items():
                row = self.rows[cid]
                if row["status"] != "active":
                    continue
                if any(i["id_type"] == id_type and i["id_value"] == id_value for i in idents):
                    return [{
                        "id": row["id"], "name": row["name"], "group_name": row["group_name"],
                        "source_type": row["source_type"], "workflow_tags": row["workflow_tags"],
                        "status": row["status"],
                    }]
            return []
        if "FROM argonath.collection_identifiers" in sql:
            collection_id = params[0]
            return [
                {"id_value": i["id_value"]} for i in self.identifiers.get(collection_id, [])
                if i["id_type"] == "preservica_ref"
            ]
        if "name = %s" in sql:
            name = params[0]
            return [
                {"id": r["id"], "group_name": r["group_name"], "source_type": r["source_type"]}
                for r in self.rows.values() if r["name"] == name and r["status"] == "active"
            ]
        if "FROM argonath.collection_registry" in sql:
            rows = [r for r in self.rows.values() if r["status"] == "active"]
            if params:
                rows = [r for r in rows if r["source_type"] == params[0]]
            return [
                {"id": r["id"], "name": r["name"], "group_name": r["group_name"],
                 "source_type": r["source_type"], "workflow_tags": r["workflow_tags"]}
                for r in rows
            ]
        raise AssertionError(f"Unexpected query: {sql}")

    def execute(self, sql, params=(), opts={}):
        if sql.startswith("INSERT INTO argonath.collection_registry"):
            name, group, source_type, tags = params
            row_id = self._next_id
            self._next_id += 1
            self.rows[row_id] = {
                "id": row_id, "name": name, "group_name": group,
                "source_type": source_type, "workflow_tags": tags, "status": "active",
            }
            return [{"id": row_id}] if opts.get("returning") else None
        if sql.startswith("UPDATE argonath.collection_registry"):
            source_type, tags, collection_id = params
            self.rows[collection_id]["source_type"] = source_type
            self.rows[collection_id]["workflow_tags"] = tags
            return None
        if sql.startswith("DELETE FROM argonath.collection_identifiers"):
            self.identifiers[params[0]] = []
            return None
        if sql.startswith("INSERT INTO argonath.collection_identifiers"):
            collection_id, id_type, id_value = params
            self.identifiers.setdefault(collection_id, []).append(
                {"id_type": id_type, "id_value": id_value}
            )
            return None
        raise AssertionError(f"Unexpected execute: {sql}")


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch):
    """Every test in this file gets an isolated in-memory collection registry —
    add()/preservica_import() call register_collection() unconditionally, so
    none of these tests may touch a real Postgres connection."""
    registry = _FakeRegistry()
    monkeypatch.setattr(collection.db, "query", registry.query)
    monkeypatch.setattr(collection.db, "execute", registry.execute)
    return registry


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


def test_add_bronze_registers_collection(tmp_path, fake_registry):
    source = tmp_path / "photos"
    source.mkdir()

    collection.add(
        str(source), "bronze", "my-collection",
        {"group": "digital-collections", "run": lambda argv, check: None},
    )

    rows = list(fake_registry.rows.values())
    assert len(rows) == 1
    assert rows[0]["name"] == "my-collection"
    assert rows[0]["group_name"] == "digital-collections"
    assert rows[0]["source_type"] == "manual"


def test_add_workspace_does_not_register_collection(tmp_path, fake_registry):
    source = tmp_path / "photos"
    source.mkdir()

    collection.add(
        str(source), "workspace", "my-collection",
        {"username": "jrmerz", "run": lambda argv, check: None},
    )

    assert fake_registry.rows == {}


def test_add_bronze_raises_on_name_collision_with_different_group(tmp_path, fake_registry):
    source = tmp_path / "photos"
    source.mkdir()
    run = lambda argv, check: None

    collection.add(str(source), "bronze", "my-collection", {"group": "group-a", "run": run})
    with pytest.raises(ValueError, match="already registered"):
        collection.add(str(source), "bronze", "my-collection", {"group": "group-b", "run": run})


def test_add_bronze_resync_same_name_and_group_does_not_raise(tmp_path, fake_registry):
    source = tmp_path / "photos"
    source.mkdir()
    run = lambda argv, check: None

    collection.add(str(source), "bronze", "my-collection", {"group": "group-a", "run": run})
    collection.add(str(source), "bronze", "my-collection", {"group": "group-a", "run": run})

    assert len(fake_registry.rows) == 1


# --- collection registry (register_collection / get_registration / list_active_collections) --

def test_init_pg_runs_the_schema_sql_file(monkeypatch):
    seen = {}
    monkeypatch.setattr(collection.db, "run_script", lambda sql, opts={}: seen.update(sql=sql, opts=opts))

    collection.init_pg()

    assert "CREATE SCHEMA IF NOT EXISTS argonath" in seen["sql"]
    assert "argonath.collection_registry" in seen["sql"]
    assert "argonath.collection_identifiers" in seen["sql"]


def test_parse_workflow_tags_always_includes_sync():
    assert collection._parse_workflow_tags("ai,public") == ["ai", "public", "sync"]


def test_parse_workflow_tags_dedupes_and_sorts():
    assert collection._parse_workflow_tags("sync,ai,sync") == ["ai", "sync"]


def test_parse_workflow_tags_empty_value_is_just_sync():
    assert collection._parse_workflow_tags("") == ["sync"]


def test_register_collection_inserts_new_row_and_identifiers(fake_registry):
    result = collection.register_collection(
        "ucd-photos", "preservica", "preservica",
        [
            {"type": "argonath", "value": "ai,public"},
            {"type": "preservica_ref", "value": "ref-1"},
            {"type": "ark", "value": "ark:/87287/d7abc"},
        ],
    )

    assert result["name"] == "ucd-photos"
    assert result["workflow_tags"] == ["ai", "public", "sync"]

    row = fake_registry.rows[result["id"]]
    assert row["group_name"] == "preservica"
    assert row["source_type"] == "preservica"

    stored = {(i["id_type"], i["id_value"]) for i in fake_registry.identifiers[result["id"]]}
    assert stored == {
        ("argonath", "ai,public"), ("preservica_ref", "ref-1"), ("ark", "ark:/87287/d7abc"),
    }


def test_register_collection_no_argonath_identifier_has_no_tags(fake_registry):
    result = collection.register_collection(
        "ucd-photos", "preservica", "preservica",
        [{"type": "preservica_ref", "value": "ref-1"}],
    )
    assert result["workflow_tags"] == []


def test_register_collection_reimport_same_ref_updates_not_collides(fake_registry):
    first = collection.register_collection(
        "ucd-photos", "preservica", "preservica",
        [{"type": "preservica_ref", "value": "ref-1"}, {"type": "argonath", "value": "sync"}],
    )
    second = collection.register_collection(
        "ucd-photos", "preservica", "preservica",
        [{"type": "preservica_ref", "value": "ref-1"}, {"type": "argonath", "value": "ai,public"}],
    )

    assert second["id"] == first["id"]
    assert len(fake_registry.rows) == 1
    assert second["workflow_tags"] == ["ai", "public", "sync"]


def test_register_collection_different_ref_same_name_raises(fake_registry):
    collection.register_collection(
        "ucd-photos", "preservica", "preservica",
        [{"type": "preservica_ref", "value": "ref-1"}],
    )
    with pytest.raises(ValueError, match="already registered"):
        collection.register_collection(
            "ucd-photos", "preservica", "preservica",
            [{"type": "preservica_ref", "value": "ref-2"}],
        )


def test_register_collection_manual_after_preservica_same_name_raises(fake_registry):
    collection.register_collection(
        "ucd-photos", "preservica", "preservica",
        [{"type": "preservica_ref", "value": "ref-1"}],
    )
    with pytest.raises(ValueError, match="already registered"):
        collection.register_collection("ucd-photos", "some-group", "manual")


def test_get_registration_finds_by_identifier(fake_registry):
    registered = collection.register_collection(
        "ucd-photos", "preservica", "preservica",
        [{"type": "preservica_ref", "value": "ref-1"}],
    )
    found = collection.get_registration("preservica_ref", "ref-1")
    assert found["id"] == registered["id"]
    assert found["name"] == "ucd-photos"


def test_get_registration_returns_none_when_not_found(fake_registry):
    assert collection.get_registration("preservica_ref", "no-such-ref") is None


def test_list_active_collections_filters_by_source_type(fake_registry):
    collection.register_collection("preservica-col", "group-a", "preservica", [
        {"type": "preservica_ref", "value": "ref-1"},
    ])
    collection.register_collection("manual-col", "group-b", "manual")

    all_active = collection.list_active_collections()
    assert {c["name"] for c in all_active} == {"preservica-col", "manual-col"}

    preservica_only = collection.list_active_collections("preservica")
    assert {c["name"] for c in preservica_only} == {"preservica-col"}


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
        self.metadata_fragments = {}  # url -> raw XML string
        self.representations = {}   # ref -> [...]
        self.files = {}             # downloadUrl -> bytes
        self.thumbnails = {}        # ref -> bytes|None
        self.raise_for = {}         # ref -> Exception, raised from get_entity
        self.get_entity_calls = []  # [(data_type, ref), ...] — for asserting no double-fetch
        self.download_calls = []    # [downloadUrl, ...] — for asserting skip-unchanged behavior
        self.updated_since_results = []  # [{'ref', 'type'}, ...] returned by get_entities_updated_since

    def get_entity(self, data_type, ref, opts={}):
        self.get_entity_calls.append((data_type, ref))
        if ref in self.raise_for:
            raise self.raise_for[ref]
        entity = self.entities[ref]
        key = "StructuralObject" if entity["type"] == "SO" else "InformationObject"
        detail = {"Title": entity["title"]}
        if entity.get("parent"):
            detail["Parent"] = entity["parent"]
        return {key: detail, "_fragment_refs": entity.get("fragment_refs", [])}

    def get_entities_updated_since(self, since, opts={}):
        return self.updated_since_results

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
        self.download_calls.append(download_url)
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
    client.metadata_fragments["http://x/frag1"] = (
        "<MetadataResponse><MetadataContainer schemaUri=\"http://oai_dc\">"
        "<Content><dc><title>Item One</title></dc></Content></MetadataContainer></MetadataResponse>"
    )
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
    assert (root / "by-ref" / "io-1" / "metadata" / "0.xml").read_text() == client.metadata_fragments["http://x/frag1"]
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


def test_preservica_import_defaults_collection_from_root_title(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Old Vines_New Lands", "type": "SO"}
    client.children["root-ref"] = []

    manifest = collection.preservica_import(
        "root-ref", group="preservica", opts={"client": client, "dest_root": str(tmp_path)},
    )

    assert manifest["collection"] == "old-vines-new-lands"
    root = tmp_path / "bronze" / "preservica" / "old-vines-new-lands" / "latest"
    assert (root / "by-ref" / "root-ref" / "entity.json").exists()
    # The root entity is only fetched once — reused for both slug-resolution and the walk.
    assert client.get_entity_calls.count(("structural-objects", "root-ref")) == 1


def test_preservica_import_defaults_collection_to_ref_when_title_missing(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": None, "type": "SO"}
    client.children["root-ref"] = []

    manifest = collection.preservica_import(
        "root-ref", group="preservica", opts={"client": client, "dest_root": str(tmp_path)},
    )

    assert manifest["collection"] == "root-ref"


def test_preservica_import_explicit_collection_skips_extra_fetch(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Old Vines_New Lands", "type": "SO"}
    client.children["root-ref"] = []

    manifest = collection.preservica_import(
        "root-ref", "explicit-name", "preservica",
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert manifest["collection"] == "explicit-name"
    assert client.get_entity_calls.count(("structural-objects", "root-ref")) == 1


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


def _client_with_one_downloadable_file():
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["io-1"] = {"title": "Item One", "type": "IO"}
    client.children["root-ref"] = [{"ref": "io-1", "title": "Item One", "type": "IO"}]
    client.representations["io-1"] = [{
        "representationType": "Preservation", "contentObjectRef": "co-1", "mimeGroup": "image",
        "filename": "page1.tif", "downloadUrl": "http://x/f1", "fileSize": 9,
        "fixity": {"algorithm": "SHA1", "value": "abc123"},
    }]
    client.files["http://x/f1"] = b"tiffbytes"
    return client


def test_preservica_import_redownloads_unchanged_file_by_default(tmp_path):
    client = _client_with_one_downloadable_file()
    opts = {"client": client, "dest_root": str(tmp_path)}

    collection.preservica_import("root-ref", "my-collection", "preservica", opts)
    collection.preservica_import("root-ref", "my-collection", "preservica", opts)

    assert client.download_calls == ["http://x/f1", "http://x/f1"]


def test_preservica_import_skip_unchanged_skips_matching_file(tmp_path):
    client = _client_with_one_downloadable_file()
    opts = {"client": client, "dest_root": str(tmp_path), "skip_unchanged": True}

    collection.preservica_import("root-ref", "my-collection", "preservica", opts)
    collection.preservica_import("root-ref", "my-collection", "preservica", opts)

    assert client.download_calls == ["http://x/f1"]


def test_preservica_import_skip_unchanged_redownloads_when_fixity_changes(tmp_path):
    client = _client_with_one_downloadable_file()
    opts = {"client": client, "dest_root": str(tmp_path), "skip_unchanged": True}
    collection.preservica_import("root-ref", "my-collection", "preservica", opts)

    client.representations["io-1"][0]["fixity"] = {"algorithm": "SHA1", "value": "different"}
    client.files["http://x/f1"] = b"newbytes"
    collection.preservica_import("root-ref", "my-collection", "preservica", opts)

    assert client.download_calls == ["http://x/f1", "http://x/f1"]
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    media_path = root / "by-ref" / "io-1" / "media" / "Preservation" / "page1.tif"
    assert media_path.read_bytes() == b"newbytes"


def test_preservica_import_skip_unchanged_always_downloads_without_fixity(tmp_path):
    client = _client_with_one_downloadable_file()
    client.representations["io-1"][0]["fixity"] = None
    opts = {"client": client, "dest_root": str(tmp_path), "skip_unchanged": True}

    collection.preservica_import("root-ref", "my-collection", "preservica", opts)
    collection.preservica_import("root-ref", "my-collection", "preservica", opts)

    assert client.download_calls == ["http://x/f1", "http://x/f1"]


def test_preservica_import_raises_without_client_or_credentials(tmp_path, monkeypatch):
    from digtk.lib import auth
    monkeypatch.setattr(auth, "resolve_preservica", lambda: {})
    with pytest.raises(ValueError, match="Preservica"):
        collection.preservica_import("root-ref", "my-collection", "preservica", {"dest_root": str(tmp_path)})


def test_preservica_import_registers_collection_even_when_unlabeled(tmp_path, fake_registry):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.children["root-ref"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )

    rows = list(fake_registry.rows.values())
    assert len(rows) == 1
    assert rows[0]["name"] == "my-collection"
    assert rows[0]["source_type"] == "preservica"
    assert rows[0]["workflow_tags"] == []


def test_preservica_import_registers_workflow_tags_from_argonath_identifier(tmp_path, fake_registry):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.identifiers["root-ref"] = [{"type": "argonath", "value": "ai,public"}]
    client.children["root-ref"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )

    rows = list(fake_registry.rows.values())
    assert rows[0]["workflow_tags"] == ["ai", "public", "sync"]
    stored = {(i["id_type"], i["id_value"]) for i in fake_registry.identifiers[rows[0]["id"]]}
    assert ("preservica_ref", "root-ref") in stored
    assert ("argonath", "ai,public") in stored


def test_preservica_import_raises_when_name_collides_with_a_different_root_ref(tmp_path, fake_registry):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["other-ref"] = {"title": "Root", "type": "SO"}
    client.children["root-ref"] = []
    client.children["other-ref"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )
    with pytest.raises(ValueError, match="already registered"):
        collection.preservica_import(
            "other-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
        )


def test_preservica_import_reimport_same_ref_does_not_raise(tmp_path, fake_registry):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.children["root-ref"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )
    collection.preservica_import(
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )

    assert len(fake_registry.rows) == 1


# --- find_changed_preservica_collections -------------------------------------

def test_find_changed_finds_registered_collection_by_direct_root_ref_match(fake_registry):
    collection.register_collection(
        "ucd-photos", "preservica", "preservica", [{"type": "preservica_ref", "value": "root-ref"}],
    )
    client = _FakePreservicaClient()
    client.updated_since_results = [{"ref": "root-ref", "type": "SO"}]

    result = collection.find_changed_preservica_collections("2026-09-01", {"client": client})

    assert result["collections"] == [{"group": "preservica", "name": "ucd-photos", "root_ref": "root-ref"}]


def test_find_changed_walks_up_parent_chain_to_owning_collection(fake_registry):
    collection.register_collection(
        "ucd-photos", "preservica", "preservica", [{"type": "preservica_ref", "value": "root-ref"}],
    )
    client = _FakePreservicaClient()
    client.entities["item-ref"] = {"title": "Item", "type": "IO", "parent": "sub-folder-ref"}
    client.entities["sub-folder-ref"] = {"title": "Sub", "type": "SO", "parent": "root-ref"}
    client.updated_since_results = [{"ref": "item-ref", "type": "IO"}]

    result = collection.find_changed_preservica_collections("2026-09-01", {"client": client})

    assert result["collections"] == [{"group": "preservica", "name": "ucd-photos", "root_ref": "root-ref"}]


def test_find_changed_auto_registers_newly_labeled_folder(fake_registry):
    client = _FakePreservicaClient()
    client.entities["new-root-ref"] = {"title": "New Collection", "type": "SO"}
    client.identifiers["new-root-ref"] = [{"type": "argonath", "value": "ai,public"}]
    client.updated_since_results = [{"ref": "new-root-ref", "type": "SO"}]

    result = collection.find_changed_preservica_collections("2026-09-01", {"client": client})

    assert result["collections"] == [
        {"group": "preservica", "name": "new-collection", "root_ref": "new-root-ref"},
    ]
    registered = collection.get_registration("preservica_ref", "new-root-ref")
    assert registered["workflow_tags"] == ["ai", "public", "sync"]


def test_find_changed_ignores_unregistered_and_unlabeled_folder(fake_registry):
    client = _FakePreservicaClient()
    client.entities["random-ref"] = {"title": "Random", "type": "SO"}
    client.identifiers["random-ref"] = []
    client.updated_since_results = [{"ref": "random-ref", "type": "SO"}]

    result = collection.find_changed_preservica_collections("2026-09-01", {"client": client})

    assert result["collections"] == []


def test_find_changed_dedupes_multiple_changes_in_same_collection(fake_registry):
    collection.register_collection(
        "ucd-photos", "preservica", "preservica", [{"type": "preservica_ref", "value": "root-ref"}],
    )
    client = _FakePreservicaClient()
    client.entities["item-1"] = {"title": "Item 1", "type": "IO", "parent": "root-ref"}
    client.entities["item-2"] = {"title": "Item 2", "type": "IO", "parent": "root-ref"}
    client.updated_since_results = [
        {"ref": "root-ref", "type": "SO"},
        {"ref": "item-1", "type": "IO"},
        {"ref": "item-2", "type": "IO"},
    ]

    result = collection.find_changed_preservica_collections("2026-09-01", {"client": client})

    assert result["collections"] == [{"group": "preservica", "name": "ucd-photos", "root_ref": "root-ref"}]


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


def test_preservica_tree_converts_metadata_and_skips_fixity_files(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["io-1"] = {"title": "Old Vines_New Lands", "type": "IO", "fragment_refs": [
        {"schemaUri": "http://oai_dc", "url": "http://x/frag1"},
    ]}
    client.metadata_fragments["http://x/frag1"] = (
        '<MetadataResponse><MetadataContainer schemaUri="http://oai_dc">'
        "<Content><dc><title>Old Vines</title></dc></Content></MetadataContainer></MetadataResponse>"
    )
    client.children["root-ref"] = [{"ref": "io-1", "title": "Old Vines_New Lands", "type": "IO"}]
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc123"}]
    client.representations["io-1"] = [{
        "representationType": "Preservation", "contentObjectRef": "co-1", "mimeGroup": "image",
        "filename": "page1.tif", "downloadUrl": "http://x/f1", "fileSize": 9,
        "fixity": {"algorithm": "SHA1", "value": "abc123"},
    }]
    client.files["http://x/f1"] = b"tiffbytes"

    collection.preservica_import(
        "root-ref", "my-collection", "preservica",
        {"client": client, "dest_root": str(tmp_path)},
    )
    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    item_dir = (
        tmp_path / "bronze" / "preservica" / "my-collection" / "latest" / "tree" / "old-vines-new-lands"
    )
    assert (item_dir / "Preservation" / "page1.tif").read_bytes() == b"tiffbytes"
    assert not (item_dir / "Preservation" / "page1.tif.fixity.json").exists()
    assert _read_json(str(item_dir / "metadata.json")) == {"dc": {"title": "Old Vines"}}


def test_preservica_tree_converts_metadata_for_folders_too(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["sub-folder"] = {"title": "Sub Folder", "type": "SO", "fragment_refs": [
        {"schemaUri": "http://oai_dc", "url": "http://x/frag1"},
    ]}
    client.metadata_fragments["http://x/frag1"] = (
        '<MetadataResponse><MetadataContainer schemaUri="http://oai_dc">'
        "<Content><dc><title>Sub Folder</title></dc></Content></MetadataContainer></MetadataResponse>"
    )
    client.children["root-ref"] = [{"ref": "sub-folder", "title": "Sub Folder", "type": "SO"}]
    client.children["sub-folder"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica",
        {"client": client, "dest_root": str(tmp_path)},
    )
    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    folder_dir = tmp_path / "bronze" / "preservica" / "my-collection" / "latest" / "tree" / "sub-folder"
    assert _read_json(str(folder_dir / "metadata.json")) == {"dc": {"title": "Sub Folder"}}


def test_preservica_tree_raises_on_multiple_metadata_fragments(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["io-1"] = {"title": "Item One", "type": "IO", "fragment_refs": [
        {"schemaUri": "http://oai_dc", "url": "http://x/frag1"},
        {"schemaUri": "http://oai_dc", "url": "http://x/frag2"},
    ]}
    fragment_xml = (
        "<MetadataResponse><MetadataContainer><Content><dc/></Content></MetadataContainer></MetadataResponse>"
    )
    client.metadata_fragments["http://x/frag1"] = fragment_xml
    client.metadata_fragments["http://x/frag2"] = fragment_xml
    client.children["root-ref"] = [{"ref": "io-1", "title": "Item One", "type": "IO"}]
    client.representations["io-1"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica",
        {"client": client, "dest_root": str(tmp_path)},
    )
    with pytest.raises(ValueError, match="metadata fragments"):
        collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})


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
