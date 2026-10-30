"""
Indexes an item's schema.org metadata into the showcase website's
Elasticsearch index - the same index fin's dbsync populates today, read by the
digital library website's /api/item and /api/collection endpoints (see
dams/services/fin/models/item/{model,transform}.js in the dams repo - this
module mirrors the {"@id", "@graph": [...]} document envelope transform.js
produces, so the website's existing search/detail lookups work unmodified
once an item is indexed here).

Item and collection documents keep their gold record's full URI @ids
(https://digital.ucdavis.edu/item/<ark>...). Media renditions live in a
root-level "clientMedia" map keyed by node @id, not on @graph nodes.
"displayImage" holds the main image, so search can skip clientMedia.

Deliberately unrelated to digtk.commands.search (LlamaIndex/pgvector RAG
search) - different index, different consumer, different purpose. Kept as
its own module/command group rather than folded into `search` so the two
don't get conflated - see dagster/lib/assets.py's index_for_showcase_search
docstring for the same reasoning on the Dagster side.

ES connection settings come from CONFIG["showcase"] (es_host, es_index,
es_page_search_index) - see digtk.lib.config.
"""
import json
import mimetypes
import posixpath
import re
import urllib.parse

import httpx

from digtk.lib import items, fs, utils
from digtk.lib.config import CONFIG
from digtk.models.digital_website import (
    audio, captions, collection_record, image, item_record, pdf, pipeline, video, visibility,
)

ES_HOST = CONFIG["showcase"]["es_host"]
ES_INDEX = CONFIG["showcase"]["es_index"]
PAGE_SEARCH_INDEX = CONFIG["showcase"]["es_page_search_index"]
COLLECTION_INDEX = CONFIG["showcase"]["es_collection_index"]

# CaskFS file API path, proxied by the dc-showcase gateway.
CASK_API_PREFIX = "/cask/api/fs/"

# custom digital.ucdavis.edu vocabulary type (dams/services/fin/ucd-lib-client/client/public/lib/
# utils/client-media/definition.js:25) marking a multi-page/scanned item - triggers
# the client's "imagelist" display mode, which crawls hasPart for page content
# instead of reading clientMedia off the root node directly.
IMAGE_LIST_TYPE = "http://digital.ucdavis.edu/schema#ImageList"


def _normalize_creator(creator):
    """Shapes an item record's creator value as the node(s) the showcase index
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


def _is_part_of(is_part_of):
    """The gold record's isPartOf as a list of {"@id"} nodes.

    :param is_part_of: Gold root node's isPartOf (dict, list, or None).
    :returns: isPartOf as a list of {"@id"}, or None if the record has none.
    :rtype: list|None
    """
    if not is_part_of:
        return None
    if isinstance(is_part_of, dict):
        is_part_of = [is_part_of]
    linked = []
    for node in is_part_of:
        node_id = node.get("@id") if isinstance(node, dict) else node
        if node_id:
            linked.append({"@id": node_id})
    return linked or None


# internal identifier types kept off the site
INTERNAL_IDENTIFIER_TYPES = ("Preservica", "CaskGoldPath")


def _public_identifiers(identifiers):
    """The gold record's identifier values, minus internal ids (Preservica
    ref, CaskGoldPath). The website reads identifier for the call number.

    :param identifiers: Gold root identifier list (PropertyValue dicts or strings).
    :returns: Identifier value strings.
    :rtype: list[str]
    """
    values = []
    for i in identifiers or []:
        if isinstance(i, str):
            values.append(i)
        elif i.get("propertyID") not in INTERNAL_IDENTIFIER_TYPES and i.get("value"):
            values.append(i["value"])
    return values


def _full_types(*type_values):
    """Combines @type values as full http://schema.org/ URIs. The website
    only keeps types that match http://schema.org/ or the digital.ucdavis.edu
    schema, so bare and prefixed names are expanded.

    :param type_values: Raw @type values (str, list, or None each).
    :returns: Unique full type URIs, in order.
    :rtype: list
    """
    types = []
    for value in type_values:
        for t in value if isinstance(value, list) else [value]:
            if not t:
                continue
            if t.startswith("schema:"):
                t = SCHEMA + t.removeprefix("schema:")
            elif t.startswith("https://schema.org/"):
                t = SCHEMA + t.removeprefix("https://schema.org/")
            elif ":" not in t:
                t = SCHEMA + t
            if t not in types:
                types.append(t)
    return types


def _cask_url(cask_uri):
    """A "cask://silver/..." reference URI as its CaskFS API download URL.

    :param str cask_uri: e.g. "cask://silver/g/c/latest/item/ocr/ocr.djvu".
    :returns: e.g. "/cask/api/fs/silver/g/c/latest/item/ocr/ocr.djvu".
    :rtype: str
    """
    return CASK_API_PREFIX + cask_uri.removeprefix("cask://")


def _file_size(url, sizes):
    """A servable CaskFS API url's file size, from a listing of its directory.
    Listings are cached in sizes, so each directory is listed once per build.

    :param str url: e.g. "/cask/api/fs/silver/g/c/latest/item/large.jpg".
    :param dict sizes: Per-build cache of {directory: {filename: size}}.
    :returns: Size in bytes, or None if unknown.
    :rtype: int|None
    """
    directory, filename = posixpath.split("cask:/" + url.removeprefix(CASK_API_PREFIX))
    if directory not in sizes:
        try:
            sizes[directory] = {e.name: e.size for e in fs.ls(directory) if e.kind == "file"}
        except FileNotFoundError:
            sizes[directory] = {}
    size = sizes[directory].get(filename)
    return int(size) if size is not None else None


def _with_file_size(entry, url, sizes):
    """Sets entry's fileSize when the url's size is known.

    :param dict entry: Download entry or media node, mutated in place.
    :param str url: Servable CaskFS API url.
    :param dict sizes: See _file_size().
    :returns: entry.
    :rtype: dict
    """
    size = _file_size(url, sizes)
    if size is not None:
        entry["fileSize"] = size
    return entry


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


# Each format's silver associatedMedia reader, in the order its entries are read.
_MEDIA_READERS = {
    "image": image.media_objects,
    "pdf": pdf.media_objects,
    "video": video.media_objects,
    "audio": audio.media_objects,
}


def _silver_media(location):
    """Every published silver associatedMedia entry for the item's formats
    (renditions, streams, transcoded copies), each stamped with the
    "argonath:sourceGroup" it was built for, with captions attached to their
    video/audio entries. The gold item record only points at bronze
    preservation masters (see item_record), so the website-facing media
    comes from silver here.

    :param digtk.lib.items.ItemLocation location:
    :returns: Flat associatedMedia entries.
    :rtype: list[dict]
    :raises FileNotFoundError: If a format's derivatives haven't been published.
    """
    formats = pipeline.detect_formats(location.bronze_binary_paths)
    entries = []
    for group, reader in _MEDIA_READERS.items():
        if group not in formats:
            continue
        objects = reader(location)
        if group in ("video", "audio"):
            captions.attach_captions(location, objects)
        for entry in objects:
            entry["argonath:sourceGroup"] = group
        entries += objects
    return entries


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


def _image_download(client_media, sizes):
    """A download entry for an image node: its largest derivative.

    :param dict client_media: _build_client_media()'s result.
    :param dict sizes: See _file_size().
    :returns: {"url", "label", "fileSize"}, or None if the node has no images.
    :rtype: dict|None
    """
    images = client_media.get("images") or {}
    best = images.get("large") or images.get("medium") or images.get("small")
    if not best:
        return None
    return _with_file_size({"url": best["url"], "label": best["url"].rsplit(".", 1)[-1]}, best["url"], sizes)


def _image_node(location, node_id, filename, entries, opts={}):
    """An ImageObject node for one source image.

    :param digtk.lib.items.ItemLocation location:
    :param str node_id: The node's @id.
    :param str filename: The source image's filename.
    :param list entries: Its associatedMedia entries.
    :param dict opts: 'position' (int — for an image-list page; also makes
        clientMedia.download a single entry, which is how the website's image
        list collects its pages' downloads, rather than a list); 'sizes'
        (see _file_size()).
    :rtype: dict
    """
    client_media = _build_client_media(location, entries, filename)
    download = _image_download(client_media, opts.get("sizes", {}))
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
    if download and "fileSize" in download:
        node["fileSize"] = download["fileSize"]
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


def _audio_node(node_id, filename, entries, opts={}):
    """An AudioObject node for one source audio file: its transcoded copy
    (clientMedia.audio.url — the website's audio player plays this) and
    captions.

    :param str node_id: The node's @id.
    :param str filename: The source audio file's filename.
    :param list entries: Its associatedMedia entries (one AudioObject).
    :param dict opts: 'sizes' (see _file_size()).
    :rtype: dict
    """
    entry = entries[0]
    url = _cask_url(entry["contentUrl"])
    encoding = entry.get("encodingFormat") or _mime(url, "audio/mpeg")
    download = _with_file_size({"url": url, "label": url.rsplit(".", 1)[-1]}, url, opts.get("sizes", {}))
    node = {
        "@id": node_id,
        "@type": [f"{SCHEMA}AudioObject"],
        "name": filename,
        "fileFormat": encoding,
        "fileFormatSimple": "audio",
        "encodingFormat": encoding,
        "clientMedia": {
            "audio": {"url": url, "encodingFormat": encoding},
            "download": [download],
        },
        "caption": _caption_links(entry),
    }
    if "fileSize" in download:
        node["fileSize"] = download["fileSize"]
    return node


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


def _media_nodes(location, gold_ids, groups, opts={}):
    """Every media node for an item: one per video, audio, PDF source, and
    either one ImageObject (a single image) or an image list whose pages are
    the image nodes (two or more images). Each node takes its gold record
    @id; a source with no gold node is dropped with a warning.

    :param digtk.lib.items.ItemLocation location:
    :param dict gold_ids: _gold_media_ids()'s result.
    :param dict groups: _group_media_by_source()'s result.
    :param dict opts: 'sizes' (see _file_size()).
    :returns: (top-level nodes in MEDIA_ORDER, child nodes) — the root links
        to the top-level ones; image-list pages are children.
    :rtype: tuple[list[dict], list[dict]]
    """
    def gold_id(key):
        node_id = gold_ids.get(key)
        if not node_id:
            utils.warn(f"no gold record node for {key or 'the image list'}, skipping it")
        return node_id

    sizes = opts.get("sizes", {})
    top, children = [], []
    for group in MEDIA_ORDER:
        sources = groups.get(group) or {}
        if group == "video":
            top += [_video_node(i, f, e) for f, e in sources.items() if (i := gold_id(f))]
        elif group == "audio":
            top += [_audio_node(i, f, e, {"sizes": sizes}) for f, e in sources.items() if (i := gold_id(f))]
        elif group == "pdf":
            top += [_pdf_node(location, i, f) for f in sources if (i := gold_id(f))]
        elif group == "image" and len(sources) == 1:
            filename, entries = next(iter(sources.items()))
            node_id = gold_id(filename)
            if node_id:
                top.append(_image_node(location, node_id, filename, entries, {"sizes": sizes}))
        elif group == "image" and sources:
            list_id = gold_id(None)
            if not list_id:
                continue
            pages = [
                _image_node(location, i, f, e, {"position": n, "sizes": sizes})
                for n, (f, e) in enumerate(sources.items(), start=1) if (i := gold_id(f))
            ]
            top.append({
                "@id": list_id,
                "@type": [f"{SCHEMA}MediaObject", IMAGE_LIST_TYPE],
                "name": "Image List",
                "clientMedia": {"download": []},
            })
            children += pages
    return top, children


def _merge_media(gold_node, node):
    """Adds a website media node's fields to its gold node. Gold values win,
    except @type, which combines both as full URIs.

    :param dict gold_node: The gold record's node, mutated in place.
    :param dict node: The website node built from silver (see _media_nodes()).
    """
    gold_types = gold_node.get("@type")
    for key, value in node.items():
        if key not in gold_node and value is not None:
            gold_node[key] = value
    gold_node["@type"] = _full_types(node.get("@type"), gold_types)


def build_document(ark, opts={}):
    """Builds the ES document for an item from its gold item.jsonld.json:
    every gold node and field, with gold's full URI @ids, plus the fields
    the website reads.

    Website fields: media nodes get @type/name/fileFormat/fileFormatSimple
    from the item's silver derivatives (see _media_nodes()), and their
    clientMedia moves to the root "clientMedia" map keyed by node @id. The
    root gets "subjects" (from about), a {name} creator and publisher,
    string identifiers and full @type URIs, matching the item index mapping.
    "about" is left out, since the mapping indexes it as text. Two
    document-level fields are required: "name" (search sorts by "name.raw")
    and "roles", from the visibility of the collection the item isPartOf
    (see visibility.roles(); an item with no collection is private).

    :param str ark: Item ARK.
    :param dict opts: 'project' — the gold app segment to read the record
        from (see item_record.gold_location(), which never reads bronze);
        'roles_cache' — see visibility.roles().
    :returns: ES document body: {"@id", "@graph", "clientMedia",
        "displayImage", "name", "roles"}.
    :rtype: dict
    :raises FileNotFoundError: If the item's record hasn't been built yet.
    """
    location, gold = item_record.gold_location(ark, opts)
    graph = json.loads(json.dumps(gold.get("@graph") or []))
    root = item_record.root_node({"@id": gold.get("@id"), "@graph": graph})
    if not root:
        raise LookupError(f"{ark}: gold record has no root node")
    by_id = {node["@id"]: node for node in graph if node.get("@id")}

    groups = _group_media_by_source(_silver_media(location))
    top, children = _media_nodes(location, _gold_media_ids(gold), groups, {"sizes": {}})
    client_media = {}
    for node in top + children:
        media = node.pop("clientMedia", None)
        if media is not None:
            client_media[node["@id"]] = media
        if node["@id"] in by_id:
            _merge_media(by_id[node["@id"]], node)
        else:
            graph.append(node)

    # website media order first
    refs = [{"@id": node["@id"]} for node in top]
    for ref in root.get("associatedMedia") or []:
        if ref not in refs:
            refs.append(ref)
    root["associatedMedia"] = refs or None
    if not root.get("image"):
        thumbnail = next((n for n in children + top if n.get("fileFormatSimple") in ("image", "pdf")), None)
        root["image"] = {"@id": thumbnail["@id"]} if thumbnail else None

    root["@type"] = _full_types(root.get("@type")) or [f"{SCHEMA}MediaObject"]
    root["subjects"] = root.pop("about", None)
    root["creator"] = _normalize_creator(root.get("creator"))
    root["publisher"] = _normalize_creator(root.get("publisher"))
    root["identifier"] = _public_identifiers(root.get("identifier")) or [ark]
    root["isPartOf"] = _is_part_of(root.get("isPartOf"))
    if root.get("encodingFormat"):
        root["fileFormat"] = root["encodingFormat"]
    for node in graph:
        if node is not root and node.get("@type"):
            node["@type"] = _full_types(node["@type"])

    graph = [{k: v for k, v in node.items() if v is not None} for node in graph]
    document = {
        "@id": root["@id"],
        "@graph": graph,
        "clientMedia": client_media,
        "name": root.get("name"),
        "roles": visibility.roles(_collection_id(root.get("isPartOf")), opts),
    }
    display_image = _display_image(graph, client_media, (root.get("image") or {}).get("@id"))
    if display_image:
        document["displayImage"] = display_image
    return document


def _collection_id(is_part_of):
    """The first collection @id in an item's isPartOf.

    :param list|None is_part_of: _is_part_of()'s result.
    :returns: The collection's ES @id, or None if the item has none.
    :rtype: str|None
    """
    return next(
        (n["@id"] for n in is_part_of or [] if n["@id"].startswith(item_record.COLLECTION_URI_PREFIX)),
        None,
    )


def _gold_media_ids(gold):
    """Gold record media @ids, keyed by source filename.

    Ids are item_record._frag() fragments: "#imagelist" for the image list
    (keyed None), "#imagelist/<file>" or "#media/<file>" for files.

    :param dict gold: The item's gold item.jsonld.json document.
    :returns: {filename|None: gold @id}.
    :rtype: dict
    """
    ids = {}
    for node in gold.get("@graph") or []:
        _, _, fragment = (node.get("@id") or "").partition("#")
        namespace, _, filename = fragment.partition("/")
        if namespace == "imagelist" and not filename:
            ids[None] = node["@id"]
        elif namespace in ("imagelist", "media") and filename:
            ids[urllib.parse.unquote(filename)] = node["@id"]
    return ids


def _display_image(graph, client_media, gold_image_id):
    """The item's main image: the gold root's schema:image (position 1, or
    the lone image), else the first PDF's first page.

    :param list[dict] graph: The document's @graph.
    :param dict client_media: The document's root clientMedia map.
    :param str|None gold_image_id: The gold root node's image @id.
    :returns: {"@id": gold @id, "images": {...}}, or None.
    :rtype: dict|None
    """
    candidates = [gold_image_id] + [n["@id"] for n in graph if n.get("fileFormatSimple") == "pdf"]
    for gold_id in candidates:
        images = (client_media.get(gold_id) or {}).get("images")
        if images:
            return {"@id": gold_id, "images": images}
    return None


def _build_client_media(location, page_media, filename=None):
    """Builds an image's clientMedia: CaskFS API urls for small/medium/large
    and OCR, plus the IIIF tiled url nested under images (the lightbox reads
    it there).

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
    :returns: {"url": "/cask/api/fs/...-ocr.djvu", "size": {...}, "imageSize": "large"}, or None.
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
    """Builds an item's ES document and writes it to the showcase index, then
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


def item_es_id(ark):
    """An item's ES document id, from its ARK alone.

    :param str ark: Item ARK.
    :returns: e.g. 'https://digital.ucdavis.edu/item/ark:/87287/d7abc1234'.
    :rtype: str
    """
    return item_record._item_uri(ark)


def bulk(actions, opts={}):
    """Sends index/delete actions to ES's _bulk API in chunks. Per-document
    failures are returned, not raised; a delete of a missing doc is a success.

    :param list[dict] actions: {"op": "index"|"delete", "index", "id", "doc"?}.
    :param dict opts: 'chunk_size' (int, default 500).
    :returns: {"indexed": int, "deleted": int, "errors": [{"id", "error"}]}.
    :rtype: dict
    :raises httpx.HTTPStatusError: If a whole _bulk request fails.
    """
    chunk_size = opts.get("chunk_size", 500)
    result = {"indexed": 0, "deleted": 0, "errors": []}
    for start in range(0, len(actions), chunk_size):
        lines = []
        for action in actions[start:start + chunk_size]:
            lines.append(json.dumps({action["op"]: {"_index": action["index"], "_id": action["id"]}}))
            if action["op"] == "index":
                lines.append(json.dumps(action["doc"]))
        resp = httpx.post(
            f"{ES_HOST}/_bulk",
            content="\n".join(lines) + "\n",
            headers={"Content-Type": "application/x-ndjson"},
        )
        resp.raise_for_status()
        for entry in resp.json().get("items", []):
            op, outcome = next(iter(entry.items()))
            status = outcome.get("status", 500)
            if op == "delete" and status in (200, 404):
                result["deleted"] += 1 if status == 200 else 0
            elif status < 300:
                result["indexed"] += 1
            else:
                result["errors"].append({"id": outcome.get("_id"), "error": outcome.get("error")})
    return result


def index_items(arks, opts={}):
    """Builds and bulk-writes several items' ES documents and their page
    documents. An item whose document can't be built is reported in "failed"
    and the rest still go out.

    :param list[str] arks: Item ARKs.
    :param dict opts: 'project' (see build_document()), 'chunk_size' (see bulk()).
    :returns: {"indexed": [ark], "failed": [{"ark", "error"}], "pages": int}.
    :rtype: dict
    :raises httpx.HTTPStatusError: If a whole _bulk request fails.
    """
    failed = []
    documents = {}
    build_opts = {k: v for k, v in opts.items() if k == "project"}
    build_opts["roles_cache"] = {}
    for ark in arks:
        try:
            documents[ark] = build_document(ark, build_opts)
        except Exception as e:
            failed.append({"ark": ark, "error": str(e)})

    actions = [
        {"op": "index", "index": ES_INDEX, "id": doc["@id"], "doc": doc}
        for doc in documents.values()
    ]
    item_errors = {e["id"]: e["error"] for e in bulk(actions, opts)["errors"]}

    indexed = []
    page_actions = []
    for ark, doc in documents.items():
        if doc["@id"] in item_errors:
            failed.append({"ark": ark, "error": json.dumps(item_errors[doc["@id"]])})
            continue
        indexed.append(ark)
        for page in build_page_documents(doc):
            page_actions.append({
                "op": "index", "index": PAGE_SEARCH_INDEX, "id": page["@graph"][0]["@id"], "doc": page,
            })

    _delete_pages([documents[ark]["@id"] for ark in indexed])
    page_result = bulk(page_actions, opts)
    for error in page_result["errors"]:
        utils.warn(f"page {error['id']} failed to index: {error['error']}")
    return {"indexed": indexed, "failed": failed, "pages": page_result["indexed"]}


def list_collection_item_ids(collection_id, opts={}):
    """Every item document id whose isPartOf is the collection, paged
    through with search_after.

    :param str collection_id: The collection document's @id.
    :param dict opts: 'page_size' (int, default 1000).
    :returns: Item ES ids, sorted.
    :rtype: list[str]
    :raises httpx.HTTPStatusError: If a search request fails.
    """
    body = {
        "size": opts.get("page_size", 1000),
        "query": {"term": {"@graph.isPartOf.@id": collection_id}},
        "sort": [{"@id": "asc"}],
        "_source": False,
    }
    ids = []
    while True:
        resp = httpx.post(f"{ES_HOST}/{ES_INDEX}/_search", json=body)
        if resp.status_code == 404:
            return ids
        resp.raise_for_status()
        hits = resp.json()["hits"]["hits"]
        ids.extend(hit["_id"] for hit in hits)
        if len(hits) < body["size"]:
            return ids
        body = {**body, "search_after": hits[-1]["sort"]}


def delete_items(item_ids, opts={}):
    """Removes items' documents and their page-search documents.

    :param list[str] item_ids: Item ES ids (see item_es_id()).
    :param dict opts: Forwarded to bulk().
    :returns: {"items": int, "pages": int, "errors": [{"id", "error"}]}.
    :rtype: dict
    :raises httpx.HTTPStatusError: If an ES request fails outright.
    """
    pages = _delete_pages(item_ids)
    result = bulk([{"op": "delete", "index": ES_INDEX, "id": i} for i in item_ids], opts)
    return {"items": result["deleted"], "pages": pages, "errors": result["errors"]}


def delete_collection(collection_id, opts={}):
    """Removes a collection's document, every item document that is part of
    it, and those items' page documents.

    :param str collection_id: The collection document's @id.
    :param dict opts: Forwarded to list_collection_item_ids() and bulk().
    :returns: {"collection": bool, "items": int, "pages": int, "errors": [...]}.
    :rtype: dict
    :raises httpx.HTTPStatusError: If an ES request fails outright.
    """
    result = delete_items(list_collection_item_ids(collection_id, opts), opts)
    resp = httpx.delete(f"{ES_HOST}/{COLLECTION_INDEX}/_doc/{urllib.parse.quote(collection_id, safe='')}")
    if resp.status_code != 404:
        resp.raise_for_status()
    return {"collection": resp.status_code != 404, **result}


def build_collection_document(name, opts={}):
    """Builds the ES document for a collection from its gold record. Items
    link to it through their isPartOf; it has no hasPart back. Its roles
    come from its visibility in dc-showcase (see visibility.roles()).

    :param str name: Collection name.
    :param dict opts: 'project' (see collection_record.read()),
        'item_count' (int, default 0) and 'conn' (see visibility.lookup()).
    :returns: ES document body: {"@id", "@graph", "clientMedia", "name", "roles"}.
    :rtype: dict
    :raises FileNotFoundError: If the collection's record hasn't been built yet.
    :raises psycopg2.Error: If dc-showcase's database can't be read.
    """
    record = collection_record.root_node(collection_record.read(name, opts))
    roles = visibility.roles(record["@id"], opts)
    ark = record["ark"]
    root = {
        "@id": record["@id"],
        "@type": _full_types(record.get("@type")) or [f"{SCHEMA}Collection"],
        "ark": ark,
        "name": record.get("name"),
        "description": record.get("description"),
        "creator": _normalize_creator(record.get("creator")),
        "publisher": _normalize_creator(record.get("publisher")),
        "license": record.get("license"),
        "subjects": record.get("about"),
        "identifier": [i["value"] for i in record.get("identifier") or [] if i.get("value")] or [ark],
        "itemCount": opts.get("item_count", 0),
        "visibility": "public" if "public" in roles else "private",
        # dc-showcase stores collection files here
        "goldPath": posixpath.dirname(collection_record.record_path(name, opts).removeprefix("cask:")),
    }
    return {
        "@id": root["@id"],
        "@graph": [{k: v for k, v in root.items() if v is not None}],
        "clientMedia": {},
        "name": record.get("name"),
        "roles": roles,
    }


def count_collection_items(collection_id):
    """Counts items in the item index whose isPartOf is the collection.

    :param str collection_id: The collection document's @id.
    :returns: Item count.
    :rtype: int
    :raises httpx.HTTPStatusError: If the count request fails.
    """
    resp = httpx.post(
        f"{ES_HOST}/{ES_INDEX}/_count",
        json={"query": {"term": {"@graph.isPartOf.@id": collection_id}}},
    )
    if resp.status_code == 404:
        return 0
    resp.raise_for_status()
    return resp.json()["count"]


def find_collection_image(collection_id, opts={}):
    """The root image of one of the collection's items, to use as the
    collection's own image. Items are checked in @id order.

    :param str collection_id: The collection document's @id.
    :param dict opts: 'size' (int, default 10) — how many items to check.
    :returns: {"@id": <item image node @id>}, or None if no item has one.
    :rtype: dict|None
    :raises httpx.HTTPStatusError: If the search request fails.
    """
    resp = httpx.post(f"{ES_HOST}/{ES_INDEX}/_search", json={
        "size": opts.get("size", 10),
        "query": {"term": {"@graph.isPartOf.@id": collection_id}},
        "sort": [{"@id": "asc"}],
        "_source": ["@id", "@graph.@id", "@graph.image"],
    })
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    for hit in resp.json()["hits"]["hits"]:
        source = hit["_source"]
        root = next((n for n in source.get("@graph") or [] if n.get("@id") == source.get("@id")), {})
        image_id = (root.get("image") or {}).get("@id")
        if image_id:
            return {"@id": image_id}
    return None


def index_collection(name, opts={}):
    """Builds a collection's ES document and writes it to the collection index.
    itemCount and image come from the collection's items at write time;
    dc-showcase's cron keeps itemCount current.

    :param str name: Collection name.
    :param dict opts: 'project' — see collection_record.read().
    :returns: {"collection", "esId", "index", "itemCount", "status": "indexed"}
    :rtype: dict
    :raises httpx.HTTPStatusError: If an ES request fails.
    """
    document = build_collection_document(name, opts)
    es_id = document["@id"]
    count = count_collection_items(es_id)
    root = document["@graph"][0]
    root["itemCount"] = count
    image = find_collection_image(es_id)
    if image:
        root["image"] = image

    resp = httpx.put(f"{ES_HOST}/{COLLECTION_INDEX}/_doc/{urllib.parse.quote(es_id, safe='')}", json=document)
    resp.raise_for_status()
    return {"collection": name, "esId": es_id, "index": COLLECTION_INDEX, "itemCount": count, "status": "indexed"}


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
    client_media = document.get("clientMedia") or {}

    def media_of(node):
        return client_media.get(node.get("@id")) or {}

    targets = []  # (book @id, page @id, position, ocr)
    for node in graph:
        if IMAGE_LIST_TYPE in (node.get("@type") or []):
            for link in node.get("hasPart") or []:
                page = by_id.get(link["@id"]) or {}
                ocr = (media_of(page).get("images") or {}).get("ocr")
                targets.append((node["@id"], page.get("@id"), page.get("position"), ocr))
        elif node.get("fileFormatSimple") == "pdf":
            for page in media_of(node).get("pages") or []:
                targets.append((node["@id"], page["@id"], page["page"], page.get("ocr")))

    pages = []
    for book_id, page_id, position, ocr in targets:
        if not page_id or not ocr:
            continue
        try:
            raw = fs.read(f"cask:/{ocr['url'].removeprefix(CASK_API_PREFIX)}")
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
            "roles": document.get("roles", visibility.ROLES[visibility.DEFAULT]),
        })
    return pages


def _delete_pages(item_ids, opts={}):
    """Removes every page-search document belonging to the items (matched on
    their top-level @id), so a re-index with fewer pages leaves none behind.

    :param list[str] item_ids: Item ES @ids.
    :param dict opts: 'chunk_size' (int, default 500) — ids per request.
    :returns: How many documents were deleted (0 if the index doesn't exist).
    :rtype: int
    :raises httpx.HTTPStatusError: If the delete fails for any other reason.
    """
    chunk_size = opts.get("chunk_size", 500)
    deleted = 0
    for start in range(0, len(item_ids), chunk_size):
        resp = httpx.post(
            f"{ES_HOST}/{PAGE_SEARCH_INDEX}/_delete_by_query",
            params={"refresh": "true", "conflicts": "proceed"},
            json={"query": {"terms": {"@id": item_ids[start:start + chunk_size]}}},
        )
        if resp.status_code == 404:
            return deleted
        resp.raise_for_status()
        deleted += resp.json().get("deleted", 0)
    return deleted


def index_pages(document):
    """Replaces an item's page-search documents with ones built from its ES
    document (see build_page_documents()).

    :param dict document: build_document()'s result.
    :returns: How many page documents were written.
    :rtype: int
    :raises httpx.HTTPStatusError: If an ES write fails.
    """
    _delete_pages([document["@id"]])
    pages = build_page_documents(document)
    for page in pages:
        page_id = page["@graph"][0]["@id"]
        resp = httpx.put(f"{ES_HOST}/{PAGE_SEARCH_INDEX}/_doc/{urllib.parse.quote(page_id, safe='')}", json=page)
        resp.raise_for_status()
    return len(pages)
