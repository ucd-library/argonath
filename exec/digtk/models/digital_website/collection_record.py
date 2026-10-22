"""
Build a collection's canonical collection.jsonld.json and promote it to gold.

The record comes from the bronze collection root's tree/metadata.json and
tree/identifiers.json, and is written to gold/[project]/[collection]/collection.jsonld.json.
Items link to it via their own isPartOf; the record has no hasPart back.
"""
import json

from digtk.lib import dc_mapping, derivative, fs
from digtk.models import collection
from digtk.models.digital_website import item_record

RECORD_FILENAME = "collection.jsonld.json"


def parse_key(key):
    """Split a collection key into its group and collection name.

    :param str key: "bronze/[group]/[collection]" or "[group]/[collection]".
    :returns: (group, collection).
    :rtype: tuple[str, str]
    :raises ValueError: If the key isn't one of those shapes.
    """
    parts = key.removeprefix("cask:").strip("/").split("/")
    if parts[0] == "bronze":
        parts = parts[1:]
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"collection key must be [bronze/]<group>/<collection>, got {key!r}")
    return parts[0], parts[1]


def record_path(name, opts={}):
    """The collection record's CaskFS path.

    :param str name: Collection name.
    :param dict opts: 'project' — see item_record._project().
    :returns: e.g. 'cask:/gold/digital-dev/haeger/collection.jsonld.json'.
    :rtype: str
    """
    dest = derivative.dest_path(
        type="gold", collection=name, rel_path=RECORD_FILENAME, opts={"app": item_record._project(opts)},
    )
    return f"cask:{dest}"


def read(name, opts={}):
    """Read a collection's built record.

    :param str name: Collection name.
    :param dict opts: 'project' — see item_record._project().
    :returns: The parsed collection.jsonld.json.
    :rtype: dict
    :raises FileNotFoundError: If build() hasn't been run for the collection yet.
    """
    return json.loads(fs.read(record_path(name, opts)))


def root_node(doc):
    """The collection's own node in a built record's @graph.

    :param dict doc: A built collection record (see build()).
    :returns: The root node, or {} if the graph has none.
    :rtype: dict
    """
    uri = f"{item_record.COLLECTION_URI_PREFIX}{doc.get('@id')}"
    return next((node for node in doc.get("@graph") or [] if node.get("@id") == uri), {})


def build(name, group, opts={}):
    """Build a collection's record from bronze and write it to gold.

    Fails for a collection with no ark, like item records do.

    :param str name: Collection name.
    :param str group: Bronze group namespace.
    :param dict opts: 'project' — see item_record._project().
    :returns: The written document.
    :rtype: dict
    :raises LookupError: If the collection has no ark (see collection.get_ark()).
    """
    ark = collection.get_ark(name, group)
    root_path = collection._bronze_latest_root(name, group)
    metadata = collection._read_bronze_json(root_path, "tree/metadata.json", {})
    raw_identifiers = collection._read_bronze_json(root_path, "tree/identifiers.json", [])

    entity = metadata.get("preservica") or {}
    mapped = dc_mapping.map_dublin_core_to_schema(metadata, ark=ark, name_fallback=entity.get("title") or name)
    root = {
        "@id": f"{item_record.COLLECTION_URI_PREFIX}{ark}",
        "@type": ["schema:Collection"],
        "ark": ark,
        **mapped,
        "identifier": item_record._identifiers_from_raw(raw_identifiers),
    }
    if not root.get("description") and entity.get("description"):
        root["description"] = entity["description"]

    doc = {"@context": item_record.RECORD_CONTEXT, "@id": ark, "@graph": [root]}
    sources = [
        path for path in (f"{root_path}/tree/metadata.json", f"{root_path}/tree/identifiers.json")
        if fs.exists(path)
    ]
    derivative.write(
        type="gold", collection=name, rel_path=RECORD_FILENAME,
        data=json.dumps(doc, indent=2).encode(), source_files=sources,
        opts={"app": item_record._project(opts), "replace": True},
    )
    return doc
