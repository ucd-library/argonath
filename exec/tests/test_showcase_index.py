import json

import pytest

from digtk.lib import showcase_index
from digtk.lib.items import ItemLocation

ITEM = ItemLocation(
    ark="ark:/87287/d7abc1234", group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
)
MANIFEST_PATH = "cask:/silver/g/c/latest/item/public-frontend-derivatives/images/photo.tiff/images/manifest.json"


@pytest.fixture
def manifest(monkeypatch):
    """Serve one manifest.json at the image's derivatives path; anything else is missing."""
    served = {}

    def fake_read(path):
        if path in served:
            return served[path]
        raise FileNotFoundError(path)
    monkeypatch.setattr(showcase_index.fs, "read", fake_read)
    monkeypatch.setattr(showcase_index.item_record.fs, "read", fake_read)

    def set_doc(doc):
        served[MANIFEST_PATH] = json.dumps(doc).encode()
    return set_doc


def test_tiled_url_reads_the_top_level_tiled_entry(manifest):
    """write_manifest() merges image.generate()'s extra content into the manifest's
    top level — there's no nested "extra" key to look under."""
    manifest({
        "outputs": ["small.jpg", "medium.jpg", "large.jpg", "tiled.tif"],
        "tiled": {
            "size": [{"width": "3072", "height": "4000"}],
            "url": "cask://silver/g/c/latest/item/public-frontend-derivatives/images/photo.tiff/images/tiled.tif",
        },
    })

    url = showcase_index._tiled_iiif_url_for_filename(ITEM, "photo.tiff")

    assert url == (
        "/iiif/silver/g/c/latest/item/public-frontend-derivatives"
        "/images/photo.tiff/images/tiled.tif"
    )


def test_tiled_url_is_none_without_a_tiled_entry(manifest):
    manifest({"outputs": ["small.jpg"]})

    assert showcase_index._tiled_iiif_url_for_filename(ITEM, "photo.tiff") is None


def test_tiled_url_is_none_when_derivatives_were_never_generated(manifest):
    assert showcase_index._tiled_iiif_url_for_filename(ITEM, "photo.tiff") is None


class _Response:
    """Minimal httpx.Response stand-in for the ES calls."""

    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise showcase_index.httpx.HTTPStatusError("error", request=None, response=None)

    def json(self):
        return self._body


@pytest.fixture
def page_deletes(monkeypatch):
    """Stub the page-search _delete_by_query call; records each request."""
    calls = []

    def fake_post(url, params=None, json=None):
        calls.append({"url": url, "params": params, "json": json})
        return _Response(200, {"deleted": 2})
    monkeypatch.setattr(showcase_index.httpx, "post", fake_post)
    return calls


def test_normalize_creator_wraps_a_string_as_a_named_node():
    # the item index maps @graph.creator as an object — a bare string is a 400
    assert showcase_index._normalize_creator("Haeger, John Winthrop") == {"name": "Haeger, John Winthrop"}


def test_normalize_creator_lists_several_and_keeps_dicts():
    assert showcase_index._normalize_creator(["A", {"@id": "x", "name": "B"}, ""]) == [
        {"name": "A"}, {"@id": "x", "name": "B"},
    ]


def test_normalize_creator_is_none_when_missing():
    assert showcase_index._normalize_creator(None) is None
    assert showcase_index._normalize_creator([]) is None


def test_is_part_of_keeps_the_gold_collection_uri():
    gold = {"@id": "https://digital.ucdavis.edu/collection/ark:/87287/d7root"}
    assert showcase_index._is_part_of(gold) == [{"@id": "https://digital.ucdavis.edu/collection/ark:/87287/d7root"}]


def test_full_types_expands_bare_and_prefixed_schema_types():
    assert showcase_index._full_types("CreativeWork") == ["http://schema.org/CreativeWork"]
    assert showcase_index._full_types(
        ["schema:MediaObject", showcase_index.IMAGE_LIST_TYPE], "https://schema.org/MediaObject",
    ) == ["http://schema.org/MediaObject", showcase_index.IMAGE_LIST_TYPE]
    assert showcase_index._full_types(None, []) == []


def test_is_part_of_keeps_other_ids_and_drops_empties():
    assert showcase_index._is_part_of([{"@id": "/collection/x"}, {}, "y"]) == [
        {"@id": "/collection/x"}, {"@id": "y"},
    ]


def test_is_part_of_is_none_when_missing():
    assert showcase_index._is_part_of(None) is None
    assert showcase_index._is_part_of([{}]) is None


def test_client_media_points_images_at_the_cask_api():
    base = "cask://silver/g/c/latest/item/public-frontend-derivatives/images/photo.tiff/images"
    widths = {"small": 256, "medium": 512, "large": 1024}
    media = [
        {"contentUrl": f"{base}/{size}.jpg", "width": width, "height": width * 2}
        for size, width in widths.items()
    ]

    client_media = showcase_index._build_client_media(ITEM, media)

    assert client_media["images"]["medium"] == {
        "url": "/cask/api/fs/silver/g/c/latest/item/public-frontend-derivatives/images/photo.tiff/images/medium.jpg",
        "size": {"width": "512", "height": "1024"},
    }
    assert set(client_media["images"]) == {"small", "medium", "large"}


def test_client_media_omits_size_when_dimensions_are_missing():
    media = [{"contentUrl": "cask://silver/g/c/latest/item/x/images/large.jpg"}]
    assert "size" not in showcase_index._build_client_media(ITEM, media)["images"]["large"]


def test_client_media_adds_the_pages_ocr_layer_for_bookreader(manifest):
    base = "cask://silver/g/c/latest/item/public-frontend-derivatives/images/photo.tiff/images"
    manifest({
        "ocr": {
            "size": {"width": "1024", "height": "1325"},
            "imageSize": "large",
            "url": "cask://silver/g/c/latest/item/ocr/photo-ocr.djvu",
        },
    })
    media = [{"contentUrl": f"{base}/large.jpg", "width": 1024, "height": 1325}]

    images = showcase_index._build_client_media(ITEM, media, "photo.tiff")["images"]

    assert images["ocr"] == {
        "url": "/cask/api/fs/silver/g/c/latest/item/ocr/photo-ocr.djvu",
        "size": {"width": "1024", "height": "1325"},
        "imageSize": "large",
    }


def test_client_media_nests_the_tiled_iiif_url_under_images(manifest):
    """The fullscreen lightbox reads tiled off the page's images object, not
    off clientMedia itself."""
    base = "cask://silver/g/c/latest/item/public-frontend-derivatives/images/photo.tiff/images"
    manifest({"tiled": {"url": f"{base}/tiled.tif"}})
    media = [{"contentUrl": f"{base}/large.jpg", "width": 1024, "height": 1325}]

    client_media = showcase_index._build_client_media(ITEM, media, "photo.tiff")

    assert "tiled" not in client_media
    assert client_media["images"]["tiled"] == {
        "iiif": (
            "/iiif/silver/g/c/latest/item/public-frontend-derivatives"
            "/images/photo.tiff/images/tiled.tif"
        ),
    }


def test_client_media_has_no_ocr_without_a_manifest_entry(manifest):
    manifest({"outputs": ["large.jpg"]})
    media = [{"contentUrl": "cask://silver/g/c/latest/item/x/images/large.jpg"}]
    assert "ocr" not in showcase_index._build_client_media(ITEM, media, "photo.tiff")["images"]


# --- page search ----------------------------------------------------------------

ITEM_ID = "https://digital.ucdavis.edu/item/ark:/87287/d7abc1234"
DJVU_URL = "/cask/api/fs/silver/g/c/latest/item/ocr/page_001-ocr.djvu"


GOLD_PAGE_ID = f"{ITEM_ID}#imagelist/page_001.tif"


def _image_list_document(ocr=True):
    images = {}
    if ocr:
        images["ocr"] = {"url": DJVU_URL, "size": {"width": "1024", "height": "1325"}, "imageSize": "large"}
    return {
        "@id": ITEM_ID,
        "roles": ["public"],
        "@graph": [
            {"@id": ITEM_ID},
            {"@id": f"{ITEM_ID}#imagelist", "@type": ["http://schema.org/MediaObject", showcase_index.IMAGE_LIST_TYPE],
             "hasPart": [{"@id": GOLD_PAGE_ID}]},
            {"@id": GOLD_PAGE_ID, "position": 1},
        ],
        "clientMedia": {GOLD_PAGE_ID: {"images": images}},
    }


def test_with_page_size_adds_width_and_height_to_a_bare_object():
    xml_out = showcase_index._with_page_size("<OBJECT><PARAM/></OBJECT>", {"width": "1024", "height": "1325"})
    assert xml_out == '<OBJECT width="1024" height="1325"><PARAM/></OBJECT>'


def test_with_page_size_keeps_existing_dimensions():
    xml_in = '<OBJECT width="10" height="20"><PARAM/></OBJECT>'
    assert showcase_index._with_page_size(xml_in, {"width": "1024", "height": "1325"}) == xml_in


def test_build_page_documents_reads_each_pages_djvu_from_silver(monkeypatch):
    seen = []
    monkeypatch.setattr(showcase_index.fs, "read", lambda path: seen.append(path) or b"<OBJECT><HIDDENTEXT/></OBJECT>")

    pages = showcase_index.build_page_documents(_image_list_document())

    assert seen == ["cask:/silver/g/c/latest/item/ocr/page_001-ocr.djvu"]
    assert pages == [{
        "@id": ITEM_ID,
        "@graph": [{
            "@id": GOLD_PAGE_ID,
            "encodesCreativeWork": f"{ITEM_ID}#imagelist",
            "position": 1,
            "content": '<OBJECT width="1024" height="1325"><HIDDENTEXT/></OBJECT>',
        }],
        "roles": ["public"],
    }]


def test_build_page_documents_skips_pages_without_ocr_and_single_image_items(monkeypatch):
    monkeypatch.setattr(showcase_index.fs, "read", lambda path: b"<OBJECT/>")
    assert showcase_index.build_page_documents(_image_list_document(ocr=False)) == []
    assert showcase_index.build_page_documents({"@id": ITEM_ID, "@graph": [{"@id": ITEM_ID}]}) == []


def test_build_page_documents_skips_a_missing_djvu(monkeypatch):
    def missing(path):
        raise FileNotFoundError(path)
    monkeypatch.setattr(showcase_index.fs, "read", missing)
    assert showcase_index.build_page_documents(_image_list_document()) == []


def test_index_pages_replaces_the_items_page_documents(monkeypatch, page_deletes):
    monkeypatch.setattr(showcase_index.fs, "read", lambda path: b"<OBJECT/>")
    puts = []
    monkeypatch.setattr(showcase_index.httpx, "put", lambda url, json=None: puts.append(url) or _Response(201))

    assert showcase_index.index_pages(_image_list_document()) == 1

    assert page_deletes[0]["url"].endswith("/page-search-write/_delete_by_query")
    assert page_deletes[0]["json"] == {"query": {"term": {"@id": ITEM_ID}}}
    assert puts == [f"{showcase_index.ES_HOST}/page-search-write/_doc/{showcase_index.urllib.parse.quote(GOLD_PAGE_ID, safe='')}"]


# --- build_document: one node per media file ----------------------------------------

ARK = "ark:/87287/d7abc1234"
SILVER = "cask://silver/g/c/latest/item"
PFD = f"{SILVER}/public-frontend-derivatives"
LOCATION = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[],
)
RECORD_PATH = "cask:/gold/digital-dev/c/item/item.jsonld.json"
GOLD = f"https://digital.ucdavis.edu/item/{ARK}"


def _image_entries(filename):
    return [
        {"@type": "ImageObject", "contentUrl": f"{PFD}/images/{filename}/images/{size}.jpg",
         "width": w, "height": w * 2, "argonath:sourceGroup": "image"}
        for size, w in (("small", 256), ("medium", 512), ("large", 1024))
    ]


@pytest.fixture
def item_files(monkeypatch):
    """A fake CaskFS for one item: its gold item record, plus any extra
    files a test adds (manifests); everything else is missing. set_media()
    sets the item's published silver associatedMedia entries."""
    files = {RECORD_PATH: {"@id": ARK, "@graph": [
        {"@id": GOLD, "name": "An item", "creator": "Someone"},
    ]}}
    media = []

    def set_media(entries):
        media[:] = entries

    def set_gold(fragments, image=None):
        root = {"@id": GOLD, "name": "An item", "creator": "Someone"}
        if image:
            root["image"] = {"@id": f"{GOLD}#{image}"}
        nodes = [{"@id": f"{GOLD}#{f}"} for f in fragments]
        pages = [n for n in nodes if n["@id"].startswith(f"{GOLD}#imagelist/")]
        for n in nodes:
            if n["@id"] == f"{GOLD}#imagelist":
                n.update({"@type": ["schema:MediaObject", showcase_index.IMAGE_LIST_TYPE],
                          "hasPart": [{"@id": p["@id"]} for p in pages]})
        for i, page in enumerate(pages, start=1):
            page.update({"position": i, "contentUrl": f"cask://bronze/{i}.tif"})
        root["associatedMedia"] = [{"@id": n["@id"]} for n in nodes if n not in pages]
        files[RECORD_PATH]["@graph"] = [root] + nodes

    def fake_read(path):
        if path not in files:
            raise FileNotFoundError(path)
        return json.dumps(files[path]).encode()

    monkeypatch.setattr(showcase_index.items, "resolve", lambda ark, opts={}: LOCATION)
    monkeypatch.setattr(showcase_index.fs, "read", fake_read)
    monkeypatch.setattr(showcase_index.item_record.fs, "read", fake_read)
    monkeypatch.setattr(showcase_index, "_silver_media", lambda location: [dict(e) for e in media])
    files["set_media"] = set_media
    files["set_gold"] = set_gold
    return files


def _graph(document):
    return {node["@id"]: node for node in document["@graph"]}


def test_build_document_gives_audio_image_and_pdf_each_a_node(item_files):
    item_files["set_media"]([
        *_image_entries("logo.jpg"),
        {"@type": "ImageObject", "contentUrl": f"{PFD}/doc.pdf/images/1/large.jpg", "position": 0,
         "argonath:sourceGroup": "pdf"},
        {"@type": "AudioObject", "identifier": "talk", "contentUrl": f"{PFD}/talk.mp3/converted.mp3",
         "encodingFormat": "audio/mp3", "argonath:sourceGroup": "audio",
         "caption": {"contentUrl": f"{SILVER}/captions/output-en.vtt", "inLanguage": "en"}},
    ])
    item_files["cask:/silver/g/c/latest/item/public-frontend-derivatives/doc.pdf/images/manifest.json"] = {"pages": [
        {"page": 1,
         "large": {"size": {"width": "1024", "height": "1325"}, "url": f"{PFD}/doc.pdf/images/1/large.jpg"},
         "ocr": {"size": {"width": "1024", "height": "1325"}, "imageSize": "large",
                 "url": f"{SILVER}/doc/ocr/page-000/ocr.djvu"}},
    ]}

    item_files["set_gold"](["media/talk.mp3", "media/logo.jpg", "media/doc.pdf"], image="media/logo.jpg")

    document = showcase_index.build_document(ARK)
    root, nodes, media = document["@graph"][0], _graph(document), document["clientMedia"]

    item = f"{GOLD}#media"
    assert root["associatedMedia"] == [{"@id": f"{item}/talk.mp3"}, {"@id": f"{item}/logo.jpg"}, {"@id": f"{item}/doc.pdf"}]
    assert not any("clientMedia" in n or "sameAs" in n for n in document["@graph"])

    audio = nodes[f"{item}/talk.mp3"]
    assert audio["@type"] == ["http://schema.org/AudioObject"] and audio["fileFormatSimple"] == "audio"
    audio_media = media[audio["@id"]]
    assert audio_media["audio"]["url"] == "/cask/api/fs/silver/g/c/latest/item/public-frontend-derivatives/talk.mp3/converted.mp3"
    assert audio_media["download"] == [{"url": audio_media["audio"]["url"], "label": "mp3"}]
    assert audio["caption"] == [{"@id": "/cask/api/fs/silver/g/c/latest/item/captions/output-en.vtt", "language": "en"}]

    image = nodes[f"{item}/logo.jpg"]
    assert image["@type"] == ["http://schema.org/ImageObject"] and image["fileFormatSimple"] == "image"
    assert image["fileFormat"] == "image/jpeg"
    image_media = media[f"{GOLD}#media/logo.jpg"]
    assert image_media["download"] == [{"url": image_media["images"]["large"]["url"], "label": "jpg"}]

    pdf = nodes[f"{item}/doc.pdf"]
    assert pdf["@type"] == ["http://schema.org/ImageObject"]
    assert (pdf["fileFormat"], pdf["fileFormatSimple"]) == ("application/pdf", "pdf")
    pdf_media = media[f"{GOLD}#media/doc.pdf"]
    assert pdf_media["pages"] == [{
        "page": 1, "@id": f"{item}/doc.pdf:0",
        "large": {"url": "/cask/api/fs/silver/g/c/latest/item/public-frontend-derivatives/doc.pdf/images/1/large.jpg",
                  "size": {"width": "1024", "height": "1325"}},
        "ocr": {"url": "/cask/api/fs/silver/g/c/latest/item/doc/ocr/page-000/ocr.djvu",
                "size": {"width": "1024", "height": "1325"}, "imageSize": "large"},
    }]
    assert pdf_media["images"] == {"large": pdf_media["pages"][0]["large"]}
    assert pdf_media["download"] == []

    assert document["displayImage"] == {"@id": f"{GOLD}#media/logo.jpg", "images": image_media["images"]}


def test_build_document_puts_video_first_with_its_stream_and_captions(item_files):
    item_files["set_media"]([
        *_image_entries("poster.jpg"),
        {"@type": "VideoObject", "identifier": "movie", "contentUrl": f"{PFD}/movie.mp4/streaming/playlist.m3u8",
         "encodingFormat": "application/vnd.apple.mpegurl", "argonath:sourceGroup": "video",
         "caption": {"contentUrl": f"{SILVER}/captions/movie-en.vtt", "inLanguage": "en"}},
    ])

    item_files["set_gold"](["media/movie.mp4", "media/poster.jpg"], image="media/poster.jpg")

    document = showcase_index.build_document(ARK)
    root, nodes = document["@graph"][0], _graph(document)

    video_id = f"{GOLD}#media/movie.mp4"
    assert root["associatedMedia"][0] == {"@id": video_id}
    video = nodes[video_id]
    assert video["@type"] == ["http://schema.org/VideoObject"]
    assert (video["fileFormat"], video["fileFormatSimple"]) == ("video/mp4", "video")
    assert document["clientMedia"][video_id] == {
        "streamingVideo": {"manifest": "/cask/api/fs/silver/g/c/latest/item/public-frontend-derivatives/movie.mp4/streaming/playlist.m3u8"},
        "download": [],
    }
    assert video["caption"] == [{"@id": "/cask/api/fs/silver/g/c/latest/item/captions/movie-en.vtt", "language": "en"}]
    assert document["displayImage"]["@id"] == f"{GOLD}#media/poster.jpg"


def test_build_document_has_no_display_image_for_audio_only(item_files):
    item_files["set_media"]([
        {"@type": "AudioObject", "contentUrl": f"{PFD}/talk.mp3/converted.mp3", "argonath:sourceGroup": "audio"},
    ])
    item_files["set_gold"](["media/talk.mp3"])

    document = showcase_index.build_document(ARK)

    assert list(document["clientMedia"]) == [f"{GOLD}#media/talk.mp3"]
    assert "displayImage" not in document


def test_build_document_makes_an_image_list_for_several_images(item_files):
    item_files["set_media"]([*_image_entries("page_002.tif"), *_image_entries("page_001.tif")])
    item_files["set_gold"](
        ["imagelist", "imagelist/page_001.tif", "imagelist/page_002.tif"], image="imagelist/page_001.tif",
    )

    document = showcase_index.build_document(ARK)
    root, nodes, media = document["@graph"][0], _graph(document), document["clientMedia"]

    list_id = f"{GOLD}#imagelist"
    assert root["associatedMedia"] == [{"@id": list_id}]
    assert nodes[list_id]["@type"] == ["http://schema.org/MediaObject", showcase_index.IMAGE_LIST_TYPE]
    assert nodes[list_id]["hasPart"] == [{"@id": f"{list_id}/page_001.tif"}, {"@id": f"{list_id}/page_002.tif"}]
    page1 = nodes[f"{list_id}/page_001.tif"]
    assert (page1["name"], page1["position"]) == ("page_001.tif", 1)
    assert page1["fileFormat"] == "image/tiff"
    assert page1["@type"] == ["http://schema.org/ImageObject"]
    # gold fields are kept
    assert page1["contentUrl"] == "cask://bronze/1.tif"
    assert set(media) == {list_id, f"{list_id}/page_001.tif", f"{list_id}/page_002.tif"}
    assert media[list_id] == {"download": []}
    # a page's download is a single entry
    assert isinstance(media[page1["@id"]]["download"], dict)
    assert document["displayImage"] == {"@id": page1["@id"], "images": media[page1["@id"]]["images"]}


def test_build_document_matches_percent_encoded_gold_ids(item_files):
    item_files["set_media"]([*_image_entries("my page_001.tif"), *_image_entries("my page_002.tif")])
    item_files["set_gold"](["imagelist", "imagelist/my%20page_001.tif", "imagelist/my%20page_002.tif"])

    document = showcase_index.build_document(ARK)

    assert f"{GOLD}#imagelist/my%20page_002.tif" in document["clientMedia"]
    assert _graph(document)[f"{GOLD}#imagelist/my%20page_002.tif"]["name"] == "my page_002.tif"


def test_build_document_uses_the_first_pdf_page_without_an_image(item_files):
    item_files["set_media"]([
        {"@type": "ImageObject", "contentUrl": f"{PFD}/doc.pdf/images/1/large.jpg", "argonath:sourceGroup": "pdf"},
    ])
    item_files["set_gold"](["media/doc.pdf"])
    item_files["cask:/silver/g/c/latest/item/public-frontend-derivatives/doc.pdf/images/manifest.json"] = {"pages": [
        {"page": 1, "large": {"size": {"width": "1024", "height": "1325"}, "url": f"{PFD}/doc.pdf/images/1/large.jpg"}},
    ]}

    document = showcase_index.build_document(ARK)

    assert document["displayImage"]["@id"] == f"{GOLD}#media/doc.pdf"
    assert set(document["displayImage"]["images"]) == {"large"}


def test_build_document_skips_media_without_a_gold_node(item_files):
    item_files["set_media"](_image_entries("logo.jpg"))

    document = showcase_index.build_document(ARK)

    assert [n["@id"] for n in document["@graph"]] == [GOLD]
    assert document["clientMedia"] == {}


def test_build_document_reads_descriptive_fields_from_the_gold_root_node(item_files):
    document = showcase_index.build_document(ARK)
    root = document["@graph"][0]
    assert (root["name"], document["name"]) == ("An item", "An item")
    assert root["creator"]


def test_build_document_keeps_gold_ids_and_fields(item_files):
    item_files[RECORD_PATH]["@graph"][0].update({
        "@type": "CreativeWork",
        "ark": ARK,
        "publisher": "UC Davis Library",
        "license": {"@id": "http://rightsstatements.org/vocab/InC-NC/1.0/"},
        "dateCreated": {"@type": "http://www.w3.org/2001/XMLSchema#gYear", "@value": "2026"},
        "about": [{"@id": "http://id.worldcat.org/fast/1"}],
        "identifier": [
            {"@type": "PropertyValue", "propertyID": "ARK", "value": ARK},
            {"@type": "PropertyValue", "propertyID": "LocalID", "value": "D-91"},
            {"@type": "PropertyValue", "propertyID": "BoxFolder", "value": "Box 3"},
            {"@type": "PropertyValue", "propertyID": "Preservica", "value": "d7933a2e-ab3e"},
            {"@type": "PropertyValue", "propertyID": "CaskGoldPath", "value": "cask://gold/x/item.jsonld.json"},
        ],
    })

    document = showcase_index.build_document(ARK)
    root = document["@graph"][0]

    assert document["@id"] == root["@id"] == GOLD
    assert root["@type"] == ["http://schema.org/CreativeWork"]
    assert (root["ark"], root["license"], root["dateCreated"]["@value"]) == (
        ARK, {"@id": "http://rightsstatements.org/vocab/InC-NC/1.0/"}, "2026",
    )
    assert root["publisher"] == {"name": "UC Davis Library"}
    # internal ids are dropped, others kept
    assert root["identifier"] == [ARK, "D-91", "Box 3"]
    # about is text in the mapping
    assert root["subjects"] == [{"@id": "http://id.worldcat.org/fast/1"}] and "about" not in root


def test_build_document_links_the_item_to_its_collection(item_files):
    item_files[RECORD_PATH]["@graph"][0]["isPartOf"] = {
        "@id": "https://digital.ucdavis.edu/collection/ark:/87287/d7root",
    }
    root = showcase_index.build_document(ARK)["@graph"][0]
    assert root["isPartOf"] == [{"@id": "https://digital.ucdavis.edu/collection/ark:/87287/d7root"}]


def test_build_document_omits_is_part_of_without_a_collection(item_files):
    assert "isPartOf" not in showcase_index.build_document(ARK)["@graph"][0]


def test_silver_media_stamps_each_entry_with_its_source_group(monkeypatch):
    location = ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff", "cask:/bronze/g/c/latest/item/talk.mp3"],
    )
    monkeypatch.setitem(showcase_index._MEDIA_READERS, "image", lambda item: [{"contentUrl": "a"}])
    monkeypatch.setitem(showcase_index._MEDIA_READERS, "audio", lambda item: [{"contentUrl": "b"}])
    monkeypatch.setattr(showcase_index.captions, "attach_captions", lambda item, entries: False)

    entries = showcase_index._silver_media(location)

    assert [(e["contentUrl"], e["argonath:sourceGroup"]) for e in entries] == [("a", "image"), ("b", "audio")]


def test_build_document_requires_the_gold_record(item_files):
    del item_files[RECORD_PATH]
    with pytest.raises(FileNotFoundError):
        showcase_index.build_document(ARK)


def test_build_document_without_derivatives_is_just_the_root(item_files):
    item_files["set_media"]([])
    document = showcase_index.build_document(ARK)
    assert [n["@id"] for n in document["@graph"]] == [GOLD]
    assert "associatedMedia" not in document["@graph"][0]


def test_build_page_documents_covers_pdf_pages(monkeypatch):
    monkeypatch.setattr(showcase_index.fs, "read", lambda path: b"<OBJECT/>")
    pdf_id = f"{ITEM_ID}#media/doc.pdf"
    document = {"@id": ITEM_ID, "roles": ["public"], "@graph": [
        {"@id": ITEM_ID},
        {"@id": pdf_id, "fileFormatSimple": "pdf"},
    ], "clientMedia": {pdf_id: {"pages": [
        {"page": 1, "@id": f"{pdf_id}:0", "ocr": {"url": DJVU_URL, "size": {"width": "10", "height": "20"}}},
        {"page": 2, "@id": f"{pdf_id}:1"},  # no OCR layer: skipped
    ]}}}

    pages = showcase_index.build_page_documents(document)

    assert [p["@graph"][0]["@id"] for p in pages] == [f"{pdf_id}:0"]
    assert pages[0]["@graph"][0]["encodesCreativeWork"] == pdf_id
    assert pages[0]["@graph"][0]["position"] == 1


COLLECTION_ARK = "ark:/87287/d7root"
COLLECTION_RECORD = {"@id": COLLECTION_ARK, "@graph": [{
    "@id": f"https://digital.ucdavis.edu/collection/{COLLECTION_ARK}",
    "@type": ["schema:Collection"],
    "ark": COLLECTION_ARK,
    "name": "Haeger Collection",
    "description": "Photos.",
    "creator": "Haeger, John Winthrop",
    "about": [{"@id": "Aerial photographs"}],
    "identifier": [
        {"@type": "PropertyValue", "propertyID": "ARK", "value": COLLECTION_ARK},
        {"@type": "PropertyValue", "propertyID": "LocalID", "value": "MC-165"},
    ],
}]}


@pytest.fixture
def collection_gold(monkeypatch):
    """Stub the collection's gold record read."""
    monkeypatch.setattr(showcase_index.collection_record, "read", lambda name, opts={}: COLLECTION_RECORD)


def test_build_collection_document_shape(collection_gold):
    document = showcase_index.build_collection_document("c", {"item_count": 4})
    root = document["@graph"][0]

    assert document["@id"] == root["@id"] == f"https://digital.ucdavis.edu/collection/{COLLECTION_ARK}"
    assert root["@type"] == ["http://schema.org/Collection"]
    assert (document["name"], document["roles"], document["clientMedia"]) == ("Haeger Collection", ["public"], {})
    assert root["identifier"] == [COLLECTION_ARK, "MC-165"]
    assert root["subjects"] == [{"@id": "Aerial photographs"}]
    assert root["creator"] == {"name": "Haeger, John Winthrop"}
    assert root["itemCount"] == 4
    assert "hasPart" not in root and "publisher" not in root


def test_count_collection_items_queries_is_part_of(monkeypatch):
    calls = []
    monkeypatch.setattr(
        showcase_index.httpx, "post",
        lambda url, json=None: calls.append((url, json)) or _Response(200, {"count": 7}),
    )

    assert showcase_index.count_collection_items("/collection/x") == 7
    assert calls[0][0].endswith(f"/{showcase_index.ES_INDEX}/_count")
    assert calls[0][1] == {"query": {"term": {"@graph.isPartOf.@id": "/collection/x"}}}


def test_count_collection_items_is_zero_without_an_item_index(monkeypatch):
    monkeypatch.setattr(showcase_index.httpx, "post", lambda url, json=None: _Response(404))
    assert showcase_index.count_collection_items("/collection/x") == 0


def test_index_collection_writes_the_counted_document(monkeypatch, collection_gold):
    monkeypatch.setattr(showcase_index, "count_collection_items", lambda collection_id: 12)
    monkeypatch.setattr(showcase_index, "find_collection_image", lambda collection_id: {"@id": "/item/a/images/1"})
    puts = []
    monkeypatch.setattr(
        showcase_index.httpx, "put", lambda url, json=None: puts.append((url, json)) or _Response(201),
    )

    result = showcase_index.index_collection("c")

    url, document = puts[0]
    collection_id = f"https://digital.ucdavis.edu/collection/{COLLECTION_ARK}"
    assert url.endswith(f"/{showcase_index.COLLECTION_INDEX}/_doc/{showcase_index.urllib.parse.quote(collection_id, safe='')}")
    assert document["@graph"][0]["itemCount"] == 12
    assert document["@graph"][0]["image"] == {"@id": "/item/a/images/1"}
    assert result == {
        "collection": "c", "esId": collection_id,
        "index": showcase_index.COLLECTION_INDEX, "itemCount": 12, "status": "indexed",
    }


def test_index_collection_raises_on_a_failed_write(monkeypatch, collection_gold):
    monkeypatch.setattr(showcase_index, "count_collection_items", lambda collection_id: 0)
    monkeypatch.setattr(showcase_index, "find_collection_image", lambda collection_id: None)
    monkeypatch.setattr(showcase_index.httpx, "put", lambda url, json=None: _Response(400))
    with pytest.raises(showcase_index.httpx.HTTPStatusError):
        showcase_index.index_collection("c")


def test_index_collection_omits_image_when_no_item_has_one(monkeypatch, collection_gold):
    monkeypatch.setattr(showcase_index, "count_collection_items", lambda collection_id: 0)
    monkeypatch.setattr(showcase_index, "find_collection_image", lambda collection_id: None)
    puts = []
    monkeypatch.setattr(
        showcase_index.httpx, "put", lambda url, json=None: puts.append(json) or _Response(201),
    )

    showcase_index.index_collection("c")

    assert "image" not in puts[0]["@graph"][0]


def _item_hit(item_id, image=None):
    root = {"@id": item_id}
    if image:
        root["image"] = {"@id": image}
    return {"_source": {"@id": item_id, "@graph": [root, {"@id": f"{item_id}/images"}]}}


def test_find_collection_image_uses_the_first_item_with_an_image(monkeypatch):
    calls = []
    hits = [_item_hit("/item/a"), _item_hit("/item/b", "/item/b/images/1")]
    monkeypatch.setattr(
        showcase_index.httpx, "post",
        lambda url, json=None: calls.append((url, json)) or _Response(200, {"hits": {"hits": hits}}),
    )

    assert showcase_index.find_collection_image("/collection/x") == {"@id": "/item/b/images/1"}
    assert calls[0][0].endswith(f"/{showcase_index.ES_INDEX}/_search")
    assert calls[0][1]["query"] == {"term": {"@graph.isPartOf.@id": "/collection/x"}}


def test_find_collection_image_is_none_without_images(monkeypatch):
    monkeypatch.setattr(
        showcase_index.httpx, "post",
        lambda url, json=None: _Response(200, {"hits": {"hits": [_item_hit("/item/a")]}}),
    )
    assert showcase_index.find_collection_image("/collection/x") is None


def test_find_collection_image_is_none_without_an_item_index(monkeypatch):
    monkeypatch.setattr(showcase_index.httpx, "post", lambda url, json=None: _Response(404))
    assert showcase_index.find_collection_image("/collection/x") is None
