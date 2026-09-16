"""
digtk.lib.dc_mapping — Dublin Core to schema.org field mapping.

Ported from preservica-etl's lib/transform/{descriptive-metadata,item}.js: the
field-for-field Dublin Core -> schema.org mapping logic only, not that tool's
surrounding architecture (its own @context/bronze/silver export machinery is
superseded here by digtk's own bronze pipeline and digtk.lib.schema's JSON-LD
shape — see digtk.models.collection for where this module's output actually
gets used).

Operates on the {'dc': {...}} shape digtk.lib.preservica.xml.parse_metadata_content()
already produces from a Preservica metadata fragment's raw XML — no network calls,
no XML parsing of its own.
"""
import re

from digtk.lib import preservica

_DATE_RE = {
    "date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "gYearMonth": re.compile(r"^\d{4}-\d{2}$"),
    "gYear": re.compile(r"^\d{4}$"),
}


def typed_date_literal(value):
    """Build an XSD-typed date literal matching a date string's precision, or
    return the plain string if it doesn't match a recognized date shape.

    Ported from preservica-etl's descriptive-metadata.js#typedDateLiteral().

    :param str|None value: A date string, e.g. "2025", "2024-09", "2024-09-07".
    :returns: {"@type": xsd-type-uri, "@value": value}, the plain string if
        unrecognized, or None if `value` is falsy.
    :rtype: dict|str|None
    """
    if not value:
        return None
    if _DATE_RE["date"].match(value):
        return {"@type": "http://www.w3.org/2001/XMLSchema#date", "@value": value}
    if _DATE_RE["gYearMonth"].match(value):
        return {"@type": "http://www.w3.org/2001/XMLSchema#gYearMonth", "@value": value}
    if _DATE_RE["gYear"].match(value):
        return {"@type": "http://www.w3.org/2001/XMLSchema#gYear", "@value": value}
    return value


def is_uri(value):
    """
    :param value: A value to test.
    :returns: True if `value` is a string that looks like an http(s) URI.
    :rtype: bool
    """
    return isinstance(value, str) and bool(re.match(r"^https?://", value))


def extract_dublin_core(content):
    """Extract Dublin Core fields from a parsed metadata fragment's Content payload.

    Ported from preservica-etl's descriptive-metadata.js#extractDublinCore(),
    adapted to digtk's already-single-fragment input shape (no multi-fragment
    search needed — digtk.models.collection._copy_metadata_to_tree() /
    _write_tree_item_jsonld() already resolved to one fragment's Content before
    calling this, and refuse more than one fragment per entity outright).

    :param dict content: digtk.lib.preservica.xml.parse_metadata_content()'s
        return, e.g. {"dc": {...}}.
    :returns: {"title", "description", "creator", "date", "rights",
        "subjects": list[str], "publisher"} — all keys present, values None
        (or [] for "subjects") if absent from the fragment.
    :rtype: dict
    """
    dc = content.get("dc") or {}

    def first_text(field):
        values = preservica.as_list(dc.get(field))
        return preservica.text_of(values[0]) if values else None

    subjects = [t for t in (preservica.text_of(v) for v in preservica.as_list(dc.get("subject"))) if t]

    return {
        "title": first_text("title"),
        "description": first_text("description"),
        "creator": first_text("creator"),
        "date": first_text("date"),
        "rights": first_text("rights"),
        "subjects": subjects,
        "publisher": first_text("publisher"),
    }


def map_dublin_core_to_schema(content, *, ark, name_fallback=None, opts={}):
    """Map a metadata fragment's Dublin Core fields to schema.org fields.

    Ported from preservica-etl's item.js#buildItemJsonLd(), scoped to just the
    DC-derived fields — @id/@type/identifier/associatedMedia/image are the
    caller's own concern (see digtk.models.collection/digtk.lib.schema.ItemMetadata),
    not this module's. Mapping: dc:title -> name, dc:description -> description
    (verbatim), dc:creator -> creator, dc:date -> dateCreated (typed literal by
    precision — see typed_date_literal(); named "dateCreated" here to match
    digtk.lib.schema.ItemMetadata's existing field, not preservica-etl's own
    "datePublished", since this target schema has no separate datePublished
    field), dc:rights -> license ({"@id": uri} if URI-shaped, else plain
    string), dc:subject (repeated) -> about (array of {"@id": value}, one per
    subject — ported as-is from the reference implementation, which does not
    check subjects are URI-shaped before using them as an "@id", unlike the
    license mapping above), dc:publisher -> publisher.

    :param dict content: digtk.lib.preservica.xml.parse_metadata_content()'s return.
    :param str ark: Item ARK, used only for context in log/error messages.
    :param str|None name_fallback: Used for "name" only when no dc:title is present
        (e.g. the entity's own Preservica title).
    :param dict opts: 'default_publisher' (str, optional) — used only when no
        dc:publisher is present. 'default_license' (str, optional, a
        rightsstatements.org-style URI) — used only when no dc:rights is present.
    :returns: Only the schema.org keys that ended up with an actual value —
        possible keys: "name", "description", "creator", "dateCreated",
        "license", "about", "publisher".
    :rtype: dict
    """
    dc = extract_dublin_core(content)
    doc = {}

    name = dc["title"] or name_fallback
    if name:
        doc["name"] = name
    if dc["description"]:
        doc["description"] = dc["description"]
    if dc["creator"]:
        doc["creator"] = dc["creator"]
    if dc["date"]:
        doc["dateCreated"] = typed_date_literal(dc["date"])
    if dc["subjects"]:
        doc["about"] = [{"@id": subject} for subject in dc["subjects"]]

    publisher = dc["publisher"] or opts.get("default_publisher")
    if publisher:
        doc["publisher"] = publisher

    rights = dc["rights"] or opts.get("default_license")
    if rights:
        doc["license"] = {"@id": rights} if is_uri(rights) else rights

    return doc
