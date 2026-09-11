"""
One-to-one XML -> JSON translation for Preservica's Entity API responses.

Ported from preservica-etl's lib/xml.js (a thin fast-xml-parser wrapper): namespace
prefixes are stripped from element and attribute names (xip:Ref -> Ref, oai_dc:dc ->
dc), attributes become '@_'-prefixed keys, and repeated elements become a list while
a single occurrence stays a scalar/dict — the same ambiguity fast-xml-parser has,
normalized on read via as_list(). Nothing beyond this is reinterpreted: no schema.org
mapping, no field renaming, no data thrown away. See digtk.models.collection for
where this is used to build bronze's metadata.json/entity.json/etc.
"""
import re

import xmltodict

_NS_PREFIX_RE = re.compile(r"^[^:@#]+:")
_XML_DECL_RE = re.compile(r"^<\?xml[^>]*>")
_DOCTYPE_HTML_RE = re.compile(r"<!DOCTYPE html", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"^<html", re.IGNORECASE)


class NonXmlResponseError(Exception):
    """Raised when a Preservica response expected to be XML is empty or an HTML
    error page instead (e.g. a metadata-fragment link that 405s and returns an
    HTML error page rather than XML) — callers should treat this as "no data"
    rather than a hard failure.

    :param str body: The raw response body that failed to parse as XML.
    """

    def __init__(self, body):
        super().__init__("Response is empty or not XML")
        self.body = body


def _strip_ns_prefix(key):
    """Strip a single namespace prefix from an XML tag or attribute key.

    :param str key: Raw parsed key, e.g. 'xip:Ref' or '@_xip:ref'.
    :returns: Key with any leading 'prefix:' segment removed (an '@_' attribute
        marker, if present, is preserved), e.g. 'Ref' or '@_ref'.
    :rtype: str
    """
    if key.startswith("@_"):
        return "@_" + _NS_PREFIX_RE.sub("", key[2:])
    return _NS_PREFIX_RE.sub("", key)


def _strip_ns_prefixes(value):
    """Recursively strip namespace prefixes from every dict key in a parsed XML tree.

    :param value: A parsed XML value (dict, list, or scalar).
    :returns: The same structure with every dict key's namespace prefix removed.
    """
    if isinstance(value, dict):
        return {_strip_ns_prefix(k): _strip_ns_prefixes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_ns_prefixes(v) for v in value]
    return value


def parse(raw_xml):
    """Parse an XML string into a plain JSON-serializable dict, one-to-one.

    :param str raw_xml: Raw XML response body.
    :returns: Parsed structure, namespace prefixes stripped.
    :rtype: dict
    :raises NonXmlResponseError: If `raw_xml` is empty, or looks like an HTML
        error page rather than XML.
    """
    trimmed = (raw_xml or "").strip()
    if not trimmed.startswith("<?xml") and not trimmed.startswith("<"):
        raise NonXmlResponseError(raw_xml)

    without_decl = _XML_DECL_RE.sub("", trimmed).strip()
    if _DOCTYPE_HTML_RE.search(trimmed[:200]) or _HTML_TAG_RE.match(without_decl):
        raise NonXmlResponseError(raw_xml)

    parsed = xmltodict.parse(raw_xml, attr_prefix="@_", dict_constructor=dict)
    return _strip_ns_prefixes(parsed)


def as_list(value):
    """Normalize a value that may be a single item or a list into always a list.

    XML's single-vs-repeated-element ambiguity means a caller reading e.g.
    ChildrenResponse.Children.Child must handle both a lone dict (one child) and
    a list (multiple children) — this collapses that into one shape.

    :param value: A parsed XML value — None, a dict/scalar, or a list.
    :returns: [] if `value` is None, `value` unchanged if already a list, else `[value]`.
    :rtype: list
    """
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def text_of(node):
    """Extract text content from a parsed XML node that may carry attributes.

    An element with only text parses as a plain string; one with both attributes
    and text parses as {'@_...': ..., '#text': ...} — this returns the text either way.

    :param node: A parsed XML value (str, or dict possibly containing '#text').
    :returns: The node's text content, or None if it has none.
    :rtype: str|None
    """
    if node is None:
        return None
    if isinstance(node, dict):
        text = node.get("#text")
        return str(text) if text is not None else None
    return str(node)


def attr_of(node, name):
    """Read an attribute from a parsed XML node.

    :param node: A parsed XML element.
    :param str name: Attribute's local name (namespace prefix already stripped), e.g. 'ref'.
    :returns: The attribute's value, or None if absent.
    :rtype: str|None
    """
    if not isinstance(node, dict):
        return None
    value = node.get(f"@_{name}")
    return None if value is None else str(value)


def parse_metadata_fragment(raw_xml):
    """Build the one-to-one {schemaUri, rawXml, parsed} shape for a descriptive
    metadata fragment (e.g. Dublin Core), written verbatim into bronze's metadata.json.

    :param str raw_xml: The fragment's raw XML body (a MetadataResponse envelope).
    :returns: {'schemaUri': str|None, 'rawXml': str, 'parsed': dict} — raw XML is
        always preserved alongside the parsed form, nothing is discarded.
    :rtype: dict
    """
    parsed = parse(raw_xml)
    container = (parsed.get("MetadataResponse") or {}).get("MetadataContainer") or {}
    schema_uri = attr_of(container, "schemaUri") or container.get("SchemaUri")
    return {"schemaUri": schema_uri, "rawXml": raw_xml, "parsed": parsed}
