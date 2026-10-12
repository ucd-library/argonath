"""
Build an item's complete, canonical item.jsonld.json, in gold.

This is the only place an item.jsonld.json is written: bronze never gets one,
and the derivative steps (image/pdf/video/audio/captions) only publish their
own files and manifests into silver. build() assembles the whole document in
one pass and writes it as a full replace:

- descriptive fields (name/description/creator/...) from the item's bronze
  sidecar, or mapped from the bronze tree folder's metadata.json Dublin Core
  (see digtk.lib.dc_mapping), or a minimal document built from
  identifiers.json when there's neither;
- associatedMedia/hasPart rebuilt from the item's published silver
  derivative manifests (see each format module's media_objects()).

It's meant to run once derivative generation/finalize are done, as its own
pipeline step (see dagster/lib/pipeline_item.py's pdc_generate_item_record op
and dagster/lib/pipeline_fanout.py's build_item_record()).

The record is written to gold/[group]/[collection]/[item_path]/item.jsonld.json,
next to a manifest.json recording the bronze folder it was built from — gold
has no time dimension, so that manifest is what digtk.lib.items.resolve() reads
to get from an ARK back to the item's bronze location.

Unlike every other step in this pipeline, this one is allowed to simply fail
for an item with no ark — the canonical public record needs a stable
identifier, and there's no fallback here (see digtk.lib.items's "ark is a
sidecar, not a requirement" convention for every *other* step).

isPartOf (collection linking) is deliberately omitted, same as
digtk.lib.showcase_index.build_document() — there's no collection-level ark to
link to until collection ark-minting exists.
"""
import json

from digtk.lib import dc_mapping, derivative, fs, items, utils
from digtk.lib.schema import SCHEMA_CONTEXT, Identifier, ItemMetadata
from digtk.models.digital_website import audio, captions, image, pdf, pipeline, video

# Each format's associatedMedia reader, in the order its entries are written.
_MEDIA_READERS = {
    "image": image.media_objects,
    "pdf": pdf.media_objects,
    "video": video.media_objects,
    "audio": audio.media_objects,
}


def _bronze_folder(item):
    """The item's bronze folder.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: e.g. '/bronze/preservica/haeger/latest/tree/quarter-mile'.
    :rtype: str
    """
    return f"{items.BRONZE_PREFIX}{item.group}/{item.collection}/{item.time}/{item.item_path}".rstrip("/")


def record_rel_path(item):
    """The item record's path relative to its gold collection root.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: e.g. 'tree/quarter-mile/item.jsonld.json'.
    :rtype: str
    """
    return f"{item.item_path}/{items.ITEM_RECORD_FILENAME}" if item.item_path else items.ITEM_RECORD_FILENAME


def record_path(item):
    """The item record's CaskFS path.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: e.g. 'cask:/gold/preservica/haeger/tree/quarter-mile/item.jsonld.json'.
    :rtype: str
    """
    dest = derivative.dest_path(
        type="gold", collection=item.collection, rel_path=record_rel_path(item), opts={"group": item.group},
    )
    return f"cask:{dest}"


def read(item):
    """Read an item's built record.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: The parsed item.jsonld.json.
    :rtype: dict
    :raises FileNotFoundError: If build() hasn't been run for the item yet.
    """
    return json.loads(fs.read(record_path(item)))


def flat_media(doc):
    """Every associatedMedia entry in a built record, including each hasPart
    entry's own, as one flat list.

    :param dict doc: A built item record (see build()).
    :returns: associatedMedia entries.
    :rtype: list[dict]
    """
    media = list(doc.get("associatedMedia") or [])
    for part in doc.get("hasPart") or []:
        media += part.get("associatedMedia") or []
    return media


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


def _read_bronze_json(path, default):
    """Read a bronze JSON file, or `default` if it doesn't exist.

    :param str path: 'cask:/bronze/...' path.
    :param default: Returned when the file is missing.
    :returns: Parsed JSON, or `default`.
    """
    return json.loads(fs.read(path)) if fs.exists(path) else default


def _base_document(item):
    """The item's descriptive-fields/identifiers baseline.

    An item ingested with a bronze '<binary>.jsonld.json' sidecar (see
    digtk.commands.ingest) adopts that sidecar's fields as-is. Otherwise, when
    the bronze folder's metadata.json carries a descriptive metadata
    fragment (anything beyond its 'preservica' entity properties — see
    digtk.models.collection._copy_metadata_to_tree()), its Dublin Core is
    mapped to schema.org fields (see digtk.lib.dc_mapping), with the entity's
    own Preservica title as the name fallback. Otherwise it's a minimal
    document built from identifiers.json alone.

    :param digtk.lib.items.ItemLocation item: Resolved item location (ark required).
    :returns: A document with at least @context/@id/ark/name set.
    :rtype: dict
    """
    if item.sidecar_path:
        doc = json.loads(fs.read(item.sidecar_path))
        return {**doc, "@context": doc.get("@context") or SCHEMA_CONTEXT, "@id": item.ark, "ark": item.ark}

    folder = _bronze_folder(item)
    content = _read_bronze_json(f"cask:{folder}/metadata.json", {})
    entity = content.get("preservica") or {}
    if any(key != "preservica" for key in content):
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
            field_sources={key: "human" for key in mapped},
        ).to_jsonld()

    doc = {
        "@context": SCHEMA_CONTEXT, "@id": item.ark, "ark": item.ark,
        "name": item.item_path.rsplit("/", 1)[-1] if item.item_path else item.collection,
    }
    identifiers = _identifiers_from_raw(_read_bronze_json(f"cask:{folder}/identifiers.json", []))
    if identifiers:
        doc["identifier"] = identifiers
    return doc


def _collect_media(item):
    """Every associatedMedia entry for the item's formats, each stamped with
    the "argonath:sourceGroup" it was built for, with captions attached to
    their video/audio entries.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: (entries, captioned) — captioned is True if any caption was attached.
    :rtype: tuple[list[dict], bool]
    :raises FileNotFoundError: If a format's derivatives haven't been published.
    """
    formats = pipeline.detect_formats(item.bronze_binary_paths)
    entries = []
    captioned = False
    for group, reader in _MEDIA_READERS.items():
        if group not in formats:
            continue
        objects = reader(item)
        if group in ("video", "audio"):
            captioned = captions.attach_captions(item, objects) or captioned
        for entry in objects:
            entry["argonath:sourceGroup"] = group
        entries += objects
    return entries, captioned


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


def _build_media(item, flat):
    """Arrange the item's associatedMedia entries by source — a single-file
    source (the common case for pdf/video/audio, or a lone image) becomes a
    flat associatedMedia entry; a multi-file source (e.g. a multi-page scanned
    item) becomes a hasPart entry per file, each one tagged with its page
    position and (via _with_bronze_source()) traceable back to its bronze
    preservation original, not just its silver derivative.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param list[dict] flat: Every associatedMedia entry (see _collect_media()).
    :returns: (associated_media, has_part) — has_part is [] when every source
        group has exactly one file.
    :rtype: tuple[list[dict], list[dict]]
    """
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
    """Assemble and write an item's complete, canonical item.jsonld.json to
    gold, plus the gold manifest.json recording the bronze folder it was built
    from.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: Unused; present for signature symmetry.
    :returns: The written document.
    :rtype: dict
    :raises LookupError: If the item has no ark.
    :raises FileNotFoundError: If a format's derivatives haven't been published.
    """
    item = items.resolve_target(target, opts)
    if not item.ark:
        raise LookupError(f"{items.item_id(item)} has no ark — cannot build its item record")

    doc = _base_document(item)
    flat, captioned = _collect_media(item)
    associated_media, has_part = _build_media(item, flat)
    doc["associatedMedia"] = associated_media
    if has_part:
        doc["hasPart"] = has_part
    if captioned:
        doc.setdefault("argonath:fieldSources", {})["caption"] = "ai"

    write_opts = {"group": item.group, "replace": True}
    derivative.write(
        type="gold", collection=item.collection, rel_path=record_rel_path(item),
        data=json.dumps(doc, indent=2).encode(), source_files=item.bronze_binary_paths,
        opts=write_opts,
    )
    folder = _bronze_folder(item)
    utils.write_manifest(
        item.collection, item.item_path,
        ["digtk", "showcase", "build-item", folder], {}, [items.ITEM_RECORD_FILENAME],
        source_files=item.bronze_binary_paths, type="gold",
        opts={"group": item.group, "extra": {"bronze": folder}},
    )
    return doc
