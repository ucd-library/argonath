"""
Build an item's complete, canonical item.jsonld.json.

Unlike digtk.models.digital_website.pipeline.update_associated_media()/
merge_dc_metadata() (which each independently merge their own slice of the
sidecar — associatedMedia, or DC-mapped descriptive fields — leaving the rest
alone), this module is the one place that assembles the *whole* document in
one pass: whatever descriptive metadata/identifiers bronze already resolved,
plus a freshly rebuilt associatedMedia/hasPart structure, written as a full
replace. It's meant to run once derivative generation/finalize are done, as
its own pipeline step (see dagster/lib/pipeline_item.py's generate_item_record
op and dagster/lib/pipeline_fanout.py's build_item_record()).

Unlike every other step in this pipeline, this one is allowed to simply fail
for an item with no ark — the canonical public record needs a stable
identifier, and there's no fallback here (see digtk.lib.items's "ark is a
sidecar, not a requirement" convention for every *other* step).

isPartOf (collection linking) is deliberately omitted, same as
digtk.lib.showcase_index.build_document() — there's no collection-level ark to
link to until collection ark-minting exists.
"""
import json

from digtk.lib import derivative, fs, items
from digtk.lib.schema import SCHEMA_CONTEXT, Identifier
from digtk.models.digital_website import pipeline


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


def _base_document(item):
    """The item's descriptive-fields/identifiers baseline: bronze's own
    item.jsonld.json if preservica_tree() already wrote one (adopts whatever
    DC-mapped fields it resolved — see digtk.models.collection's
    _write_tree_item_jsonld()), else a minimal document built from
    identifiers.json alone.

    Deliberately does not redo Preservica DC-fragment extraction/mapping
    itself — that stays _write_tree_item_jsonld()'s job (a bronze-build-time,
    Preservica-specific concern); if that gate didn't pass (no metadata
    fragment found), descriptive fields are simply absent here too.

    :param digtk.lib.items.ItemLocation item: Resolved item location (ark required).
    :returns: A document with at least @context/@id/ark/name set.
    :rtype: dict
    """
    if item.sidecar_path and item.sidecar_path.endswith(f"/{items.ITEM_SIDECAR_FILENAME}") and fs.exists(item.sidecar_path):
        doc = json.loads(fs.read(item.sidecar_path))
        doc["@context"] = doc.get("@context") or SCHEMA_CONTEXT
        doc["@id"] = item.ark
        doc["ark"] = item.ark
        return doc

    bronze_folder = f"{items.BRONZE_PREFIX}{item.group}/{item.collection}/{item.time}/{item.item_path}".rstrip("/")
    identifiers_path = f"cask:{bronze_folder}/identifiers.json"
    raw_identifiers = json.loads(fs.read(identifiers_path)) if fs.exists(identifiers_path) else []

    doc = {
        "@context": SCHEMA_CONTEXT, "@id": item.ark, "ark": item.ark,
        "name": item.item_path.rsplit("/", 1)[-1] if item.item_path else item.collection,
    }
    identifiers = _identifiers_from_raw(raw_identifiers)
    if identifiers:
        doc["identifier"] = identifiers
    return doc


def _with_bronze_source(entries, filename, bronze_by_filename):
    """Stamp each media entry with `isBasedOn` pointing at its bronze
    preservation original, when one is found by filename.

    :param list[dict] entries: associatedMedia entries for one source file.
    :param str filename: That source file's bronze filename.
    :param dict bronze_by_filename: {filename: 'cask:/...' bronze path}.
    :returns: `entries`, each with `isBasedOn` added when a bronze match exists.
    :rtype: list[dict]
    """
    bronze_path = bronze_by_filename.get(filename)
    if not bronze_path:
        return entries
    ref = pipeline.cask_public_ref(bronze_path)
    return [{**entry, "isBasedOn": ref} for entry in entries]


def _build_media(item, silver_doc):
    """Rebuild associatedMedia/hasPart from the item's currently-published
    silver derivatives, grouped by source — a single-file source (the common
    case for pdf/video/audio, or a lone image) becomes a flat associatedMedia
    entry; a multi-file source (e.g. a multi-page scanned item) becomes a
    hasPart entry per file, each one tagged with its page position and
    (via _with_bronze_source()) traceable back to its bronze preservation
    original, not just its silver derivative.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param dict silver_doc: The item's current silver sidecar, if any (read
        for its already-published associatedMedia — itself built by reading
        silver derivative manifests, during each format's derivatives()/
        finalize() run) — {} for an item with no derivatives published yet.
    :returns: (associated_media, has_part) — has_part is [] when every source
        group has exactly one file.
    :rtype: tuple[list[dict], list[dict]]
    """
    flat = silver_doc.get("associatedMedia") or []
    if not isinstance(flat, list):
        flat = [flat]
    grouped = pipeline.group_media_by_source(flat)
    bronze_by_filename = {
        path.rsplit("/", 1)[-1]: path
        for path in pipeline.drop_access_duplicates(item.bronze_binary_paths)
    }

    associated_media = []
    has_part = []
    for group, files in grouped.items():
        if len(files) == 1:
            (filename, entries), = files.items()
            associated_media.extend(_with_bronze_source(entries, filename, bronze_by_filename))
            continue
        for position, (filename, entries) in enumerate(files.items(), start=1):
            has_part.append({
                "argonath:sourceGroup": group,
                "position": position,
                "associatedMedia": _with_bronze_source(entries, filename, bronze_by_filename),
            })
    return associated_media, has_part


def build(target, opts={}):
    """Assemble and write an item's complete, canonical item.jsonld.json.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: Unused; present for signature symmetry.
    :returns: The written document.
    :rtype: dict
    :raises LookupError: If the item has no ark.
    """
    item = items.resolve_target(target, opts)
    if not item.ark:
        raise LookupError(f"{items.item_id(item)} has no ark — cannot build its item record")

    doc = _base_document(item)

    rel_path = f"{item.item_path}/item.jsonld.json" if item.item_path else "item.jsonld.json"
    write_opts = {"group": item.group, "time": item.time, "replace": True}
    dest = derivative.dest_path(type="silver", collection=item.collection, rel_path=rel_path, opts=write_opts)
    sidecar_path = f"cask:{dest}"
    silver_doc = json.loads(fs.read(sidecar_path)) if fs.exists(sidecar_path) else {}

    associated_media, has_part = _build_media(item, silver_doc)
    doc["associatedMedia"] = associated_media
    if has_part:
        doc["hasPart"] = has_part
    if silver_doc.get("argonath:fieldSources"):
        doc["argonath:fieldSources"] = silver_doc["argonath:fieldSources"]

    derivative.write(
        type="silver", collection=item.collection, rel_path=rel_path,
        data=json.dumps(doc, indent=2).encode(), source_files=item.bronze_binary_paths,
        opts=write_opts,
    )
    return doc
