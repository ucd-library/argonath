import json
import os
from contextlib import contextmanager

import pytest

from digtk.lib.items import ItemLocation
from digtk.models.digital_website import pipeline
from digtk.models.digital_website import pdf as pd

ARK = "ark:/87287/d7abc1234"
WIDTHS = "256,512,1024"

ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/document.pdf"],
)

MULTI_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="box-1/folder-3",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/box-1/folder-3/doc-a.pdf",
        "cask:/bronze/g/c/latest/box-1/folder-3/doc-b.pdf",
    ],
)

# Page counts pdf_render.page_count() would report for each source, by filename
# stem — pdf.py's own orchestration logic is what's under test here, so
# pdf_render's actual pdfinfo/ImageMagick-backed rasterization is faked out (see
# fake_pdf_render below); it has its own dedicated tests in test_pdf_render.py.
PAGE_COUNTS_BY_STEM = {"document": 2, "doc-a": 1, "doc-b": 1}

PDF_BYTES_BY_PATH = {
    "cask:/bronze/g/c/latest/item/document.pdf": b"fake-pdf-bytes",
    "cask:/bronze/g/c/latest/box-1/folder-3/doc-a.pdf": b"fake-pdf-bytes",
    "cask:/bronze/g/c/latest/box-1/folder-3/doc-b.pdf": b"fake-pdf-bytes",
}

# An archival transfer supplying both a compiled PDF and a per-page TIFF for the same
# physical document, both supplied directly by the archiving team (see
# docs/cask-conventions.md) — digital_website/pdf.py must only ever touch the PDF. The TIFF
# is never staged, so it needs no entry in PDF_BYTES_BY_PATH.
MIXED_FORMAT_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/document.pdf",
        "cask:/bronze/g/c/latest/item/photo.tiff",
    ],
)


@pytest.fixture(autouse=True)
def fake_items(monkeypatch):
    monkeypatch.setattr(pd.items, "resolve", lambda ark, opts={}: ITEM)


def use_multi_item(monkeypatch):
    monkeypatch.setattr(pd.items, "resolve", lambda ark, opts={}: MULTI_ITEM)


@pytest.fixture
def fake_fs(monkeypatch):
    read_stream_calls = []

    @contextmanager
    def fake_read_stream(path, opts={}):
        read_stream_calls.append(path)
        yield iter([PDF_BYTES_BY_PATH[path]])

    monkeypatch.setattr(pipeline.fs, "read_stream", fake_read_stream)
    monkeypatch.setattr(pipeline.fs, "exists", lambda path: False)
    monkeypatch.setattr(pipeline.fs, "read", lambda path: b"{}")
    return {"read_stream_calls": read_stream_calls}


@pytest.fixture
def fake_pdf_render(monkeypatch):
    """Fakes pdf_render.page_count()/rasterize_page() so this file tests pdf.py's
    own page-looping/publishing orchestration only — pdf_render's actual
    pdfinfo/ImageMagick-backed rasterization has its own dedicated tests in
    test_pdf_render.py."""
    calls = {"rasterize": []}

    def fake_page_count(local_path, opts={}):
        stem = os.path.splitext(os.path.basename(local_path))[0]
        return PAGE_COUNTS_BY_STEM[stem]

    def fake_rasterize(pdf_path, page_index, dest_path, opts={}):
        calls["rasterize"].append({"pdf_path": pdf_path, "page_index": page_index, "dest_path": dest_path})
        with open(dest_path, "wb") as f:
            f.write(b"fake-raster-bytes")
        return {"dest_path": dest_path, "dpi": 600, "width_px": 3072, "height_px": 3976}

    monkeypatch.setattr(pd.pdf_render, "page_count", fake_page_count)
    monkeypatch.setattr(pd.pdf_render, "rasterize_page", fake_rasterize)
    return calls


@pytest.fixture
def fake_derivative(monkeypatch):
    calls = []

    def fake_write(**kwargs):
        calls.append(kwargs)
        dest = pipeline.derivative.dest_path(
            type=kwargs["type"], collection=kwargs["collection"],
            rel_path=kwargs["rel_path"], opts=kwargs.get("opts", {}),
        )
        return {"path": f"cask:{dest}", "links": []}

    monkeypatch.setattr(pipeline.derivative, "write", fake_write)
    return calls


@pytest.fixture
def fake_image_magick(monkeypatch):
    calls = {"thumbnail": [], "convert_format": [], "deskew": [], "prep_ocr": [], "tile": [], "detect_skew_angle": []}

    def record(kind):
        def fn(src, dest, opts={}):
            calls[kind].append({"src": src, "dest": dest, "opts": opts})
            with open(dest, "wb") as f:
                f.write(b"fake-image-bytes")
            return {"dest_path": dest}
        return fn

    def record_thumbnail():
        def fn(src, dest, width, opts={}):
            calls["thumbnail"].append({"src": src, "dest": dest, "width": width, "opts": opts})
            with open(dest, "wb") as f:
                f.write(b"fake-image-bytes")
            return {"dest_path": dest}
        return fn

    def fake_detect_skew_angle(src, opts={}):
        calls["detect_skew_angle"].append({"src": src, "opts": opts})
        return 0.0

    monkeypatch.setattr(pd.image_magick, "thumbnail", record_thumbnail())
    monkeypatch.setattr(pd.image_magick, "convert_format", record("convert_format"))
    monkeypatch.setattr(pd.image_magick, "deskew", record("deskew"))
    monkeypatch.setattr(pd.image_magick, "prep_ocr", record("prep_ocr"))
    monkeypatch.setattr(pd.image_magick, "tile", record("tile"))
    monkeypatch.setattr(pd.image_magick, "detect_skew_angle", fake_detect_skew_angle)
    monkeypatch.setattr(pd.image_magick, "probe_dimensions", lambda path, opts={}: {"width": 100, "height": 200})
    monkeypatch.setattr(
        pd.image_magick, "probe_all_frame_dimensions",
        lambda path, opts={}: [{"width": 3072, "height": 3976}, {"width": 1536, "height": 1988}],
    )
    return calls


@pytest.fixture
def fake_djvu(monkeypatch):
    """Stubs out digtk.lib.djvu (via pipeline.build_ocr_products()'s own
    reference to it) so tests don't need real hOCR files on disk -- mirrors
    test_models_digital_website_pipeline.py's fixture of the same name."""
    def fake_parse_hocr(hocr_path):
        return {"careas": []}

    def fake_build_djvu_xml(page_structure, **kwargs):
        return "<OBJECT/>"

    def fake_compute_ocr_stats(page_structures, *, selected, opts={}):
        return {"selected": selected, "original": {}, "deskewed": {}, "config": {}}

    monkeypatch.setattr(pipeline.djvu, "parse_hocr", fake_parse_hocr)
    monkeypatch.setattr(pipeline.djvu, "build_djvu_xml", fake_build_djvu_xml)
    monkeypatch.setattr(pipeline.djvu, "compute_ocr_stats", fake_compute_ocr_stats)


def fake_compare_orientations(selected):
    def fn(candidates, opts={}):
        return {
            "selected": selected,
            "stats": {
                "original": {"count": 10, "hocr_path": "/tmp/orig.hocr"},
                "deskewed": {"count": 20, "hocr_path": "/tmp/deskew.hocr"},
            },
        }
    return fn


# --- page_count ---------------------------------------------------------------

def test_page_count_reports_per_source(fake_fs, fake_pdf_render):
    result = pd.page_count(ARK)
    assert result == {"status": "success", "ark": ARK, "pages": {"document": 2}}


def test_page_count_multi_pdf_item(fake_fs, fake_pdf_render, monkeypatch):
    use_multi_item(monkeypatch)
    result = pd.page_count(ARK)
    assert result["pages"] == {"doc-a": 1, "doc-b": 1}


# --- mixed-format item: PDF + TIFF supplied for the same document -----------

def test_page_count_ignores_tiff_sibling_and_stages_only_the_pdf(fake_fs, fake_pdf_render, monkeypatch):
    monkeypatch.setattr(pd.items, "resolve", lambda ark, opts={}: MIXED_FORMAT_ITEM)
    result = pd.page_count(ARK)

    assert fake_fs["read_stream_calls"] == ["cask:/bronze/g/c/latest/item/document.pdf"]
    assert result["pages"] == {"document": 2}


def test_derivatives_raises_when_item_has_no_pdf_binaries(fake_fs, fake_pdf_render, monkeypatch):
    tiff_only_item = ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
    )
    monkeypatch.setattr(pd.items, "resolve", lambda ark, opts={}: tiff_only_item)

    with pytest.raises(ValueError, match="no pdf binaries found"):
        pd.derivatives(ARK)


# --- ad hoc single-page commands -----------------------------------------------

def test_thumbnail_publishes_under_pdf_page_subdir(fake_fs, fake_pdf_render, fake_derivative, fake_image_magick):
    result = pd.thumbnail(ARK, opts={"page": 1, "widths": WIDTHS, "app": "digital-dev"})
    pdf_result = result["pdfs"]["document.pdf"]
    assert pdf_result["dir"] == "cask:/gold/digital-dev/c/item/public-frontend-derivatives/document.pdf/images/2"
    assert sorted(pdf_result["outputs"]) == ["large.jpg", "medium.jpg", "small.jpg"]
    write_call = fake_derivative[0]
    assert write_call["opts"]["link_relation"] == "https://lib.ucdavis.edu/argonath/ns#derivativeOf"


def test_convert_writes_converted_output(fake_fs, fake_pdf_render, fake_derivative, fake_image_magick):
    result = pd.convert(ARK, opts={"page": 0, "format": "png", "app": "digital-dev"})
    assert result["outputs"] == ["converted.png"]
    assert result["dir"] == "cask:/gold/digital-dev/c/item/public-frontend-derivatives/page-000"


def test_deskew_standalone(fake_fs, fake_pdf_render, fake_derivative, fake_image_magick):
    result = pd.deskew(ARK, opts={"page": 0, "threshold": 25, "app": "digital-dev"})
    assert result["outputs"] == ["deskewed.jpg"]
    assert result["dir"] == "cask:/gold/digital-dev/c/item/public-frontend-derivatives/page-000"
    assert fake_image_magick["deskew"][0]["opts"]["threshold"] == 25


def test_prep_ocr_standalone_publishes_to_ocr_subdir(fake_fs, fake_pdf_render, fake_derivative, fake_image_magick):
    result = pd.prep_ocr(ARK, opts={"page": 0})
    assert result["outputs"] == ["prep.jpg"]
    assert result["dir"] == "cask:/silver/g/c/latest/item/ocr/page-000"


# --- derivatives() umbrella -----------------------------------------------------

def test_derivatives_processes_every_page(fake_fs, fake_pdf_render, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    result = pd.derivatives(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    doc = result["pdfs"]["document.pdf"]
    assert doc["pages"] == 2
    assert sorted(doc["orientation"].keys()) == ["1", "2"]
    assert [doc["derivatives"]["pages"][k]["dir"] for k in ("1", "2")] == [
        "cask:/gold/digital-dev/c/item/public-frontend-derivatives/document.pdf/images/1",
        "cask:/gold/digital-dev/c/item/public-frontend-derivatives/document.pdf/images/2",
    ]
    assert [p["dir"] for p in doc["ocr"]["pages"]] == [
        "cask:/silver/g/c/latest/item/ocr/page-000",
        "cask:/silver/g/c/latest/item/ocr/page-001",
    ]


def test_derivatives_writes_aggregate_manifest(fake_fs, fake_pdf_render, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("deskewed"))
    pd.derivatives(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    manifest_call = next(
        c for c in fake_derivative
        if c["rel_path"] == "item/public-frontend-derivatives/document.pdf/images/manifest.json"
    )
    manifest = json.loads(manifest_call["data"])
    assert manifest["params"]["pages"] == 2
    assert manifest["params"]["orientation"]["1"]["deskew_selected"] is True


def test_derivatives_writes_per_page_manifest(fake_fs, fake_pdf_render, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    pd.derivatives(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    page_manifest_call = next(
        c for c in fake_derivative
        if c["rel_path"] == "item/public-frontend-derivatives/document.pdf/images/1/manifest.json"
    )
    manifest = json.loads(page_manifest_call["data"])
    assert manifest["params"]["page"] == 0


def test_derivatives_records_rich_manifest_content_per_page(fake_fs, fake_pdf_render, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    pd.derivatives(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    page_manifest_call = next(
        c for c in fake_derivative
        if c["rel_path"] == "item/public-frontend-derivatives/document.pdf/images/1/manifest.json"
    )
    manifest = json.loads(page_manifest_call["data"])

    assert manifest["page"] == 1
    assert manifest["extractDensity"] == 600
    assert "original" not in manifest
    for label in ("small", "medium", "large"):
        assert manifest[label] == {
            "size": {"width": "100", "height": "200"},
            "url": f"cask://gold/digital-dev/c/item/public-frontend-derivatives/document.pdf/images/1/{label}.jpg",
        }
    assert manifest["tiled"] == {
        "size": [{"width": "3072", "height": "3976"}, {"width": "1536", "height": "1988"}],
        "url": "cask://gold/digital-dev/c/item/public-frontend-derivatives/document.pdf/images/1/tiled.tif",
    }
    assert manifest["ocr"] == {
        "size": {"width": "100", "height": "200"},
        "imageSize": "large",
        "url": "cask://silver/g/c/latest/item/ocr/page-000/ocr.djvu",
    }


def test_derivatives_writes_pages_array_on_aggregate_manifest(fake_fs, fake_pdf_render, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    pd.derivatives(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    agg_manifest_call = next(
        c for c in fake_derivative
        if c["rel_path"] == "item/public-frontend-derivatives/document.pdf/images/manifest.json"
    )
    manifest = json.loads(agg_manifest_call["data"])

    assert [p["page"] for p in manifest["pages"]] == [1, 2]
    assert manifest["pages"][0]["small"]["url"].endswith("/images/1/small.jpg")
    assert manifest["pages"][1]["small"]["url"].endswith("/images/2/small.jpg")


def test_derivatives_decides_orientation_independently_per_page(fake_fs, fake_pdf_render, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    calls = []

    def selective_compare(candidates, opts={}):
        calls.append(candidates)
        selected = "deskewed" if len(calls) == 1 else "original"
        return {
            "selected": selected,
            "stats": {
                "original": {"count": 1, "hocr_path": "/tmp/orig.hocr"},
                "deskewed": {"count": 2, "hocr_path": "/tmp/deskew.hocr"},
            },
        }

    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", selective_compare)
    result = pd.derivatives(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    assert len(calls) == 2
    orientation = result["pdfs"]["document.pdf"]["orientation"]
    assert orientation["1"]["deskew"] is True
    assert orientation["2"]["deskew"] is False


def test_derivatives_associated_media_covers_every_page_with_position(fake_fs, fake_pdf_render, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    pd.derivatives(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    doc = json.loads(sidecar_write["data"])
    assert len(doc["associatedMedia"]) == 6  # 2 pages x 3 sizes each
    assert sorted(m["position"] for m in doc["associatedMedia"]) == [0, 0, 0, 1, 1, 1]
    for m in doc["associatedMedia"]:
        assert m["@type"] == "ImageObject"


def test_derivatives_multi_pdf_item_nests_by_filename(fake_fs, fake_pdf_render, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    use_multi_item(monkeypatch)
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))

    result = pd.derivatives(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    assert set(result["pdfs"].keys()) == {"doc-a.pdf", "doc-b.pdf"}
    assert result["pdfs"]["doc-a.pdf"]["derivatives"]["pages"]["1"]["dir"] == (
        "cask:/gold/digital-dev/c/box-1/folder-3/public-frontend-derivatives/doc-a.pdf/images/1"
    )
