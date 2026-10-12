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
import mimetypes
import os
import re
import urllib.parse

import httpx

from digtk.lib import items, fs
from digtk.models.digital_website import item_record, pipeline

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


def _normalize_type(type_value, default):
    """Normalizes a JSON-LD @type value to a list.

    dams's client code (RecordGraph.js, client-media/model.js) always calls
    .includes()/.map() on a node's @type, so it must be an array - never a
    bare string, which is what a naive sidecar.get("@type") read would
    produce for a single-valued type.

    :param type_value: Item record's raw @type value (str, list, or None).
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
    """Shapes an item record's creator value as the node(s) the DAMS index
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


SCHEMA = "http://schema.org/"

# Order of the root's associatedMedia links, by source group. Video comes
# first: the showcase's video player only ever reads the first media group
# (dc-showcase app-video-viewer.js). Within a group, sources sort by filename.
MEDIA_ORDER = ["video", "audio", "image", "pdf"]


def _cask_url(cask_uri):
    """A "cask://silver/..." reference URI as the URL the dc-showcase gateway
    serves it at ("/cask/file/silver/...").

    :param str cask_uri: e.g. "cask://silver/g/c/latest/item/ocr/ocr.djvu".
    :returns: e.g. "/cask/file/silver/g/c/latest/item/ocr/ocr.djvu".
    :rtype: str
    """
    return CASK_FILE_PREFIX + cask_uri.removeprefix("cask://")


def _mime(filename, default):
    """A filename's mime type, guessed from its extension.

    :param str filename: e.g. "movie.mp4".
    :param str default: Returned when the extension isn't recognized.
    :rtype: str
    """
    return mimetypes.guess_type(filename)[0] or default


# source_filename()/group_media_by_source() moved to
# digtk.models.digital_website.pipeline (shared with item_record.py) — kept as
# module-level aliases here so existing call sites below don't need touching.
_source_filename = pipeline.source_filename
_group_media_by_source = pipeline.group_media_by_source


def _caption_links(entry):
    """An audio/video entry's captions as the website's player reads them:
    [{"@id": servable .vtt URL, "language"}] — it uses @id as the <track> src.

    :param dict entry: A VideoObject/AudioObject associatedMedia entry.
    :returns: Caption links, or None if the entry has none.
    :rtype: list|None
    """
    captions = entry.get("caption") or []
    if not isinstance(captions, list):
        captions = [captions]
    links = [
        {"@id": _cask_url(c["contentUrl"]), "language": c.get("inLanguage")}
        for c in captions if c.get("contentUrl")
    ]
    return links or None


def _image_download(client_media):
    """A download entry for an image node: its largest derivative.

    :param dict client_media: _build_client_media()'s result.
    :returns: {"url", "label"}, or None if the node has no images.
    :rtype: dict|None
    """
    images = client_media.get("images") or {}
    best = images.get("large") or images.get("medium") or images.get("small")
    if not best:
        return None
    return {"url": best["url"], "label": best["url"].rsplit(".", 1)[-1]}


def _image_node(location, node_id, filename, entries, opts={}):
    """An ImageObject node for one source image.

    :param digtk.lib.items.ItemLocation location:
    :param str node_id: The node's @id.
    :param str filename: The source image's filename.
    :param list entries: Its associatedMedia entries.
    :param dict opts: 'position' (int — for an image-list page; also makes
        clientMedia.download a single entry, which is how the website's image
        list collects its pages' downloads, rather than a list).
    :rtype: dict
    """
    client_media = _build_client_media(location, entries, filename)
    download = _image_download(client_media)
    if "position" in opts:
        client_media["download"] = download or {}
    else:
        client_media["download"] = [download] if download else []
    node = {
        "@id": node_id,
        "@type": [f"{SCHEMA}ImageObject"],
        "name": filename,
        "fileFormat": _mime(filename, "image/jpeg"),
        "fileFormatSimple": "image",
        "clientMedia": client_media,
    }
    if "position" in opts:
        # required, not decorative: the website numbers image-list pages from
        # each hasPart node's position (client-media/model.js handleImageList).
        node["position"] = opts["position"]
    return node


def _video_node(node_id, filename, entries):
    """A VideoObject node for one source video: its HLS stream and captions.

    :param str node_id: The node's @id.
    :param str filename: The source video's filename.
    :param list entries: Its associatedMedia entries (one VideoObject).
    :rtype: dict
    """
    entry = entries[0]
    return {
        "@id": node_id,
        "@type": [f"{SCHEMA}VideoObject"],
        "name": filename,
        "fileFormat": _mime(filename, "video/mp4"),
        "fileFormatSimple": "video",
        "encodingFormat": entry.get("encodingFormat"),
        "clientMedia": {
            "streamingVideo": {"manifest": _cask_url(entry["contentUrl"])},
            # an HLS ladder isn't a downloadable file
            "download": [],
        },
        "caption": _caption_links(entry),
    }


def _audio_node(node_id, filename, entries):
    """An AudioObject node for one source audio file: its transcoded copy
    (clientMedia.audio.url — the website's audio player plays this) and
    captions.

    :param str node_id: The node's @id.
    :param str filename: The source audio file's filename.
    :param list entries: Its associatedMedia entries (one AudioObject).
    :rtype: dict
    """
    entry = entries[0]
    url = _cask_url(entry["contentUrl"])
    encoding = entry.get("encodingFormat") or _mime(url, "audio/mpeg")
    return {
        "@id": node_id,
        "@type": [f"{SCHEMA}AudioObject"],
        "name": filename,
        "fileFormat": encoding,
        "fileFormatSimple": "audio",
        "encodingFormat": encoding,
        "clientMedia": {
            "audio": {"url": url, "encodingFormat": encoding},
            "download": [{"url": url, "label": url.rsplit(".", 1)[-1]}],
        },
        "caption": _caption_links(entry),
    }


def _pdf_pages(location, node_id, filename):
    """A PDF's pages as the website's BookReader reads them, from the PDF's
    aggregate derivatives manifest (its "pages" array — see
    docs/cask-conventions.md's "PDF Manifest Content"), with servable URLs.

    :param digtk.lib.items.ItemLocation location:
    :param str node_id: The PDF node's @id (page ids are "<node_id>:<index>").
    :param str filename: The PDF's filename.
    :returns: [{"page", "@id", "small"/"medium"/"large": {"url", "size"},
        "ocr": {"url", "size", "imageSize"}}], or [] if the manifest is missing.
    :rtype: list[dict]
    """
    images_dir = pipeline.derivative_dir(location, f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}/images")
    try:
        manifest = json.loads(fs.read(f"{images_dir}/manifest.json"))
    except FileNotFoundError:
        return []

    pages = []
    for page in manifest.get("pages") or []:
        entry = {"page": page["page"], "@id": f"{node_id}:{page['page'] - 1}"}
        for label in ("small", "medium", "large"):
            if page.get(label, {}).get("url"):
                entry[label] = {"url": _cask_url(page[label]["url"]), "size": page[label].get("size")}
        ocr = page.get("ocr") or {}
        if ocr.get("url") and ocr.get("size"):
            entry["ocr"] = {
                "url": _cask_url(ocr["url"]), "size": ocr["size"], "imageSize": ocr.get("imageSize", "large"),
            }
        pages.append(entry)
    return sorted(pages, key=lambda p: p["page"])


def _pdf_node(location, node_id, filename):
    """A PDF node. Typed ImageObject with fileFormat application/pdf — how the
    website recognizes a PDF media group — with its pages embedded in
    clientMedia.pages (see _pdf_pages()) and the first page as its images.

    :param digtk.lib.items.ItemLocation location:
    :param str node_id: The node's @id.
    :param str filename: The PDF's filename.
    :rtype: dict
    """
    pages = _pdf_pages(location, node_id, filename)
    client_media = {"pages": pages, "download": []}
    if pages:
        client_media["images"] = {k: v for k, v in pages[0].items() if k in ("small", "medium", "large")}
    return {
        "@id": node_id,
        "@type": [f"{SCHEMA}ImageObject"],
        "name": filename,
        "fileFormat": "application/pdf",
        "fileFormatSimple": "pdf",
        "clientMedia": client_media,
    }


def _media_nodes(location, fin_path, groups):
    """Every media node for an item: one per video, audio, PDF source, and
    either one ImageObject (a single image) or an image list whose pages are
    the image nodes (two or more images).

    :param digtk.lib.items.ItemLocation location:
    :param str fin_path: The item's @id ("/item/<ark>").
    :param dict groups: _group_media_by_source()'s result.
    :returns: (top-level nodes in MEDIA_ORDER, child nodes) — the root links
        to the top-level ones; image-list pages are children.
    :rtype: tuple[list[dict], list[dict]]
    """
    top, children = [], []
    for group in MEDIA_ORDER:
        sources = groups.get(group) or {}
        if group == "video":
            top += [_video_node(f"{fin_path}/{f}", f, e) for f, e in sources.items()]
        elif group == "audio":
            top += [_audio_node(f"{fin_path}/{f}", f, e) for f, e in sources.items()]
        elif group == "pdf":
            top += [_pdf_node(location, f"{fin_path}/{f}", f) for f in sources]
        elif group == "image" and len(sources) == 1:
            filename, entries = next(iter(sources.items()))
            top.append(_image_node(location, f"{fin_path}/{filename}", filename, entries))
        elif group == "image" and sources:
            list_id = f"{fin_path}/images"
            pages = [
                _image_node(location, f"{list_id}/{i}", f, e, {"position": i})
                for i, (f, e) in enumerate(sources.items(), start=1)
            ]
            top.append({
                "@id": list_id,
                "@type": [f"{SCHEMA}MediaObject", IMAGE_LIST_TYPE],
                "name": "Image List",
                "hasPart": [{"@id": page["@id"]} for page in pages],
                "clientMedia": {"download": []},
            })
            children += pages
    return top, children


def build_document(ark):
    """Builds the ES document for an item: a metadata root node plus one node
    per media file, all in @graph, linked from the root's associatedMedia —
    every image, PDF, video and audio source the item has (see
    _media_nodes()), so the website can decide what to render.

    Shaped like fin's item documents, confirmed against a real fin/fedora ES
    document (ark:/87293/d3zp3w73h): the root stays a plain metadata node; a
    multi-image item's images hang off a separate image-list node typed
    IMAGE_LIST_TYPE with hasPart pages (see dams/services/fin/ucd-lib-client's
    RecordGraph.js and client-media/definition.js: MEDIA_LINK
    associatedMedia/image vs. CHILD_LINKS hasPart). Two document-level fields
    outside @graph are required: "name" (item/model.js sorts search results by
    "name.raw") and "roles" (["public"] — a document without it doesn't show
    up in search).

    isPartOf (collection linking) is deliberately omitted: fin's collection
    linking expects an ark-identified collection @id, but argonath
    collections are only named strings (group/collection) today - there's no
    collection-level ark to link to until collection ark-minting exists.

    Everything comes from the item's gold item.jsonld.json (see
    digtk.models.digital_website.item_record), so it must be built first.

    :param str ark: Item ARK.
    :returns: ES document body, shaped like transform.js's {"@id", "@graph": [...]}.
    :rtype: dict
    :raises FileNotFoundError: If the item's record hasn't been built yet.
    """
    location = items.resolve(ark)
    record = item_record.read(location)
    associated_media = item_record.flat_media(record)

    fin_path = f"/item/{ark}"
    base_type = _normalize_type(record.get("@type"), "http://schema.org/MediaObject")
    name = record.get("name")

    root = {
        "@id": fin_path,
        "@type": base_type,
        "name": name,
        "description": record.get("description"),
        "creator": _normalize_creator(record.get("creator")),
        "subjects": record.get("about"),
        "identifier": ark,
        "fileFormat": record.get("encodingFormat"),
    }

    top, children = _media_nodes(location, fin_path, _group_media_by_source(associated_media))
    if top:
        root["associatedMedia"] = [{"@id": node["@id"]} for node in top]
        thumbnail = next((n for n in children + top if n.get("fileFormatSimple") in ("image", "pdf")), None)
        if thumbnail:
            root["image"] = {"@id": thumbnail["@id"]}

    graph = [root]
    for node in top:
        graph.append(node)
        if IMAGE_LIST_TYPE in node["@type"]:
            graph += children
    graph = [{k: v for k, v in node.items() if v is not None} for node in graph]

    return {
        "@id": fin_path,
        "@graph": graph,
        "name": name,
        "roles": ["public"],
    }


def _build_client_media(location, page_media, filename=None):
    """Builds the clientMedia urls the dams webapp reads off an image node (a
    single image, or one page of an image list),
    pointing at the dc-showcase gateway's /cask/file/ prefix (CASK_FILE_PREFIX
    — see dc-showcase/services/gateway/index.js) for images, instead of fin's
    /svc:gcs/ - the gateway proxies that prefix straight to the caskfs file
    API, so this can point directly at wherever generate_derivatives wrote
    silver outputs, with no ark lookup needed at request time. The IIIF
    deep-zoom viewer url uses a separate /iiif/ prefix instead,
    which the dams-router forwards to the new argonath-iiif (Cantaloupe)
    service rather than the argonath-adapter - see _tiled_iiif_url_for_filename().
    It's nested in images (images.tiled.iiif), like the legacy fin image
    manifest, because the fullscreen lightbox reads it off the page's images
    object (app-image-viewer-lightbox.js renderCanvas()).

    :param digtk.lib.items.ItemLocation location:
    :param list page_media: This page's associatedMedia entries (from
        _group_media_by_source()).
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
            image = {"url": _cask_url(content_url)}
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
        if tiled_iiif_url and images:
            images["tiled"] = {"iiif": tiled_iiif_url}
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
        "url": _cask_url(ocr["url"]),
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
    :returns: "/iiif/{tiled.tif caskfs path}" (slashes kept, other unsafe
        characters percent-encoded), or None.
    :rtype: str|None
    """
    if manifest is None:
        manifest = _image_manifest(location, filename)
    tiled = (manifest or {}).get("tiled")
    if not tiled:
        return None

    tiled_path = tiled["url"].removeprefix("cask://")
    return "/iiif/" + urllib.parse.quote(tiled_path)


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
    page that has an OCR text layer — every image-list page and every page of
    every PDF — shaped like fin's page-search transform produced them:
    {"@id": item, "@graph": [{"@id": page, "encodesCreativeWork": the image
    list or PDF node, "position", "content": DjVu XML}]}. The page search
    matches on encodesCreativeWork (the BookReader searches by the @id of the
    image list or PDF it's showing) and full-text searches content.

    A single image has no pages (and no BookReader), so it gets no page
    documents. A page whose DjVu file is missing is skipped.

    :param dict document: build_document()'s result.
    :returns: Page documents, in graph/page order.
    :rtype: list[dict]
    """
    graph = document["@graph"]
    by_id = {node["@id"]: node for node in graph}

    targets = []  # (book @id, page @id, position, ocr)
    for node in graph:
        if IMAGE_LIST_TYPE in (node.get("@type") or []):
            for link in node.get("hasPart") or []:
                page = by_id.get(link["@id"]) or {}
                ocr = ((page.get("clientMedia") or {}).get("images") or {}).get("ocr")
                targets.append((node["@id"], page.get("@id"), page.get("position"), ocr))
        elif node.get("fileFormatSimple") == "pdf":
            for page in (node.get("clientMedia") or {}).get("pages") or []:
                targets.append((node["@id"], page["@id"], page["page"], page.get("ocr")))

    pages = []
    for book_id, page_id, position, ocr in targets:
        if not page_id or not ocr:
            continue
        try:
            raw = fs.read(f"cask:/{ocr['url'].removeprefix(CASK_FILE_PREFIX)}")
        except FileNotFoundError:
            continue
        content = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        pages.append({
            "@id": document["@id"],
            "@graph": [{
                "@id": page_id,
                "encodesCreativeWork": book_id,
                "position": position,
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
