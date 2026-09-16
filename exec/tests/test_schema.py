from digtk.lib.schema import ARGONATH_NS, SCHEMA_CONTEXT, Identifier, ItemMetadata

ARK = "ark:/87287/d7abc1234"


def test_to_jsonld_includes_context_and_required_fields():
    item = ItemMetadata(id=ARK, ark=ARK)
    doc = item.to_jsonld()
    assert doc["@context"] == SCHEMA_CONTEXT
    assert doc["@id"] == ARK
    assert doc["ark"] == ARK
    assert doc["@type"] == "CreativeWork"


def test_to_jsonld_omits_unset_optional_fields():
    item = ItemMetadata(id=ARK, ark=ARK)
    doc = item.to_jsonld()
    for key in ("name", "description", "creator", "dateCreated", "license", "publisher", "about", "identifier", "argonath:fieldSources"):
        assert key not in doc


def test_to_jsonld_includes_set_fields():
    item = ItemMetadata(
        id=ARK, ark=ARK, name="Old Vines", description="A vineyard photo", creator="Jane Doe",
        date_created={"@type": "http://www.w3.org/2001/XMLSchema#gYear", "@value": "2024"},
        license="http://rightsstatements.org/vocab/InC-NC/1.0/",
        publisher="UC Davis Library", about=[{"@id": "Wine"}],
        field_sources={"name": "human"},
    )
    doc = item.to_jsonld()
    assert doc["name"] == "Old Vines"
    assert doc["description"] == "A vineyard photo"
    assert doc["creator"] == "Jane Doe"
    assert doc["dateCreated"] == {"@type": "http://www.w3.org/2001/XMLSchema#gYear", "@value": "2024"}
    assert doc["license"] == "http://rightsstatements.org/vocab/InC-NC/1.0/"
    assert doc["publisher"] == "UC Davis Library"
    assert doc["about"] == [{"@id": "Wine"}]
    assert doc["argonath:fieldSources"] == {"name": "human"}


def test_to_jsonld_serializes_identifiers_by_alias():
    item = ItemMetadata(id=ARK, ark=ARK, identifiers=[Identifier(property_id="ARK", value=ARK)])
    doc = item.to_jsonld()
    assert doc["identifier"] == [{"@type": "PropertyValue", "propertyID": "ARK", "value": ARK}]


def test_from_jsonld_round_trips_to_jsonld_output():
    original = ItemMetadata(
        id=ARK, ark=ARK, name="Old Vines", creator="Jane Doe",
        license={"@id": "http://rightsstatements.org/vocab/InC-NC/1.0/"},
        about=[{"@id": "Wine"}],
        identifiers=[Identifier(property_id="ARK", value=ARK)],
        field_sources={"name": "human"},
    )
    doc = original.to_jsonld()

    restored = ItemMetadata.from_jsonld(doc)

    assert restored.id == ARK
    assert restored.ark == ARK
    assert restored.name == "Old Vines"
    assert restored.creator == "Jane Doe"
    assert restored.license == {"@id": "http://rightsstatements.org/vocab/InC-NC/1.0/"}
    assert restored.about == [{"@id": "Wine"}]
    assert restored.identifiers == [Identifier(property_id="ARK", value=ARK)]
    assert restored.field_sources == {"name": "human"}
    assert restored.to_jsonld() == doc


def test_from_jsonld_defaults_missing_optional_fields():
    restored = ItemMetadata.from_jsonld({"@id": ARK})
    assert restored.ark == ARK
    assert restored.type == "CreativeWork"
    assert restored.name is None
    assert restored.about == []
    assert restored.identifiers == []
    assert restored.field_sources == {}


def test_argonath_namespace_matches_schema_context():
    assert SCHEMA_CONTEXT["argonath"] == ARGONATH_NS
