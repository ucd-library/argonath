"""
Build an item's complete, canonical item.jsonld.json and promote it to gold.

This is the only place an item.jsonld.json is written: bronze never gets one,
and the derivative steps (image/pdf/video/audio/captions) only publish their
own files and manifests into silver. This module assembles the *whole* record
in one pass — descriptive metadata/identifiers from bronze (an ingested
sidecar, or the tree folder's metadata.json Dublin Core — see
_load_bronze_sidecar()), plus a freshly rebuilt media graph — and promotes it
as a full replace into gold (see
digtk.lib.derivative.dest_path(type="gold"), under a project/app segment —
build()'s own 'project' opt, default CONFIG["showcase"]["gold_project"]).
It's meant to run once derivative generation/finalize are done, as its own
pipeline step (see dagster/lib/pipeline_item.py's pdc_generate_item_record op
and dagster/lib/pipeline_fanout.py's build_item_record()).

The record is written to gold/[project]/[collection]/[item_path]/item.jsonld.json,
next to a manifest.json recording the bronze folder it was built from — gold
has no time dimension, so that manifest is what digtk.lib.items.resolve() reads
to get from an ARK back to the item's bronze location.

Unlike every other step in this pipeline, this one is allowed to simply fail
for an item with no ark, or whose collection has none either — the canonical
public record needs stable identifiers for both ends of isPartOf, and there's
no fallback here (see digtk.lib.items's "ark is a sidecar, not a requirement"
convention for every *other* step).

The document is a flat JSON-LD graph: {"@context", "@id", "@graph": [...]} —
RECORD_CONTEXT is the same schema.org `@vocab`/argonath context bronze's own
sidecars use (plus a `schema:` prefix for the prefixed @type values), so
CaskFS's linked-data index sees real triples — all in the named graph <ark>
(the document's own @id), which is how digtk.lib.items.resolve() finds this
record by ARK (see digtk.lib.fs.find_by_graph()) — with one
sibling node per real thing (the item itself, each media file, an
ImageList for a multi-image item) related only by `{"@id": ...}` references,
never inlined into each other. This matches the legacy DAMS *-metadata
JSON-LD shape in spirit, minus its Fedora/LDP container machinery
(ldp:DirectContainer, ArchivalGroup, hasMemberRelation/membershipResource),
which the live system already treats as implicit/generated rather than
source data — so this module never emits it, and relates nodes directly
instead (e.g. an ImageList's `hasPart` straight to its page nodes).

Every media node (image, PDF, audio, video) is a bare {@id, contentUrl}
pointer straight at its bronze preservation master — none carry presentation
renditions (small/medium/large JPEGs, streaming playlists, etc.), since those
generated derivatives aren't independently addressable things in the
collection's content model. `associatedMedia` means only genuine links
between two real nodes (e.g. an image's associatedMedia -> the pdf it was
also supplied as).

This document carries no CaskFS lineage/provenance data (e.g. a bronze
source pointer on each node) — that bookkeeping belongs to CaskFS itself
(see digtk.lib.derivative's own lineage links, recorded separately by
derivative.write()'s source_files), not to the public record. That lineage
is deliberately scoped to just the bronze files this module actually reads
the content of (sidecar, identifiers.json, tree metadata.json — see
_lineage_source_files()), not every bronze binary path: a multi-hundred-page
book item's preservation TIFFs are only ever referenced by URL (their bytes
are never read), so linking every one of them as a "source" of this record
would be noise, not real provenance.

No internal bookkeeping fields survive into the written document either
(e.g. no `argonath:fieldSources`, no raw-media carry-forward) — only real
public-record properties.

build() never reads from or writes to silver at all — it's a pure function
of bronze (the item's own item.jsonld.json sidecar, identifiers.json, and
binary paths) plus the accessibility alt-text derivative (read directly from
its own derivative path, never from silver's associatedMedia). The media side
of the graph is built straight from item.bronze_binary_paths, grouped by
format via _group_bronze_by_format() (FORMAT_BY_EXTENSION, with
ASSOCIATED_MEDIA_RULES as a not-yet-populated per-file override point — see
below). Since bronze is immutable/append-only, this makes build() trivially
idempotent: re-running build-item reads the exact same input and reproduces
the exact same gold output every time, with no ordering dependency on any
other pipeline step.

Audio/video nodes currently carry no rendition (encoding) or transcript data
— that information only ever existed because it was read off silver's flat
associatedMedia, which this module no longer touches. They're bare
{@id, @type, argonath:sourceGroup, contentUrl} nodes, same shape as image/PDF
nodes. This may return later via ASSOCIATED_MEDIA_RULES or some other
bronze/derivative-based source, but isn't implemented here.

digtk.lib.showcase_index reads this record's root node (see root_node()) for
the Elasticsearch document's descriptive fields, but builds its media nodes
from silver's published derivatives directly, since none of those renditions
are carried here.
"""
import json
import os
import urllib.parse

from digtk.lib import dc_mapping, derivative, fs, items, utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import SCHEMA_CONTEXT, Identifier, ItemMetadata
from digtk.models import collection
from digtk.models.digital_website import pipeline

ITEM_URI_PREFIX = "https://digital.ucdavis.edu/item/"
COLLECTION_URI_PREFIX = "https://digital.ucdavis.edu/collection/"

# The record's document-level JSON-LD context: bronze's own sidecar context,
# plus the "schema:" prefix used by prefixed @type values (e.g. ImageList's
# "schema:MediaObject").
RECORD_CONTEXT = {**SCHEMA_CONTEXT, "schema": "https://schema.org/"}

# One graph node per bronze audio/video source file, typed by which digtk
# pipeline produced it. Image and PDF nodes carry no @type — see _image_node()/
# _pdf_node(); they're bare {@id, contentUrl[, position]} pointers.
_TYPE_BY_GROUP = {
    "video": ["schema:VideoObject", "schema:CreativeWork"],
    "audio": ["schema:AudioObject", "schema:CreativeWork"],
}

# Placeholder for future per-filename/per-extension overrides to the default
# extension-based format detection in _group_bronze_by_format() below (e.g.
# forcing a misdetected file into a different associatedMedia group, or
# excluding one). Currently always empty -- every item's media grouping comes
# from FORMAT_BY_EXTENSION alone until this is populated.
ASSOCIATED_MEDIA_RULES = {}


def _project(opts={}):
    """The gold "app" segment a record is promoted under.

    :param dict opts: 'project' (str, default CONFIG["showcase"]["gold_project"]).
    :returns: e.g. 'digital-dev'.
    :rtype: str
    """
    return opts.get("project") or CONFIG["showcase"]["gold_project"]


def record_rel_path(item):
    """The item record's path relative to its gold collection root.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: e.g. 'tree/quarter-mile/item.jsonld.json'.
    :rtype: str
    """
    return f"{item.item_path}/{items.ITEM_RECORD_FILENAME}" if item.item_path else items.ITEM_RECORD_FILENAME


def record_path(item, opts={}):
    """The item record's CaskFS path.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param dict opts: 'project' — see _project().
    :returns: e.g. 'cask:/gold/digital-dev/haeger/tree/quarter-mile/item.jsonld.json'.
    :rtype: str
    """
    dest = derivative.dest_path(
        type="gold", collection=item.collection, rel_path=record_rel_path(item), opts={"app": _project(opts)},
    )
    return f"cask:{dest}"


def read(item, opts={}):
    """Read an item's built record.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param dict opts: 'project' — see _project().
    :returns: The parsed item.jsonld.json.
    :rtype: dict
    :raises FileNotFoundError: If build() hasn't been run for the item yet.
    """
    return json.loads(fs.read(record_path(item, opts)))


def gold_location(ark, opts={}):
    """An item's location and gold record, read from gold only. The bronze
    folder comes from the record's manifest.json and the binary paths from
    its media nodes' contentUrls, so bronze is never read.

    :param str ark: Item ARK.
    :param dict opts: 'project' — see items.find_gold_record().
    :returns: (ItemLocation, record dict).
    :rtype: tuple
    :raises FileNotFoundError: If the item has no gold record or manifest.
    :raises LookupError: If the manifest names no bronze folder, or several records match.
    """
    path = items.find_gold_record(ark, opts)
    if not path:
        raise FileNotFoundError(f"{ark}: no gold item record")
    record = json.loads(fs.read(f"cask:{path}"))
    manifest_path = f"cask:{path.rsplit('/', 1)[0]}/manifest.json"
    bronze = json.loads(fs.read(manifest_path)).get("bronze")
    if not bronze:
        raise LookupError(f"{manifest_path} names no bronze folder for {ark}")
    group, collection, time, item_path = items._parse_bronze_folder(bronze)
    binaries = sorted({
        "cask:" + node["contentUrl"][len("cask:/"):]
        for node in record.get("@graph") or []
        if str(node.get("contentUrl", "")).startswith("cask://bronze/")
    })
    location = items.ItemLocation(
        ark=ark, group=group, collection=collection, time=time, item_path=item_path,
        bronze_binary_paths=binaries,
    )
    return location, record


def root_node(doc):
    """The item's own node in a built record's @graph.

    :param dict doc: A built item record (see build()).
    :returns: The root node, or {} if the graph has none.
    :rtype: dict
    """
    item_uri = _item_uri(doc.get("@id"))
    return next((node for node in doc.get("@graph") or [] if node.get("@id") == item_uri), {})


def _item_uri(ark):
    """The item's own node/document @id.

    :param str ark: The item's ark, e.g. 'ark:/87287/d7abc1234'.
    :rtype: str
    """
    return f"{ITEM_URI_PREFIX}{ark}"


def _frag(item_uri, *parts):
    """A synthetic graph node's @id, as a URI fragment off the item's own
    @id — so every @id in the document is a real absolute URI, and every
    synthetic node's identity is collision-proof per item.

    :param str item_uri: This item's own @id (see _item_uri()).
    :param str parts: Path segments, URI-encoded and joined with '/'.
    :rtype: str
    """
    return item_uri + "#" + "/".join(urllib.parse.quote(p, safe="") for p in parts)


def _identifiers_from_raw(raw_identifiers):
    """Bronze identifiers.json's raw [{'type', 'value'}, ...] entries as
    schema:PropertyValue identifier entries.

    :param list[dict] raw_identifiers: An item's identifiers.json content.
    :returns: [{"@type": "PropertyValue", "propertyID": str, "value": str}, ...]
    :rtype: list[dict]
    """
    out = []
    for entry in raw_identifiers:
        value = entry.get("value")
        if not value:
            continue
        property_id = entry.get("type") or ("ARK" if value.startswith("ark:/") else "identifier")
        out.append(Identifier(property_id=property_id, value=value).model_dump(by_alias=True))
    return out


def _root_identifiers(item):
    """An item's raw bronze identifiers.json, as schema:PropertyValue entries
    — the fallback used when bronze's own item.jsonld.json sidecar (if any)
    doesn't already carry its own `identifier` (see _seed_graph_from_bronze()
    and _merge_node()'s never-override-bronze rule). Preservica's own
    identifiers API (see digtk.models.collection._fetch_and_write_identifiers())
    is copied here verbatim, so any LocalID/box-folder id Preservica itself
    assigned the item already comes through this list with no extra handling.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :rtype: list[dict]
    """
    identifiers_path = f"cask:{_bronze_folder(item)}/identifiers.json"
    raw_identifiers = json.loads(fs.read(identifiers_path)) if fs.exists(identifiers_path) else []
    return _identifiers_from_raw(raw_identifiers)


def _bronze_folder(item):
    """The item's own bronze folder, as a CaskFS-absolute path (no 'cask:'
    dispatch prefix).

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :rtype: str
    """
    return f"{items.BRONZE_PREFIX}{item.group}/{item.collection}/{item.time}/{item.item_path}".rstrip("/")


def _preservica_identifier(item):
    """The item's Preservica entity ref, as a PropertyValue identifier entry
    — read from bronze's own tree metadata.json (see
    digtk.models.collection._copy_metadata_to_tree()), which is the only
    place this ref is recorded; Preservica's own identifiers.json (see
    _root_identifiers()) never carries it. Items from a non-Preservica
    source (e.g. a local/manual collection) have no such file, so this is
    just absent for them.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: The identifier entry, or None.
    :rtype: dict|None
    """
    metadata_path = f"cask:{_bronze_folder(item)}/metadata.json"
    if not fs.exists(metadata_path):
        return None
    metadata = json.loads(fs.read(metadata_path))
    ref = (metadata.get("preservica") or {}).get("ref")
    if not ref:
        return None
    return Identifier(property_id="Preservica", value=ref).model_dump(by_alias=True)


def _lineage_source_files(item):
    """The bronze files this record's build() actually read the content of
    — not every bronze binary path. A binary's path is only ever referenced
    (to build its contentUrl), never read, so recording it as a CaskFS
    lineage "source" of this derivative would be noise, not real provenance
    — and for a multi-hundred-page book item, noise multiplied by every page.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: Existing paths among {item.sidecar_path, bronze identifiers.json,
        bronze tree metadata.json}, in that order. Falls back to
        item.bronze_binary_paths if none of those exist, since
        derivative.write() requires at least one source.
    :rtype: list[str]
    """
    candidates = [
        item.sidecar_path,
        f"cask:{_bronze_folder(item)}/identifiers.json",
        f"cask:{_bronze_folder(item)}/metadata.json",
    ]
    sources = [path for path in candidates if path and fs.exists(path)]
    return sources or item.bronze_binary_paths


def _gold_path_identifier(item, rel_path, project):
    """This record's own CaskFS gold destination, as a PropertyValue
    identifier entry — lets a consumer of the public record resolve
    straight back to its canonical storage location.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str rel_path: build()'s own computed gold rel_path.
    :param str project: build()'s own resolved gold "app" segment.
    :rtype: dict
    """
    gold_path = derivative.dest_path(type="gold", collection=item.collection, rel_path=rel_path, opts={"app": project})
    gold_ref = pipeline.cask_public_ref(f"cask:{gold_path}")
    return Identifier(property_id="CaskGoldPath", value=gold_ref).model_dump(by_alias=True)


def _load_bronze_sidecar(item):
    """The item's bronze descriptive metadata, as a document to seed the
    graph from (see _seed_graph_from_bronze()).

    An item ingested with a bronze '<binary>.jsonld.json' sidecar (see
    digtk.commands.ingest, item.sidecar_path) adopts that sidecar as-is.
    Otherwise, when the bronze folder's metadata.json carries a descriptive
    metadata fragment (anything beyond its 'preservica' entity properties —
    see digtk.models.collection._copy_metadata_to_tree()), its Dublin Core is
    mapped to schema.org fields (see digtk.lib.dc_mapping), with the entity's
    own Preservica title as the name fallback.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: The descriptive document, or None if the item has neither.
    :rtype: dict|None
    """
    if item.sidecar_path and fs.exists(item.sidecar_path):
        return json.loads(fs.read(item.sidecar_path))

    metadata_path = f"cask:{_bronze_folder(item)}/metadata.json"
    content = json.loads(fs.read(metadata_path)) if fs.exists(metadata_path) else {}
    if not any(key != "preservica" for key in content):
        return None
    entity = content.get("preservica") or {}
    mapped = dc_mapping.map_dublin_core_to_schema(content, ark=item.ark, name_fallback=entity.get("title"))
    return ItemMetadata(
        id=item.ark,
        ark=item.ark,
        name=mapped.get("name"),
        description=mapped.get("description"),
        creator=mapped.get("creator"),
        date_created=mapped.get("dateCreated"),
        license=mapped.get("license"),
        publisher=mapped.get("publisher"),
        about=mapped.get("about", []),
        identifiers=[Identifier(property_id="ARK", value=item.ark)],
    ).to_jsonld()


def _seed_graph_from_bronze(bronze_doc, item_uri):
    """Bronze's item.jsonld.json as a seed {@id: node} map to merge computed
    nodes into (see _merge_node()) — bronze-seeded properties always win,
    only absent ones are ever added.

    Handles today's single-document shape (the whole doc IS the root node)
    the same way it would a future {"@graph": [...]} shape: the root node's
    own identity is normalized to the resolved item's real @id (not
    "content" subject to the never-override rule — it's the same item either
    way, just possibly addressed differently in bronze).

    :param dict|None bronze_doc: _load_bronze_sidecar()'s result.
    :param str item_uri: This item's own @id (see _item_uri()).
    :returns: {@id: node}, in bronze's own node order.
    :rtype: dict
    """
    if not bronze_doc:
        return {}
    graph = bronze_doc.get("@graph")
    nodes = graph if graph is not None else [bronze_doc]
    is_single_document = graph is None
    seed = {}
    for node in nodes:
        node = dict(node)
        if is_single_document or node.get("@id") == bronze_doc.get("@id"):
            node["@id"] = item_uri
        # "@context" is bronze's own *document*-level keyword, not a property
        # of the node it wraps — this module's output document carries its own
        # (RECORD_CONTEXT) at the top level instead. "argonath:fieldSources"
        # is DC-mapping provenance bookkeeping, not a public-record property.
        # Neither belongs in the output, however they arrived in bronze's own
        # stored document (and the "never override bronze" merge rule would
        # otherwise preserve them straight through).
        node.pop("@context", None)
        node.pop("argonath:fieldSources", None)
        if node.get("@id"):
            seed[node["@id"]] = node
    return seed


def _merge_node(graph_by_id, order, node):
    """Add `node` into the graph being assembled: a brand-new @id is appended
    as-is; an existing @id (from the bronze seed, or an earlier merge) only
    gains properties it doesn't already have — never replaced.

    :param dict graph_by_id: {@id: node}, mutated in place.
    :param list[str] order: @id insertion order, mutated in place.
    :param dict node: The computed node to merge in.
    """
    existing = graph_by_id.get(node["@id"])
    if existing is None:
        graph_by_id[node["@id"]] = dict(node)
        order.append(node["@id"])
        return
    for key, value in node.items():
        if key != "@id":
            existing.setdefault(key, value)


def _merge_identifiers(bronze_identifiers, computed_identifiers):
    """Union bronze's own `identifier` entries with build()'s own computed
    ones, bronze's own always first and unchanged.

    Unlike every other property (see _merge_node()'s all-or-nothing
    never-override-bronze rule), `identifier` is merged rather than replaced
    outright: bronze's descriptive seed typically carries only a single ARK
    entry (see _load_bronze_sidecar()),
    which would otherwise hide every other identifier build() computes
    (LocalID, Preservica ref, gold path — see _root_identifiers()/
    _preservica_identifier()/_gold_path_identifier()).

    :param list[dict] bronze_identifiers: Bronze-seeded identifier entries.
    :param list[dict] computed_identifiers: build()'s own computed entries.
    :returns: bronze_identifiers, plus any computed entry not already present
        (matched by (propertyID, value)).
    :rtype: list[dict]
    """
    seen = {(entry.get("propertyID"), entry.get("value")) for entry in bronze_identifiers}
    merged = list(bronze_identifiers)
    for entry in computed_identifiers:
        key = (entry.get("propertyID"), entry.get("value"))
        if key not in seen:
            seen.add(key)
            merged.append(entry)
    return merged


def _collection_ark_or_raise(item):
    """The item's collection's own ark, for isPartOf — or a clear failure if
    none is resolvable yet.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :rtype: str
    :raises LookupError: See digtk.models.collection.get_ark().
    """
    try:
        return collection.get_ark(item.collection, item.group)
    except LookupError as e:
        raise LookupError(f"{items.item_id(item)}: cannot build isPartOf — {e}") from e


def _bronze_content_url(filename, bronze_by_filename):
    """The cask public ref URL to a bronze source file — its preservation
    master — for node types whose `contentUrl` should point straight at the
    original binary rather than any generated derivative/rendition.

    :param str filename: The bronze source filename.
    :param dict bronze_by_filename: {filename: 'cask:/...' bronze path}.
    :returns: The cask public ref URL, or None if no bronze file is found.
    :rtype: str|None
    """
    bronze_path = bronze_by_filename.get(filename)
    return pipeline.cask_public_ref(bronze_path) if bronze_path else None


def _pdf_node(item_uri, filename, bronze_by_filename):
    """One minimal PDF graph node: its bronze file's own content url, and
    nothing else — the generated per-page image renditions aren't
    independently addressable things, so none of their metadata is kept here.

    :param str item_uri: This item's own @id.
    :param str filename: The bronze PDF filename.
    :param dict bronze_by_filename: {filename: 'cask:/...' bronze path}.
    :rtype: dict
    """
    node = {"@id": _frag(item_uri, "media", filename)}
    content_url = _bronze_content_url(filename, bronze_by_filename)
    if content_url:
        node["contentUrl"] = content_url
    return node


def _alt_text_doc(item, filename, multi_image, page_no=None):
    """An image's AI-generated alt-text derivative, if accessibility.py has
    already published one — mirrors accessibility.generate_alt_text()'s own
    path convention exactly (its multi_image flag and prep.output_name()
    naming), since a mismatch here would just silently find nothing.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str filename: The bronze source filename.
    :param bool multi_image: Whether this item has more than one image binary.
    :param int page_no: Unused for images (kept for a future per-page PDF
        model — see this module's docstring on PDF alt text being deferred).
    :returns: {"alt": str, "description": str|None, "flags": [str]}, or None.
    :rtype: dict|None
    """
    out_filename = pipeline.output_name(os.path.splitext(filename)[0], "alt-text.json", multi_image)
    path = f"{items.dest_dir(item, 'accessibility')}/{out_filename}"
    try:
        return json.loads(fs.read(path))
    except FileNotFoundError:
        return None


def _apply_alt_text(node, alt_doc):
    """Fold an alt-text derivative onto an image node, using schema.org's own
    accessibility terms rather than inventing new ones.

    :param dict node: An ImageObject graph node, mutated in place.
    :param dict|None alt_doc: _alt_text_doc()'s result.
    """
    if not alt_doc:
        return
    if alt_doc.get("alt"):
        node["accessibilitySummary"] = alt_doc["alt"]
    if alt_doc.get("description"):
        node["description"] = alt_doc["description"]
    if alt_doc.get("flags"):
        node["accessibilityFeature"] = alt_doc["flags"]


def _image_node(item_uri, namespace, filename, bronze_by_filename, position=None):
    """One minimal image graph node: its bronze preservation master's content
    url, and its position when it's an ImageList child — no @type, no
    rendition/technical metadata, no lineage pointer. Accessibility data (see
    _apply_alt_text()) is the one addition left on top of this, applied by
    the caller.

    :param str item_uri: This item's own @id.
    :param str namespace: The @id fragment namespace ('media' for a lone
        image, 'imagelist' for an ImageList child).
    :param str filename: The bronze source filename.
    :param dict bronze_by_filename: {filename: 'cask:/...' bronze path}.
    :param int position: This image's position within its ImageList, if any.
    :rtype: dict
    """
    node = {"@id": _frag(item_uri, namespace, filename)}
    content_url = _bronze_content_url(filename, bronze_by_filename)
    if content_url:
        node["contentUrl"] = content_url
    if position is not None:
        node["position"] = position
    return node


def _image_nodes(item, item_uri, filenames, bronze_by_filename):
    """Build this item's image-side graph node(s): a single minimal image
    node for one image, or an ImageList + its hasPart children for several.

    Each child's ordering is parsed from its filename's trailing `_\\d+`
    suffix (schema:position — see pipeline.parse_sequence_number()); if any
    filename in the group has no parseable number, every child falls back to
    stable enumeration order instead, rather than guessing a partial order.
    If two filenames parse to the same number, children keep the parsed
    order but are numbered 1..n so positions stay unique.
    The root item's own schema:image is the lowest-position child.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str item_uri: This item's own @id.
    :param list[str] filenames: Bronze filenames in the 'image' source group.
    :param dict bronze_by_filename: {filename: 'cask:/...' bronze path}.
    :returns: (top_node, child_nodes, root_image_ref) — child_nodes is []
        for a single image (top_node itself is then the only image node).
    :rtype: tuple[dict, list[dict], dict]
    """
    filenames = list(filenames)

    if len(filenames) == 1:
        filename = filenames[0]
        node = _image_node(item_uri, "media", filename, bronze_by_filename)
        _apply_alt_text(node, _alt_text_doc(item, filename, multi_image=False))
        return node, [], {"@id": node["@id"]}

    parsed = {f: pipeline.parse_sequence_number(f) for f in filenames}
    if all(v is not None for v in parsed.values()):
        ordered = sorted(filenames, key=lambda f: parsed[f])
        positions = {f: parsed[f] for f in filenames}
        if len(set(parsed.values())) < len(filenames):
            # duplicate numbers, so number in order
            positions = {f: i for i, f in enumerate(ordered, start=1)}
    else:
        ordered = filenames
        positions = {f: i for i, f in enumerate(filenames, start=1)}

    children = []
    for filename in ordered:
        child = _image_node(item_uri, "imagelist", filename, bronze_by_filename, position=positions[filename])
        _apply_alt_text(child, _alt_text_doc(item, filename, multi_image=True))
        children.append(child)

    imagelist = {
        "@id": _frag(item_uri, "imagelist"),
        "@type": ["schema:MediaObject", pipeline.IMAGE_LIST_TYPE],
        "hasPart": [{"@id": c["@id"]} for c in children],
    }
    return imagelist, children, {"@id": children[0]["@id"]}


def _group_bronze_by_format(bronze_by_filename, rules):
    """Group an item's bronze filenames by associatedMedia format.

    :param dict bronze_by_filename: {filename: 'cask:/...' bronze path}.
    :param dict rules: ASSOCIATED_MEDIA_RULES — {filename: format} overrides,
        consulted before falling back to pipeline.FORMAT_BY_EXTENSION.
        Currently always empty.
    :returns: {format: [filename, ...]}, filenames sorted within each format.
        Only "image"/"pdf"/"video"/"audio" are included; anything else (or an
        unrecognized extension) is silently left out.
    :rtype: dict
    """
    groups = {}
    for filename in bronze_by_filename:
        fmt = rules.get(filename) or pipeline.FORMAT_BY_EXTENSION.get(os.path.splitext(filename)[1].lower())
        if fmt in ("image", "pdf", "video", "audio"):
            groups.setdefault(fmt, []).append(filename)
    return {fmt: sorted(files) for fmt, files in groups.items()}


def _av_node(item_uri, group, filename, bronze_by_filename):
    """One minimal audio/video graph node: its bronze file's own content url,
    and nothing else — same bare shape as _image_node()/_pdf_node().

    :param str item_uri: This item's own @id.
    :param str group: 'video' or 'audio'.
    :param str filename: The bronze source filename.
    :param dict bronze_by_filename: {filename: 'cask:/...' bronze path}.
    :rtype: dict
    """
    node = {
        "@id": _frag(item_uri, "media", filename),
        "@type": _TYPE_BY_GROUP[group],
        "argonath:sourceGroup": group,
    }
    content_url = _bronze_content_url(filename, bronze_by_filename)
    if content_url:
        node["contentUrl"] = content_url
    return node


def _build_media_graph(item, item_uri):
    """Build this item's whole media side of the graph: one node per bronze
    source file (video/audio/pdf), the image-side node(s) (rule 7), and the
    image<->pdf relationship (rule 4/5) — all driven directly by
    item.bronze_binary_paths, grouped via _group_bronze_by_format().

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str item_uri: This item's own @id.
    :returns: (root_media_refs, root_image_ref, extra_nodes) — root_media_refs
        is this item's own associatedMedia (as {"@id": ...} refs, in
        video/audio/image-or-imagelist/pdf order); root_image_ref is
        {"@id": ...} for the item's schema:image, or None; extra_nodes are
        every media/ImageList node to add to the graph.
    :rtype: tuple[list[dict], dict|None, list[dict]]
    """
    bronze_by_filename = {
        path.rsplit("/", 1)[-1]: path
        for path in pipeline.drop_access_duplicates(item.bronze_binary_paths)
    }
    groups = _group_bronze_by_format(bronze_by_filename, ASSOCIATED_MEDIA_RULES)

    root_refs = []
    extra_nodes = []

    for group in ("video", "audio"):
        for filename in groups.get(group) or []:
            node = _av_node(item_uri, group, filename, bronze_by_filename)
            extra_nodes.append(node)
            root_refs.append({"@id": node["@id"]})

    images_top_node = None
    root_image_ref = None
    image_filenames = groups.get("image") or []
    if image_filenames:
        images_top_node, children, root_image_ref = _image_nodes(item, item_uri, image_filenames, bronze_by_filename)
        extra_nodes.append(images_top_node)
        extra_nodes.extend(children)
        root_refs.append({"@id": images_top_node["@id"]})

    pdf_top_node = None
    for filename in groups.get("pdf") or []:
        pdf_top_node = _pdf_node(item_uri, filename, bronze_by_filename)
        extra_nodes.append(pdf_top_node)
        root_refs.append({"@id": pdf_top_node["@id"]})

    if images_top_node and pdf_top_node:
        # Rule 4: the image side links to the pdf; the pdf stays a normal
        # root associatedMedia entry too (additive — see the approved plan's
        # design note), consistent with how video/audio/image/pdf entries
        # all otherwise coexist in root associatedMedia regardless of group.
        images_top_node["associatedMedia"] = [{"@id": pdf_top_node["@id"]}]

    return root_refs, root_image_ref, extra_nodes


def build(target, opts={}):
    """Assemble and write an item's complete, canonical item.jsonld.json to
    gold, plus the gold manifest.json recording the bronze folder it was built
    from.

    Reads only from bronze (its descriptive sidecar, identifiers.json, tree
    metadata.json, and binary paths) plus the accessibility alt-text
    derivative — never from silver — and writes the finished record to gold,
    promoted under a project/app segment (see
    digtk.lib.derivative.dest_path(type="gold")). Since bronze is
    immutable/append-only and never written here, build() is idempotent by
    construction: re-running it with no change to bronze always reproduces
    the exact same gold output. The gold write's own CaskFS lineage
    (source_files) is scoped to just the bronze files actually read (see
    _lineage_source_files()), not every bronze binary path.

    The returned document's own top-level `@id` is the item's bare ark (e.g.
    "ark:/87287/d7abc1234") — matching the ark-as-document-@id convention
    used by bronze '<binary>.jsonld.json' sidecars —
    distinct from the item node's own `@id` within `@graph`, which is its
    full https://digital.ucdavis.edu/item/<ark> URI (see ITEM_URI_PREFIX).

    The root node's `identifier` array always includes every raw bronze
    identifiers.json entry (see _root_identifiers()), a Preservica entity
    ref if bronze's tree metadata.json records one (see
    _preservica_identifier()), and this record's own CaskFS gold path (see
    _gold_path_identifier()). Unlike every other property, this holds even
    when bronze's descriptive seed already carries its own `identifier`
    array (typically just a single ARK entry — see _load_bronze_sidecar()):
    bronze's own entries
    are kept as-is and never reordered or dropped, but build()'s own computed
    entries are still unioned in (see _merge_identifiers()) rather than
    being hidden by the usual never-override-bronze rule (_merge_node()).

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'project' (str, default CONFIG["showcase"]["gold_project"]
        i.e. "digital-dev") — the gold "app" segment this record is promoted
        under; forwarded to digtk.lib.items.resolve_target() otherwise.
    :returns: The written document.
    :rtype: dict
    :raises LookupError: If the item has no ark, or its collection has none
        (see digtk.models.collection.get_ark()).
    """
    item = items.resolve_target(target, opts)
    if not item.ark:
        raise LookupError(f"{items.item_id(item)} has no ark — cannot build its item record")
    item_uri = _item_uri(item.ark)
    collection_ark = _collection_ark_or_raise(item)
    project = _project(opts)

    graph_by_id = {}
    order = []
    for node_id, node in _seed_graph_from_bronze(_load_bronze_sidecar(item), item_uri).items():
        graph_by_id[node_id] = node
        order.append(node_id)

    rel_path = record_rel_path(item)

    root_media_refs, root_image_ref, media_nodes = _build_media_graph(item, item_uri)

    root = {
        "@id": item_uri, "ark": item.ark,
        "isPartOf": {"@id": f"{COLLECTION_URI_PREFIX}{collection_ark}"},
        "name": item.item_path.rsplit("/", 1)[-1] if item.item_path else item.collection,
        "associatedMedia": root_media_refs,
    }
    identifiers = _root_identifiers(item)
    preservica_identifier = _preservica_identifier(item)
    if preservica_identifier:
        identifiers.append(preservica_identifier)
    identifiers.append(_gold_path_identifier(item, rel_path, project))

    bronze_root = graph_by_id.get(item_uri)
    if bronze_root and isinstance(bronze_root.get("identifier"), list):
        bronze_root["identifier"] = _merge_identifiers(bronze_root["identifier"], identifiers)
    else:
        root["identifier"] = identifiers
    if root_image_ref:
        root["image"] = root_image_ref

    _merge_node(graph_by_id, order, root)
    for node in media_nodes:
        _merge_node(graph_by_id, order, node)

    doc = {"@context": RECORD_CONTEXT, "@id": item.ark, "@graph": [graph_by_id[node_id] for node_id in order]}

    derivative.write(
        type="gold", collection=item.collection, rel_path=rel_path,
        data=json.dumps(doc, indent=2).encode(), source_files=_lineage_source_files(item),
        opts={"app": project, "replace": True},
    )
    folder = _bronze_folder(item)
    utils.write_manifest(
        item.collection, item.item_path,
        ["digtk", "showcase", "build-item", folder], {}, [items.ITEM_RECORD_FILENAME],
        source_files=_lineage_source_files(item), type="gold",
        opts={"app": project, "extra": {"bronze": folder}},
    )
    return doc
