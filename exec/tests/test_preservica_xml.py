import pytest

from digtk.lib.preservica import xml


def test_parse_strips_namespace_prefixes_from_elements():
    raw = (
        '<?xml version="1.0"?>'
        '<EntityResponse xmlns:xip="http://preservica.com/XIP/v6.5">'
        '<xip:StructuralObject><xip:Ref>abc-123</xip:Ref><xip:Title>My Folder</xip:Title>'
        '</xip:StructuralObject></EntityResponse>'
    )
    parsed = xml.parse(raw)
    assert parsed["EntityResponse"]["StructuralObject"]["Ref"] == "abc-123"
    assert parsed["EntityResponse"]["StructuralObject"]["Title"] == "My Folder"


def test_parse_strips_namespace_prefixes_from_attributes():
    raw = '<Child xip:ref="r1" xip:type="IO">https://example.com/r1</Child>'
    parsed = xml.parse(raw)
    assert parsed["Child"]["@_ref"] == "r1"
    assert parsed["Child"]["@_type"] == "IO"
    assert parsed["Child"]["#text"] == "https://example.com/r1"


def test_parse_repeated_elements_become_a_list():
    raw = "<Children><Child>a</Child><Child>b</Child></Children>"
    parsed = xml.parse(raw)
    assert parsed["Children"]["Child"] == ["a", "b"]


def test_parse_single_element_stays_scalar():
    raw = "<Children><Child>a</Child></Children>"
    parsed = xml.parse(raw)
    assert parsed["Children"]["Child"] == "a"


def test_parse_preserves_dublin_core_field_values():
    raw = (
        '<MetadataResponse><MetadataContainer schemaUri="http://oai_dc">'
        '<Content><dc><title>My Title</title><subject>a</subject><subject>b</subject></dc></Content>'
        '</MetadataContainer></MetadataResponse>'
    )
    parsed = xml.parse(raw)
    dc = parsed["MetadataResponse"]["MetadataContainer"]["Content"]["dc"]
    assert dc["title"] == "My Title"
    assert dc["subject"] == ["a", "b"]


def test_parse_raises_on_empty_body():
    with pytest.raises(xml.NonXmlResponseError):
        xml.parse("")


def test_parse_raises_on_non_xml_body():
    with pytest.raises(xml.NonXmlResponseError):
        xml.parse("not xml at all")


def test_parse_raises_on_html_error_page():
    with pytest.raises(xml.NonXmlResponseError):
        xml.parse("<!DOCTYPE html><html><body>405 Method Not Allowed</body></html>")


def test_parse_raises_on_html_error_page_after_xml_declaration():
    with pytest.raises(xml.NonXmlResponseError):
        xml.parse('<?xml version="1.0"?><html><body>error</body></html>')


def test_as_list_none_becomes_empty_list():
    assert xml.as_list(None) == []


def test_as_list_scalar_becomes_single_item_list():
    assert xml.as_list({"a": 1}) == [{"a": 1}]


def test_as_list_list_passes_through_unchanged():
    assert xml.as_list([1, 2]) == [1, 2]


def test_text_of_plain_string():
    assert xml.text_of("hello") == "hello"


def test_text_of_node_with_attributes_and_text():
    assert xml.text_of({"@_ref": "r1", "#text": "https://x"}) == "https://x"


def test_text_of_none():
    assert xml.text_of(None) is None


def test_text_of_node_with_no_text_key():
    assert xml.text_of({"@_ref": "r1"}) is None


def test_attr_of_reads_prefixed_attribute():
    assert xml.attr_of({"@_ref": "r1"}, "ref") == "r1"


def test_attr_of_missing_attribute_is_none():
    assert xml.attr_of({"@_ref": "r1"}, "type") is None


def test_attr_of_non_dict_node_is_none():
    assert xml.attr_of("plain text", "ref") is None


def test_strip_attributes_removes_prefixed_keys_recursively():
    value = {"@_ref": "r1", "title": "T", "nested": {"@_type": "IO", "value": "v"}}
    assert xml.strip_attributes(value) == {"title": "T", "nested": {"value": "v"}}


def test_strip_attributes_recurses_into_lists():
    value = [{"@_ref": "r1", "title": "a"}, {"@_ref": "r2", "title": "b"}]
    assert xml.strip_attributes(value) == [{"title": "a"}, {"title": "b"}]


def test_strip_attributes_leaves_scalars_unchanged():
    assert xml.strip_attributes("plain text") == "plain text"


def test_parse_metadata_content_extracts_content_only():
    raw_xml = (
        '<?xml version="1.0"?>'
        '<MetadataResponse><MetadataContainer schemaUri="http://www.openarchives.org/OAI/2.0/oai_dc/">'
        '<Content><dc><title>T</title></dc></Content></MetadataContainer></MetadataResponse>'
    )
    assert xml.parse_metadata_content(raw_xml) == {"dc": {"title": "T"}}


def test_parse_metadata_content_strips_attribute_keys():
    raw_xml = (
        '<MetadataResponse><MetadataContainer schemaUri="http://oai_dc">'
        '<Content><dc xmlns:xsi="http://x" xsi:foo="bar"><title>T</title></dc></Content>'
        "</MetadataContainer></MetadataResponse>"
    )
    assert xml.parse_metadata_content(raw_xml) == {"dc": {"title": "T"}}


def test_parse_metadata_content_empty_when_no_content():
    raw_xml = "<MetadataResponse><MetadataContainer schemaUri=\"http://x\"/></MetadataResponse>"
    assert xml.parse_metadata_content(raw_xml) == {}
