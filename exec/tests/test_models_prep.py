from contextlib import contextmanager

import pytest

from digtk.lib import fs, image_magick, ocr_utils, pdf_render
from digtk.lib.items import ItemLocation
from digtk.models import prep

ARK = "ark:/87287/d7abc1234"

MIXED_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/photo.tiff",
        "cask:/bronze/g/c/latest/item/document.pdf",
        "cask:/bronze/g/c/latest/item/reel.mp4",
    ],
)


@pytest.fixture
def fake_staging(monkeypatch):
    """Stubs fs.read_stream (staging) and pdf_render (a 3-page PDF), recording calls."""
    calls = {"read_stream": [], "rasterize_page": []}

    @contextmanager
    def fake_read_stream(path, opts={}):
        calls["read_stream"].append(path)
        yield iter([b"BYTES"])

    def fake_rasterize_page(pdf_path, page_index, dest_path, opts={}):
        calls["rasterize_page"].append({"pdf_path": pdf_path, "page_index": page_index, "dest_path": dest_path})
        with open(dest_path, "wb") as f:
            f.write(b"RASTER")
        return {"dest_path": dest_path, "dpi": 300.0, "width_px": 3072, "height_px": 4000}

    monkeypatch.setattr(fs, "read_stream", fake_read_stream)
    monkeypatch.setattr(pdf_render, "page_count", lambda pdf_path, opts={}: 3)
    monkeypatch.setattr(pdf_render, "rasterize_page", fake_rasterize_page)
    return calls


def test_stage_pages_one_page_per_image_and_per_pdf_page(tmp_path, fake_staging):
    pages = prep.stage_pages(MIXED_ITEM, str(tmp_path))

    assert [(p["kind"], p["filename"], p["page_no"]) for p in pages] == [
        ("image", "photo.tiff", None),
        ("pdf", "document.pdf", 1),
        ("pdf", "document.pdf", 2),
        ("pdf", "document.pdf", 3),
    ]
    image = pages[0]
    assert image["source_path"] == "cask:/bronze/g/c/latest/item/photo.tiff"
    assert image["stem"] == "photo"
    assert image["page_tag"] is None
    assert image["local_path"] == str(tmp_path / "photo.tiff")

    pdf_page = pages[2]
    assert pdf_page["source_path"] == "cask:/bronze/g/c/latest/item/document.pdf"
    assert pdf_page["count"] == 3
    assert pdf_page["page_tag"] == "page-001"
    assert pdf_page["local_path"] == str(tmp_path / "document-page-001-raster.tif")
    assert [c["page_index"] for c in fake_staging["rasterize_page"]] == [0, 1, 2]


def test_stage_pages_files_opt_limits_staging_to_those_filenames(tmp_path, fake_staging):
    pages = prep.stage_pages(MIXED_ITEM, str(tmp_path), opts={"files": {"photo.tiff"}})

    assert [p["filename"] for p in pages] == ["photo.tiff"]
    assert fake_staging["read_stream"] == ["cask:/bronze/g/c/latest/item/photo.tiff"]
    assert fake_staging["rasterize_page"] == []


def test_stage_pages_skips_non_image_non_pdf_binaries(tmp_path, fake_staging):
    prep.stage_pages(MIXED_ITEM, str(tmp_path))

    assert "cask:/bronze/g/c/latest/item/reel.mp4" not in fake_staging["read_stream"]


def test_stage_pages_formats_opt_limits_what_is_staged(tmp_path, fake_staging):
    pages = prep.stage_pages(MIXED_ITEM, str(tmp_path), opts={"formats": {"pdf"}})

    assert {p["kind"] for p in pages} == {"pdf"}
    assert fake_staging["read_stream"] == ["cask:/bronze/g/c/latest/item/document.pdf"]


@pytest.fixture
def fake_magick(monkeypatch):
    calls = {"prep_ocr": [], "thumbnail": []}

    def fake_prep_ocr(src_path, dest_path, opts={}):
        calls["prep_ocr"].append({"src": src_path, "dest": dest_path, "opts": opts})
        with open(dest_path, "wb") as f:
            f.write(b"PREP")
        return {"dest_path": dest_path}

    def fake_thumbnail(src_path, dest_path, width, opts={}):
        calls["thumbnail"].append({"src": src_path, "dest": dest_path, "width": width, "opts": opts})
        return {"dest_path": dest_path}

    monkeypatch.setattr(image_magick, "prep_ocr", fake_prep_ocr)
    monkeypatch.setattr(image_magick, "thumbnail", fake_thumbnail)
    monkeypatch.setattr(image_magick, "detect_skew_angle", lambda src, opts={}: 2.5)
    return calls


PAGE = {
    "source_path": "cask:/bronze/g/c/latest/item/document.pdf", "filename": "document.pdf",
    "stem": "document", "kind": "pdf", "page_no": 2, "count": 3, "page_tag": "page-001",
    "local_path": "/tmp/x/document-page-001-raster.tif",
}


@pytest.mark.parametrize("selected, expected_name, deskew", [
    ("deskewed", "document-page-001-prep-deskew.jpg", True),
    ("original", "document-page-001-prep.jpg", False),
])
def test_ocr_image_renders_the_orientation_comparisons_winner(
    tmp_path, monkeypatch, fake_magick, selected, expected_name, deskew,
):
    monkeypatch.setattr(ocr_utils, "compare_orientations", lambda candidates, opts={}: {
        "selected": selected, "stats": {"original": {}, "deskewed": {}},
    })

    result = prep.ocr_image(PAGE, str(tmp_path), opts={"psm": 6})

    assert result == {"local_path": str(tmp_path / expected_name), "deskew": deskew, "angle": 2.5}
    final = fake_magick["prep_ocr"][-1]
    assert final["src"] == PAGE["local_path"]
    assert final["dest"] == str(tmp_path / expected_name)
    assert final["opts"] == {"psm": 6, "deskew": deskew}
    # Two orientation candidates first, then the final render.
    assert len(fake_magick["prep_ocr"]) == 3


def test_display_image_renders_unbinarized_with_same_opts(tmp_path, fake_magick):
    dest = prep.display_image(PAGE, str(tmp_path), opts={"deskew": True, "enhance": "auto"})

    assert dest == str(tmp_path / "document-page-001-display.jpg")
    call = fake_magick["prep_ocr"][0]
    assert call["src"] == PAGE["local_path"]
    assert call["opts"] == {"deskew": True, "enhance": "auto", "binarize": False}


def test_vision_image_defaults_to_the_largest_thumbnail_width(tmp_path, fake_magick):
    dest = prep.vision_image(PAGE, str(tmp_path))

    assert dest == str(tmp_path / "document-page-001-vision.jpg")
    call = fake_magick["thumbnail"][0]
    assert call["src"] == PAGE["local_path"]
    assert call["width"] == prep.parse_widths(prep.CONFIG["image"]["thumbnail_widths"])[-1]


def test_vision_image_honors_vision_width(tmp_path, fake_magick):
    image_page = {**PAGE, "kind": "image", "stem": "photo", "page_no": None, "page_tag": None}

    dest = prep.vision_image(image_page, str(tmp_path), opts={"vision_width": 800})

    assert dest == str(tmp_path / "photo-vision.jpg")
    assert fake_magick["thumbnail"][0]["width"] == 800


def test_natural_key_orders_numbers_by_value():
    names = ["page_10.tif", "page_2.tif", "Page_1.tif", "cover.tif"]
    assert sorted(names, key=prep.natural_key) == ["cover.tif", "Page_1.tif", "page_2.tif", "page_10.tif"]
