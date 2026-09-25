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
        if "WHERE name = %s AND group_name = %s" in sql:
            name, group = params
            return [
                {"id": r["id"], "name": r["name"], "group_name": r["group_name"],
                 "source_type": r["source_type"], "workflow_tags": r["workflow_tags"]}
                for r in self.rows.values()
                if r["name"] == name and r["group_name"] == group and r["status"] == "active"
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
        if sql.startswith("UPDATE argonath.collection_registry SET last_synced_at = now() WHERE id"):
            (collection_id,) = params
            self.rows[collection_id]["last_synced_at_touched"] = True
            return None
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


# --- list_item_arks -----------------------------------------------------------

def test_list_item_arks_reads_ark_from_each_matching_item_jsonld(monkeypatch):
    monkeypatch.setattr(collection.fs, "find_by_partition_keys", lambda keys, opts={}: [
        "/bronze/preservica/my-collection/latest/tree/box-1/item-1/item.jsonld.json",
        "/bronze/preservica/my-collection/latest/tree/box-1/item-2/item.jsonld.json",
    ])
    docs = {
        "cask:/bronze/preservica/my-collection/latest/tree/box-1/item-1/item.jsonld.json":
            json.dumps({"ark": "ark:/1/one"}),
        "cask:/bronze/preservica/my-collection/latest/tree/box-1/item-2/item.jsonld.json":
            json.dumps({"ark": "ark:/1/two"}),
    }
    monkeypatch.setattr(collection.fs, "read", lambda path: docs[path])

    assert collection.list_item_arks("my-collection") == ["ark:/1/one", "ark:/1/two"]


def test_list_item_arks_falls_back_to_at_id_when_ark_field_missing(monkeypatch):
    monkeypatch.setattr(collection.fs, "find_by_partition_keys", lambda keys, opts={}: [
        "/bronze/preservica/my-collection/latest/tree/item-1/item.jsonld.json",
    ])
    monkeypatch.setattr(collection.fs, "read", lambda path: json.dumps({"@id": "ark:/1/one"}))

    assert collection.list_item_arks("my-collection") == ["ark:/1/one"]


def test_list_item_arks_ignores_non_item_jsonld_matches(monkeypatch):
    # metadata.json/identifiers.json share the same partition keys as
    # item.jsonld.json (see _tree_partition_keys()) -- only the latter is an
    # item roster entry.
    monkeypatch.setattr(collection.fs, "find_by_partition_keys", lambda keys, opts={}: [
        "/bronze/preservica/my-collection/latest/tree/box-1/metadata.json",
        "/bronze/preservica/my-collection/latest/tree/box-1/item-1/item.jsonld.json",
    ])
    monkeypatch.setattr(collection.fs, "read", lambda path: json.dumps({"ark": "ark:/1/one"}))

    assert collection.list_item_arks("my-collection") == ["ark:/1/one"]


def test_list_item_arks_empty_when_no_matches(monkeypatch):
    monkeypatch.setattr(collection.fs, "find_by_partition_keys", lambda keys, opts={}: [])
    assert collection.list_item_arks("my-collection") == []


def test_list_item_arks_queries_medallion_bronze_and_collection_name(monkeypatch):
    seen = {}

    def _find(keys, opts={}):
        seen["keys"] = keys
        return []

    monkeypatch.setattr(collection.fs, "find_by_partition_keys", _find)
    collection.list_item_arks("my-collection")
    assert seen["keys"] == ["medallion-bronze", "collection-my-collection"]


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
        self.get_children_calls = []  # [ref, ...] — for asserting no cascade into new subfolders

    def get_entity(self, data_type, ref, opts={}):
        self.get_entity_calls.append((data_type, ref))
        if ref in self.raise_for:
            raise self.raise_for[ref]
        entity = self.entities[ref]
        key = "StructuralObject" if entity["type"] == "SO" else "InformationObject"
        detail = {"Title": entity["title"]}
        if entity.get("description") is not None:
            detail["Description"] = entity["description"]
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
        self.get_children_calls.append(ref)
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
        "ref": "root-ref", "title": "My Collection", "description": None, "type": "SO", "metadataFragmentRefs": [],
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

    assert result["collections"] == [{
        "group": "preservica", "name": "ucd-photos", "root_ref": "root-ref",
        "changed": [{"ref": "root-ref", "type": "SO"}],
    }]


def test_find_changed_walks_up_parent_chain_to_owning_collection(fake_registry):
    collection.register_collection(
        "ucd-photos", "preservica", "preservica", [{"type": "preservica_ref", "value": "root-ref"}],
    )
    client = _FakePreservicaClient()
    client.entities["item-ref"] = {"title": "Item", "type": "IO", "parent": "sub-folder-ref"}
    client.entities["sub-folder-ref"] = {"title": "Sub", "type": "SO", "parent": "root-ref"}
    client.updated_since_results = [{"ref": "item-ref", "type": "IO"}]

    result = collection.find_changed_preservica_collections("2026-09-01", {"client": client})

    assert result["collections"] == [{
        "group": "preservica", "name": "ucd-photos", "root_ref": "root-ref",
        "changed": [{"ref": "item-ref", "type": "IO"}],
    }]


def test_find_changed_auto_registers_newly_labeled_folder(fake_registry):
    client = _FakePreservicaClient()
    client.entities["new-root-ref"] = {"title": "New Collection", "type": "SO"}
    client.identifiers["new-root-ref"] = [{"type": "argonath", "value": "ai,public"}]
    client.updated_since_results = [{"ref": "new-root-ref", "type": "SO"}]

    result = collection.find_changed_preservica_collections("2026-09-01", {"client": client})

    assert result["collections"] == [{
        "group": "preservica", "name": "new-collection", "root_ref": "new-root-ref",
        "changed": [{"ref": "new-root-ref", "type": "SO"}],
    }]
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

    assert len(result["collections"]) == 1
    assert result["collections"][0]["changed"] == [
        {"ref": "root-ref", "type": "SO"},
        {"ref": "item-1", "type": "IO"},
        {"ref": "item-2", "type": "IO"},
    ]


def test_find_changed_dedupes_repeated_entity_in_same_batch(fake_registry):
    collection.register_collection(
        "ucd-photos", "preservica", "preservica", [{"type": "preservica_ref", "value": "root-ref"}],
    )
    client = _FakePreservicaClient()
    client.updated_since_results = [
        {"ref": "root-ref", "type": "SO"},
        {"ref": "root-ref", "type": "SO"},
    ]

    result = collection.find_changed_preservica_collections("2026-09-01", {"client": client})

    assert result["collections"][0]["changed"] == [{"ref": "root-ref", "type": "SO"}]


# --- preservica_sync_entities -------------------------------------------------

def test_sync_entities_new_item_is_added(tmp_path, fake_registry):
    collection.register_collection("my-collection", "preservica", "preservica")
    client = _FakePreservicaClient()
    client.entities["io-1"] = {"title": "Item One", "type": "IO"}
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc123"}]
    client.representations["io-1"] = []

    result = collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "io-1", "type": "IO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert result["added"] == ["d7abc123"]
    assert result["changed"] == []
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    assert (root / "by-ref" / "io-1" / "entity.json").exists()


def test_sync_entities_existing_item_is_changed(tmp_path, fake_registry):
    collection.register_collection("my-collection", "preservica", "preservica")
    client = _FakePreservicaClient()
    client.entities["io-1"] = {"title": "Item One", "type": "IO"}
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc123"}]
    client.representations["io-1"] = []
    opts = {"client": client, "dest_root": str(tmp_path)}

    collection.preservica_sync_entities("my-collection", "preservica", [{"ref": "io-1", "type": "IO"}], opts)
    result = collection.preservica_sync_entities("my-collection", "preservica", [{"ref": "io-1", "type": "IO"}], opts)

    assert result["added"] == []
    assert result["changed"] == ["d7abc123"]


def test_sync_entities_picks_up_description_edit(tmp_path, fake_registry):
    collection.register_collection("my-collection", "preservica", "preservica")
    client = _FakePreservicaClient()
    client.entities["io-1"] = {"title": "Item One", "type": "IO", "description": "Old description"}
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc123"}]
    client.representations["io-1"] = []
    opts = {"client": client, "dest_root": str(tmp_path)}

    collection.preservica_sync_entities("my-collection", "preservica", [{"ref": "io-1", "type": "IO"}], opts)
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    assert _read_json(str(root / "by-ref" / "io-1" / "entity.json"))["description"] == "Old description"

    client.entities["io-1"]["description"] = "testing! 2"
    collection.preservica_sync_entities("my-collection", "preservica", [{"ref": "io-1", "type": "IO"}], opts)

    assert _read_json(str(root / "by-ref" / "io-1" / "entity.json"))["description"] == "testing! 2"


def _seed_folder_with_one_child(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["io-1"] = {"title": "Item One", "type": "IO"}
    client.children["root-ref"] = [{"ref": "io-1", "title": "Item One", "type": "IO"}]
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc123"}]
    client.representations["io-1"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )
    client.get_entity_calls = []
    client.get_children_calls = []
    return client


def test_sync_entities_folder_refresh_does_not_touch_unrelated_children(tmp_path, fake_registry):
    client = _seed_folder_with_one_child(tmp_path)

    result = collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "root-ref", "type": "SO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert ("information-objects", "io-1") not in client.get_entity_calls
    assert ("structural-objects", "root-ref") in client.get_entity_calls
    assert result["added"] == []
    assert result["changed"] == []


def test_sync_entities_new_child_io_discovered_via_children_diff(tmp_path, fake_registry):
    client = _seed_folder_with_one_child(tmp_path)
    client.entities["io-2"] = {"title": "Item Two", "type": "IO"}
    client.identifiers["io-2"] = [{"type": None, "value": "ark:/87287/d7xyz999"}]
    client.representations["io-2"] = []
    client.children["root-ref"] = [
        {"ref": "io-1", "title": "Item One", "type": "IO"},
        {"ref": "io-2", "title": "Item Two", "type": "IO"},
    ]

    result = collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "root-ref", "type": "SO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert result["added"] == ["d7xyz999"]
    assert ("information-objects", "io-1") not in client.get_entity_calls


def test_sync_entities_new_child_folder_is_shallow_no_cascade(tmp_path, fake_registry):
    client = _seed_folder_with_one_child(tmp_path)
    client.entities["sub-new"] = {"title": "New Sub", "type": "SO"}
    client.children["root-ref"] = [
        {"ref": "io-1", "title": "Item One", "type": "IO"},
        {"ref": "sub-new", "title": "New Sub", "type": "SO"},
    ]

    result = collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "root-ref", "type": "SO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert result["added"] == []
    assert result["changed"] == []
    assert "sub-new" not in client.get_children_calls
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    assert (root / "by-ref" / "sub-new" / "entity.json").exists()


def test_sync_entities_removed_child_item_is_pruned_but_bronze_kept(tmp_path, fake_registry):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["io-1"] = {"title": "Item One", "type": "IO"}
    client.entities["io-2"] = {"title": "Item Two", "type": "IO"}
    client.children["root-ref"] = [
        {"ref": "io-1", "title": "Item One", "type": "IO"},
        {"ref": "io-2", "title": "Item Two", "type": "IO"},
    ]
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc123"}]
    client.identifiers["io-2"] = [{"type": None, "value": "ark:/87287/d7xyz999"}]
    client.representations["io-1"] = []
    client.representations["io-2"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )
    client.children["root-ref"] = [{"ref": "io-1", "title": "Item One", "type": "IO"}]

    result = collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "root-ref", "type": "SO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert result["removed"] == ["d7xyz999"]
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    assert (root / "by-ref" / "io-2" / "entity.json").exists()


def test_sync_entities_removed_child_folder_is_not_reported(tmp_path, fake_registry):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["sub-1"] = {"title": "Sub One", "type": "SO"}
    client.children["root-ref"] = [{"ref": "sub-1", "title": "Sub One", "type": "SO"}]
    client.children["sub-1"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )
    client.children["root-ref"] = []

    result = collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "root-ref", "type": "SO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert result["removed"] == []
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    assert (root / "by-ref" / "sub-1" / "entity.json").exists()


def test_sync_entities_warns_when_no_ark(tmp_path, fake_registry):
    collection.register_collection("my-collection", "preservica", "preservica")
    client = _FakePreservicaClient()
    client.entities["io-1"] = {"title": "Item One", "type": "IO"}
    client.identifiers["io-1"] = [{"type": "LocalID", "value": "loc-1"}]
    client.representations["io-1"] = []

    result = collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "io-1", "type": "IO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert result["added"] == ["loc-1"]
    assert result["warnings"] == [
        {"ref": "io-1", "stage": "identifiers", "message": "no ARK-shaped identifier found"},
    ]


def test_sync_entities_records_error_without_aborting(tmp_path, fake_registry):
    collection.register_collection("my-collection", "preservica", "preservica")
    client = _FakePreservicaClient()
    client.entities["io-2"] = {"title": "Item Two", "type": "IO"}
    client.raise_for["io-1"] = RuntimeError("boom")
    client.identifiers["io-2"] = [{"type": None, "value": "ark:/87287/d7xyz"}]
    client.representations["io-2"] = []

    result = collection.preservica_sync_entities(
        "my-collection", "preservica",
        [{"ref": "io-1", "type": "IO"}, {"ref": "io-2", "type": "IO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert result["errors"] == [{"ref": "io-1", "stage": "entity", "message": "boom"}]
    assert result["added"] == ["d7xyz"]


def test_sync_entities_warns_on_unsupported_entity_type(tmp_path, fake_registry):
    collection.register_collection("my-collection", "preservica", "preservica")
    client = _FakePreservicaClient()

    result = collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "co-1", "type": "CO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert result["added"] == result["removed"] == result["changed"] == []
    assert result["warnings"] == [
        {"ref": "co-1", "stage": "sync", "message": "unsupported entity type 'CO' — skipped"},
    ]


def test_sync_entities_raises_for_unregistered_collection(tmp_path, fake_registry):
    client = _FakePreservicaClient()
    with pytest.raises(ValueError, match="No active registered collection"):
        collection.preservica_sync_entities(
            "nope", "preservica", [{"ref": "io-1", "type": "IO"}],
            {"client": client, "dest_root": str(tmp_path)},
        )


def test_sync_entities_dedupes_repeated_input_entities(tmp_path, fake_registry):
    collection.register_collection("my-collection", "preservica", "preservica")
    client = _FakePreservicaClient()
    client.entities["io-1"] = {"title": "Item One", "type": "IO"}
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc"}]
    client.representations["io-1"] = []

    collection.preservica_sync_entities(
        "my-collection", "preservica",
        [{"ref": "io-1", "type": "IO"}, {"ref": "io-1", "type": "IO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert client.get_entity_calls.count(("information-objects", "io-1")) == 1


def test_sync_entities_touches_last_synced(tmp_path, fake_registry):
    registered = collection.register_collection("my-collection", "preservica", "preservica")
    client = _FakePreservicaClient()
    client.entities["io-1"] = {"title": "Item One", "type": "IO"}
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc"}]
    client.representations["io-1"] = []

    collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "io-1", "type": "IO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    assert fake_registry.rows[registered["id"]].get("last_synced_at_touched") is True


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

    count, item_metadata = collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    assert count == 1
    assert item_metadata == []
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


def test_preservica_tree_rebuild_uses_live_title_after_targeted_rename(tmp_path, fake_registry):
    # A targeted sync of just the renamed IO (mirroring Preservica's real
    # "updated since" feed, which reports the IO's own ref, not its parent
    # folder's) never touches root-ref's cached children.json — so the tree
    # rebuild must read the new title from io-1's own entity.json, not from
    # that stale cache, to pick the correct new slug.
    client = _seed_folder_with_one_child(tmp_path)
    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    assert (root / "tree" / "item-one").exists()

    client.entities["io-1"]["title"] = "Item One Renamed"
    collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "io-1", "type": "IO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    assert not (root / "tree" / "item-one").exists()
    assert (root / "tree" / "item-one-renamed").exists()


def test_copy_metadata_to_tree_tags_cask_writes_with_ref_partition_key(monkeypatch):
    # Regression test for a real bug: CaskFS only treats a write as JSON-LD
    # given an explicit 'application/ld+json' Content-Type (or 'jsonld=true'),
    # and otherwise routes a guessed-from-extension 'application/json' body
    # straight into its Turtle/N-Triples parser, which fails outright on a
    # JSON body. Tagging metadata.json (a plain file CaskFS already writes)
    # with a partition key sidesteps that entirely — no RDF/JSON-LD content
    # negotiation involved at all — so this checks the write opts directly.
    reads = {"by-ref/io-1/entity.json": {"ref": "io-1", "title": "Item One", "type": "IO"}}
    writes = []

    monkeypatch.setattr(collection, "_read_bronze_json", lambda root, rel_path, default=None: reads.get(rel_path, default))
    monkeypatch.setattr(fs, "exists", lambda path: False)
    monkeypatch.setattr(fs, "write", lambda path, data=None, src=None, opts={}: writes.append((path, opts)))
    monkeypatch.setattr(fs, "add_derivative_link", lambda path, source_path, opts={}: None)

    collection._copy_metadata_to_tree(
        "cask:/bronze/preservica/my-collection/latest", "by-ref/io-1", "tree/item-one", "io-1", "my-collection",
    )

    assert len(writes) == 1
    path, opts = writes[0]
    assert path == "cask:/bronze/preservica/my-collection/latest/tree/item-one/metadata.json"
    assert opts["partition_keys"] == ["medallion-bronze", "collection-my-collection", "ref-io-1"]


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
    assert _read_json(str(item_dir / "metadata.json")) == {
        "dc": {"title": "Old Vines"},
        "preservica": {"ref": "io-1", "title": "Old Vines_New Lands", "description": None, "type": "IO"},
    }
    # An IO with both an ARK identifier and a descriptive metadata fragment
    # also gets a DC-mapped schema.org sidecar, alongside metadata.json.
    doc = _read_json(str(item_dir / "item.jsonld.json"))
    assert doc["@id"] == "ark:/87287/d7abc123"
    assert doc["ark"] == "ark:/87287/d7abc123"
    assert doc["name"] == "Old Vines"
    assert doc["argonath:fieldSources"] == {"name": "human"}


def test_preservica_tree_returns_item_metadata_for_dc_mapped_items(tmp_path):
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
    client.representations["io-1"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )
    count, item_metadata = collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    assert count == 1
    assert len(item_metadata) == 1
    assert item_metadata[0].ark == "ark:/87287/d7abc123"
    assert item_metadata[0].name == "Old Vines"


def test_preservica_tree_skips_item_jsonld_when_no_ark(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["io-1"] = {"title": "Item One", "type": "IO", "fragment_refs": [
        {"schemaUri": "http://oai_dc", "url": "http://x/frag1"},
    ]}
    client.metadata_fragments["http://x/frag1"] = (
        '<MetadataResponse><MetadataContainer schemaUri="http://oai_dc">'
        "<Content><dc><title>Item One</title></dc></Content></MetadataContainer></MetadataResponse>"
    )
    client.children["root-ref"] = [{"ref": "io-1", "title": "Item One", "type": "IO"}]
    client.identifiers["io-1"] = []  # no ARK minted yet
    client.representations["io-1"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )
    count, item_metadata = collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    assert count == 1
    assert item_metadata == []
    item_dir = tmp_path / "bronze" / "preservica" / "my-collection" / "latest" / "tree" / "item-one"
    assert not (item_dir / "item.jsonld.json").exists()


def test_preservica_tree_skips_item_jsonld_when_no_metadata_fragment(tmp_path):
    # Reuses the no-fragment-at-all shape already covered by
    # test_preservica_tree_writes_preservica_metadata_without_dc_fragment.
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["io-1"] = {"title": "Item One", "type": "IO", "description": "An item"}
    client.children["root-ref"] = [{"ref": "io-1", "title": "Item One", "type": "IO"}]
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc123"}]
    client.representations["io-1"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )
    count, item_metadata = collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    assert item_metadata == []
    item_dir = tmp_path / "bronze" / "preservica" / "my-collection" / "latest" / "tree" / "item-one"
    assert not (item_dir / "item.jsonld.json").exists()


def test_preservica_tree_never_writes_item_jsonld_for_a_folder(tmp_path):
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
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )
    count, item_metadata = collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    assert item_metadata == []
    folder_dir = tmp_path / "bronze" / "preservica" / "my-collection" / "latest" / "tree" / "sub-folder"
    assert not (folder_dir / "item.jsonld.json").exists()


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
    assert _read_json(str(folder_dir / "metadata.json")) == {
        "dc": {"title": "Sub Folder"},
        "preservica": {"ref": "sub-folder", "title": "Sub Folder", "description": None, "type": "SO"},
    }


def test_preservica_tree_writes_preservica_metadata_without_dc_fragment(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["io-1"] = {"title": "Item One", "type": "IO", "description": "An item"}
    client.children["root-ref"] = [{"ref": "io-1", "title": "Item One", "type": "IO"}]
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc123"}]
    client.representations["io-1"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica",
        {"client": client, "dest_root": str(tmp_path)},
    )
    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    item_dir = tmp_path / "bronze" / "preservica" / "my-collection" / "latest" / "tree" / "item-one"
    assert _read_json(str(item_dir / "metadata.json")) == {
        "preservica": {"ref": "io-1", "title": "Item One", "description": "An item", "type": "IO"},
    }


def test_preservica_tree_writes_root_metadata_json(tmp_path):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "My Collection", "type": "SO", "fragment_refs": [
        {"schemaUri": "http://oai_dc", "url": "http://x/frag-root"},
    ]}
    client.metadata_fragments["http://x/frag-root"] = (
        '<MetadataResponse><MetadataContainer schemaUri="http://oai_dc">'
        "<Content><dc><title>My Collection</title></dc></Content></MetadataContainer></MetadataResponse>"
    )
    client.children["root-ref"] = []

    collection.preservica_import(
        "root-ref", "my-collection", "preservica",
        {"client": client, "dest_root": str(tmp_path)},
    )
    count, item_metadata = collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    assert count == 0
    assert item_metadata == []
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    assert _read_json(str(root / "tree" / "metadata.json")) == {
        "dc": {"title": "My Collection"},
        "preservica": {"ref": "root-ref", "title": "My Collection", "description": None, "type": "SO"},
    }


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


# --- preservica_tree_partial ---------------------------------------------

@pytest.fixture
def local_find_by_partition_keys(monkeypatch, tmp_path):
    """Fake fs.find_by_partition_keys() that scans the local dest_root for
    metadata.json files whose embedded 'preservica.ref' matches the
    'ref-<ref>' partition key — lets preservica_tree_partial() (which depends
    on this CaskFS-only lookup) be tested against a local dest_root instead
    of a real CaskFS connection. Partition keys themselves aren't persisted
    for a local dest_root (see _copy_metadata_to_tree()'s cask:-only guard),
    so this reads the ref back out of the file's own JSON content instead —
    the same ref value that would have been tagged as the partition key."""
    def _find(partition_keys, opts={}):
        ref = next(k[len("ref-"):] for k in partition_keys if k.startswith("ref-"))
        matches = []
        for path in tmp_path.rglob("metadata.json"):
            try:
                content = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if content.get("preservica", {}).get("ref") == ref:
                matches.append(str(path))
        return matches

    monkeypatch.setattr(fs, "find_by_partition_keys", _find)
    return _find


def test_tree_partial_renames_io_moves_directory_without_recopying(tmp_path, fake_registry, local_find_by_partition_keys):
    client = _seed_folder_with_one_child(tmp_path)
    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    old_dir = root / "tree" / "item-one"
    assert old_dir.exists()
    media_inode_before = os.stat(old_dir / "identifiers.json").st_ino

    client.entities["io-1"]["title"] = "Item One Renamed"
    collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "io-1", "type": "IO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    result = collection.preservica_tree_partial(
        "my-collection", "preservica", [{"ref": "io-1", "type": "IO"}], {"dest_root": str(tmp_path)},
    )

    new_dir = root / "tree" / "item-one-renamed"
    assert result == {"moved": ["io-1"], "added": [], "removed": [], "unresolved": [], "item_metadata": []}
    assert not old_dir.exists()
    assert new_dir.exists()
    # A real move, not a delete+rewrite — same inode survives the rename.
    assert os.stat(new_dir / "identifiers.json").st_ino == media_inode_before


def test_tree_partial_rename_is_noop_when_slug_unchanged(tmp_path, fake_registry, local_find_by_partition_keys):
    client = _seed_folder_with_one_child(tmp_path)
    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    client.entities["io-1"]["description"] = "just a description edit, same title"
    collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "io-1", "type": "IO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    result = collection.preservica_tree_partial(
        "my-collection", "preservica", [{"ref": "io-1", "type": "IO"}], {"dest_root": str(tmp_path)},
    )
    assert result == {"moved": [], "added": [], "removed": [], "unresolved": [], "item_metadata": []}
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    assert (root / "tree" / "item-one").exists()


def test_tree_partial_renames_folder_moves_whole_subtree(tmp_path, fake_registry, local_find_by_partition_keys):
    client = _FakePreservicaClient()
    client.entities["root-ref"] = {"title": "Root", "type": "SO"}
    client.entities["sub-folder"] = {"title": "Sub Folder", "type": "SO"}
    client.entities["io-1"] = {"title": "Item One", "type": "IO"}
    client.children["root-ref"] = [{"ref": "sub-folder", "title": "Sub Folder", "type": "SO"}]
    client.children["sub-folder"] = [{"ref": "io-1", "title": "Item One", "type": "IO"}]
    client.identifiers["io-1"] = [{"type": None, "value": "ark:/87287/d7abc123"}]
    client.representations["io-1"] = [{
        "representationType": "Preservation", "contentObjectRef": "co-1", "mimeGroup": "image",
        "filename": "page1.tif", "downloadUrl": "http://x/f1", "fileSize": 9, "fixity": None,
    }]
    client.files["http://x/f1"] = b"tiffbytes"

    collection.preservica_import(
        "root-ref", "my-collection", "preservica", {"client": client, "dest_root": str(tmp_path)},
    )
    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    assert (root / "tree" / "sub-folder" / "item-one" / "Preservation" / "page1.tif").exists()

    client.entities["sub-folder"]["title"] = "Sub Folder Renamed"
    collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "sub-folder", "type": "SO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    result = collection.preservica_tree_partial(
        "my-collection", "preservica", [{"ref": "sub-folder", "type": "SO"}], {"dest_root": str(tmp_path)},
    )

    assert result == {"moved": ["sub-folder"], "added": [], "removed": [], "unresolved": [], "item_metadata": []}
    assert not (root / "tree" / "sub-folder").exists()
    assert (
        root / "tree" / "sub-folder-renamed" / "item-one" / "Preservation" / "page1.tif"
    ).read_bytes() == b"tiffbytes"


def test_tree_partial_adds_new_item_via_parent_fallback_scan(tmp_path, fake_registry, local_find_by_partition_keys):
    client = _seed_folder_with_one_child(tmp_path)
    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})

    client.entities["io-2"] = {"title": "Item Two", "type": "IO"}
    client.identifiers["io-2"] = [{"type": None, "value": "ark:/87287/d7xyz999"}]
    client.representations["io-2"] = []
    client.children["root-ref"] = [
        {"ref": "io-1", "title": "Item One", "type": "IO"},
        {"ref": "io-2", "title": "Item Two", "type": "IO"},
    ]
    collection.preservica_sync_entities(
        "my-collection", "preservica", [{"ref": "root-ref", "type": "SO"}],
        {"client": client, "dest_root": str(tmp_path)},
    )

    # root-ref (io-2's parent) is deliberately NOT in this batch, forcing
    # _find_parent_so_ref()'s by-ref/ fallback scan rather than the cheap
    # in-batch check.
    result = collection.preservica_tree_partial(
        "my-collection", "preservica", [{"ref": "io-2", "type": "IO"}], {"dest_root": str(tmp_path)},
    )

    assert result == {"moved": [], "added": ["io-2"], "removed": [], "unresolved": [], "item_metadata": []}
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    assert (root / "tree" / "item-two" / "identifiers.json").exists()
    assert (root / "tree" / "item-one").exists()


def test_tree_partial_removed_entity_deletes_tree_entry(tmp_path, fake_registry, local_find_by_partition_keys):
    _seed_folder_with_one_child(tmp_path)
    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"
    assert (root / "tree" / "item-one").exists()

    # Simulate the ref no longer existing in by-ref/ (preservica_sync_entities
    # itself never deletes bronze — this exercises the safety-net path).
    fs.delete(str(root / "by-ref" / "io-1" / "entity.json"))

    result = collection.preservica_tree_partial(
        "my-collection", "preservica", [{"ref": "io-1", "type": "IO"}], {"dest_root": str(tmp_path)},
    )

    assert result == {"moved": [], "added": [], "removed": ["io-1"], "unresolved": [], "item_metadata": []}
    assert not (root / "tree" / "item-one").exists()


def test_tree_partial_unresolved_when_parent_cannot_be_found(tmp_path, fake_registry, local_find_by_partition_keys):
    _seed_folder_with_one_child(tmp_path)
    collection.preservica_tree("my-collection", "preservica", {"dest_root": str(tmp_path)})
    root = tmp_path / "bronze" / "preservica" / "my-collection" / "latest"

    # A by-ref/ entry with no parent referencing it anywhere — shouldn't
    # normally happen, but must be reported rather than guessed at.
    collection._write_bronze_json(str(root), "by-ref/orphan-1/entity.json", {"ref": "orphan-1", "title": "Orphan", "type": "IO"})
    collection._write_bronze_json(str(root), "by-ref/orphan-1/identifiers.json", [])

    result = collection.preservica_tree_partial(
        "my-collection", "preservica", [{"ref": "orphan-1", "type": "IO"}], {"dest_root": str(tmp_path)},
    )

    assert result == {"moved": [], "added": [], "removed": [], "unresolved": ["orphan-1"], "item_metadata": []}
    assert not (root / "tree" / "orphan").exists()
    assert not any((root / "tree").glob("orphan*"))


def test_tree_partial_raises_without_prior_import(tmp_path, fake_registry):
    with pytest.raises(FileNotFoundError):
        collection.preservica_tree_partial(
            "no-such-collection", "preservica", [{"ref": "io-1", "type": "IO"}], {"dest_root": str(tmp_path)},
        )
