import json

import pytest

from digtk.lib import ai, derivative, fs, items, ocr_utils
from digtk.lib.items import ItemLocation
from digtk.models import ocr, pdf_inplace, prep

ARK = "ark:/87287/d7abc1234"

ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
)

MULTI_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="box-1/folder-3",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/box-1/folder-3/photo-a.tiff",
        "cask:/bronze/g/c/latest/box-1/folder-3/photo-b.tiff",
    ],
)

PDF_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/document.pdf"],
)

VIDEO_ONLY_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/reel.mp4"],
)

MIXED_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/photo.tiff",
        "cask:/bronze/g/c/latest/item/document.pdf",
    ],
)

WORKSPACE_ITEM = ItemLocation(
    ark=None, group="jdoe", collection="box-12", time=None, item_path="folder-3",
    bronze_binary_paths=["cask:/workspace/jdoe/box-12/source/folder-3/photo.tiff"],
    dest_type="workspace", dest_prefix="derivatives",
)

PDF_PAGE_COUNT = 2


@pytest.fixture(autouse=True)
def scanned_pdfs(monkeypatch):
    """Fake PDFs read as all-scan pages unless a test says otherwise."""
    monkeypatch.setattr(pdf_inplace, "text_pages", lambda path: [False] * PDF_PAGE_COUNT)


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


@pytest.fixture
def fake_tesseract(monkeypatch):
    calls = []

    def fake_run_tesseract(image_path, output_base, opts={}):
        calls.append({"image_path": image_path, "opts": opts})
        hocr_path = output_base + ".hocr"
        with open(hocr_path, "w", encoding="utf-8") as f:
            f.write("<html>fake hocr</html>")
        return {"hocr_path": hocr_path, "txt_path": output_base + ".txt", "confidence": 88.5}

    monkeypatch.setattr(ocr_utils, "run_tesseract", fake_run_tesseract)
    return calls


@pytest.fixture
def fake_vision(monkeypatch):
    calls = []

    def fake_chat_completion(message, opts={}):
        calls.append({"message": message, "opts": opts})
        return "The transcribed page text."

    monkeypatch.setattr(ai, "chat_completion", fake_chat_completion)
    return calls


@pytest.fixture
def fake_prep(monkeypatch):
    """Stubs digtk.models.prep's staging/prep steps with deterministic local
    paths: an image binary is one page, a PDF binary is PDF_PAGE_COUNT pages;
    ocr_image() picks deskew for page 2 only."""
    calls = {"stage_pages": [], "ocr_image": [], "vision_image": []}

    def fake_stage_pages(item, tmp_dir, opts={}):
        calls["stage_pages"].append(opts)
        files = opts.get("files")
        pages = []
        for path in item.bronze_binary_paths:
            filename = path.rsplit("/", 1)[-1]
            if files and filename not in files:
                continue
            stem, ext = filename.rsplit(".", 1)
            base = {"source_path": path, "filename": filename, "stem": stem}
            if ext == "pdf":
                for i in range(PDF_PAGE_COUNT):
                    pages.append({**base, "kind": "pdf", "page_no": i + 1, "count": PDF_PAGE_COUNT,
                                  "page_tag": f"page-{i:03d}", "local_path": f"{tmp_dir}/{stem}-{i}.tif",
                                  "pdf_path": f"{tmp_dir}/{filename}"})
            elif ext in ("tiff", "jpg"):
                pages.append({**base, "kind": "image", "page_no": None, "count": None,
                              "page_tag": None, "local_path": f"{tmp_dir}/{filename}"})
        return pages

    def fake_ocr_image(page, tmp_dir, opts={}):
        calls["ocr_image"].append({"page": page, "opts": opts})
        return {"local_path": f"{page['local_path']}-prep.jpg", "deskew": page["page_no"] == 2, "angle": 0.0}

    def fake_vision_image(page, tmp_dir, opts={}):
        calls["vision_image"].append(page)
        return f"{page['local_path']}-vision.jpg"

    monkeypatch.setattr(prep, "stage_pages", fake_stage_pages)
    monkeypatch.setattr(prep, "ocr_image", fake_ocr_image)
    monkeypatch.setattr(prep, "vision_image", fake_vision_image)
    return calls


def _stub_resolve(monkeypatch, item):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: item)


# --- images ------------------------------------------------------------------

def test_run_ocr_image_runs_both_engines(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    _stub_resolve(monkeypatch, ITEM)

    result = ocr.run_ocr(ARK)

    assert result["status"] == "success"
    assert result["ark"] == ARK
    image_result = result["images"]["photo.tiff"]
    assert image_result["tesseract_confidence"] == 88.5
    assert image_result["deskew"] is False
    assert image_result["hocr_path"].endswith("item/text/ocr.hocr")
    assert image_result["transcript_path"].endswith("item/text/transcript.txt")

    assert len(fake_tesseract) == 1
    assert fake_tesseract[0]["image_path"].endswith("photo.tiff-prep.jpg")

    assert len(fake_vision) == 1
    assert fake_vision[0]["opts"]["image"].endswith("photo.tiff-vision.jpg")

    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert rel_paths == {
        "item/text/ocr.hocr", "item/text/transcript.txt", "item/text/ocr-result.json",
    }
    for write in fake_derivative:
        assert write["type"] == "silver"
        assert write["source_files"] == ["cask:/bronze/g/c/latest/item/photo.tiff"]
    result_write = next(w for w in fake_derivative if w["rel_path"] == "item/text/ocr-result.json")
    assert json.loads(result_write["data"]) == {
        "tesseract_confidence": 88.5, "deskew": False, "prep": prep.geometry_opts({}),
    }


def test_run_ocr_never_reads_showcase_derivatives(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    """Regression guard: run_ocr() prepares its own inputs — it must not read
    anything `digtk showcase image|pdf derivatives` publishes (ocr/manifest.json,
    OCR-prep images, public-frontend-derivatives large.jpg)."""
    _stub_resolve(monkeypatch, MIXED_ITEM)

    def fake_read(path):
        raise AssertionError(f"unexpected fs.read({path})")
    monkeypatch.setattr(fs, "read", fake_read)

    ocr.run_ocr(ARK)


def test_run_ocr_disambiguates_multi_image_filenames(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    _stub_resolve(monkeypatch, MULTI_ITEM)

    result = ocr.run_ocr(ARK)

    assert set(result["images"]) == {"photo-a.tiff", "photo-b.tiff"}
    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert rel_paths == {
        "box-1/folder-3/text/photo-a-ocr.hocr", "box-1/folder-3/text/photo-a-transcript.txt",
        "box-1/folder-3/text/photo-a-ocr-result.json",
        "box-1/folder-3/text/photo-b-ocr.hocr", "box-1/folder-3/text/photo-b-transcript.txt",
        "box-1/folder-3/text/photo-b-ocr-result.json",
    }


def test_run_ocr_no_vision_skips_samwise_call(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    _stub_resolve(monkeypatch, ITEM)

    result = ocr.run_ocr(ARK, opts={"vision": False})

    assert fake_vision == []
    assert fake_prep["vision_image"] == []
    assert result["images"]["photo.tiff"]["transcript_path"] is None
    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert rel_paths == {"item/text/ocr.hocr", "item/text/ocr-result.json"}


def test_run_ocr_never_writes_into_showcase_ocr_dir(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    """Regression guard: showcase's image.py/pdf.py own the item's ocr/
    directory (OCR-prep images, djvu, manifest.json); run_ocr() writes only
    under text/."""
    _stub_resolve(monkeypatch, MIXED_ITEM)

    ocr.run_ocr(ARK)

    assert fake_derivative
    assert not any("/ocr/" in w["rel_path"] for w in fake_derivative)
    assert not any(w["rel_path"].endswith("manifest.json") for w in fake_derivative)


def test_run_ocr_forwards_tesseract_opts(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    _stub_resolve(monkeypatch, ITEM)

    ocr.run_ocr(ARK, opts={"lang": "fra", "psm": 6, "vision": True})

    assert fake_tesseract[0]["opts"] == {"lang": "fra", "psm": 6}
    assert fake_prep["ocr_image"][0]["opts"] == {"lang": "fra", "psm": 6}


# --- PDF pages -----------------------------------------------------------------

def test_run_ocr_pdf_runs_both_engines_per_page(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    _stub_resolve(monkeypatch, PDF_ITEM)

    result = ocr.run_ocr(ARK)

    assert "images" not in result
    pages = result["pdfs"]["document.pdf"]["pages"]
    assert set(pages) == {"1", "2"}
    assert pages["1"]["hocr_path"].endswith("text/page-000/ocr.hocr")
    assert pages["2"]["hocr_path"].endswith("text/page-001/ocr.hocr")
    assert pages["1"]["deskew"] is False
    assert pages["2"]["deskew"] is True

    assert len(fake_tesseract) == 2
    assert len(fake_vision) == 2

    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert rel_paths == {
        "item/text/page-000/ocr.hocr", "item/text/page-000/transcript.txt", "item/text/page-000/ocr-result.json",
        "item/text/page-001/ocr.hocr", "item/text/page-001/transcript.txt", "item/text/page-001/ocr-result.json",
    }


def test_run_ocr_mixed_item_namespaces_pdf_text_dir(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    """An item with both image and PDF binaries sharing one item_path
    namespaces the PDF's per-page text/ dirs by its own stem, so they can't
    collide with the image's own text/ outputs."""
    _stub_resolve(monkeypatch, MIXED_ITEM)

    result = ocr.run_ocr(ARK)

    assert result["images"]["photo.tiff"]["hocr_path"].endswith("item/text/ocr.hocr")
    pages = result["pdfs"]["document.pdf"]["pages"]
    assert pages["1"]["hocr_path"].endswith("item/document/text/page-000/ocr.hocr")
    assert pages["2"]["hocr_path"].endswith("item/document/text/page-001/ocr.hocr")


def test_run_ocr_raises_when_no_image_or_pdf_binaries(monkeypatch):
    _stub_resolve(monkeypatch, VIDEO_ONLY_ITEM)

    with pytest.raises(ValueError, match="no image or PDF binaries found"):
        ocr.run_ocr(ARK)


# --- workspace items -----------------------------------------------------------

def test_run_ocr_workspace_item_writes_to_workspace_derivatives(
    monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep,
):
    result = ocr.run_ocr(WORKSPACE_ITEM)

    assert result["ark"] is None
    assert result["item"] == "cask:/workspace/jdoe/box-12/source/folder-3"
    assert result["images"]["photo.tiff"]["hocr_path"] == (
        "cask:/workspace/jdoe/box-12/derivatives/folder-3/text/ocr.hocr"
    )
    for write in fake_derivative:
        assert write["type"] == "workspace"
        assert write["opts"]["group"] == "jdoe"
        assert "time" not in write["opts"]
        assert write["source_files"] == ["cask:/workspace/jdoe/box-12/source/folder-3/photo.tiff"]


def test_run_ocr_accepts_workspace_path_string(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    seen = {}
    monkeypatch.setattr(items, "resolve_workspace", lambda path: seen.update(path=path) or WORKSPACE_ITEM)

    ocr.run_ocr("cask:/workspace/jdoe/box-12/source/folder-3")

    assert seen["path"] == "cask:/workspace/jdoe/box-12/source/folder-3"


# --- per-page failures ---------------------------------------------------------

def test_run_ocr_keeps_tesseract_output_when_a_pages_vision_call_fails(
    monkeypatch, capsys, fake_derivative, fake_tesseract, fake_prep,
):
    _stub_resolve(monkeypatch, PDF_ITEM)
    calls = []

    def flaky_chat_completion(message, opts={}):
        calls.append(opts["image"])
        if len(calls) == 1:
            raise RuntimeError("Samwise chat completion failed after 6 tries: Connection error.")
        return "Page two text."
    monkeypatch.setattr(ai, "chat_completion", flaky_chat_completion)

    result = ocr.run_ocr(ARK)

    assert result["status"] == "partial"
    pages = result["pdfs"]["document.pdf"]["pages"]
    assert pages["1"]["transcript_path"] is None
    assert pages["1"]["transcript_error"].startswith("Samwise chat completion failed")
    assert pages["1"]["hocr_path"].endswith("text/page-000/ocr.hocr")
    assert pages["2"]["transcript_path"].endswith("text/page-001/transcript.txt")
    assert result["failed"] == [{
        "file": "document.pdf", "page": 1, "stage": "transcript",
        "error": "Samwise chat completion failed after 6 tries: Connection error.",
    }]
    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert "item/text/page-000/ocr.hocr" in rel_paths
    assert "item/text/page-000/ocr-result.json" in rel_paths
    assert "item/text/page-000/transcript.txt" not in rel_paths
    assert "document.pdf page 1/2: transcript failed" in capsys.readouterr().err


def test_run_ocr_skips_a_page_whose_ocr_step_fails(monkeypatch, capsys, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    _stub_resolve(monkeypatch, PDF_ITEM)
    real_ocr_image = prep.ocr_image

    def failing_on_page_2(page, tmp_dir, opts={}):
        if page["page_no"] == 2:
            raise OSError("convert: corrupt image")
        return real_ocr_image(page, tmp_dir, opts)
    monkeypatch.setattr(prep, "ocr_image", failing_on_page_2)

    result = ocr.run_ocr(ARK)

    assert result["status"] == "partial"
    assert set(result["pdfs"]["document.pdf"]["pages"]) == {"1"}
    assert result["failed"] == [{"file": "document.pdf", "page": 2, "stage": "ocr", "error": "convert: corrupt image"}]
    assert not any("page-001" in w["rel_path"] for w in fake_derivative)
    assert "document.pdf page 2/2: OCR failed, skipping page" in capsys.readouterr().err


def test_run_ocr_raises_when_every_page_fails(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    _stub_resolve(monkeypatch, PDF_ITEM)

    def always_fails(page, tmp_dir, opts={}):
        raise OSError("tesseract not found")
    monkeypatch.setattr(prep, "ocr_image", always_fails)

    with pytest.raises(RuntimeError, match="OCR failed for all 2 page"):
        ocr.run_ocr(ARK)


def test_run_ocr_reports_no_failures_on_a_clean_run(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    _stub_resolve(monkeypatch, ITEM)

    result = ocr.run_ocr(ARK)

    assert result["status"] == "success"
    assert result["failed"] == []
    assert result["images"]["photo.tiff"]["transcript_error"] is None


def test_run_ocr_files_opt_keeps_whole_item_naming(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    """A per-file run of one image in a multi-image item keeps the item's
    '{stem}-' prefix, so it can't collide with the item's other files' outputs."""
    _stub_resolve(monkeypatch, MULTI_ITEM)

    result = ocr.run_ocr(ARK, opts={"files": {"photo-a.tiff"}})

    assert set(result["images"]) == {"photo-a.tiff"}
    assert {w["rel_path"] for w in fake_derivative} == {
        "box-1/folder-3/text/photo-a-ocr.hocr", "box-1/folder-3/text/photo-a-transcript.txt",
        "box-1/folder-3/text/photo-a-ocr-result.json",
    }
    assert fake_prep["stage_pages"] == [{"files": {"photo-a.tiff"}, "rasterize": False}]
    assert "files" not in fake_tesseract[0]["opts"]


def test_run_ocr_files_opt_keeps_mixed_item_pdf_namespacing(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    _stub_resolve(monkeypatch, MIXED_ITEM)

    result = ocr.run_ocr(ARK, opts={"files": {"document.pdf"}})

    assert result["images"] == {}
    assert result["pdfs"]["document.pdf"]["pages"]["1"]["hocr_path"].endswith("item/document/text/page-000/ocr.hocr")


# --- page_location / correct_ocr ----------------------------------------------

def _page(kind, stem="document", page_no=None, tag=None):
    return {"filename": f"{stem}.{'pdf' if kind == 'pdf' else 'tiff'}", "stem": stem, "kind": kind,
            "page_no": page_no, "count": 2 if kind == "pdf" else None, "page_tag": tag,
            "source_path": "cask:/bronze/x", "local_path": None}


def test_page_location_matches_run_ocr_naming():
    assert ocr.page_location(ITEM, _page("image", "photo")) == (("text",), "photo", False)
    assert ocr.page_location(MULTI_ITEM, _page("image", "photo-a"))[2] is True
    assert ocr.page_location(PDF_ITEM, _page("pdf", page_no=1, tag="page-000")) == (
        ("text", "page-000"), "document", False)
    assert ocr.page_location(MIXED_ITEM, _page("pdf", page_no=1, tag="page-000"))[0] == (
        "document/text", "page-000")


HOCR = ("<div class='ocr_page'><span class='ocrx_word' title='bbox 0 0 10 10; x_wconf 90'>The</span>"
        "<span class='ocrx_word' title='bbox 20 0 60 10; x_wconf 80'>hrown</span>"
        "<span class='ocrx_word' title='bbox 70 0 99 10; x_wconf 85'>dog</span></div>")


def _serve(monkeypatch, table):
    def fake_read(path):
        if path in table:
            return table[path]
        raise FileNotFoundError(path)
    monkeypatch.setattr(fs, "read", fake_read)


def test_read_page_ocr_returns_words_transcript_and_record(monkeypatch):
    base = "cask:/silver/g/c/latest/item/text"
    _serve(monkeypatch, {
        f"{base}/ocr.hocr": HOCR.encode(),
        f"{base}/transcript.txt": b"The brown dog",
        f"{base}/ocr-result.json": b'{"deskew": true}',
    })
    got = ocr.read_page_ocr(ITEM, _page("image", "photo"))
    assert [w["text"] for w in got["words"]] == ["The", "hrown", "dog"]
    assert got["transcript"] == "The brown dog"
    assert got["record"] == {"deskew": True}
    assert len(got["hocr_sha1"]) == 40


def test_read_page_ocr_none_without_hocr(monkeypatch):
    _serve(monkeypatch, {})
    assert ocr.read_page_ocr(ITEM, _page("image", "photo")) is None


def test_correct_ocr_publishes_corrections_per_page(monkeypatch, fake_derivative, fake_prep):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: PDF_ITEM)
    base = "cask:/silver/g/c/latest/item/text"
    table = {}
    for tag in ("page-000", "page-001"):
        table[f"{base}/{tag}/ocr.hocr"] = HOCR.encode()
        table[f"{base}/{tag}/transcript.txt"] = b"The brown dog"
    _serve(monkeypatch, table)

    result = ocr.correct_ocr(ARK)

    assert result["status"] == "success"
    assert result["pdfs"]["document.pdf"]["pages"]["1"]["changes"] == 1
    assert fake_prep["stage_pages"][0]["rasterize"] is False
    write = fake_derivative[0]
    assert write["rel_path"] == "item/text/page-000/ocr-corrected.json"
    assert write["source_files"] == [f"{base}/page-000/ocr.hocr", f"{base}/page-000/transcript.txt"]
    assert write["opts"]["link_relation"] == ocr.CORRECTED_LINK_RELATION
    doc = json.loads(write["data"])
    assert [w["text"] for w in doc["words"]] == ["The", "brown", "dog"]
    assert doc["changes"] == [{"i": 1, "old": "hrown", "new": "brown"}]


def test_correct_ocr_skips_pages_without_transcript(monkeypatch, fake_derivative, fake_prep):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: PDF_ITEM)
    base = "cask:/silver/g/c/latest/item/text"
    _serve(monkeypatch, {
        f"{base}/page-000/ocr.hocr": HOCR.encode(),
        f"{base}/page-000/transcript.txt": b"The brown dog",
        f"{base}/page-001/ocr.hocr": HOCR.encode(),
    })

    result = ocr.correct_ocr(ARK)

    assert result["status"] == "success"
    assert result["failed"] == []
    assert result["pdfs"]["document.pdf"]["pages"]["2"] == {"skipped": "no vision transcript (ocr ran without vision)"}


def test_correct_ocr_skips_pdf_pages_with_text(monkeypatch, fake_derivative, fake_prep):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: PDF_ITEM)
    monkeypatch.setattr(pdf_inplace, "text_pages", lambda path: [True, False])
    base = "cask:/silver/g/c/latest/item/text"
    _serve(monkeypatch, {f"{base}/page-001/ocr.hocr": HOCR.encode()})  # no transcript either

    result = ocr.correct_ocr(ARK)

    assert result["status"] == "success"
    assert result["pdfs"]["document.pdf"]["pages"] == {
        "1": {"skipped": "page has text"},
        "2": {"skipped": "no vision transcript (ocr ran without vision)"},
    }


def test_correct_ocr_raises_when_nothing_was_ocrd(monkeypatch, fake_derivative, fake_prep):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: PDF_ITEM)
    _serve(monkeypatch, {})
    with pytest.raises(RuntimeError, match="run `digtk ocr tesseract` first"):
        ocr.correct_ocr(ARK)


def test_run_ocr_skip_existing_skips_pages_with_outputs(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: ITEM)
    monkeypatch.setattr(fs, "exists", lambda path: True)

    result = ocr.run_ocr(ARK, opts={"skip_existing": True})

    assert result["images"]["photo.tiff"]["skipped"] == "outputs exist"
    assert fake_tesseract == [] and fake_vision == []


def test_run_ocr_skip_existing_reruns_page_missing_its_transcript(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: ITEM)
    monkeypatch.setattr(fs, "exists", lambda path: not path.endswith("transcript.txt"))

    ocr.run_ocr(ARK, opts={"skip_existing": True})

    assert len(fake_tesseract) == 1
    assert "skip_existing" not in fake_tesseract[0]["opts"]


def test_run_ocr_skips_pdf_pages_that_have_text(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: PDF_ITEM)
    monkeypatch.setattr(pdf_inplace, "text_pages", lambda path: [True, False])

    result = ocr.run_ocr(ARK)

    pages = result["pdfs"]["document.pdf"]["pages"]
    assert pages["1"] == {"skipped": "page has text"}
    assert "hocr_path" in pages["2"]
    assert len(fake_tesseract) == 1
    assert fake_prep["stage_pages"][0]["rasterize"] is False


def test_run_ocr_ocr_text_pages_forces_ocr(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: PDF_ITEM)
    monkeypatch.setattr(pdf_inplace, "text_pages", lambda path: [True, True])

    ocr.run_ocr(ARK, opts={"ocr_text_pages": True})

    assert len(fake_tesseract) == 2
    assert all("ocr_text_pages" not in c["opts"] for c in fake_tesseract)



# --- vision line OCR ------------------------------------------------------------

def test_parse_lines_scales_normalized_boxes():
    raw = 'Here: [{"text": "Dear Sir", "bbox": [0, 0, 500, 100]}, {"text": "", "bbox": [1, 1, 2, 2]}]'
    assert ocr._parse_lines(raw, 2000, 1000) == [{"text": "Dear Sir", "bbox": [0.0, 0.0, 1000.0, 100.0]}]


def test_parse_lines_rejects_degenerate_answer():
    raw = '[{"text": "a", "bbox": [1, 2, 3, 4]}, {"bbox": [1, 2, 3, 4]}, {"bbox": [1, 2, 3, 4]}, {"bbox": [1, 2, 3, 4]}]'
    assert ocr._parse_lines(raw, 100, 100) is None
    assert ocr._parse_lines("no json", 100, 100) is None


def _page_png(tmp_path):
    import pymupdf
    path = tmp_path / "page.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 1000, 500))
    pix.clear_with(255)
    pix.save(str(path))
    return str(path)


def _lines_json(n):
    return json.dumps([{"text": f"line {i}", "bbox": [0, i * 10, 100, i * 10 + 8]} for i in range(n)])


def test_ocr_page_lines_retries_thin_answers_at_other_sizes(monkeypatch, tmp_path):
    from digtk.lib import image_magick
    page = _page_png(tmp_path)
    resized = []
    monkeypatch.setattr(image_magick, "thumbnail", lambda src, dest, width, opts={}: resized.append(width))
    answers = iter([_lines_json(1), _lines_json(2), _lines_json(5)])
    monkeypatch.setattr(ai, "chat_completion", lambda message, opts={}: next(answers))

    lines = ocr.ocr_page_lines(page, str(tmp_path))

    assert len(lines) == 5
    assert resized == [1400, 2600]
    assert lines[1]["bbox"] == [0.0, 5.0, 100.0, 9.0]  # 0-1000 -> 1000x500 px


def test_ocr_page_lines_stops_when_enough_lines(monkeypatch, tmp_path):
    page = _page_png(tmp_path)
    calls = []
    monkeypatch.setattr(ai, "chat_completion", lambda message, opts={}: calls.append(opts) or _lines_json(4))
    assert len(ocr.ocr_page_lines(page, str(tmp_path))) == 4
    assert len(calls) == 1 and calls[0]["temperature"] == 0.0


def test_vision_lines_uses_matching_cache(monkeypatch, fake_derivative, tmp_path):
    page = {"filename": "photo.tiff", "stem": "photo", "kind": "image", "page_no": None,
            "count": None, "page_tag": None, "source_path": "cask:/bronze/x/photo.tiff"}
    path = "cask:/silver/g/c/latest/item/text/qwen-lines.json"
    cached = {"prep": prep.geometry_opts({}), "lines": [{"text": "hi", "bbox": [0, 0, 1, 1]}]}
    monkeypatch.setattr(fs, "read", lambda p: json.dumps(cached).encode() if p == path else (_ for _ in ()).throw(FileNotFoundError(p)))
    monkeypatch.setattr(ocr, "ocr_page_lines", lambda *a, **k: pytest.fail("no model call"))

    assert ocr.vision_lines(ITEM, page, "img.jpg", str(tmp_path)) == {"lines": cached["lines"], "path": path}


def test_vision_lines_redoes_cache_from_other_geometry(monkeypatch, fake_derivative, tmp_path):
    page = {"filename": "photo.tiff", "stem": "photo", "kind": "image", "page_no": None,
            "count": None, "page_tag": None, "source_path": "cask:/bronze/x/photo.tiff"}
    stale = {"prep": {**prep.geometry_opts({}), "size": 1}, "lines": []}
    monkeypatch.setattr(fs, "read", lambda p: json.dumps(stale).encode())
    fresh = [{"text": "new", "bbox": [0, 0, 1, 1]}]
    monkeypatch.setattr(ocr, "ocr_page_lines", lambda *a, **k: fresh)

    result = ocr.vision_lines(ITEM, page, "img.jpg", str(tmp_path))

    assert result["lines"] == fresh
    assert fake_derivative[-1]["rel_path"] == "item/text/qwen-lines.json"
    assert json.loads(fake_derivative[-1]["data"])["prep"] == prep.geometry_opts({})


def test_vision_lines_does_not_cache_unusable_answers(monkeypatch, fake_derivative, tmp_path):
    page = {"filename": "photo.tiff", "stem": "photo", "kind": "image", "page_no": None,
            "count": None, "page_tag": None, "source_path": "cask:/bronze/x/photo.tiff"}
    monkeypatch.setattr(fs, "read", lambda p: (_ for _ in ()).throw(FileNotFoundError(p)))
    monkeypatch.setattr(ocr, "ocr_page_lines", lambda *a, **k: None)
    assert ocr.vision_lines(ITEM, page, "img.jpg", str(tmp_path)) == {"lines": [], "path": None}
    assert fake_derivative == []
