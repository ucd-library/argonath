import json

import pytest

from digtk.lib import derivative, fs, items, ocr_utils
from digtk.lib.items import ItemLocation
from digtk.models import accessibility, alt_utils, layout, pdf_tag, pdf_utils, prep

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
def fake_prep(monkeypatch):
    """Stubs digtk.models.prep's staging/prep steps with deterministic local
    paths: an image binary is one page, a PDF binary is PDF_PAGE_COUNT pages."""
    calls = {"stage_pages": [], "ocr_image": [], "vision_image": []}

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
                                  "page_tag": f"page-{i:03d}", "local_path": f"{tmp_dir}/{stem}-{i}.tif"})
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

    monkeypatch.setattr(prep, "stage_pages", fake_stage_pages)
    monkeypatch.setattr(prep, "ocr_image", fake_ocr_image)
    monkeypatch.setattr(prep, "vision_image", fake_vision_image)
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
        return "A wine menu.", None, []

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
    assert json.loads(write["data"]) == {"alt": "A wine menu.", "description": None, "flags": []}
    assert write["opts"]["link_relation"] == accessibility.ALT_TEXT_LINK_RELATION


def test_generate_alt_text_disambiguates_multi_image_filenames(monkeypatch, fake_derivative):
    _stub_lookups(monkeypatch, MULTI_ITEM)
    monkeypatch.setattr(alt_utils, "generate_alt", lambda image, metadata, opts={}: ("An image.", None, []))

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
        return "alt", None, []

    monkeypatch.setattr(alt_utils, "generate_alt", fake_generate_alt)

    accessibility.generate_alt_text(ARK, opts={"want_description": True, "max_alt_chars": 100})

    assert seen["opts"] == {"want_description": True, "max_alt_chars": 100}


def test_generate_alt_text_surfaces_review_flags(monkeypatch, fake_derivative):
    _stub_lookups(monkeypatch, ITEM)
    monkeypatch.setattr(alt_utils, "generate_alt", lambda image, metadata, opts={}: ("", None, ["ERROR:unparseable-model-output"]))

    result = accessibility.generate_alt_text(ARK)

    assert result["images"]["photo.tiff"]["flags"] == ["ERROR:unparseable-model-output"]


# --- PDF pages -----------------------------------------------------------------

def test_generate_alt_text_pdf_pages(monkeypatch, fake_derivative):
    _stub_lookups(monkeypatch, PDF_ITEM)
    seen_calls = []

    def fake_generate_alt(image, metadata, opts={}):
        seen_calls.append((image, metadata))
        return "A page.", None, []

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
    monkeypatch.setattr(alt_utils, "generate_alt", lambda image, metadata, opts={}: ("alt", None, []))

    def fake_read(path):
        raise AssertionError(f"unexpected fs.read({path})")
    monkeypatch.setattr(fs, "read", fake_read)

    accessibility.generate_alt_text(ARK)


def test_generate_alt_text_workspace_item_has_no_sidecar_lineage(monkeypatch, fake_derivative):
    monkeypatch.setattr(items, "metadata_for", lambda location: EMPTY_METADATA)
    monkeypatch.setattr(alt_utils, "generate_alt", lambda image, metadata, opts={}: ("alt", None, []))

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
    monkeypatch.setattr(alt_utils, "generate_alt", lambda image, metadata, opts={}: ("alt", None, []))

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


def test_tag_pdf_forwards_min_conf_to_hocr_words_not_tesseract(monkeypatch, fake_derivative, fake_pdf_pipeline):
    _stub_lookups(monkeypatch, PDF_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())
    seen_tesseract_opts = []
    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda image_path, output_base, opts={}: (
        seen_tesseract_opts.append(opts) or {"hocr_path": output_base + ".hocr", "txt_path": output_base + ".txt", "confidence": 90.0}
    ))
    seen_min_conf = []
    monkeypatch.setattr(pdf_utils, "hocr_words", lambda hocr_path, min_conf=0.0: (
        seen_min_conf.append(min_conf) or [{"text": "hi", "bbox": [0, 0, 10, 10], "conf": 90.0}]
    ))

    accessibility.tag_pdf(ARK, opts={"min_conf": 70.0})

    assert seen_tesseract_opts
    for opts in seen_tesseract_opts:
        assert "min_conf" not in opts
    assert seen_min_conf == [70.0, 70.0]


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
        "document-0.tif-prep.jpg", "document-1.tif-prep.jpg",
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
    cached_layout = json.dumps({"blocks": [{"type": "text", "text": "cached"}]}).encode()
    monkeypatch.setattr(fs, "read", _make_fs_read({LAYOUT_1_PATH: cached_layout, LAYOUT_2_PATH: cached_layout}))
    seen_layout_calls = []
    monkeypatch.setattr(layout, "page_layout", lambda image, opts={}: seen_layout_calls.append(image))

    accessibility.tag_pdf(ARK, opts={"tables": True})

    assert seen_layout_calls == []
    assert fake_pdf_pipeline["tag_scan_pdf"]["opts"]["layouts"] == [
        {"blocks": [{"type": "text", "text": "cached"}]},
        {"blocks": [{"type": "text", "text": "cached"}]},
    ]
    assert not any(w["rel_path"].endswith("layout.json") for w in fake_derivative)


def test_tag_pdf_only_stages_pdf_binaries(monkeypatch, fake_derivative, fake_pdf_pipeline, fake_prep):
    _stub_lookups(monkeypatch, MIXED_ITEM)
    monkeypatch.setattr(fs, "read", _make_fs_read())

    result = accessibility.tag_pdf(ARK)

    assert fake_prep["stage_pages"] == [{"formats": {"pdf"}, "files": None}]
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
        return "Page two.", None, []
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
    monkeypatch.setattr(alt_utils, "generate_alt", lambda image, metadata, opts={}: ("alt", None, []))

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
    assert fake_prep["stage_pages"] == [{"formats": {"pdf"}, "files": {"b.pdf"}}]
