from digtk.lib import dc_mapping

ARK = "ark:/87287/d7abc1234"


def dc_content(**fields):
    return {"dc": fields}


# --- typed_date_literal() ----------------------------------------------------

def test_typed_date_literal_full_date():
    assert dc_mapping.typed_date_literal("2024-09-07") == {
        "@type": "http://www.w3.org/2001/XMLSchema#date", "@value": "2024-09-07",
    }


def test_typed_date_literal_year_month():
    assert dc_mapping.typed_date_literal("2024-09") == {
        "@type": "http://www.w3.org/2001/XMLSchema#gYearMonth", "@value": "2024-09",
    }


def test_typed_date_literal_year_only():
    assert dc_mapping.typed_date_literal("2024") == {
        "@type": "http://www.w3.org/2001/XMLSchema#gYear", "@value": "2024",
    }


def test_typed_date_literal_falls_back_to_plain_string():
    assert dc_mapping.typed_date_literal("circa 1920s") == "circa 1920s"


def test_typed_date_literal_none_for_falsy_value():
    assert dc_mapping.typed_date_literal(None) is None
    assert dc_mapping.typed_date_literal("") is None


# --- is_uri() -----------------------------------------------------------------

def test_is_uri_true_for_http_and_https():
    assert dc_mapping.is_uri("http://example.com/x")
    assert dc_mapping.is_uri("https://example.com/x")


def test_is_uri_false_for_plain_text_or_non_string():
    assert not dc_mapping.is_uri("just some text")
    assert not dc_mapping.is_uri(None)
    assert not dc_mapping.is_uri(123)


# --- extract_dublin_core() -----------------------------------------------------

def test_extract_dublin_core_reads_scalar_fields():
    content = dc_content(title="Old Vines", description="A vineyard photo", creator="Jane Doe", date="2024", rights="Public Domain", publisher="UC Davis")
    dc = dc_mapping.extract_dublin_core(content)
    assert dc["title"] == "Old Vines"
    assert dc["description"] == "A vineyard photo"
    assert dc["creator"] == "Jane Doe"
    assert dc["date"] == "2024"
    assert dc["rights"] == "Public Domain"
    assert dc["publisher"] == "UC Davis"


def test_extract_dublin_core_handles_multi_valued_subject():
    content = dc_content(subject=["Wine", "Vineyards"])
    dc = dc_mapping.extract_dublin_core(content)
    assert dc["subjects"] == ["Wine", "Vineyards"]


def test_extract_dublin_core_handles_single_subject_not_as_list():
    content = dc_content(subject="Wine")
    dc = dc_mapping.extract_dublin_core(content)
    assert dc["subjects"] == ["Wine"]


def test_extract_dublin_core_all_fields_absent():
    dc = dc_mapping.extract_dublin_core({"dc": {}})
    assert dc == {
        "title": None, "description": None, "creator": None, "date": None,
        "rights": None, "subjects": [], "publisher": None,
    }


def test_extract_dublin_core_no_dc_key_at_all():
    dc = dc_mapping.extract_dublin_core({})
    assert dc["title"] is None
    assert dc["subjects"] == []


def test_extract_dublin_core_reads_text_from_attributed_node():
    # xmltodict shape for an element with both attributes and text content.
    content = dc_content(title={"@_lang": "en", "#text": "Old Vines"})
    dc = dc_mapping.extract_dublin_core(content)
    assert dc["title"] == "Old Vines"


# --- map_dublin_core_to_schema() ----------------------------------------------

def test_map_dublin_core_to_schema_maps_every_field():
    content = dc_content(
        title="Old Vines", description="A vineyard photo", creator="Jane Doe",
        date="2024-09", rights="http://rightsstatements.org/vocab/InC-NC/1.0/",
        subject=["Wine", "Vineyards"], publisher="UC Davis Library",
    )
    doc = dc_mapping.map_dublin_core_to_schema(content, ark=ARK)

    assert doc["name"] == "Old Vines"
    assert doc["description"] == "A vineyard photo"
    assert doc["creator"] == "Jane Doe"
    assert doc["dateCreated"] == {"@type": "http://www.w3.org/2001/XMLSchema#gYearMonth", "@value": "2024-09"}
    assert doc["license"] == {"@id": "http://rightsstatements.org/vocab/InC-NC/1.0/"}
    assert doc["about"] == [{"@id": "Wine"}, {"@id": "Vineyards"}]
    assert doc["publisher"] == "UC Davis Library"


def test_map_dublin_core_to_schema_uses_name_fallback_when_no_title():
    doc = dc_mapping.map_dublin_core_to_schema(dc_content(), ark=ARK, name_fallback="Fallback Title")
    assert doc["name"] == "Fallback Title"


def test_map_dublin_core_to_schema_omits_name_when_neither_title_nor_fallback():
    doc = dc_mapping.map_dublin_core_to_schema(dc_content(), ark=ARK)
    assert "name" not in doc


def test_map_dublin_core_to_schema_license_plain_string_when_not_uri_shaped():
    doc = dc_mapping.map_dublin_core_to_schema(dc_content(rights="Public Domain"), ark=ARK)
    assert doc["license"] == "Public Domain"


def test_map_dublin_core_to_schema_license_falls_back_to_default():
    doc = dc_mapping.map_dublin_core_to_schema(
        dc_content(), ark=ARK, opts={"default_license": "http://rightsstatements.org/vocab/InC/1.0/"},
    )
    assert doc["license"] == {"@id": "http://rightsstatements.org/vocab/InC/1.0/"}


def test_map_dublin_core_to_schema_publisher_falls_back_to_default():
    doc = dc_mapping.map_dublin_core_to_schema(dc_content(), ark=ARK, opts={"default_publisher": "UC Davis Library"})
    assert doc["publisher"] == "UC Davis Library"


def test_map_dublin_core_to_schema_about_includes_non_uri_subjects_verbatim():
    # Ported as-is from preservica-etl's item.js#buildItemJsonLd(), which does
    # not check subjects are URI-shaped before using them as an "@id".
    doc = dc_mapping.map_dublin_core_to_schema(dc_content(subject="Wine"), ark=ARK)
    assert doc["about"] == [{"@id": "Wine"}]


def test_map_dublin_core_to_schema_all_fields_absent_returns_empty_dict():
    doc = dc_mapping.map_dublin_core_to_schema(dc_content(), ark=ARK)
    assert doc == {}
