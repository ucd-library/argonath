import json

import pytest

from digtk.lib import ai, derivative, fs, items, ocr_utils
from digtk.lib.items import ItemLocation
from digtk.models import ocr

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

IMAGE_OCR_MANIFEST = json.dumps({"params": {"orientation": {"photo": {"deskew_selected": False}}}}).encode()
MULTI_IMAGE_OCR_MANIFEST = json.dumps({"params": {"orientation": {
    "photo-a": {"deskew_selected": False}, "photo-b": {"deskew_selected": True},
}}}).encode()
PDF_OCR_MANIFEST = json.dumps({"params": {
    "pages": 2,
    "orientation": {"1": {"deskew_selected": False}, "2": {"deskew_selected": True}},
}}).encode()


@pytest.fixture
def fake_derivative(monkeypatch):
    written = []

    def fake_write(*, type, collection, rel_path, data, source_files, opts={}):
        written.append({
            "type": type, "collection": collection, "rel_path": rel_path,
            "data": data, "source_files": source_files, "opts": opts,
        })
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


def _stub_resolve(monkeypatch, item):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: item)


def _stub_fs_read(monkeypatch, table):
    def fake_read(path):
        if path in table:
            return table[path]
        return b"IMGDATA"  # any staged image path not otherwise specified
    monkeypatch.setattr(fs, "read", fake_read)


# --- images ------------------------------------------------------------------

def test_run_ocr_image_runs_both_engines(monkeypatch, fake_derivative, fake_tesseract, fake_vision):
    _stub_resolve(monkeypatch, ITEM)
    _stub_fs_read(monkeypatch, {"cask:/silver/g/c/latest/item/ocr/manifest.json": IMAGE_OCR_MANIFEST})

    result = ocr.run_ocr(ARK)

    assert result["status"] == "success"
    image_result = result["images"]["photo.tiff"]
    assert image_result["tesseract_confidence"] == 88.5
    assert image_result["hocr_path"].endswith("item/ocr/ocr.hocr")
    assert image_result["transcript_path"].endswith("item/ocr/transcript.txt")

    assert len(fake_tesseract) == 1
    assert fake_tesseract[0]["image_path"].endswith(".jpg")

    assert len(fake_vision) == 1
    assert fake_vision[0]["opts"]["image"] == (
        "cask:/silver/g/c/latest/item/public-frontend-derivatives/images/photo.tiff/images/large.jpg"
    )

    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert rel_paths == {
        "item/ocr/ocr.hocr", "item/ocr/transcript.txt", "item/ocr/ocr-result.json",
    }
    result_write = next(w for w in fake_derivative if w["rel_path"] == "item/ocr/ocr-result.json")
    assert json.loads(result_write["data"]) == {"tesseract_confidence": 88.5}


def test_run_ocr_disambiguates_multi_image_filenames(monkeypatch, fake_derivative, fake_tesseract, fake_vision):
    _stub_resolve(monkeypatch, MULTI_ITEM)
    _stub_fs_read(monkeypatch, {
        "cask:/silver/g/c/latest/box-1/folder-3/ocr/manifest.json": MULTI_IMAGE_OCR_MANIFEST,
    })

    result = ocr.run_ocr(ARK)

    assert set(result["images"]) == {"photo-a.tiff", "photo-b.tiff"}
    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert rel_paths == {
        "box-1/folder-3/ocr/photo-a-ocr.hocr", "box-1/folder-3/ocr/photo-a-transcript.txt",
        "box-1/folder-3/ocr/photo-a-ocr-result.json",
        "box-1/folder-3/ocr/photo-b-ocr.hocr", "box-1/folder-3/ocr/photo-b-transcript.txt",
        "box-1/folder-3/ocr/photo-b-ocr-result.json",
    }


def test_run_ocr_no_vision_skips_samwise_call(monkeypatch, fake_derivative, fake_tesseract, fake_vision):
    _stub_resolve(monkeypatch, ITEM)
    _stub_fs_read(monkeypatch, {"cask:/silver/g/c/latest/item/ocr/manifest.json": IMAGE_OCR_MANIFEST})

    result = ocr.run_ocr(ARK, opts={"vision": False})

    assert fake_vision == []
    assert result["images"]["photo.tiff"]["transcript_path"] is None
    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert rel_paths == {"item/ocr/ocr.hocr", "item/ocr/ocr-result.json"}
    assert "item/ocr/transcript.txt" not in rel_paths


def test_run_ocr_never_touches_ocr_manifest_json(monkeypatch, fake_derivative, fake_tesseract, fake_vision):
    """Regression guard: image.py/pdf.py already write ocr/manifest.json to
    record orientation decisions; run_ocr() must never write to that same
    path (it would silently overwrite that content)."""
    _stub_resolve(monkeypatch, ITEM)
    _stub_fs_read(monkeypatch, {"cask:/silver/g/c/latest/item/ocr/manifest.json": IMAGE_OCR_MANIFEST})

    ocr.run_ocr(ARK)

    assert not any(w["rel_path"].endswith("manifest.json") for w in fake_derivative)


def test_run_ocr_forwards_tesseract_opts(monkeypatch, fake_derivative, fake_tesseract, fake_vision):
    _stub_resolve(monkeypatch, ITEM)
    _stub_fs_read(monkeypatch, {"cask:/silver/g/c/latest/item/ocr/manifest.json": IMAGE_OCR_MANIFEST})

    ocr.run_ocr(ARK, opts={"lang": "fra", "psm": 6})

    assert fake_tesseract[0]["opts"] == {"lang": "fra", "psm": 6}


# --- PDF pages -----------------------------------------------------------------

def test_run_ocr_pdf_runs_both_engines_per_page(monkeypatch, fake_derivative, fake_tesseract, fake_vision):
    _stub_resolve(monkeypatch, PDF_ITEM)
    _stub_fs_read(monkeypatch, {"cask:/silver/g/c/latest/item/ocr/manifest.json": PDF_OCR_MANIFEST})

    result = ocr.run_ocr(ARK)

    assert "images" not in result
    pages = result["pdfs"]["document.pdf"]["pages"]
    assert set(pages) == {"1", "2"}
    assert pages["1"]["hocr_path"].endswith("ocr/page-000/ocr.hocr")
    assert pages["2"]["hocr_path"].endswith("ocr/page-001/ocr.hocr")

    assert len(fake_tesseract) == 2
    assert len(fake_vision) == 2
    vision_images = {c["opts"]["image"] for c in fake_vision}
    assert vision_images == {
        "cask:/silver/g/c/latest/item/public-frontend-derivatives/document.pdf/images/1/large.jpg",
        "cask:/silver/g/c/latest/item/public-frontend-derivatives/document.pdf/images/2/large.jpg",
    }

    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert rel_paths == {
        "item/ocr/page-000/ocr.hocr", "item/ocr/page-000/transcript.txt", "item/ocr/page-000/ocr-result.json",
        "item/ocr/page-001/ocr.hocr", "item/ocr/page-001/transcript.txt", "item/ocr/page-001/ocr-result.json",
    }


def test_run_ocr_mixed_item_namespaces_pdf_ocr_to_avoid_manifest_collision(
    monkeypatch, fake_derivative, fake_tesseract, fake_vision,
):
    """Regression test: an item with both image and PDF binaries sharing one
    item_path must not have the PDF's OCR manifest collide with the image's
    at the same bare "ocr/manifest.json" path (this exact case broke against
    a real item — the PDF's page-number-keyed manifest silently overwrote the
    image's stem-keyed one, since a single PDF's OCR output wasn't namespaced
    unless there was more than one PDF)."""
    _stub_resolve(monkeypatch, MIXED_ITEM)
    _stub_fs_read(monkeypatch, {
        "cask:/silver/g/c/latest/item/ocr/manifest.json": IMAGE_OCR_MANIFEST,
        "cask:/silver/g/c/latest/item/document/ocr/manifest.json": PDF_OCR_MANIFEST,
    })

    result = ocr.run_ocr(ARK)

    assert result["images"]["photo.tiff"]["hocr_path"].endswith("item/ocr/ocr.hocr")
    pages = result["pdfs"]["document.pdf"]["pages"]
    assert pages["1"]["hocr_path"].endswith("item/document/ocr/page-000/ocr.hocr")
    assert pages["2"]["hocr_path"].endswith("item/document/ocr/page-001/ocr.hocr")

    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert "item/ocr/ocr.hocr" in rel_paths  # the image's, in the bare ocr/ dir
    assert "item/document/ocr/page-000/ocr.hocr" in rel_paths  # the PDF's, namespaced


def test_run_ocr_raises_if_pdf_derivatives_not_published(monkeypatch, fake_tesseract, fake_vision):
    _stub_resolve(monkeypatch, PDF_ITEM)

    def fake_read(path):
        raise FileNotFoundError(path)
    monkeypatch.setattr(fs, "read", fake_read)

    with pytest.raises(FileNotFoundError):
        ocr.run_ocr(ARK)


def test_run_ocr_raises_when_no_image_or_pdf_binaries(monkeypatch):
    _stub_resolve(monkeypatch, VIDEO_ONLY_ITEM)

    with pytest.raises(ValueError, match="no image or PDF binaries found"):
        ocr.run_ocr(ARK)
