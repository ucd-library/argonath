"""
Indexes an item's schema.org metadata into the DAMS website's Elasticsearch
index - the same index fin's dbsync populates today, read by the digital
library website's /api/item and /api/collection endpoints (see
dams/services/fin/models/item/{model,transform}.js in the dams repo - this
module mirrors the {"@id", "@graph": [...]} document envelope transform.js
produces, so the website's existing search/detail lookups work unmodified
once an item is indexed here).

Deliberately unrelated to digtk.commands.search (LlamaIndex/pgvector RAG
search) - different index, different consumer, different purpose. Kept as
its own module/command group rather than folded into `search` so the two
don't get conflated - see dagster/lib/assets.py's index_for_dams_search
docstring for the same reasoning on the Dagster side.

ES connection settings are plain env vars here rather than going through
digtk.lib.config's central SETTINGS registry - this is new/experimental, and
migrating it into the shared config system can happen once the shape of what
this needs settles.
"""
import json
import os
import urllib.parse

import httpx

from digtk.lib import items, fs
from digtk.models.digital_website import pipeline

ES_HOST_ENV = "DAMS_ES_HOST"
ES_INDEX_ENV = "DAMS_ES_INDEX"

ES_HOST = os.environ.get(ES_HOST_ENV, "http://elasticsearch:9200")

# fin's ElasticSearchModel base class (fin/services/fin/node-utils/lib/data-models/
# elastic-search/index.js:19-20) writes through a "-write" alias and reads through
# a "-read" alias - normally both point at the same underlying index, but diverge
# during a reindex/mapping-change cutover (see fin's es-index-management service).
# This indexer is a write path, so it targets item-write, matching dbsync.
ES_INDEX = os.environ.get(ES_INDEX_ENV, "item-write")

# custom DAMS vocabulary type (dams/services/fin/ucd-lib-client/client/public/lib/
# utils/client-media/definition.js:25) marking a multi-page/scanned item - triggers
# the client's "imagelist" display mode, which crawls hasPart for page content
# instead of reading clientMedia off the root node directly.
IMAGE_LIST_TYPE = "http://digital.ucdavis.edu/schema#ImageList"


def _load_bronze_sidecar(sidecar_path):
    """Reads and parses the item's bronze JSON-LD sidecar - the original
    ingested metadata (name, description, creator, etc.), immutable/append-only.

    :param str sidecar_path: A 'cask:/....jsonld.json' path, as returned in
        ItemLocation.sidecar_path.
    :returns: Parsed JSON-LD.
    :rtype: dict
    """
    raw = fs.read(sidecar_path)
    return json.loads(raw)


def _load_derivative_sidecar(location):
    """Reads the item's silver item.jsonld.json - NOT the bronze sidecar.

    associatedMedia is deliberately never written to the bronze sidecar
    (bronze is immutable/append-only) - it lives in this separate silver
    file instead, written by pipeline.update_associated_media(). Reuses
    pipeline.derivative_dir() to build the exact same path that function
    writes to, rather than duplicating the convention here (its
    rel_path = f"{item.item_path}/{subdir}" matches
    update_associated_media()'s own rel_path exactly when subdir is
    "item.jsonld.json").

    :param digtk.lib.items.ItemLocation location:
    :returns: Parsed JSON-LD, or {} if derivatives haven't been generated yet.
    :rtype: dict
    """
    sidecar_path = pipeline.derivative_dir(location, "item.jsonld.json")
    try:
        raw = fs.read(sidecar_path)
    except FileNotFoundError:
        return {}
    return json.loads(raw)


def _normalize_type(type_value, default):
    """Normalizes a JSON-LD @type value to a list.

    dams's client code (RecordGraph.js, client-media/model.js) always calls
    .includes()/.map() on a node's @type, so it must be an array - never a
    bare string, which is what a naive sidecar.get("@type") read would
    produce for a single-valued type.

    :param type_value: Bronze sidecar's raw @type value (str, list, or None).
    :param str default: Fallback type if type_value is empty.
    :returns: @type as a list.
    :rtype: list
    """
    if not type_value:
        return [default]
    if isinstance(type_value, list):
        return type_value
    return [type_value]


# An item can legitimately have both a compiled PDF and individual page
# scans as separate bronze binaries (see pipeline.update_associated_media()'s
# docstring) - each media-generation pipeline stamps its own
# "argonath:sourceGroup" on the entries it writes, and they coexist in one
# flat associatedMedia list rather than overwriting each other. When both are
# present, the image (raw scan) group takes precedence over the pdf
# (rasterized) group for building the page list - confirmed with the user
# after real yearbook data showed both groups present at once, which without
# this precedence double-counted every page (256 instead of 128).
MEDIA_GROUP_PRECEDENCE = ["image", "pdf"]


def _group_media_by_page(associated_media):
    """Groups a flat associatedMedia list by the source filename embedded in
    each entry's contentUrl.

    image.py's generate() publishes each source image's derivatives under its
    own .../images/{filename}/images/{label}.jpg directory (see
    pipeline.PUBLIC_FRONTEND_DIR), but accumulates every source's ImageObject
    entries into one flat associatedMedia list on the item's silver sidecar
    (pipeline.update_associated_media() merges by source_group, not by
    source file) - so a multi-binary item's pages have to be reconstructed by
    grouping on that embedded filename segment.

    First selects one source_group per MEDIA_GROUP_PRECEDENCE (image wins
    over pdf when both are present), then groups that group's entries by
    filename - see MEDIA_GROUP_PRECEDENCE's comment for why.

    ASSUMPTION: page order is derived by sorting group keys (filenames)
    alphabetically - nothing in the sidecar records an explicit page order.
    Correct for zero-padded sequential scan filenames (e.g. page-001.jpg,
    page-002.jpg, a common convention), but not verified against a real
    multi-page item end to end.

    :param list associated_media: Flat associatedMedia list.
    :returns: {filename: [entries]}, ordered by sorted filename.
    :rtype: dict
    """
    by_source_group = {}
    for entry in associated_media:
        by_source_group.setdefault(entry.get("argonath:sourceGroup"), []).append(entry)

    selected = associated_media
    for source_group in MEDIA_GROUP_PRECEDENCE:
        if by_source_group.get(source_group):
            selected = by_source_group[source_group]
            break

    groups = {}
    for entry in selected:
        content_url = entry.get("contentUrl", "")
        if "/images/" not in content_url:
            continue
        filename = content_url.split("/images/")[1].split("/")[0]
        groups.setdefault(filename, []).append(entry)

    return {filename: groups[filename] for filename in sorted(groups)}


def build_document(ark):
    """Builds the ES document for an item.

    Single-binary items (e.g. one photo) produce one flat node - clientMedia
    lives directly on the root, matching the plain ImageObject/VideoObject/etc.
    display the client already handles. Multi-binary items (e.g. a scanned
    yearbook) produce a compound @graph, but NOT by typing the root itself as
    an ImageList - confirmed against a real fin/fedora ES document
    (ark:/87293/d3zp3w73h) that the root stays a plain metadata node (with a
    single representative `image` reference to the first page, and an
    `associatedMedia` reference to a *separate* images-container child node),
    and that separate child node is the one typed IMAGE_LIST_TYPE and
    carrying `hasPart` - see dams/services/fin/ucd-lib-client's RecordGraph.js
    (compound-graph resolution) and client-media/definition.js
    (MEDIA_LINK: associatedMedia/image vs. CHILD_LINKS: hasPart - two
    different navigation steps, not one).

    Also confirmed from that same real document: two document-level fields
    outside @graph are required, not @graph.* fields - "name" (promoted copy
    of the root's name; item/model.js sorts search results by "name.raw", not
    "@graph.name.raw") and "roles" (["public"] - access-control gate; a
    document search can't be missing this and still show up). Both were
    absent before this fix, which likely explains "visible in Kibana but not
    in search" independent of the graph-shape issue above.

    isPartOf (collection linking) is deliberately omitted: fin's collection
    linking expects an ark-identified collection @id, but argonath
    collections are only named strings (group/collection) today - there's no
    collection-level ark to link to until collection ark-minting exists.

    :param str ark: Item ARK.
    :returns: ES document body, shaped like transform.js's {"@id", "@graph": [...]}.
    :rtype: dict
    """
    location = items.resolve(ark)
    bronze_sidecar = _load_bronze_sidecar(location.sidecar_path)
    derivative_sidecar = _load_derivative_sidecar(location)

    associated_media = derivative_sidecar.get("associatedMedia") or []
    if not isinstance(associated_media, list):
        associated_media = [associated_media]
    pages = _group_media_by_page(associated_media)

    fin_path = f"/item/{ark}"
    base_type = _normalize_type(bronze_sidecar.get("@type"), "http://schema.org/MediaObject")
    name = bronze_sidecar.get("name")

    root = {
        "@id": fin_path,
        "@type": base_type,
        "name": name,
        "description": bronze_sidecar.get("description"),
        "creator": bronze_sidecar.get("creator"),
        "subjects": bronze_sidecar.get("about"),
        "identifier": ark,
        "fileFormat": bronze_sidecar.get("encodingFormat"),
    }

    if len(location.bronze_binary_paths) == 1 and len(pages) <= 1:
        filename, page_media = next(iter(pages.items()), (None, []))
        root["clientMedia"] = _build_client_media(location, page_media, filename)
        graph = [root]
    else:
        images_container_id = f"{fin_path}/images"
        page_nodes = []
        for i, (filename, page_media) in enumerate(pages.items(), start=1):
            page_nodes.append({
                "@id": f"{images_container_id}/{i}",
                "@type": ["http://schema.org/ImageObject"],
                "name": filename,
                # required, not decorative: dams's client reads this directly
                # off each hasPart node (client-media/model.js:294,
                # `parseInt(node.position)`) to number pages in its viewer -
                # without it every page's clientMedia.images.page is NaN.
                "position": i,
                "clientMedia": _build_client_media(location, page_media, filename),
            })

        images_container = {
            "@id": images_container_id,
            "@type": ["http://schema.org/MediaObject", IMAGE_LIST_TYPE],
            "name": "Image List",
            "hasPart": [{"@id": page["@id"]} for page in page_nodes],
        }

        root["image"] = {"@id": page_nodes[0]["@id"]}
        root["associatedMedia"] = [{"@id": images_container_id}]
        graph = [root, images_container, *page_nodes]

    graph = [{k: v for k, v in node.items() if v is not None} for node in graph]

    return {
        "@id": fin_path,
        "@graph": graph,
        "name": name,
        "roles": ["public"],
    }


def _build_client_media(location, page_media, filename=None):
    """Builds the clientMedia urls the dams webapp reads off a node (the root
    node for a single-binary item, or one page node for a multi-binary item),
    pointing at the argonath-adapter's /svc:cask/ prefix (see
    dams/services/router and dams/services/argonath-adapter) for images,
    instead of fin's /svc:gcs/ - the adapter resolves that prefix straight to
    a caskfs path, so this can point directly at wherever generate_derivatives
    wrote silver outputs, with no ark lookup needed at request time. The IIIF
    deep-zoom viewer url uses a separate /svc:cask-iiif/ prefix instead,
    which the dams-router forwards to the new argonath-iiif (Cantaloupe)
    service rather than the argonath-adapter - see _tiled_iiif_url_for_filename().

    :param digtk.lib.items.ItemLocation location:
    :param list page_media: This page's associatedMedia entries (from
        _group_media_by_page()).
    :param str|None filename: This page's source filename, used to locate its
        tiled/IIIF manifest - None skips the IIIF lookup (no page to look up).
    :returns: clientMedia object, or {} if there's no derivative info yet.
    :rtype: dict
    """
    images = {}
    for candidate in page_media:
        content_url = candidate.get("contentUrl", "")
        name = content_url.rsplit("/", 1)[-1].split(".")[0]
        if name in ("small", "medium", "large"):
            # cask_public_ref() produces "cask://silver/..." - strip the full
            # "cask://" scheme (not just "cask:/") so the result doesn't carry
            # a leading slash into the /svc:cask/ prefix and create a
            # "/svc:cask//silver/..." double-slash.
            images[name] = {"url": "/svc:cask/" + content_url.removeprefix("cask://")}

    client_media = {}
    if images:
        client_media["images"] = images

    if filename:
        tiled_iiif_url = _tiled_iiif_url_for_filename(location, filename)
        if tiled_iiif_url:
            client_media["tiled"] = {"iiif": tiled_iiif_url}

    return client_media


def _tiled_iiif_url_for_filename(location, filename):
    """Finds one page's tiled/deep-zoom derivative and returns its IIIF
    viewer base url, or None if it hasn't been generated.

    The tiled derivative is deliberately not part of associatedMedia the way
    small/medium/large are (see image.py's generate() - only the thumbnail
    set becomes ImageObject entries) - it only exists in the per-image
    manifest.json written alongside those thumbnails. This reconstructs that
    manifest's path the same way generate() does, via pipeline.derivative_dir(),
    rather than duplicating the path convention here, and reads it directly.

    ASSUMPTION, same caveat as build_document()'s multi-page composition: not
    yet checked against a real generate_derivatives run end to end.

    :param digtk.lib.items.ItemLocation location:
    :param str filename: This page's source filename (the {filename} segment
        of its images/{filename}/images/ derivative directory).
    :returns: "/svc:cask-iiif/{percent-encoded tiled.tif caskfs path}", or None.
    :rtype: str|None
    """
    images_dir = pipeline.derivative_dir(location, f"{pipeline.PUBLIC_FRONTEND_DIR}/images/{filename}/images")

    try:
        raw = fs.read(f"{images_dir}/manifest.json")
    except FileNotFoundError:
        return None

    # utils.write_manifest() merges its "extra" content into the manifest's top
    # level (no nested "extra" key), so "tiled" sits right alongside
    # small/medium/large.
    tiled = json.loads(raw).get("tiled")
    if not tiled:
        return None

    tiled_path = tiled["url"].removeprefix("cask://")
    return "/svc:cask-iiif/" + urllib.parse.quote(tiled_path, safe="")


def index_item(ark):
    """Builds an item's ES document and writes it to the DAMS index.

    :param str ark: Item ARK to index.
    :returns: {"ark": ..., "esId": ..., "index": ..., "status": "indexed"}
    :rtype: dict
    :raises httpx.HTTPStatusError: If the ES write fails.
    """
    document = build_document(ark)
    es_id = document["@id"]
    url = f"{ES_HOST}/{ES_INDEX}/_doc/{urllib.parse.quote(es_id, safe='')}"

    resp = httpx.put(url, json=document)
    resp.raise_for_status()

    return {"ark": ark, "esId": es_id, "index": ES_INDEX, "status": "indexed"}
