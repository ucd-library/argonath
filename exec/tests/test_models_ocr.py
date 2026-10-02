import json

import pytest

from digtk.lib import ai, derivative, fs, items, ocr_utils
from digtk.lib.items import ItemLocation
from digtk.models import ocr, prep

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
                                  "page_tag": f"page-{i:03d}", "local_path": f"{tmp_dir}/{stem}-{i}.tif"})
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
    assert json.loads(result_write["data"]) == {"tesseract_confidence": 88.5}


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
    assert fake_prep["stage_pages"] == [{"files": {"photo-a.tiff"}}]
    assert "files" not in fake_tesseract[0]["opts"]


def test_run_ocr_files_opt_keeps_mixed_item_pdf_namespacing(monkeypatch, fake_derivative, fake_tesseract, fake_vision, fake_prep):
    _stub_resolve(monkeypatch, MIXED_ITEM)

    result = ocr.run_ocr(ARK, opts={"files": {"document.pdf"}})

    assert result["images"] == {}
    assert result["pdfs"]["document.pdf"]["pages"]["1"]["hocr_path"].endswith("item/document/text/page-000/ocr.hocr")
