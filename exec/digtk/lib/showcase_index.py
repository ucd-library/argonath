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
import re
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

# The website's page-level full-text index — one document per page of an image
# list, holding that page's DjVu text layer — which its BookReader search
# (/api/page-search/ia) queries. Same "-write" alias convention as ES_INDEX.
PAGE_SEARCH_INDEX_ENV = "DAMS_ES_PAGE_SEARCH_INDEX"
PAGE_SEARCH_INDEX = os.environ.get(PAGE_SEARCH_INDEX_ENV, "page-search-write")

# URL prefix the dc-showcase gateway serves CaskFS files under: a request for
# /cask/file/silver/... is proxied to the caskfs file API's /fs/silver/...
# (see dc-showcase/services/gateway/index.js). Replaces the old /svc:cask/.
CASK_FILE_PREFIX = "/cask/file/"

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


def _normalize_creator(creator):
    """Shapes a bronze sidecar's creator value as the node(s) the DAMS index
    expects.

    The item index maps @graph.creator as an object ({"@id", "name"}), so a
    bare string is rejected with a 400 (mapper_parsing_exception), and the
    website reads it as `root.creator?.name` (RecordVcModel.js). A single
    creator stays a single object, since that's what the website reads; several
    become a list of them.

    :param creator: Raw creator value — str, dict, a list of either, or None.
    :returns: {"name": ...} (or the dict as given), a list of those, or None.
    :rtype: dict | list | None
    """
    values = creator if isinstance(creator, list) else [creator]
    nodes = [v if isinstance(v, dict) else {"name": v} for v in values if v]
    if not nodes:
        return None
    return nodes[0] if len(nodes) == 1 else nodes


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
        "creator": _normalize_creator(bronze_sidecar.get("creator")),
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
    pointing at the dc-showcase gateway's /cask/file/ prefix (CASK_FILE_PREFIX
    — see dc-showcase/services/gateway/index.js) for images, instead of fin's
    /svc:gcs/ - the gateway proxies that prefix straight to the caskfs file
    API, so this can point directly at wherever generate_derivatives wrote
    silver outputs, with no ark lookup needed at request time. The IIIF
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
            # a leading slash into the prefix and create a
            # "/cask/file//silver/..." double-slash.
            image = {"url": CASK_FILE_PREFIX + content_url.removeprefix("cask://")}
            # the record page's viewer builds its srcset from size.width and
            # crashes without it (app-image-viewer.js _renderImg())
            if candidate.get("width") and candidate.get("height"):
                image["size"] = {"width": str(candidate["width"]), "height": str(candidate["height"])}
            images[name] = image

    client_media = {}
    if images:
        client_media["images"] = images

    if filename:
        manifest = _image_manifest(location, filename)
        tiled_iiif_url = _tiled_iiif_url_for_filename(location, filename, manifest)
        if tiled_iiif_url:
            client_media["tiled"] = {"iiif": tiled_iiif_url}
        ocr = _ocr_from_manifest(manifest)
        if ocr and images:
            images["ocr"] = ocr

    return client_media


def _image_manifest(location, filename):
    """Reads one page's per-image derivative manifest.json — where the tiled
    and OCR outputs are recorded (they aren't part of associatedMedia the way
    small/medium/large are; see image.py's generate()). The path is rebuilt
    the same way generate() builds it, via pipeline.derivative_dir().

    utils.write_manifest() merges its "extra" content into the manifest's top
    level (no nested "extra" key), so "tiled" and "ocr" sit right alongside
    small/medium/large.

    :param digtk.lib.items.ItemLocation location:
    :param str filename: This page's source filename (the {filename} segment
        of its images/{filename}/images/ derivative directory).
    :returns: The parsed manifest, or None if derivatives were never generated.
    :rtype: dict|None
    """
    images_dir = pipeline.derivative_dir(location, f"{pipeline.PUBLIC_FRONTEND_DIR}/images/{filename}/images")
    try:
        return json.loads(fs.read(f"{images_dir}/manifest.json"))
    except FileNotFoundError:
        return None


def _ocr_from_manifest(manifest):
    """One page's OCR text layer as the record page's BookReader expects it
    on each page's clientMedia images: {"url", "size", "imageSize"} (see
    dc-showcase BookReaderModel._isValidPage() — a page without it is dropped,
    so an image list with no OCR shows no pages at all).

    :param dict|None manifest: _image_manifest()'s result.
    :returns: {"url": "/cask/file/...-ocr.djvu", "size": {...}, "imageSize": "large"}, or None.
    :rtype: dict|None
    """
    ocr = (manifest or {}).get("ocr")
    if not ocr or not ocr.get("url") or not ocr.get("size"):
        return None
    return {
        "url": CASK_FILE_PREFIX + ocr["url"].removeprefix("cask://"),
        "size": ocr["size"],
        "imageSize": ocr.get("imageSize", "large"),
    }


def _tiled_iiif_url_for_filename(location, filename, manifest=None):
    """Finds one page's tiled/deep-zoom derivative and returns its IIIF
    viewer base url, or None if it hasn't been generated.

    :param digtk.lib.items.ItemLocation location:
    :param str filename: This page's source filename (the {filename} segment
        of its images/{filename}/images/ derivative directory).
    :param dict|None manifest: The page's manifest, if already read (see
        _image_manifest()) — read here otherwise.
    :returns: "/svc:cask-iiif/{percent-encoded tiled.tif caskfs path}", or None.
    :rtype: str|None
    """
    if manifest is None:
        manifest = _image_manifest(location, filename)
    tiled = (manifest or {}).get("tiled")
    if not tiled:
        return None

    tiled_path = tiled["url"].removeprefix("cask://")
    return "/svc:cask-iiif/" + urllib.parse.quote(tiled_path, safe="")


def index_item(ark):
    """Builds an item's ES document and writes it to the DAMS index, then
    replaces its page-search documents (see index_pages()).

    :param str ark: Item ARK to index.
    :returns: {"ark": ..., "esId": ..., "index": ..., "status": "indexed", "pages": int}
    :rtype: dict
    :raises httpx.HTTPStatusError: If an ES write fails.
    """
    document = build_document(ark)
    es_id = document["@id"]
    url = f"{ES_HOST}/{ES_INDEX}/_doc/{urllib.parse.quote(es_id, safe='')}"

    resp = httpx.put(url, json=document)
    resp.raise_for_status()

    pages = index_pages(document)
    return {"ark": ark, "esId": es_id, "index": ES_INDEX, "status": "indexed", "pages": pages}


def _with_page_size(djvu_xml, size):
    """Adds width/height attributes to a DjVu XML document's OBJECT element
    when it doesn't have them — page search reads them to scale its highlight
    boxes (dc-showcase models/page-search iaSearchResultTransform()). OCR
    written before digtk.lib.djvu.build_djvu_xml() took page_size lacks them.

    :param str djvu_xml: The page's DjVu XML.
    :param dict size: {"width", "height"} of the image the word coordinates are in.
    :returns: The XML, with OBJECT's width/height set.
    :rtype: str
    """
    if re.match(r"<OBJECT\b[^>]*\bwidth=", djvu_xml):
        return djvu_xml
    return re.sub(
        r"^<OBJECT\b", f'<OBJECT width="{size["width"]}" height="{size["height"]}"', djvu_xml, count=1,
    )


def build_page_documents(document):
    """Builds an item's page-search documents from its ES document: one per
    image-list page that has an OCR text layer, shaped like fin's page-search
    transform produced them — {"@id": item, "@graph": [{"@id": page,
    "encodesCreativeWork": image list, "position", "content": DjVu XML}]}.
    The page search matches on encodesCreativeWork (the BookReader searches
    by its image list's @id) and full-text searches content.

    A single-image item has no image list (and no BookReader), so it gets no
    page documents. A page whose DjVu file is missing is skipped.

    :param dict document: build_document()'s result.
    :returns: Page documents, in page order.
    :rtype: list[dict]
    """
    graph = document["@graph"]
    image_list = next((n for n in graph if IMAGE_LIST_TYPE in (n.get("@type") or [])), None)
    if not image_list:
        return []

    pages = []
    for node in graph:
        ocr = ((node.get("clientMedia") or {}).get("images") or {}).get("ocr")
        if "position" not in node or not ocr:
            continue
        try:
            raw = fs.read(f"cask:/{ocr['url'].removeprefix(CASK_FILE_PREFIX)}")
        except FileNotFoundError:
            continue
        content = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        pages.append({
            "@id": document["@id"],
            "@graph": [{
                "@id": node["@id"],
                "encodesCreativeWork": image_list["@id"],
                "position": node["position"],
                "content": _with_page_size(content, ocr["size"]),
            }],
            "roles": document.get("roles", ["public"]),
        })
    return pages


def _delete_pages(item_id):
    """Removes every page-search document belonging to an item (matched on the
    item's top-level @id) — so a re-index with fewer pages leaves none behind.

    :param str item_id: The item's ES @id, "/item/<ark>".
    :returns: How many documents were deleted (0 if the index doesn't exist).
    :rtype: int
    :raises httpx.HTTPStatusError: If the delete fails for any other reason.
    """
    resp = httpx.post(
        f"{ES_HOST}/{PAGE_SEARCH_INDEX}/_delete_by_query",
        params={"refresh": "true", "conflicts": "proceed"},
        json={"query": {"term": {"@id": item_id}}},
    )
    if resp.status_code == 404:
        return 0
    resp.raise_for_status()
    return resp.json().get("deleted", 0)


def index_pages(document):
    """Replaces an item's page-search documents with ones built from its ES
    document (see build_page_documents()).

    :param dict document: build_document()'s result.
    :returns: How many page documents were written.
    :rtype: int
    :raises httpx.HTTPStatusError: If an ES write fails.
    """
    _delete_pages(document["@id"])
    pages = build_page_documents(document)
    for page in pages:
        page_id = page["@graph"][0]["@id"]
        resp = httpx.put(f"{ES_HOST}/{PAGE_SEARCH_INDEX}/_doc/{urllib.parse.quote(page_id, safe='')}", json=page)
        resp.raise_for_status()
    return len(pages)
