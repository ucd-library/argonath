import json

import pytest

from digtk.lib.items import ItemLocation
from digtk.models.digital_website import item_record

ARK = "ark:/87287/d7abc1234"
COLLECTION_ARK = "ark:/87287/d7collection"
ITEM_URI = item_record.ITEM_URI_PREFIX + ARK
COLLECTION_URI = item_record.COLLECTION_URI_PREFIX + COLLECTION_ARK

NO_MEDIA_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[],
    sidecar_path="cask:/bronze/g/c/latest/item/item.jsonld.json",
)

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
    bronze_binary_paths=[],
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

IMAGE_PDF_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/photo.tiff",
        "cask:/bronze/g/c/latest/item/document.pdf",
    ],
    sidecar_path=None,
)

MULTI_IMAGE_PDF_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/page_1.tiff",
        "cask:/bronze/g/c/latest/item/page_2.tiff",
        "cask:/bronze/g/c/latest/item/document.pdf",
    ],
    sidecar_path=None,
)

PDF_ONLY_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/document.pdf"],
    sidecar_path=None,
)

TREE_ITEM = ItemLocation(
    ark=ARK, group="preservica", collection="haeger", time="latest", item_path="tree/quarter-mile",
    bronze_binary_paths=["cask:/bronze/preservica/haeger/latest/tree/quarter-mile/Preservation/photo.tiff"],
)
TREE_FOLDER = "cask:/bronze/preservica/haeger/latest/tree/quarter-mile"

AV_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/interview.mp4"],
    sidecar_path=None,
)


def _node(doc, node_id):
    return next(n for n in doc["@graph"] if n["@id"] == node_id)


@pytest.fixture(autouse=True)
def fake_collection_ark(monkeypatch):
    monkeypatch.setattr(item_record.collection, "get_ark", lambda collection, group, opts={}: COLLECTION_ARK)


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


def _fs(monkeypatch, exists_paths=(), reads=None):
    """Patch item_record.fs.exists/read: `exists_paths` is any iterable or
    predicate of paths that exist; `reads` maps a path to its (unparsed) JSON
    content, defaulting to {} for any existing path with no entry."""
    reads = reads or {}

    def fake_exists(path):
        return path in exists_paths

    def fake_read(path):
        return json.dumps(reads.get(path, {}))

    monkeypatch.setattr(item_record.fs, "exists", fake_exists)
    monkeypatch.setattr(item_record.fs, "read", fake_read)


def test_build_raises_for_ark_less_item(monkeypatch, fake_derivative):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_ARK_ITEM)
    with pytest.raises(LookupError, match="no ark"):
        item_record.build(NO_ARK_ITEM)


def test_build_raises_when_collection_has_no_ark(monkeypatch, fake_derivative):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_MEDIA_ITEM)
    monkeypatch.setattr(
        item_record.collection, "get_ark",
        lambda collection, group, opts={}: (_ for _ in ()).throw(LookupError("MISSING ARK")),
    )
    _fs(monkeypatch, exists_paths=(NO_MEDIA_ITEM.sidecar_path,))
    with pytest.raises(LookupError, match="cannot build isPartOf"):
        item_record.build(NO_MEDIA_ITEM)


def test_build_adopts_bronze_sidecar_descriptive_fields(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_MEDIA_ITEM)
    bronze_doc = {
        "@context": {"@vocab": "https://schema.org/"}, "@id": ARK, "ark": ARK,
        "name": "Quarter Mile", "description": "A scan",
        "identifier": [{"@type": "PropertyValue", "propertyID": "ARK", "value": ARK}],
    }
    _fs(monkeypatch, exists_paths=(NO_MEDIA_ITEM.sidecar_path,), reads={NO_MEDIA_ITEM.sidecar_path: bronze_doc})

    doc = item_record.build(NO_MEDIA_ITEM)

    assert doc["@id"] == ARK
    assert doc["@context"] == item_record.RECORD_CONTEXT
    assert doc["@context"]["@vocab"] == "https://schema.org/"
    root = _node(doc, ITEM_URI)
    assert root["name"] == "Quarter Mile"
    assert root["description"] == "A scan"
    assert root["ark"] == ARK
    assert root["isPartOf"] == {"@id": COLLECTION_URI}
    # Bronze's own ARK entry is kept as-is, but build()'s own computed
    # identifiers (here just the gold path) are still unioned in rather than
    # hidden by the usual never-override-bronze rule (see _merge_identifiers()).
    assert root["identifier"][0] == bronze_doc["identifier"][0]
    assert {
        "@type": "PropertyValue", "propertyID": "CaskGoldPath",
        "value": "cask://gold/digital-dev/c/item/item.jsonld.json",
    } in root["identifier"]
    assert root["associatedMedia"] == []
    assert "@context" not in root
    assert len(doc["@graph"]) == 1
    assert fake_derivative[0]["type"] == "gold"
    assert fake_derivative[0]["rel_path"] == "item/item.jsonld.json"
    assert fake_derivative[0]["opts"]["app"] == "digital-dev"


def test_build_promotes_to_gold_under_custom_project(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_MEDIA_ITEM)
    _fs(monkeypatch, exists_paths=(NO_MEDIA_ITEM.sidecar_path,), reads={
        NO_MEDIA_ITEM.sidecar_path: {"@id": ARK, "ark": ARK, "name": "Photo"},
    })

    item_record.build(NO_MEDIA_ITEM, opts={"project": "digital-prod"})

    assert [c["rel_path"] for c in fake_derivative] == ["item/item.jsonld.json", "item/manifest.json"]
    for call in fake_derivative:
        assert call["type"] == "gold"
        assert call["opts"]["app"] == "digital-prod"
        assert "group" not in call["opts"]
        assert "time" not in call["opts"]


def test_build_drops_field_sources_bookkeeping(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_MEDIA_ITEM)
    bronze_doc = {"@id": ARK, "ark": ARK, "name": "Photo", "argonath:fieldSources": {"name": "human"}}
    _fs(monkeypatch, exists_paths=(NO_MEDIA_ITEM.sidecar_path,), reads={NO_MEDIA_ITEM.sidecar_path: bronze_doc})

    doc = item_record.build(NO_MEDIA_ITEM)

    root = _node(doc, ITEM_URI)
    assert "argonath:fieldSources" not in root
    assert "argonath:fieldSources" not in doc


def test_build_drops_field_sources_and_context_baked_into_bronze_seed(fake_derivative, monkeypatch):
    """Real-world shape: bronze's own item.jsonld.json sidecar already
    carries its own `@context` and `argonath:fieldSources` as stored
    content (not something build() itself ever adds) — the "never override
    bronze" merge rule would otherwise let both leak straight through onto
    the root node."""
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_MEDIA_ITEM)
    bronze_doc = {
        "@context": {"@vocab": "https://schema.org/"}, "@id": ARK, "ark": ARK,
        "name": "Photo", "argonath:fieldSources": {"name": "human"},
    }
    _fs(monkeypatch, exists_paths=(NO_MEDIA_ITEM.sidecar_path,), reads={NO_MEDIA_ITEM.sidecar_path: bronze_doc})

    doc = item_record.build(NO_MEDIA_ITEM)

    root = _node(doc, ITEM_URI)
    assert "@context" not in root
    assert "argonath:fieldSources" not in root
    assert "argonath:fieldSources" not in doc


def test_build_minimal_doc_from_identifiers_json_when_no_bronze_sidecar(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_SIDECAR_ITEM)
    identifiers_path = "cask:/bronze/g/c/latest/item/identifiers.json"
    _fs(monkeypatch, exists_paths=(identifiers_path,), reads={
        identifiers_path: [{"type": "LocalID", "value": "abc123"}, {"type": None, "value": ARK}],
    })

    doc = item_record.build(NO_SIDECAR_ITEM)

    assert doc["@id"] == ARK
    root = _node(doc, ITEM_URI)
    assert root["name"] == "item"
    assert root["isPartOf"] == {"@id": COLLECTION_URI}
    assert {"@type": "PropertyValue", "propertyID": "LocalID", "value": "abc123"} in root["identifier"]
    assert {"@type": "PropertyValue", "propertyID": "ARK", "value": ARK} in root["identifier"]
    assert {
        "@type": "PropertyValue", "propertyID": "CaskGoldPath",
        "value": "cask://gold/digital-dev/c/item/item.jsonld.json",
    } in root["identifier"]
    assert root["associatedMedia"] == []


def test_build_identifier_array_includes_preservica_ref_when_bronze_metadata_has_one(fake_derivative, monkeypatch):
    metadata_path = "cask:/bronze/g/c/latest/item/metadata.json"
    _fs(monkeypatch, exists_paths=(metadata_path,), reads={
        metadata_path: {"preservica": {"ref": "11111111-2222-3333-4444-555555555555", "type": "IO"}},
    })
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_MEDIA_ITEM)

    doc = item_record.build(NO_MEDIA_ITEM)

    root = _node(doc, ITEM_URI)
    assert {
        "@type": "PropertyValue", "propertyID": "Preservica",
        "value": "11111111-2222-3333-4444-555555555555",
    } in root["identifier"]


def test_build_identifier_array_omits_preservica_ref_when_bronze_has_no_metadata(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_MEDIA_ITEM)
    _fs(monkeypatch)

    doc = item_record.build(NO_MEDIA_ITEM)

    root = _node(doc, ITEM_URI)
    assert all(entry["propertyID"] != "Preservica" for entry in root["identifier"])


def test_build_unions_computed_identifiers_onto_bronze_seeded_identifier_array(fake_derivative, monkeypatch):
    """Real-world shape: bronze's own tree item.jsonld.json sidecar always
    seeds a single ARK-only `identifier` entry (see
    digtk.models.collection._write_tree_item_jsonld()) — unlike every other
    property, this must not hide build()'s own computed identifiers
    (Preservica ref, gold path) behind the usual never-override-bronze rule."""
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_MEDIA_ITEM)
    bronze_doc = {
        "@id": ARK, "ark": ARK, "name": "Photo",
        "identifier": [{"@type": "PropertyValue", "propertyID": "ARK", "value": ARK}],
    }
    metadata_path = "cask:/bronze/g/c/latest/item/metadata.json"
    _fs(monkeypatch, exists_paths=(NO_MEDIA_ITEM.sidecar_path, metadata_path), reads={
        NO_MEDIA_ITEM.sidecar_path: bronze_doc,
        metadata_path: {"preservica": {"ref": "11111111-2222-3333-4444-555555555555"}},
    })

    doc = item_record.build(NO_MEDIA_ITEM)

    root = _node(doc, ITEM_URI)
    assert root["identifier"][0] == {"@type": "PropertyValue", "propertyID": "ARK", "value": ARK}
    property_ids = {entry["propertyID"] for entry in root["identifier"]}
    assert property_ids == {"ARK", "Preservica", "CaskGoldPath"}


def test_build_graph_merge_does_not_override_bronze_seed(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_MEDIA_ITEM)
    bronze_doc = {"@id": ARK, "@graph": [{"@id": ARK, "name": "Bronze Name"}]}
    _fs(monkeypatch, exists_paths=(NO_MEDIA_ITEM.sidecar_path,), reads={NO_MEDIA_ITEM.sidecar_path: bronze_doc})

    doc = item_record.build(NO_MEDIA_ITEM)

    root = _node(doc, ITEM_URI)
    assert root["name"] == "Bronze Name"
    assert root["isPartOf"] == {"@id": COLLECTION_URI}
    assert root["associatedMedia"] == []
    assert len(doc["@graph"]) == 1


def test_build_flat_associated_media_for_single_file_item(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: ITEM)
    _fs(monkeypatch, exists_paths=(ITEM.sidecar_path,), reads={
        ITEM.sidecar_path: {"@id": ARK, "ark": ARK, "name": "Photo"},
    })

    doc = item_record.build(ITEM)

    root = _node(doc, ITEM_URI)
    assert len(root["associatedMedia"]) == 1
    media_node = _node(doc, root["associatedMedia"][0]["@id"])
    assert media_node == {"@id": media_node["@id"], "contentUrl": "cask://bronze/g/c/latest/item/photo.tiff"}
    assert root["image"] == {"@id": media_node["@id"]}
    assert len(doc["@graph"]) == 2


def test_build_gold_lineage_excludes_bronze_binaries_when_a_descriptive_file_was_read(fake_derivative, monkeypatch):
    """A multi-hundred-page book item's preservation binaries are only ever
    referenced by URL, never read -- the gold record's CaskFS lineage should
    name only the bronze files whose content actually went into it."""
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: MULTI_ITEM)
    identifiers_path = "cask:/bronze/g/c/latest/box-1/folder-3/identifiers.json"
    _fs(monkeypatch, exists_paths=(identifiers_path,), reads={identifiers_path: []})

    item_record.build(MULTI_ITEM)

    assert fake_derivative[0]["source_files"] == [identifiers_path]


def test_build_gold_lineage_falls_back_to_bronze_binaries_when_no_descriptive_file_exists(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: MULTI_ITEM)
    _fs(monkeypatch)

    item_record.build(MULTI_ITEM)

    assert fake_derivative[0]["source_files"] == MULTI_ITEM.bronze_binary_paths


def test_build_is_idempotent_across_reruns(fake_derivative, monkeypatch):
    """build() never reads from or writes to silver (see the module
    docstring) — it's a pure function of bronze, so re-running build-item
    with no change to bronze reproduces the exact same gold output."""
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: ITEM)
    _fs(monkeypatch, exists_paths=(ITEM.sidecar_path,), reads={
        ITEM.sidecar_path: {"@id": ARK, "ark": ARK, "name": "Photo"},
    })

    first = item_record.build(ITEM)
    second = item_record.build(ITEM)

    assert first == second
    assert len(_node(first, ITEM_URI)["associatedMedia"]) == 1
    assert {call["type"] for call in fake_derivative} == {"gold"}


def test_build_has_part_for_multi_file_item(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: MULTI_ITEM)
    _fs(monkeypatch)

    doc = item_record.build(MULTI_ITEM)

    root = _node(doc, ITEM_URI)
    assert len(root["associatedMedia"]) == 1
    imagelist = _node(doc, root["associatedMedia"][0]["@id"])
    assert len(imagelist["hasPart"]) == 2
    children = [_node(doc, ref["@id"]) for ref in imagelist["hasPart"]]
    assert [c["position"] for c in children] == [1, 2]
    assert children[0] == {
        "@id": children[0]["@id"], "position": 1,
        "contentUrl": "cask://bronze/g/c/latest/box-1/folder-3/photo-a.tiff",
    }
    assert children[1] == {
        "@id": children[1]["@id"], "position": 2,
        "contentUrl": "cask://bronze/g/c/latest/box-1/folder-3/photo-b.tiff",
    }
    assert root["image"] == {"@id": children[0]["@id"]}
    assert len(doc["@graph"]) == 4


def test_build_multi_image_position_from_filename_out_of_lexicographic_order(fake_derivative, monkeypatch):
    item = ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=[
            "cask:/bronze/g/c/latest/item/page_10.tif",
            "cask:/bronze/g/c/latest/item/page_2.tif",
        ],
        sidecar_path=None,
    )
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: item)
    _fs(monkeypatch)

    doc = item_record.build(item)

    root = _node(doc, ITEM_URI)
    imagelist = _node(doc, root["associatedMedia"][0]["@id"])
    children = [_node(doc, ref["@id"]) for ref in imagelist["hasPart"]]
    # page_10.tif sorts first lexicographically, but page_2.tif has the lower
    # parsed sequence number and must come first by position.
    assert {c["@id"].rsplit("/", 1)[-1]: c["position"] for c in children} == {
        "page_10.tif": 10, "page_2.tif": 2,
    }
    lowest = next(c for c in children if c["position"] == 2)
    assert root["image"] == {"@id": lowest["@id"]}


def test_build_image_associated_media_points_at_pdf(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: IMAGE_PDF_ITEM)
    _fs(monkeypatch)

    doc = item_record.build(IMAGE_PDF_ITEM)

    root = _node(doc, ITEM_URI)
    refs = {ref["@id"].rsplit("#media/", 1)[-1]: ref["@id"] for ref in root["associatedMedia"]}
    image_node = _node(doc, refs["photo.tiff"])
    pdf_node = _node(doc, refs["document.pdf"])
    assert image_node["associatedMedia"] == [{"@id": pdf_node["@id"]}]
    # The pdf stays directly reachable from root too (additive).
    assert {"@id": pdf_node["@id"]} in root["associatedMedia"]


def test_build_imagelist_associated_media_points_at_pdf(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: MULTI_IMAGE_PDF_ITEM)
    _fs(monkeypatch)

    doc = item_record.build(MULTI_IMAGE_PDF_ITEM)

    root = _node(doc, ITEM_URI)
    imagelist = _node(doc, item_record._frag(ITEM_URI, "imagelist"))
    pdf_node = _node(doc, item_record._frag(ITEM_URI, "media", "document.pdf"))
    assert {"@id": imagelist["@id"]} in root["associatedMedia"]
    assert {"@id": pdf_node["@id"]} in root["associatedMedia"]
    assert imagelist["associatedMedia"] == [{"@id": pdf_node["@id"]}]
    for child_ref in imagelist["hasPart"]:
        child = _node(doc, child_ref["@id"])
        assert "associatedMedia" not in child


def test_build_pdf_only_item_uses_root_associated_media(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: PDF_ONLY_ITEM)
    _fs(monkeypatch)

    doc = item_record.build(PDF_ONLY_ITEM)

    root = _node(doc, ITEM_URI)
    assert len(root["associatedMedia"]) == 1
    pdf_node = _node(doc, root["associatedMedia"][0]["@id"])
    assert pdf_node == {"@id": pdf_node["@id"], "contentUrl": "cask://bronze/g/c/latest/item/document.pdf"}
    assert "image" not in root


def test_build_audio_video_node_is_bare(fake_derivative, monkeypatch):
    """No encoding renditions or transcript correlation — those were only
    ever sourced from silver's flat associatedMedia, which build() no longer
    reads at all (see the module docstring)."""
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: AV_ITEM)
    _fs(monkeypatch)

    doc = item_record.build(AV_ITEM)

    root = _node(doc, ITEM_URI)
    assert len(root["associatedMedia"]) == 1
    video_node = _node(doc, root["associatedMedia"][0]["@id"])
    assert video_node == {
        "@id": video_node["@id"],
        "@type": ["schema:VideoObject", "schema:CreativeWork"],
        "argonath:sourceGroup": "video",
        "contentUrl": "cask://bronze/g/c/latest/item/interview.mp4",
    }


def test_build_folds_alt_text_onto_image_node(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: ITEM)
    alt_text_path = "cask:/silver/g/c/latest/item/accessibility/alt-text.json"
    alt_doc = {"alt": "A scanned photograph", "description": "Longer description", "flags": ["low-contrast"]}
    _fs(monkeypatch, exists_paths=(alt_text_path,), reads={alt_text_path: alt_doc})

    doc = item_record.build(ITEM)

    root = _node(doc, ITEM_URI)
    media_node = _node(doc, root["associatedMedia"][0]["@id"])
    assert media_node["accessibilitySummary"] == "A scanned photograph"
    assert media_node["description"] == "Longer description"
    assert media_node["accessibilityFeature"] == ["low-contrast"]


def test_build_writes_a_gold_manifest_recording_the_bronze_folder(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: TREE_ITEM)
    _fs(monkeypatch)

    item_record.build(TREE_ITEM)

    record, manifest = fake_derivative
    assert record["rel_path"] == "tree/quarter-mile/item.jsonld.json"
    assert manifest["rel_path"] == "tree/quarter-mile/manifest.json"
    assert manifest["opts"]["app"] == "digital-dev"
    assert json.loads(manifest["data"])["bronze"] == "/bronze/preservica/haeger/latest/tree/quarter-mile"


def test_record_path_is_under_the_gold_project():
    assert item_record.record_path(TREE_ITEM) == "cask:/gold/digital-dev/haeger/tree/quarter-mile/item.jsonld.json"
    assert item_record.record_path(TREE_ITEM, {"project": "digital-prod"}) == (
        "cask:/gold/digital-prod/haeger/tree/quarter-mile/item.jsonld.json"
    )


def test_read_and_root_node_round_trip_a_built_record(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: NO_MEDIA_ITEM)
    _fs(monkeypatch, exists_paths=(NO_MEDIA_ITEM.sidecar_path,), reads={
        NO_MEDIA_ITEM.sidecar_path: {"@id": ARK, "ark": ARK, "name": "Photo"},
    })
    doc = item_record.build(NO_MEDIA_ITEM)
    monkeypatch.setattr(item_record.fs, "read", lambda path: fake_derivative[0]["data"])

    record = item_record.read(NO_MEDIA_ITEM)

    assert record == doc
    assert item_record.root_node(record)["name"] == "Photo"


def test_build_maps_tree_metadata_json_dublin_core_without_a_sidecar(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: TREE_ITEM)
    metadata_path = f"{TREE_FOLDER}/metadata.json"
    _fs(monkeypatch, exists_paths=(metadata_path,), reads={metadata_path: {
        "dc": {"title": "Quarter Mile", "description": "A scan"},
        "preservica": {"ref": "io-1", "title": "Quarter_Mile", "type": "IO"},
    }})

    doc = item_record.build(TREE_ITEM)

    root = _node(doc, ITEM_URI)
    assert root["name"] == "Quarter Mile"
    assert root["description"] == "A scan"
    assert "argonath:fieldSources" not in root
    assert root["identifier"][0] == {"@type": "PropertyValue", "propertyID": "ARK", "value": ARK}
    assert {"@type": "PropertyValue", "propertyID": "Preservica", "value": "io-1"} in root["identifier"]


def test_build_skips_dublin_core_mapping_without_a_metadata_fragment(fake_derivative, monkeypatch):
    monkeypatch.setattr(item_record.items, "resolve_target", lambda target, opts={}: TREE_ITEM)
    metadata_path = f"{TREE_FOLDER}/metadata.json"
    _fs(monkeypatch, exists_paths=(metadata_path,), reads={metadata_path: {"preservica": {"ref": "io-1", "title": "Quarter_Mile"}}})

    doc = item_record.build(TREE_ITEM)

    assert _node(doc, ITEM_URI)["name"] == "quarter-mile"
