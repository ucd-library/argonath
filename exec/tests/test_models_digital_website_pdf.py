import json
from contextlib import contextmanager

import pymupdf
import pytest

from digtk.lib.items import ItemLocation
from digtk.models.digital_website import pipeline
from digtk.models.digital_website import pdf as pd

ARK = "ark:/87287/d7abc1234"


def _pdf_bytes(num_pages):
    doc = pymupdf.open()
    for _ in range(num_pages):
        doc.new_page(width=300, height=150)
    data = doc.tobytes()
    doc.close()
    return data

ONE_PAGE_PDF = _pdf_bytes(1)
TWO_PAGE_PDF = _pdf_bytes(2)

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

PDF_BYTES_BY_PATH = {
    "cask:/bronze/g/c/latest/item/document.pdf": TWO_PAGE_PDF,
    "cask:/bronze/g/c/latest/box-1/folder-3/doc-a.pdf": ONE_PAGE_PDF,
    "cask:/bronze/g/c/latest/box-1/folder-3/doc-b.pdf": ONE_PAGE_PDF,
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
    calls = {"thumbnail": [], "convert_format": [], "deskew": [], "prep_ocr": []}

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

    monkeypatch.setattr(pd.image_magick, "thumbnail", record_thumbnail())
    monkeypatch.setattr(pd.image_magick, "convert_format", record("convert_format"))
    monkeypatch.setattr(pd.image_magick, "deskew", record("deskew"))
    monkeypatch.setattr(pd.image_magick, "prep_ocr", record("prep_ocr"))
    monkeypatch.setattr(pd.image_magick, "probe_dimensions", lambda path, opts={}: {"width": 100, "height": 200})
    return calls


def fake_compare_orientations(selected):
    def fn(candidates, opts={}):
        return {"selected": selected, "stats": {"original": {"count": 10}, "deskewed": {"count": 20}}}
    return fn


# --- page_count ---------------------------------------------------------------

def test_page_count_reports_per_source(fake_fs):
    result = pd.page_count(ARK)
    assert result == {"status": "success", "ark": ARK, "pages": {"document": 2}}


def test_page_count_multi_pdf_item(fake_fs, monkeypatch):
    use_multi_item(monkeypatch)
    result = pd.page_count(ARK)
    assert result["pages"] == {"doc-a": 1, "doc-b": 1}


# --- mixed-format item: PDF + TIFF supplied for the same document -----------

def test_page_count_ignores_tiff_sibling_and_stages_only_the_pdf(fake_fs, monkeypatch):
    monkeypatch.setattr(pd.items, "resolve", lambda ark, opts={}: MIXED_FORMAT_ITEM)
    result = pd.page_count(ARK)

    assert fake_fs["read_stream_calls"] == ["cask:/bronze/g/c/latest/item/document.pdf"]
    assert result["pages"] == {"document": 2}


def test_derivatives_raises_when_item_has_no_pdf_binaries(fake_fs, monkeypatch):
    tiff_only_item = ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
    )
    monkeypatch.setattr(pd.items, "resolve", lambda ark, opts={}: tiff_only_item)

    with pytest.raises(ValueError, match="no pdf binaries found"):
        pd.derivatives(ARK)


# --- ad hoc single-page commands -----------------------------------------------

def test_thumbnail_publishes_under_page_subdir(fake_fs, fake_derivative, fake_image_magick):
    result = pd.thumbnail(ARK, opts={"page": 1, "widths": "256"})
    assert result["dir"] == "cask:/silver/g/c/latest/item/derivatives/page-001"
    assert result["outputs"] == ["web-256.jpg"]
    write_call = fake_derivative[0]
    assert write_call["opts"]["link_relation"] == "https://lib.ucdavis.edu/argonath/ns#derivativeOf"


def test_convert_writes_converted_output(fake_fs, fake_derivative, fake_image_magick):
    result = pd.convert(ARK, opts={"page": 0, "format": "png"})
    assert result["outputs"] == ["converted.png"]
    assert result["dir"] == "cask:/silver/g/c/latest/item/derivatives/page-000"


def test_deskew_standalone(fake_fs, fake_derivative, fake_image_magick):
    result = pd.deskew(ARK, opts={"page": 0, "threshold": 25})
    assert result["outputs"] == ["deskewed.jpg"]
    assert fake_image_magick["deskew"][0]["opts"]["threshold"] == 25


def test_prep_ocr_standalone_publishes_to_ocr_subdir(fake_fs, fake_derivative, fake_image_magick):
    result = pd.prep_ocr(ARK, opts={"page": 0})
    assert result["outputs"] == ["prep.jpg"]
    assert result["dir"] == "cask:/silver/g/c/latest/item/ocr/page-000"


# --- derivatives() umbrella -----------------------------------------------------

def test_derivatives_processes_every_page(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    result = pd.derivatives(ARK, opts={"widths": "256"})

    doc = result["pdfs"]["document"]
    assert doc["pages"] == 2
    assert sorted(doc["orientation"].keys()) == ["page-000", "page-001"]
    assert [p["dir"] for p in doc["derivatives"]["pages"]] == [
        "cask:/silver/g/c/latest/item/derivatives/page-000",
        "cask:/silver/g/c/latest/item/derivatives/page-001",
    ]
    assert [p["dir"] for p in doc["ocr"]["pages"]] == [
        "cask:/silver/g/c/latest/item/ocr/page-000",
        "cask:/silver/g/c/latest/item/ocr/page-001",
    ]


def test_derivatives_writes_aggregate_manifest(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("deskewed"))
    pd.derivatives(ARK, opts={"widths": "256"})

    manifest_call = next(c for c in fake_derivative if c["rel_path"] == "item/derivatives/manifest.json")
    manifest = json.loads(manifest_call["data"])
    assert manifest["params"]["pages"] == 2
    assert manifest["params"]["orientation"]["page-000"]["deskew_selected"] is True


def test_derivatives_decides_orientation_independently_per_page(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    calls = []

    def selective_compare(candidates, opts={}):
        calls.append(candidates)
        selected = "deskewed" if len(calls) == 1 else "original"
        return {"selected": selected, "stats": {"original": {"count": 1}, "deskewed": {"count": 2}}}

    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", selective_compare)
    result = pd.derivatives(ARK, opts={"widths": "256"})

    assert len(calls) == 2
    orientation = result["pdfs"]["document"]["orientation"]
    assert orientation["page-000"]["deskew"] is True
    assert orientation["page-001"]["deskew"] is False


def test_derivatives_associated_media_covers_every_page_with_position(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    pd.derivatives(ARK, opts={"widths": "256"})

    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    doc = json.loads(sidecar_write["data"])
    assert len(doc["associatedMedia"]) == 2
    assert sorted(m["position"] for m in doc["associatedMedia"]) == [0, 1]
    for m in doc["associatedMedia"]:
        assert m["@type"] == "ImageObject"


def test_derivatives_multi_pdf_item_nests_by_stem(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    use_multi_item(monkeypatch)
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))

    result = pd.derivatives(ARK, opts={"widths": "256"})

    assert set(result["pdfs"].keys()) == {"doc-a", "doc-b"}
    doc_a_dirs = [p["dir"] for p in result["pdfs"]["doc-a"]["derivatives"]["pages"]]
    assert doc_a_dirs == ["cask:/silver/g/c/latest/box-1/folder-3/doc-a/derivatives/page-000"]
