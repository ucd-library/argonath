import json

import pytest

from digtk.lib import derivative, fs, items, ocr_utils
from digtk.lib.items import ItemLocation
from digtk.models import accessibility, alt_utils, figures, layout, ocr, pdf_inplace, pdf_tag, pdf_utils, prep

ARK = "ark:/87287/d7abc1234"

ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
    sidecar_path="cask:/bronze/g/c/latest/item/photo.tiff.jsonld.json",
)

MULTI_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="box-1/folder-3",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/box-1/folder-3/photo-a.tiff",
        "cask:/bronze/g/c/latest/box-1/folder-3/photo-b.tiff",
    ],
    sidecar_path="cask:/bronze/g/c/latest/box-1/folder-3/photo-a.tiff.jsonld.json",
)

PDF_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/document.pdf"],
    sidecar_path="cask:/bronze/g/c/latest/item/document.pdf.jsonld.json",
)

MIXED_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/photo.tiff",
        "cask:/bronze/g/c/latest/item/document.pdf",
    ],
    sidecar_path="cask:/bronze/g/c/latest/item/photo.tiff.jsonld.json",
)

VIDEO_ONLY_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/reel.mp4"],
    sidecar_path="cask:/bronze/g/c/latest/item/reel.mp4.jsonld.json",
)

WORKSPACE_PDF_ITEM = ItemLocation(
    ark=None, group="jdoe", collection="box-12", time=None, item_path="",
    bronze_binary_paths=["cask:/workspace/jdoe/box-12/source/document.pdf"],
    dest_type="workspace", dest_prefix="derivatives",
)

METADATA = {"title": "Wine menu", "description": None, "date": "1973", "creator": "Society"}
EMPTY_METADATA = {"title": None, "description": None, "date": None, "creator": None}

PDF_PAGE_COUNT = 2


@pytest.fixture
def fake_derivative(monkeypatch):
    written = []

    def fake_write(*, type, collection, rel_path, data, source_files, opts={}):
        written.append({
            "type": type, "collection": collection, "rel_path": rel_path,
            "data": data, "source_files": source_files, "opts": opts,
        })
        if type == "workspace":
            return {"path": f"cask:/workspace/{opts['group']}/{collection}/{rel_path}", "links": []}
        return {"path": f"cask:/silver/{opts['group']}/{collection}/{opts['time']}/{rel_path}", "links": []}

    monkeypatch.setattr(derivative, "write", fake_write)
    return written


@pytest.fixture(autouse=True)
def scanned_pdfs(monkeypatch):
    """Fake PDFs read as all-scan pages, so 'auto' mode rebuilds them."""
    monkeypatch.setattr(pdf_inplace, "text_pages", lambda path: [False] * PDF_PAGE_COUNT)


@pytest.fixture(autouse=True)
def fake_prep(monkeypatch):
    """Stubs digtk.models.prep's staging/prep steps with deterministic local
    paths: an image binary is one page, a PDF binary is PDF_PAGE_COUNT pages."""
    calls = {"stage_pages": [], "ocr_image": [], "vision_image": [], "display_image": []}

    def fake_stage_pages(item, tmp_dir, opts={}):
        calls["stage_pages"].append(opts)
        formats = opts.get("formats", {"image", "pdf"})
        files = opts.get("files")
        pages = []
        for path in item.bronze_binary_paths:
            filename = path.rsplit("/", 1)[-1]
            if files and filename not in files:
                continue
            stem, ext = filename.rsplit(".", 1)
            base = {"source_path": path, "filename": filename, "stem": stem}
            if ext == "pdf" and "pdf" in formats:
                for i in range(PDF_PAGE_COUNT):
                    pages.append({**base, "kind": "pdf", "page_no": i + 1, "count": PDF_PAGE_COUNT,
                                  "page_tag": f"page-{i:03d}", "local_path": f"{tmp_dir}/{stem}-{i}.tif",
                                  "pdf_path": f"{tmp_dir}/{filename}"})
            elif ext == "tiff" and "image" in formats:
                pages.append({**base, "kind": "image", "page_no": None, "count": None,
                              "page_tag": None, "local_path": f"{tmp_dir}/{filename}"})
        return pages

    def fake_ocr_image(page, tmp_dir, opts={}):
        calls["ocr_image"].append({"page": page, "opts": opts})
        return {"local_path": f"{page['local_path']}-prep.jpg", "deskew": False, "angle": 0.0}

    def fake_vision_image(page, tmp_dir, opts={}):
        calls["vision_image"].append(page)
        return f"{page['local_path']}-vision.jpg"

    def fake_display_image(page, tmp_dir, opts={}):
        calls["display_image"].append({"page": page, "opts": opts})
        return f"{page['local_path']}-display.jpg"

    monkeypatch.setattr(prep, "stage_pages", fake_stage_pages)
    monkeypatch.setattr(prep, "ocr_image", fake_ocr_image)
    monkeypatch.setattr(prep, "vision_image", fake_vision_image)
    monkeypatch.setattr(prep, "display_image", fake_display_image)
    return calls


def _stub_lookups(monkeypatch, item, metadata=METADATA):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: item)
    monkeypatch.setattr(items, "metadata_for", lambda location: metadata)


def test_generate_alt_text_single_image(monkeypatch, fake_derivative):
    _stub_lookups(monkeypatch, ITEM)
    seen = {}

    def fake_generate_alt(image, metadata, opts={}):
        seen["image"] = image
        seen["metadata"] = metadata
        return "A wine menu.", None, None, []

    monkeypatch.setattr(alt_utils, "generate_alt", fake_generate_alt)

    result = accessibility.generate_alt_text(ARK)

    assert result["status"] == "success"
    assert result["ark"] == ARK
    assert "pdfs" not in result
    assert seen["image"].endswith("photo.tiff-vision.jpg")
    assert seen["metadata"] == METADATA

    image_result = result["images"]["photo.tiff"]
    assert image_result["alt"] == "A wine menu."
    assert image_result["description"] is None
    assert image_result["flags"] == []

    write = fake_derivative[0]
    assert write["type"] == "silver"
    assert write["collection"] == "c"
    assert write["rel_path"] == "item/accessibility/alt-text.json"
    assert write["source_files"] == [
        "cask:/bronze/g/c/latest/item/photo.tiff",
        "cask:/bronze/g/c/latest/item/photo.tiff.jsonld.json",
    ]
    assert json.loads(write["data"]) == {
        "alt": "A wine menu.", "description": None, "long_description": None, "flags": [],
    }
    assert write["opts"]["link_relation"] == accessibility.ALT_TEXT_LINK_RELATION


def test_generate_alt_text_disambiguates_multi_image_filenames(monkeypatch, fake_derivative):
    _stub_lookups(monkeypatch, MULTI_ITEM)
    monkeypatch.setattr(alt_utils, "generate_alt", lambda image, metadata, opts={}: ("An image.", None, None, []))

    result = accessibility.generate_alt_text(ARK)

    assert set(result["images"]) == {"photo-a.tiff", "photo-b.tiff"}
    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert rel_paths == {
        "box-1/folder-3/accessibility/photo-a-alt-text.json",
        "box-1/folder-3/accessibility/photo-b-alt-text.json",
    }


def test_generate_alt_text_forwards_opts_to_generate_alt(monkeypatch, fake_derivative):
    _stub_lookups(monkeypatch, ITEM)
    seen = {}

    def fake_generate_alt(image, metadata, opts={}):
        seen["opts"] = opts
        return "alt", None, None, []

    monkeypatch.setattr(alt_utils, "generate_alt", fake_generate_alt)

    accessibility.generate_alt_text(ARK, opts={"want_description": True, "max_alt_chars": 100})

    assert seen["opts"] == {"want_description": True, "max_alt_chars": 100}


def test_generate_alt_text_surfaces_review_flags(monkeypatch, fake_derivative):
    _stub_lookups(monkeypatch, ITEM)
    monkeypatch.setattr(alt_utils, "generate_alt", lambda image, metadata, opts={}: ("", None, None, ["ERROR:unparseable-model-output"]))

    result = accessibility.generate_alt_text(ARK)

    assert result["images"]["photo.tiff"]["flags"] == ["ERROR:unparseable-model-output"]


# --- PDF pages -----------------------------------------------------------------

def test_generate_alt_text_pdf_pages(monkeypatch, fake_derivative):
    _stub_lookups(monkeypatch, PDF_ITEM)
    seen_calls = []

    def fake_generate_alt(image, metadata, opts={}):
        seen_calls.append((image, metadata))
        return "A page.", None, None, []

    monkeypatch.setattr(alt_utils, "generate_alt", fake_generate_alt)

    result = accessibility.generate_alt_text(ARK)

    assert "images" not in result
    pages = result["pdfs"]["document.pdf"]["pages"]
    assert set(pages) == {"1", "2"}
    assert pages["1"]["alt"] == "A page."

    images_by_page = {metadata["page"]: image for image, metadata in seen_calls}
    assert images_by_page[(1, 2)].endswith("document-0.tif-vision.jpg")
    assert images_by_page[(2, 2)].endswith("document-1.tif-vision.jpg")
    for _, metadata in seen_calls:
        assert metadata["title"] == "Wine menu"

    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert rel_paths == {
        "item/accessibility/document.pdf/1/alt-text.json",
        "item/accessibility/document.pdf/2/alt-text.json",
    }


def test_generate_alt_text_never_reads_showcase_derivatives(monkeypatch, fake_derivative):
    """Regression guard: alt text is generated from its own resized image
    (prep.vision_image()), not showcase's published large.jpg/manifest.json."""
    _stub_lookups(monkeypatch, MIXED_ITEM)
    monkeypatch.setattr(alt_utils, "generate_alt", lambda image, metadata, opts={}: ("alt", None, None, []))

    def fake_read(path):
        raise AssertionError(f"unexpected fs.read({path})")
    monkeypatch.setattr(fs, "read", fake_read)

    accessibility.generate_alt_text(ARK)


def test_generate_alt_text_workspace_item_has_no_sidecar_lineage(monkeypatch, fake_derivative):
    monkeypatch.setattr(items, "metadata_for", lambda location: EMPTY_METADATA)
    monkeypatch.setattr(alt_utils, "generate_alt", lambda image, metadata, opts={}: ("alt", None, None, []))

    result = accessibility.generate_alt_text(WORKSPACE_PDF_ITEM)

    assert result["ark"] is None
    assert set(result["pdfs"]["document.pdf"]["pages"]) == {"1", "2"}
    for write in fake_derivative:
        assert write["type"] == "workspace"
        assert write["source_files"] == ["cask:/workspace/jdoe/box-12/source/document.pdf"]
    assert {w["rel_path"] for w in fake_derivative} == {
        "derivatives/accessibility/document.pdf/1/alt-text.json",
        "derivatives/accessibility/document.pdf/2/alt-text.json",
    }


# --- Mixed / unsupported formats -------------------------------------------------

def test_generate_alt_text_handles_mixed_image_and_pdf_item(monkeypatch, fake_derivative):
    _stub_lookups(monkeypatch, MIXED_ITEM)
    monkeypatch.setattr(alt_utils, "generate_alt", lambda image, metadata, opts={}: ("alt", None, None, []))

    result = accessibility.generate_alt_text(ARK)

    assert set(result["images"]) == {"photo.tiff"}
    assert set(result["pdfs"]) == {"document.pdf"}
    assert set(result["pdfs"]["document.pdf"]["pages"]) == {"1", "2"}


def test_generate_alt_text_raises_when_no_image_or_pdf_binaries(monkeypatch):
    _stub_lookups(monkeypatch, VIDEO_ONLY_ITEM)

    with pytest.raises(ValueError, match="no image or PDF binaries found"):
        accessibility.generate_alt_text(ARK)


# --- tag_pdf ---------------------------------------------------------------

ALT_1_PATH = "cask:/silver/g/c/latest/item/accessibility/document.pdf/1/alt-text.json"
ALT_2_PATH = "cask:/silver/g/c/latest/item/accessibility/document.pdf/2/alt-text.json"
LAYOUT_1_PATH = "cask:/silver/g/c/latest/item/accessibility/document.pdf/1/layout.json"
LAYOUT_2_PATH = "cask:/silver/g/c/latest/item/accessibility/document.pdf/2/layout.json"


def _make_fs_read(extra=None):
    """fs.read stub: serves only `extra`; anything else (alt-text.json/
    layout.json paths, by default) raises FileNotFoundError."""
    table = dict(extra or {})

    def fake_read(path):
        if path in table:
            return table[path]
        raise FileNotFoundError(path)
    return fake_read


@pytest.fixture
def fake_pdf_pipeline(monkeypatch):
    """Mocks every external step tag_pdf() calls out to, recording each call.
    ocr_utils.run_tesseract/pdf_utils.hocr_words are stubbed with fixed word
    geometry; pdf_tag.tag_scan_pdf writes real bytes to dest_path (tag_pdf()
    reads that file back to publish it) and returns fixed stats;
    pdf_tag.validate_ua returns a fixed compliance result."""
    calls = {"build_searchable_pdf": None, "tag_scan_pdf": None, "validate_ua": None, "run_tesseract": []}

    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda image_path, output_base, opts={}: (
        calls["run_tesseract"].append(image_path) or {"hocr_path": output_base + ".hocr", "txt_path": output_base + ".txt", "confidence": 90.0}
    ))
    monkeypatch.setattr(
        pdf_utils, "hocr_words",
        lambda hocr_path, min_conf=0.0: [{"text": "hi", "bbox": [0, 0, 10, 10], "conf": 90.0}],
    )

    def fake_build_searchable_pdf(image_paths, words_per_page, dest_path, opts={}):
        calls["build_searchable_pdf"] = {"image_paths": image_paths, "words_per_page": words_per_page, "opts": opts}
        with open(dest_path, "wb") as f:
            f.write(b"SEARCHABLEPDF")
        return {"pages": len(image_paths), "words_placed": 2}
    monkeypatch.setattr(pdf_utils, "build_searchable_pdf", fake_build_searchable_pdf)

    def fake_tag_scan_pdf(src_path, dest_path, title, opts={}):
        calls["tag_scan_pdf"] = {"src_path": src_path, "title": title, "opts": opts}
        with open(dest_path, "wb") as f:
            f.write(b"TAGGEDPDF")
        return {"pages": 2, "words": 2, "lines": 2, "tables": 0, "headings": 0, "captions": 0, "artifacts": 0}
    monkeypatch.setattr(pdf_tag, "tag_scan_pdf", fake_tag_scan_pdf)

    def fake_validate_ua(pdf_path, opts={}):
        calls["validate_ua"] = {"pdf_path": pdf_path, "opts": opts}
        return {"compliant": True, "passed_rules": 10, "failed_rules": 0}
    monkeypatch.setattr(pdf_tag, "validate_ua", fake_validate_ua)

    return calls


def test_tag_pdf_embeds_display_render_and_ocrs_prep_render(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    accessibility.tag_pdf(ARK, opts={"enhance": "gray"})

    assert [p.rsplit("/", 1)[-1] for p in fake_pdf_pipeline["run_tesseract"]] == [
        "document-0.tif-prep.jpg", "document-1.tif-prep.jpg",
    ]
    assert [p.rsplit("/", 1)[-1] for p in fake_pdf_pipeline["build_searchable_pdf"]["image_paths"]] == [
        "document-0.tif-display.jpg", "document-1.tif-display.jpg",
    ]
    # Matches the OCR render's enhance and deskew.
    for call in fake_prep["display_image"]:
        assert call["opts"]["enhance"] == "gray"
        assert call["opts"]["deskew"] is False
    for call in fake_prep["ocr_image"]:
        assert call["opts"]["enhance"] == "gray"


def test_tag_pdf_builds_tags_and_publishes(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    result = accessibility.tag_pdf(ARK)

    assert result["status"] == "success"
    pdf_result = result["pdfs"]["document.pdf"]
    assert pdf_result["pages"] == 2
    assert pdf_result["compliant"] is True
    assert pdf_result["passed_rules"] == 10

    assert len(fake_pdf_pipeline["run_tesseract"]) == 2  # one Tesseract pass per page
    assert fake_pdf_pipeline["run_tesseract"][0].endswith("document-0.tif-prep.jpg")
    assert fake_pdf_pipeline["build_searchable_pdf"]["opts"]["title"] == "Wine menu"
    assert fake_pdf_pipeline["tag_scan_pdf"]["opts"]["alt"] == [None, None]
    assert fake_pdf_pipeline["tag_scan_pdf"]["opts"]["layouts"] is None

    tagged_write = next(w for w in fake_derivative if w["rel_path"].endswith("tagged.pdf"))
    assert tagged_write["rel_path"] == "item/accessibility/document.pdf/tagged.pdf"
    assert tagged_write["data"] == b"TAGGEDPDF"
    assert set(tagged_write["source_files"]) == {
        "cask:/bronze/g/c/latest/item/document.pdf.jsonld.json",
        "cask:/bronze/g/c/latest/item/document.pdf",
    }
    assert tagged_write["opts"]["link_relation"] == accessibility.TAGGED_PDF_LINK_RELATION


def test_tag_pdf_does_not_forward_document_lang_to_tesseract(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep):
    """Regression test: opts['lang'] is a document language (e.g. 'en-US') for
    pdf_tag.tag_scan_pdf, not a Tesseract language code — forwarding it
    verbatim into ocr_utils.run_tesseract() broke real runs (Tesseract has no
    'en-US' traineddata, only 'eng')."""
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())
    seen_tesseract_opts = []
    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda image_path, output_base, opts={}: (
        seen_tesseract_opts.append(opts) or {"hocr_path": output_base + ".hocr", "txt_path": output_base + ".txt", "confidence": 90.0}
    ))

    accessibility.tag_pdf(ARK, opts={"lang": "en-US"})

    assert seen_tesseract_opts
    for opts in seen_tesseract_opts:
        assert "lang" not in opts
    # Also never reaches the orientation comparison's own Tesseract passes.
    for call in fake_prep["ocr_image"]:
        assert "lang" not in call["opts"]
    assert fake_pdf_pipeline["tag_scan_pdf"]["opts"]["lang"] == "en-US"


def test_tag_pdf_applies_min_conf_to_the_text_layer_not_tesseract(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())
    seen_tesseract_opts = []
    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda image_path, output_base, opts={}: (
        seen_tesseract_opts.append(opts) or {"hocr_path": output_base + ".hocr", "txt_path": output_base + ".txt", "confidence": 90.0}
    ))
    monkeypatch.setattr(pdf_utils, "hocr_words", lambda hocr_path, min_conf=0.0: [
        {"text": "keep", "bbox": [0, 0, 10, 10], "conf": 90.0},
        {"text": "drop", "bbox": [0, 0, 10, 10], "conf": 20.0},
    ])

    accessibility.tag_pdf(ARK, opts={"min_conf": 70.0})

    assert seen_tesseract_opts
    for opts in seen_tesseract_opts:
        assert "min_conf" not in opts
    assert [[w["text"] for w in page] for page in fake_pdf_pipeline["build_searchable_pdf"]["words_per_page"]] == [
        ["keep"], ["keep"],
    ]


def test_tag_pdf_uses_alt_text_when_present(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read({
        ALT_1_PATH: json.dumps({"alt": "Page one.", "description": None, "flags": []}).encode(),
    }))

    accessibility.tag_pdf(ARK)

    assert fake_pdf_pipeline["tag_scan_pdf"]["opts"]["alt"] == ["Page one.", None]
    tagged_write = next(w for w in fake_derivative if w["rel_path"].endswith("tagged.pdf"))
    assert ALT_1_PATH in tagged_write["source_files"]


def test_tag_pdf_skips_validation_when_disabled(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    result = accessibility.tag_pdf(ARK, opts={"validate": False})

    assert fake_pdf_pipeline["validate_ua"] is None
    assert "compliant" not in result["pdfs"]["document.pdf"]


def test_tag_pdf_generates_and_caches_layout_when_tables_enabled(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())
    seen_layout_calls = []
    monkeypatch.setattr(layout, "page_layout", lambda image, opts={}: (
        seen_layout_calls.append(image) or {"blocks": [{"type": "heading", "text": "Title"}]}
    ))

    accessibility.tag_pdf(ARK, opts={"tables": True})

    assert [image.rsplit("/", 1)[-1] for image in seen_layout_calls] == [
        "document-0.tif-vision.jpg", "document-1.tif-vision.jpg",
    ]
    assert fake_pdf_pipeline["tag_scan_pdf"]["opts"]["layouts"] == [
        {"blocks": [{"type": "heading", "text": "Title"}]},
        {"blocks": [{"type": "heading", "text": "Title"}]},
    ]
    layout_writes = [w for w in fake_derivative if w["rel_path"].endswith("layout.json")]
    assert {w["rel_path"] for w in layout_writes} == {
        "item/accessibility/document.pdf/1/layout.json",
        "item/accessibility/document.pdf/2/layout.json",
    }
    assert layout_writes[0]["opts"]["link_relation"] == accessibility.LAYOUT_LINK_RELATION
    assert layout_writes[0]["source_files"] == ["cask:/bronze/g/c/latest/item/document.pdf"]
    tagged_write = next(w for w in fake_derivative if w["rel_path"].endswith("tagged.pdf"))
    assert "cask:/silver/g/c/latest/item/accessibility/document.pdf/1/layout.json" in tagged_write["source_files"]


def test_tag_pdf_reuses_cached_layout_without_calling_vision(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    cached = {"version": layout.LAYOUT_VERSION, "blocks": [{"type": "text", "text": "cached"}]}
    cached_layout = json.dumps(cached).encode()
    monkeypatch.setattr(fs, "read", _make_fs_read({LAYOUT_1_PATH: cached_layout, LAYOUT_2_PATH: cached_layout}))
    seen_layout_calls = []
    monkeypatch.setattr(layout, "page_layout", lambda image, opts={}: seen_layout_calls.append(image))

    accessibility.tag_pdf(ARK, opts={"tables": True})

    assert seen_layout_calls == []
    assert fake_pdf_pipeline["tag_scan_pdf"]["opts"]["layouts"] == [cached, cached]
    assert not any(w["rel_path"].endswith("layout.json") for w in fake_derivative)


def test_tag_pdf_regenerates_layout_cached_by_older_prompt(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    stale = json.dumps({"blocks": [{"type": "text", "text": "old"}]}).encode()
    monkeypatch.setattr(fs, "read", _make_fs_read({LAYOUT_1_PATH: stale, LAYOUT_2_PATH: stale}))
    fresh = {"version": layout.LAYOUT_VERSION, "blocks": [{"type": "heading", "level": 1, "text": "New"}]}
    monkeypatch.setattr(layout, "page_layout", lambda image, opts={}: fresh)

    accessibility.tag_pdf(ARK, opts={"tables": True})

    assert fake_pdf_pipeline["tag_scan_pdf"]["opts"]["layouts"] == [fresh, fresh]
    layout_writes = [w for w in fake_derivative if w["rel_path"].endswith("layout.json")]
    assert len(layout_writes) == 2
    assert json.loads(layout_writes[0]["data"])["version"] == layout.LAYOUT_VERSION


def test_tag_pdf_forwards_paragraphs_to_tagger(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    accessibility.tag_pdf(ARK, opts={"paragraphs": True})

    assert fake_pdf_pipeline["tag_scan_pdf"]["opts"]["paragraphs"] is True


def test_tag_pdf_only_stages_pdf_binaries(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep):
    _stub_lookups(monkeypatch, MIXED_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    result = accessibility.tag_pdf(ARK)

    assert fake_prep["stage_pages"] == [{"formats": {"pdf"}, "files": None, "rasterize": False}]
    assert set(result["pdfs"]) == {"document.pdf"}


def test_tag_pdf_workspace_item_titles_from_filename(monkeypatch, fake_derivative, fake_pdf_pipeline):
    monkeypatch.setattr(items, "metadata_for", lambda location: EMPTY_METADATA)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    result = accessibility.tag_pdf(WORKSPACE_PDF_ITEM)

    assert result["ark"] is None
    assert fake_pdf_pipeline["tag_scan_pdf"]["title"] == "document"
    tagged_write = next(w for w in fake_derivative if w["rel_path"].endswith("tagged.pdf"))
    assert tagged_write["type"] == "workspace"
    assert tagged_write["rel_path"] == "derivatives/accessibility/document.pdf/tagged.pdf"
    assert tagged_write["source_files"] == ["cask:/workspace/jdoe/box-12/source/document.pdf"]


def test_tag_pdf_raises_when_no_pdf_binaries(monkeypatch):
    _stub_lookups(monkeypatch, ITEM)

    with pytest.raises(ValueError, match="no PDF binaries found"):
        accessibility.tag_pdf(ARK)


# --- per-page failures ---------------------------------------------------------

def test_generate_alt_text_skips_a_failed_page_and_keeps_the_rest(monkeypatch, capsys, fake_derivative):
    _stub_lookups(monkeypatch, PDF_ITEM)

    def flaky_generate_alt(image, metadata, opts={}):
        if metadata["page"] == (1, 2):
            raise RuntimeError("Samwise chat completion failed after 6 tries: Read timed out.")
        return "Page two.", None, None, []
    monkeypatch.setattr(alt_utils, "generate_alt", flaky_generate_alt)

    result = accessibility.generate_alt_text(ARK)

    assert result["status"] == "partial"
    assert set(result["pdfs"]["document.pdf"]["pages"]) == {"2"}
    assert result["failed"] == [{
        "file": "document.pdf", "page": 1, "stage": "alt-text",
        "error": "Samwise chat completion failed after 6 tries: Read timed out.",
    }]
    assert {w["rel_path"] for w in fake_derivative} == {"item/accessibility/document.pdf/2/alt-text.json"}
    assert "document.pdf page 1/2: alt text failed, skipping page" in capsys.readouterr().err


def test_generate_alt_text_raises_when_every_page_fails(monkeypatch, fake_derivative):
    _stub_lookups(monkeypatch, PDF_ITEM)

    def always_fails(image, metadata, opts={}):
        raise RuntimeError("Samwise down")
    monkeypatch.setattr(alt_utils, "generate_alt", always_fails)

    with pytest.raises(RuntimeError, match="alt text failed for all 2 page"):
        accessibility.generate_alt_text(ARK)


def test_generate_alt_text_files_opt_keeps_whole_item_naming(monkeypatch, fake_derivative, fake_prep):
    """One file of a multi-image item (a per-file Dagster step) still gets the
    item's multi-image filename prefix, so it writes the same path a whole-item
    run would."""
    _stub_lookups(monkeypatch, MULTI_ITEM)
    monkeypatch.setattr(alt_utils, "generate_alt", lambda image, metadata, opts={}: ("alt", None, None, []))

    result = accessibility.generate_alt_text(ARK, opts={"files": {"photo-b.tiff"}})

    assert set(result["images"]) == {"photo-b.tiff"}
    assert [w["rel_path"] for w in fake_derivative] == ["box-1/folder-3/accessibility/photo-b-alt-text.json"]
    assert fake_prep["stage_pages"] == [{"files": {"photo-b.tiff"}}]


def test_tag_pdf_files_opt_only_tags_that_pdf(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep):
    item = ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=["cask:/bronze/g/c/latest/item/a.pdf", "cask:/bronze/g/c/latest/item/b.pdf"],
    )
    _stub_lookups(monkeypatch, item)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    result = accessibility.tag_pdf(ARK, opts={"files": {"b.pdf"}})

    assert set(result["pdfs"]) == {"b.pdf"}
    assert fake_prep["stage_pages"] == [{"formats": {"pdf"}, "files": {"b.pdf"}, "rasterize": False}]


# --- reusing the ocr step's output --------------------------------------------

TEXT_DIR_PATH = "cask:/silver/g/c/latest/item/text"
HOCR_PAGE = (
    "<div class='ocr_page'><span class='ocrx_word' title='bbox 10 10 90 40; x_wconf 95'>The</span>"
    "<span class='ocrx_word' title='bbox 100 10 200 40; x_wconf 90'>hrown</span>"
    "<span class='ocrx_word' title='bbox 210 10 300 40; x_wconf 92'>dog</span></div>"
)


def _ocr_outputs(record=None, transcript="The brown dog", corrected=None):
    """fs.read table with both pages' ocr-step outputs."""
    record = {"tesseract_confidence": 90.0, "deskew": True, "prep": prep.geometry_opts({}),
              **(record or {})}
    table = {}
    for tag in ("page-000", "page-001"):
        base = f"{TEXT_DIR_PATH}/{tag}"
        table[f"{base}/ocr.hocr"] = HOCR_PAGE.encode()
        table[f"{base}/ocr-result.json"] = json.dumps(record).encode()
        if transcript is not None:
            table[f"{base}/transcript.txt"] = transcript.encode()
        if corrected is not None:
            table[f"{base}/ocr-corrected.json"] = json.dumps(corrected).encode()
    return table


def _no_tesseract(monkeypatch):
    calls = []
    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda *a, **k: calls.append(a) or {})
    return calls


def _texts(words_per_page):
    return [[w["text"] for w in page] for page in words_per_page]


def test_tag_pdf_reuses_ocr_step_output(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read(_ocr_outputs()))
    tesseract_calls = _no_tesseract(monkeypatch)

    result = accessibility.tag_pdf(ARK)

    assert tesseract_calls == []
    assert fake_prep["ocr_image"] == []
    assert [c["opts"]["deskew"] for c in fake_prep["display_image"]] == [True, True]
    assert _texts(fake_pdf_pipeline["build_searchable_pdf"]["words_per_page"]) == [["The", "hrown", "dog"]] * 2
    pdf_result = result["pdfs"]["document.pdf"]
    assert pdf_result["reused_ocr_pages"] == 2
    tagged = next(w for w in fake_derivative if w["rel_path"].endswith("tagged.pdf"))
    assert f"{TEXT_DIR_PATH}/page-000/ocr.hocr" in tagged["source_files"]


def test_tag_pdf_reruns_tesseract_when_ocr_geometry_differs(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep):
    _stub_lookups(monkeypatch, PDF_ITEM)
    stale = _ocr_outputs(record={"prep": {**prep.geometry_opts({}), "enhance": "gray"}})
    monkeypatch.setattr(fs, "read", _make_fs_read(stale))

    result = accessibility.tag_pdf(ARK)

    assert len(fake_pdf_pipeline["run_tesseract"]) == 2
    assert len(fake_prep["ocr_image"]) == 2
    assert result["pdfs"]["document.pdf"]["reused_ocr_pages"] == 0


def test_tag_pdf_reuse_ocr_false_always_runs_tesseract(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read(_ocr_outputs()))

    accessibility.tag_pdf(ARK, opts={"reuse_ocr": False})

    assert len(fake_pdf_pipeline["run_tesseract"]) == 2


def test_tag_pdf_publishes_searchable_pdf_and_document_text(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read(_ocr_outputs(transcript="Page text\n")))
    _no_tesseract(monkeypatch)

    result = accessibility.tag_pdf(ARK)

    searchable = next(w for w in fake_derivative if w["rel_path"].endswith("searchable.pdf"))
    assert searchable["rel_path"] == "item/accessibility/document.pdf/searchable.pdf"
    assert searchable["data"] == b"SEARCHABLEPDF"
    assert searchable["opts"]["link_relation"] == accessibility.SEARCHABLE_PDF_LINK_RELATION
    text = next(w for w in fake_derivative if w["rel_path"].endswith("text.txt"))
    assert text["data"].decode() == "Page text\fPage text\n"
    assert result["pdfs"]["document.pdf"]["text_path"].endswith("document.pdf/text.txt")


def test_tag_pdf_text_falls_back_to_ocr_words(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    accessibility.tag_pdf(ARK)

    text = next(w for w in fake_derivative if w["rel_path"].endswith("text.txt"))
    assert text["data"].decode() == "hi\fhi\n"


def test_tag_pdf_correct_uses_vision_spelling(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read(_ocr_outputs()))
    _no_tesseract(monkeypatch)

    result = accessibility.tag_pdf(ARK, opts={"correct": True})

    assert _texts(fake_pdf_pipeline["build_searchable_pdf"]["words_per_page"]) == [["The", "brown", "dog"]] * 2
    pdf_result = result["pdfs"]["document.pdf"]
    assert pdf_result["corrections"] == 2
    assert pdf_result["uncorrected_pages"] == []
    corrected = [w for w in fake_derivative if w["rel_path"].endswith("ocr-corrected.json")]
    assert [w["rel_path"] for w in corrected] == [
        "item/text/page-000/ocr-corrected.json", "item/text/page-001/ocr-corrected.json",
    ]
    doc = json.loads(corrected[0]["data"])
    assert doc["changes"] == [{"i": 1, "old": "hrown", "new": "brown"}]


def test_tag_pdf_correct_reuses_current_correction(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    import hashlib
    cached = {"hocr_sha1": hashlib.sha1(HOCR_PAGE.encode()).hexdigest(),
              "words": [{"text": "Cached", "bbox": [0, 0, 10, 10], "conf": 90.0}],
              "changes": [{"i": 0, "old": "x", "new": "Cached"}]}
    monkeypatch.setattr(fs, "read", _make_fs_read(_ocr_outputs(corrected=cached)))
    _no_tesseract(monkeypatch)

    accessibility.tag_pdf(ARK, opts={"correct": True})

    assert _texts(fake_pdf_pipeline["build_searchable_pdf"]["words_per_page"]) == [["Cached"]] * 2
    assert not any(w["rel_path"].endswith("ocr-corrected.json") for w in fake_derivative)


def test_tag_pdf_correct_ignores_correction_of_older_hocr(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    stale = {"hocr_sha1": "0" * 40, "words": [{"text": "Stale", "bbox": [0, 0, 1, 1]}], "changes": []}
    monkeypatch.setattr(fs, "read", _make_fs_read(_ocr_outputs(corrected=stale)))
    _no_tesseract(monkeypatch)

    accessibility.tag_pdf(ARK, opts={"correct": True})

    assert _texts(fake_pdf_pipeline["build_searchable_pdf"]["words_per_page"]) == [["The", "brown", "dog"]] * 2


def test_tag_pdf_correct_without_ocr_output_reports_uncorrected_pages(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    result = accessibility.tag_pdf(ARK, opts={"correct": True})

    assert result["pdfs"]["document.pdf"]["uncorrected_pages"] == [1, 2]
    assert result["pdfs"]["document.pdf"]["corrections"] == 0


# --- build_pdf ----------------------------------------------------------------

IMAGES_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/page_10.tif",
        "cask:/bronze/g/c/latest/item/page_2.tif",
        "cask:/bronze/g/c/latest/item/notes.pdf",
    ],
    sidecar_path="cask:/bronze/g/c/latest/item/page_10.tif.jsonld.json",
)


@pytest.fixture
def image_pages(monkeypatch):
    """stage_pages stub for .tif images (fake_prep only knows .tiff)."""
    seen = []

    def fake_stage_pages(item, tmp_dir, opts={}):
        seen.append(opts)
        pages = []
        for path in item.bronze_binary_paths:
            filename = path.rsplit("/", 1)[-1]
            stem, ext = filename.rsplit(".", 1)
            if ext == "tif" and "image" in opts.get("formats", {"image"}):
                pages.append({"source_path": path, "filename": filename, "stem": stem, "kind": "image",
                              "page_no": None, "count": None, "page_tag": None,
                              "local_path": f"{tmp_dir}/{filename}"})
        return pages
    monkeypatch.setattr(prep, "stage_pages", fake_stage_pages)
    return seen


def test_build_pdf_builds_images_in_natural_order(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, image_pages):
    _stub_lookups(monkeypatch, IMAGES_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    result = accessibility.build_pdf(ARK)

    assert image_pages == [{"formats": {"image"}}]
    shown = [p.rsplit("/", 1)[-1] for p in fake_pdf_pipeline["build_searchable_pdf"]["image_paths"]]
    assert shown == ["page_2.tif-display.jpg", "page_10.tif-display.jpg"]
    assert fake_pdf_pipeline["build_searchable_pdf"]["opts"]["title"] == "Wine menu"
    written = {w["rel_path"] for w in fake_derivative}
    assert {"item/accessibility/document/tagged.pdf", "item/accessibility/document/searchable.pdf",
            "item/accessibility/document/text.txt"} <= written
    tagged = next(w for w in fake_derivative if w["rel_path"].endswith("tagged.pdf"))
    assert tagged["source_files"][:2] == [
        "cask:/bronze/g/c/latest/item/page_2.tif", "cask:/bronze/g/c/latest/item/page_10.tif",
    ]
    assert result["document"]["pages"] == 2


def test_build_pdf_uses_each_images_alt_text(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, image_pages):
    _stub_lookups(monkeypatch, IMAGES_ITEM)
    alt = json.dumps({"alt": "A typed letter."}).encode()
    monkeypatch.setattr(fs, "read", _make_fs_read({
        "cask:/silver/g/c/latest/item/accessibility/page_10-alt-text.json": alt,
    }))

    accessibility.build_pdf(ARK)

    assert fake_pdf_pipeline["tag_scan_pdf"]["opts"]["alt"] == [None, "A typed letter."]


def test_build_pdf_caches_layouts_under_document(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, image_pages):
    _stub_lookups(monkeypatch, IMAGES_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())
    monkeypatch.setattr(layout, "page_layout", lambda image, opts={}: {"version": layout.LAYOUT_VERSION, "blocks": []})

    accessibility.build_pdf(ARK, opts={"tables": True})

    layout_writes = [w["rel_path"] for w in fake_derivative if w["rel_path"].endswith("layout.json")]
    assert layout_writes == ["item/accessibility/document/1/layout.json", "item/accessibility/document/2/layout.json"]


def test_build_pdf_raises_without_images(monkeypatch):
    _stub_lookups(monkeypatch, PDF_ITEM)
    with pytest.raises(ValueError, match="no image binaries"):
        accessibility.build_pdf(ARK)


def test_build_pdf_skip_existing_skips_before_staging(monkeypatch, fake_derivative, image_pages):
    _stub_lookups(monkeypatch, IMAGES_ITEM)
    monkeypatch.setattr(fs, "exists", lambda path: True)

    result = accessibility.build_pdf(ARK, opts={"skip_existing": True})

    assert result["document"]["skipped"] == "outputs exist"
    assert result["document"]["path"] == "cask:/silver/g/c/latest/item/accessibility/document/tagged.pdf"
    assert image_pages == []
    assert fake_derivative == []


def test_build_pdf_skip_existing_builds_when_an_output_is_missing(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, image_pages):
    _stub_lookups(monkeypatch, IMAGES_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())
    monkeypatch.setattr(fs, "exists", lambda path: not path.endswith("text.txt"))

    result = accessibility.build_pdf(ARK, opts={"skip_existing": True})

    assert "skipped" not in result["document"]


def test_tag_pdf_skip_existing_only_stages_unfinished_pdfs(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep):
    two_pdfs = ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=["cask:/bronze/g/c/latest/item/a.pdf", "cask:/bronze/g/c/latest/item/b.pdf"],
        sidecar_path="cask:/bronze/g/c/latest/item/a.pdf.jsonld.json",
    )
    _stub_lookups(monkeypatch, two_pdfs)
    monkeypatch.setattr(fs, "read", _make_fs_read())
    monkeypatch.setattr(fs, "exists", lambda path: "/accessibility/a.pdf/" in path)

    result = accessibility.tag_pdf(ARK, opts={"skip_existing": True})

    assert result["pdfs"]["a.pdf"]["skipped"] == "outputs exist"
    assert "skipped" not in result["pdfs"]["b.pdf"]
    assert fake_prep["stage_pages"][-1]["files"] == {"b.pdf"}


def test_alt_text_skip_existing_skips_pages_with_alt_text(monkeypatch, fake_derivative, fake_prep):
    _stub_lookups(monkeypatch, ITEM)
    monkeypatch.setattr(fs, "exists", lambda path: True)
    monkeypatch.setattr(alt_utils, "generate_alt", lambda *a, **k: pytest.fail("should not call the model"))

    result = accessibility.generate_alt_text(ARK, opts={"skip_existing": True})

    assert result["images"]["photo.tiff"]["skipped"] == "outputs exist"
    assert fake_derivative == []


# --- tag_pdf modes ------------------------------------------------------------

@pytest.fixture
def fake_inplace(monkeypatch):
    """Stubs pdf_inplace and add_text_layer; a 2-page PDF, page 1 born-digital."""
    calls = {"tag": None, "layer": None, "embed": [], "flat_prep": []}
    monkeypatch.setattr(pdf_inplace, "text_pages", lambda path: [True, False])
    monkeypatch.setattr(pdf_inplace, "page_texts", lambda path: ["Born text", ""])
    monkeypatch.setattr(pdf_inplace, "page_widths", lambda path: [612.0, 612.0])
    monkeypatch.setattr(pdf_inplace, "unembedded_fonts", lambda path: [])

    def fake_layer(src, dest, pages, opts={}):
        calls["layer"] = {"src": src, "pages": pages, "opts": opts}
        with open(dest, "wb") as f:
            f.write(b"SEARCHABLE")
        return {"pages": len(pages), "words_placed": 1}
    monkeypatch.setattr(pdf_utils, "add_text_layer", fake_layer)

    def fake_tag(src, dest, title, opts={}):
        calls["tag"] = {"src": src, "title": title, "opts": opts}
        with open(dest, "wb") as f:
            f.write(b"TAGGED")
        return {"pages": 2, "words": 3, "untagged_images": 0, "unmatched_spans": 0}
    monkeypatch.setattr(pdf_inplace, "tag_pdf_inplace", fake_tag)
    monkeypatch.setattr(accessibility.image_magick, "prep_ocr",
                        lambda src, dest, opts={}: calls["flat_prep"].append(opts) or {"dest_path": dest})
    return calls


def test_tag_pdf_auto_tags_born_digital_pdf_in_place(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, fake_inplace):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    result = accessibility.tag_pdf(ARK)

    pdf_result = result["pdfs"]["document.pdf"]
    assert pdf_result["mode"] == "inplace"
    assert pdf_result["ocr_pages"] == 1
    # only the scan page is OCR'd, flat, onto the original
    assert list(fake_inplace["layer"]["pages"]) == [1]
    assert fake_inplace["layer"]["pages"][1]["scale"] == 612.0 / prep.geometry_opts({})["size"]
    assert [o["deskew"] for o in fake_inplace["flat_prep"]] == [False]
    assert fake_prep["ocr_image"] == [] and fake_prep["display_image"] == []
    assert fake_pdf_pipeline["build_searchable_pdf"] is None
    text = next(w for w in fake_derivative if w["rel_path"].endswith("text.txt"))
    assert text["data"].decode() == "Born text\fhi\n"
    assert {w["rel_path"].rsplit("/", 1)[-1] for w in fake_derivative} >= {"tagged.pdf", "searchable.pdf", "text.txt"}


def test_tag_pdf_rebuild_mode_ignores_text(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, fake_inplace):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    result = accessibility.tag_pdf(ARK, opts={"mode": "rebuild"})

    assert result["pdfs"]["document.pdf"]["mode"] == "rebuild"
    assert fake_inplace["tag"] is None
    assert fake_pdf_pipeline["build_searchable_pdf"] is not None


def test_tag_pdf_inplace_mode_on_an_all_scan_pdf(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, fake_inplace):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())
    monkeypatch.setattr(pdf_inplace, "text_pages", lambda path: [False, False])

    result = accessibility.tag_pdf(ARK, opts={"mode": "inplace"})

    assert result["pdfs"]["document.pdf"]["ocr_pages"] == 2
    assert list(fake_inplace["layer"]["pages"]) == [0, 1]


def test_tag_pdf_rejects_unknown_mode(monkeypatch, fake_derivative):
    _stub_lookups(monkeypatch, PDF_ITEM)
    with pytest.raises(ValueError, match="unknown tag-pdf mode"):
        accessibility.tag_pdf(ARK, opts={"mode": "sideways"})


def test_tag_pdf_inplace_embeds_missing_fonts(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, fake_inplace):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())
    monkeypatch.setattr(pdf_inplace, "unembedded_fonts", lambda path: ["Helvetica"])
    embedded = []
    monkeypatch.setattr(pdf_inplace, "embed_fonts", lambda src, dest, opts={}: embedded.append(dest) or True)

    result = accessibility.tag_pdf(ARK)

    assert result["pdfs"]["document.pdf"]["fonts_embedded"] == ["Helvetica"]
    assert fake_inplace["layer"]["src"] == embedded[0]


def test_tag_pdf_inplace_reports_fonts_it_could_not_embed(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, fake_inplace):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())
    monkeypatch.setattr(pdf_inplace, "unembedded_fonts", lambda path: ["Helvetica"])
    monkeypatch.setattr(pdf_inplace, "embed_fonts", lambda src, dest, opts={}: False)

    result = accessibility.tag_pdf(ARK)

    assert result["pdfs"]["document.pdf"]["fonts_not_embedded"] == ["Helvetica"]


def test_tag_pdf_no_embed_fonts_skips_the_check(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, fake_inplace):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())
    monkeypatch.setattr(pdf_inplace, "unembedded_fonts", lambda path: pytest.fail("should not check fonts"))

    accessibility.tag_pdf(ARK, opts={"embed_fonts": False})


def test_tag_pdf_inplace_reuses_only_undeskewed_ocr(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, fake_inplace):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read(_ocr_outputs(record={"deskew": False})))
    tesseract_calls = _no_tesseract(monkeypatch)

    result = accessibility.tag_pdf(ARK)

    assert tesseract_calls == []
    assert result["pdfs"]["document.pdf"]["reused_ocr_pages"] == 1
    assert [w["text"] for w in fake_inplace["layer"]["pages"][1]["words"]] == ["The", "hrown", "dog"]


def test_tag_pdf_inplace_reruns_tesseract_over_deskewed_ocr(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, fake_inplace):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read(_ocr_outputs(record={"deskew": True})))

    result = accessibility.tag_pdf(ARK)

    assert result["pdfs"]["document.pdf"]["reused_ocr_pages"] == 0
    assert len(fake_pdf_pipeline["run_tesseract"]) == 1


# --- figures ------------------------------------------------------------------

CHART_RECORD = {"n": 1, "bbox": [0.1, 0.1, 0.5, 0.5], "kind": "chart", "caption": "Fig 1",
                "decorative": False, "alt": "Sales rose.", "long_description": "Bars.", "flags": []}


@pytest.fixture
def fake_figures(monkeypatch):
    calls = {"page_figures": [], "appended": []}

    def fake_page_figures(item, page, image, opts={}):
        calls["page_figures"].append({"page": page, "image": image, "opts": opts})
        return [CHART_RECORD], page["page_no"] == 2  # page 2 freshly detected
    monkeypatch.setattr(figures, "page_figures", fake_page_figures)
    monkeypatch.setattr(figures, "read_review", lambda item, filename: {})
    monkeypatch.setattr(figures, "append_review",
                        lambda item, filename, source, rows: calls["appended"].append((filename, rows)) or "csv")
    return calls


def test_tag_pdf_figures_passes_specs_and_records_new_pages(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_figures):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    accessibility.tag_pdf(ARK, opts={"figures": True, "collection_context": "Wine"})

    spec = {"bbox": [0.1, 0.1, 0.5, 0.5], "alt": "Sales rose. Bars.", "caption": "Fig 1", "kind": "chart"}
    assert fake_pdf_pipeline["tag_scan_pdf"]["opts"]["figures"] == [[spec], [spec]]
    images = [c["image"].rsplit("/", 1)[-1] for c in fake_figures["page_figures"]]
    assert images == ["document-0.tif-vision.jpg", "document-1.tif-vision.jpg"]
    assert fake_figures["page_figures"][0]["opts"]["collection_context"] == "Wine"
    assert fake_figures["page_figures"][0]["opts"]["page_alt"] is None  # two pages
    assert [(f, [r["page"] for r in rows]) for f, rows in fake_figures["appended"]] == [("document.pdf", [2])]


def test_tag_pdf_without_figures_skips_detection(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_figures):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    accessibility.tag_pdf(ARK)

    assert fake_figures["page_figures"] == []
    assert fake_pdf_pipeline["tag_scan_pdf"]["opts"]["figures"] is None


def test_tag_pdf_in_place_figures_use_resized_images_not_rasters(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, fake_inplace, fake_figures):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    accessibility.tag_pdf(ARK, opts={"figures": True})

    assert len(fake_inplace["tag"]["opts"]["figures"]) == 2
    # full rasters exceed Samwise's upload limit
    assert [c["image"].rsplit("/", 1)[-1] for c in fake_figures["page_figures"]] == [
        "document-0.tif-vision.jpg", "document-1.tif-vision.jpg"]


def test_build_pdf_figures_append_review_per_image(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep, image_pages, fake_figures):
    _stub_lookups(monkeypatch, IMAGES_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    accessibility.build_pdf(ARK, opts={"figures": True})

    assert len(fake_pdf_pipeline["tag_scan_pdf"]["opts"]["figures"]) == 2


def test_detect_figures_step(monkeypatch, fake_derivative, fake_prep, fake_figures):
    _stub_lookups(monkeypatch, PDF_ITEM)

    result = accessibility.detect_figures(ARK, opts={"collection_context": "Wine"})

    assert result["status"] == "success"
    pages = result["files"]["document.pdf"]["pages"]
    assert pages == {"1": {"figures": 1, "tagged": 1, "fresh": False},
                     "2": {"figures": 1, "tagged": 1, "fresh": True}}
    assert result["files"]["document.pdf"]["review_path"] == "csv"
    assert fake_figures["page_figures"][0]["opts"]["title"] == "Wine menu"
    assert [c["image"].rsplit("/", 1)[-1] for c in fake_figures["page_figures"]] == [
        "document-0.tif-vision.jpg", "document-1.tif-vision.jpg"]


def test_detect_figures_keeps_going_past_a_failed_page(monkeypatch, fake_derivative, fake_prep, fake_figures):
    _stub_lookups(monkeypatch, PDF_ITEM)

    def flaky(item, page, image, opts={}):
        if page["page_no"] == 1:
            raise RuntimeError("Samwise down")
        return [CHART_RECORD], True
    monkeypatch.setattr(figures, "page_figures", flaky)

    result = accessibility.detect_figures(ARK)

    assert result["status"] == "partial"
    assert result["failed"][0]["error"] == "Samwise down"
    assert list(result["files"]["document.pdf"]["pages"]) == ["2"]


# --- geometry qwen --------------------------------------------------------------

def test_tag_pdf_qwen_geometry_uses_vision_lines(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())
    tesseract_calls = _no_tesseract(monkeypatch)
    lines = [{"text": "Dear Sir", "bbox": [10, 10, 200, 40]}, {"text": "I write", "bbox": [10, 50, 180, 80]}]
    seen = []
    monkeypatch.setattr(ocr, "vision_lines", lambda item, page, image, tmp_dir, opts={}: (
        seen.append(image) or {"lines": lines, "path": "cask:/x/qwen-lines.json"}))

    result = accessibility.tag_pdf(ARK, opts={"geometry": "qwen", "correct": True})

    assert tesseract_calls == [] and fake_prep["ocr_image"] == []
    assert [c["opts"]["deskew"] for c in fake_prep["display_image"]] == [False, False]
    assert fake_pdf_pipeline["build_searchable_pdf"]["words_per_page"] == [lines, lines]
    text = next(w for w in fake_derivative if w["rel_path"].endswith("text.txt"))
    assert text["data"].decode() == "Dear Sir\nI write\fDear Sir\nI write\n"
    assert result["pdfs"]["document.pdf"]["uncorrected_pages"] == []


def test_unknown_geometry_raises(monkeypatch):
    _stub_lookups(monkeypatch, PDF_ITEM)
    with pytest.raises(ValueError, match="unknown geometry"):
        accessibility.tag_pdf(ARK, opts={"geometry": "ruler"})
