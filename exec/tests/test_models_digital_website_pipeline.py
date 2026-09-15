import json
import os
from contextlib import contextmanager

import pytest

from digtk.lib.items import ItemLocation
from digtk.models.digital_website import pipeline

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

# A real shape: an archival transfer supplying both a compiled PDF and per-page TIFF
# masters for the same physical document, both directly from the archiving team (see
# docs/cask-conventions.md) — neither format derived from the other.
MIXED_FORMAT_PATHS = [
    "cask:/bronze/g/c/latest/item/document.pdf",
    "cask:/bronze/g/c/latest/item/page-001.tif",
    "cask:/bronze/g/c/latest/item/page-002.tif",
]


def test_filter_binaries_by_format_returns_only_matching_extension():
    assert pipeline.filter_binaries_by_format(MIXED_FORMAT_PATHS, "pdf") == ["cask:/bronze/g/c/latest/item/document.pdf"]
    assert pipeline.filter_binaries_by_format(MIXED_FORMAT_PATHS, "image") == [
        "cask:/bronze/g/c/latest/item/page-001.tif", "cask:/bronze/g/c/latest/item/page-002.tif",
    ]


def test_filter_binaries_by_format_accepts_a_set_of_formats():
    paths = ["cask:/bronze/g/c/latest/item/movie.mp4", "cask:/bronze/g/c/latest/item/interview.wav"]
    assert pipeline.filter_binaries_by_format(paths, {"video", "audio"}) == paths


def test_filter_binaries_by_format_raises_when_nothing_matches():
    with pytest.raises(ValueError, match="no audio binaries found"):
        pipeline.filter_binaries_by_format(MIXED_FORMAT_PATHS, "audio")


def test_filter_binaries_by_format_raises_on_unrecognized_extension():
    with pytest.raises(ValueError, match="unrecognized media extension"):
        pipeline.filter_binaries_by_format(["cask:/bronze/g/c/latest/item/mystery.xyz"], "image")


def test_partition_by_format_splits_images_and_pdfs():
    assert pipeline.partition_by_format(MIXED_FORMAT_PATHS) == (
        ["cask:/bronze/g/c/latest/item/page-001.tif", "cask:/bronze/g/c/latest/item/page-002.tif"],
        ["cask:/bronze/g/c/latest/item/document.pdf"],
    )


def test_partition_by_format_ignores_unrecognized_and_other_formats():
    paths = [
        "cask:/bronze/g/c/latest/item/photo.tiff",
        "cask:/bronze/g/c/latest/item/reel.mp4",
        "cask:/bronze/g/c/latest/item/mystery.xyz",
    ]
    assert pipeline.partition_by_format(paths) == (["cask:/bronze/g/c/latest/item/photo.tiff"], [])


def test_partition_by_format_empty_when_no_binaries_match():
    assert pipeline.partition_by_format([]) == ([], [])


# --- pdf_ocr_prep_images() -------------------------------------------------------

PDF_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/document.pdf"],
)


def test_pdf_ocr_prep_images_builds_paths_per_page(monkeypatch):
    manifest = json.dumps({"params": {
        "pages": 2,
        "orientation": {"1": {"deskew_selected": False}, "2": {"deskew_selected": True}},
    }}).encode()
    monkeypatch.setattr(pipeline.fs, "read", lambda path: manifest)

    images, count = pipeline.pdf_ocr_prep_images(
        PDF_ITEM, "cask:/bronze/g/c/latest/item/document.pdf", namespace_ocr=False,
    )

    assert count == 2
    assert images == [
        "cask:/silver/g/c/latest/item/ocr/page-000/document-page-000-prep.jpg",
        "cask:/silver/g/c/latest/item/ocr/page-001/document-page-001-prep-deskew.jpg",
    ]


def test_pdf_ocr_prep_images_uses_stem_prefixed_ocr_dir_for_multi_pdf_items(monkeypatch):
    monkeypatch.setattr(pipeline.fs, "read", lambda path: json.dumps({
        "params": {"pages": 1, "orientation": {"1": {"deskew_selected": False}}},
    }).encode())

    images, count = pipeline.pdf_ocr_prep_images(
        PDF_ITEM, "cask:/bronze/g/c/latest/item/document.pdf", namespace_ocr=True,
    )

    assert images == ["cask:/silver/g/c/latest/item/document/ocr/page-000/document-page-000-prep.jpg"]


def test_pdf_ocr_prep_images_raises_if_derivatives_not_published(monkeypatch):
    def fake_read(path):
        raise FileNotFoundError(path)
    monkeypatch.setattr(pipeline.fs, "read", fake_read)

    with pytest.raises(FileNotFoundError):
        pipeline.pdf_ocr_prep_images(PDF_ITEM, "cask:/bronze/g/c/latest/item/document.pdf", namespace_ocr=False)


# --- image_ocr_prep_image() -------------------------------------------------------

def test_image_ocr_prep_image_builds_path_from_manifest(monkeypatch):
    monkeypatch.setattr(pipeline.fs, "read", lambda path: json.dumps({
        "params": {"orientation": {"photo": {"deskew_selected": False}}},
    }).encode())

    path = pipeline.image_ocr_prep_image(ITEM, "cask:/bronze/g/c/latest/item/photo.tiff", multi_image=False)

    assert path == "cask:/silver/g/c/latest/item/ocr/prep.jpg"


def test_image_ocr_prep_image_uses_deskewed_filename_when_selected(monkeypatch):
    monkeypatch.setattr(pipeline.fs, "read", lambda path: json.dumps({
        "params": {"orientation": {"photo": {"deskew_selected": True}}},
    }).encode())

    path = pipeline.image_ocr_prep_image(ITEM, "cask:/bronze/g/c/latest/item/photo.tiff", multi_image=False)

    assert path == "cask:/silver/g/c/latest/item/ocr/prep-deskew.jpg"


def test_image_ocr_prep_image_disambiguates_multi_image_items(monkeypatch):
    monkeypatch.setattr(pipeline.fs, "read", lambda path: json.dumps({
        "params": {"orientation": {"photo-a": {"deskew_selected": False}, "photo-b": {"deskew_selected": True}}},
    }).encode())

    a = pipeline.image_ocr_prep_image(MULTI_ITEM, "cask:/bronze/g/c/latest/box-1/folder-3/photo-a.tiff", multi_image=True)
    b = pipeline.image_ocr_prep_image(MULTI_ITEM, "cask:/bronze/g/c/latest/box-1/folder-3/photo-b.tiff", multi_image=True)

    assert a == "cask:/silver/g/c/latest/box-1/folder-3/ocr/photo-a-prep.jpg"
    assert b == "cask:/silver/g/c/latest/box-1/folder-3/ocr/photo-b-prep-deskew.jpg"


def test_image_ocr_prep_image_raises_if_derivatives_not_published(monkeypatch):
    def fake_read(path):
        raise FileNotFoundError(path)
    monkeypatch.setattr(pipeline.fs, "read", fake_read)

    with pytest.raises(FileNotFoundError):
        pipeline.image_ocr_prep_image(ITEM, "cask:/bronze/g/c/latest/item/photo.tiff", multi_image=False)

# generate_thumbnail_set(), select_orientation(), and update_associated_media() are
# already exercised end-to-end via test_models_digital_website_image.py's dw.generate() tests
# (same code, invoked through the still-image-specific orchestrator on top) — this file
# focuses on the pure helpers and on publish()'s link_relation behavior, which changed
# during the extraction from digital_website/image.py's private _publish().


def test_parse_widths_sorts_and_parses_comma_separated_string():
    assert pipeline.parse_widths("1024,256,512") == [256, 512, 1024]


def test_parse_widths_passes_through_a_list():
    assert pipeline.parse_widths([512, 256]) == [256, 512]


def test_output_name_prefixes_only_when_multi():
    assert pipeline.output_name("photo-a", "thumb-256.jpg", multi=True) == "photo-a-thumb-256.jpg"
    assert pipeline.output_name("photo", "thumb-256.jpg", multi=False) == "thumb-256.jpg"


def test_page_tag_pads_to_at_least_three_digits():
    assert pipeline.page_tag(0, 5) == "page-000"
    assert pipeline.page_tag(4, 5) == "page-004"


def test_page_tag_widens_padding_for_large_counts():
    assert pipeline.page_tag(0, 1500) == "page-0000"
    assert pipeline.page_tag(1499, 1500) == "page-1499"


# --- build_ocr_products() ---------------------------------------------------

ORIENTATION = {
    "deskew": False,
    "angle": 0.4,
    "stats": {
        "original": {"count": 10, "avg": 90.0, "median": 90.0, "hocr_path": "/tmp/orig.hocr"},
        "deskewed": {"count": 5, "avg": 60.0, "median": 60.0, "hocr_path": "/tmp/deskew.hocr"},
    },
}


@pytest.fixture
def fake_djvu(monkeypatch):
    calls = {"parse_hocr": [], "build_djvu_xml": [], "compute_ocr_stats": []}

    def fake_parse_hocr(hocr_path):
        calls["parse_hocr"].append(hocr_path)
        return {"careas": [], "_source": hocr_path}

    def fake_build_djvu_xml(page_structure, **kwargs):
        calls["build_djvu_xml"].append({"page_structure": page_structure, **kwargs})
        return "<OBJECT/>"

    def fake_compute_ocr_stats(page_structures, *, selected, opts={}):
        calls["compute_ocr_stats"].append({"page_structures": page_structures, "selected": selected, "opts": opts})
        return {"selected": selected, "original": {}, "deskewed": {}, "config": {}}

    monkeypatch.setattr(pipeline.djvu, "parse_hocr", fake_parse_hocr)
    monkeypatch.setattr(pipeline.djvu, "build_djvu_xml", fake_build_djvu_xml)
    monkeypatch.setattr(pipeline.djvu, "compute_ocr_stats", fake_compute_ocr_stats)
    return calls


def test_build_ocr_products_writes_djvu_and_stats_files(tmp_path, fake_djvu):
    outputs = pipeline.build_ocr_products(
        ORIENTATION, str(tmp_path), "photo", False, {"width": 1024, "height": 1313},
    )

    assert [fn for fn, _ in outputs] == ["ocr.djvu", "ocr-stats.json"]
    djvu_path = dict(outputs)["ocr.djvu"]
    stats_path = dict(outputs)["ocr-stats.json"]
    assert open(djvu_path).read() == "<OBJECT/>"
    assert json.load(open(stats_path))["selected"] == "original"


def test_build_ocr_products_uses_output_name_prefix_when_multi(tmp_path, fake_djvu):
    outputs = pipeline.build_ocr_products(
        ORIENTATION, str(tmp_path), "photo-a", True, {"width": 1024, "height": 1313},
    )
    assert [fn for fn, _ in outputs] == ["photo-a-ocr.djvu", "photo-a-ocr-stats.json"]


def test_build_ocr_products_parses_hocr_for_both_orientation_candidates(tmp_path, fake_djvu):
    pipeline.build_ocr_products(ORIENTATION, str(tmp_path), "photo", False, {"width": 1024, "height": 1313})
    assert set(fake_djvu["parse_hocr"]) == {"/tmp/orig.hocr", "/tmp/deskew.hocr"}


def test_build_ocr_products_computes_scale_factor_from_ocr_prep_size_and_large_width(tmp_path, fake_djvu):
    pipeline.build_ocr_products(
        ORIENTATION, str(tmp_path), "photo", False, {"width": 1024, "height": 1313},
        opts={"ocr_prep_size": 2048},
    )
    assert fake_djvu["build_djvu_xml"][0]["scale_factor"] == pytest.approx(2.0)


def test_build_ocr_products_builds_djvu_from_the_selected_orientation(tmp_path, fake_djvu):
    deskewed_orientation = {**ORIENTATION, "deskew": True}
    pipeline.build_ocr_products(deskewed_orientation, str(tmp_path), "photo", False, {"width": 1024, "height": 1313})
    assert fake_djvu["build_djvu_xml"][0]["page_structure"]["_source"] == "/tmp/deskew.hocr"


def test_build_ocr_products_forwards_measured_angle_to_stats(tmp_path, fake_djvu):
    pipeline.build_ocr_products(ORIENTATION, str(tmp_path), "photo", False, {"width": 1024, "height": 1313})
    assert fake_djvu["compute_ocr_stats"][0]["opts"]["angle"] == 0.4


def test_cask_public_ref_converts_dispatch_prefix():
    internal = "cask:/silver/g/c/latest/item/derivatives/thumb-256.jpg"
    assert pipeline.cask_public_ref(internal) == "cask://silver/g/c/latest/item/derivatives/thumb-256.jpg"


def test_size_as_strings_stringifies_width_and_height():
    assert pipeline.size_as_strings({"width": 256, "height": 326}) == {"width": "256", "height": "326"}


def test_derivative_dir_matches_what_publish_would_use():
    assert pipeline.derivative_dir(ITEM, "public-frontend-derivatives/images/photo.tiff/images") == (
        "cask:/silver/g/c/latest/item/public-frontend-derivatives/images/photo.tiff/images"
    )


def test_derivative_dir_honors_item_path_for_multi_item():
    assert pipeline.derivative_dir(MULTI_ITEM, "ocr") == "cask:/silver/g/c/latest/box-1/folder-3/ocr"


def test_generate_thumbnail_set_names_by_semantic_size(tmp_path, monkeypatch):
    calls = []

    def fake_thumbnail(src, dest, width, opts={}):
        calls.append(width)
        with open(dest, "wb") as f:
            f.write(b"fake")

    monkeypatch.setattr(pipeline.image_magick, "thumbnail", fake_thumbnail)
    outputs = pipeline.generate_thumbnail_set("src.jpg", str(tmp_path), [256, 512, 1024], "photo", {})

    assert [filename for filename, _ in outputs] == ["small.jpg", "medium.jpg", "large.jpg"]
    assert calls == [256, 512, 1024]
    for filename, local_path in outputs:
        assert os.path.basename(local_path) != filename  # local scratch name stays stem-disambiguated
        assert os.path.exists(local_path)


def test_generate_thumbnail_set_requires_exactly_three_widths():
    with pytest.raises(ValueError, match="requires exactly 3 widths"):
        pipeline.generate_thumbnail_set("src.jpg", "/tmp", [256, 512], "photo", {})


@pytest.fixture
def fake_fs(monkeypatch):
    read_stream_calls = []

    @contextmanager
    def fake_read_stream(path, opts={}):
        read_stream_calls.append(path)
        yield iter([b"fake-source-bytes"])

    monkeypatch.setattr(pipeline.fs, "read_stream", fake_read_stream)
    return {"read_stream_calls": read_stream_calls}


def test_stage_sources_streams_every_binary_and_derives_stem(fake_fs, tmp_path):
    sources = pipeline.stage_sources(MULTI_ITEM, str(tmp_path))
    assert [s["stem"] for s in sources] == ["photo-a", "photo-b"]
    assert sorted(fake_fs["read_stream_calls"]) == sorted(MULTI_ITEM.bronze_binary_paths)
    for s in sources:
        with open(s["local_path"], "rb") as f:
            assert f.read() == b"fake-source-bytes"


def test_stage_sources_honors_explicit_bronze_binary_paths_override(fake_fs, tmp_path):
    mixed_item = ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=MIXED_FORMAT_PATHS,
    )
    pdf_only = pipeline.filter_binaries_by_format(MIXED_FORMAT_PATHS, "pdf")

    sources = pipeline.stage_sources(mixed_item, str(tmp_path), opts={"bronze_binary_paths": pdf_only})

    assert [s["stem"] for s in sources] == ["document"]
    assert fake_fs["read_stream_calls"] == ["cask:/bronze/g/c/latest/item/document.pdf"]


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


def test_publish_omits_link_relation_when_not_given(fake_derivative, tmp_path):
    local_path = tmp_path / "thumb-256.jpg"
    local_path.write_bytes(b"data")

    pipeline.publish(
        ITEM, "derivatives",
        [("thumb-256.jpg", str(local_path), "cask:/bronze/g/c/latest/item/photo.tiff")],
        command=["digtk", "image", "thumbnail", ARK], params={},
    )
    assert "link_relation" not in fake_derivative[0]["opts"]


def test_publish_forwards_explicit_link_relation(fake_derivative, tmp_path):
    local_path = tmp_path / "page-000" / "thumb-256.jpg"
    local_path.parent.mkdir()
    local_path.write_bytes(b"data")

    pipeline.publish(
        ITEM, "derivatives/page-000",
        [("thumb-256.jpg", str(local_path), "cask:/bronze/g/c/latest/item/photo.tiff")],
        command=["digtk", "pdf", "derivatives", ARK], params={},
        opts={"link_relation": "https://lib.ucdavis.edu/argonath/ns#derivativeOf"},
    )
    assert fake_derivative[0]["opts"]["link_relation"] == "https://lib.ucdavis.edu/argonath/ns#derivativeOf"
    assert fake_derivative[0]["rel_path"] == "item/derivatives/page-000/thumb-256.jpg"


def test_publish_records_manifest_and_source_files(fake_derivative, tmp_path):
    local_path = tmp_path / "thumb-256.jpg"
    local_path.write_bytes(b"data")

    result = pipeline.publish(
        ITEM, "derivatives",
        [("thumb-256.jpg", str(local_path), "cask:/bronze/g/c/latest/item/photo.tiff")],
        command=["digtk", "image", "thumbnail", ARK], params={"widths": [256]},
    )
    assert result["outputs"] == ["thumb-256.jpg"]
    assert result["dir"] == "cask:/silver/g/c/latest/item/derivatives"
    manifest_call = next(c for c in fake_derivative if c["rel_path"].endswith("manifest.json"))
    manifest = json.loads(manifest_call["data"])
    assert manifest["command"] == ["digtk", "image", "thumbnail", ARK]
    assert manifest["params"] == {"widths": [256]}


def test_update_associated_media_creates_new_sidecar(fake_derivative, monkeypatch):
    monkeypatch.setattr(pipeline.fs, "exists", lambda path: False)
    media = [{"@type": "ImageObject", "contentUrl": "cask://silver/g/c/latest/item/derivatives/thumb-256.jpg"}]

    doc = pipeline.update_associated_media(ITEM, media, ["cask:/bronze/g/c/latest/item/photo.tiff"], "image")
    assert doc["@id"] == ARK
    assert doc["associatedMedia"] == media
    assert doc["associatedMedia"][0]["argonath:sourceGroup"] == "image"


def test_update_associated_media_preserves_other_source_groups(fake_derivative, monkeypatch):
    existing_doc = {
        "@id": ARK, "ark": ARK,
        "associatedMedia": [
            {"@type": "ImageObject", "contentUrl": "cask://.../thumb-256.jpg", "argonath:sourceGroup": "image"},
        ],
    }
    monkeypatch.setattr(pipeline.fs, "exists", lambda path: True)
    monkeypatch.setattr(pipeline.fs, "read", lambda path: json.dumps(existing_doc).encode())

    pdf_media = [{"@type": "ImageObject", "contentUrl": "cask://.../page-000/thumb-256.jpg"}]
    doc = pipeline.update_associated_media(ITEM, pdf_media, ["cask:/bronze/g/c/latest/item/document.pdf"], "pdf")

    groups = {e["argonath:sourceGroup"] for e in doc["associatedMedia"]}
    assert groups == {"image", "pdf"}
    assert len(doc["associatedMedia"]) == 2


def test_update_associated_media_replaces_only_matching_source_group(fake_derivative, monkeypatch):
    existing_doc = {
        "@id": ARK, "ark": ARK,
        "associatedMedia": [
            {"@type": "ImageObject", "contentUrl": "cask://.../old-thumb-256.jpg", "argonath:sourceGroup": "image"},
            {"@type": "ImageObject", "contentUrl": "cask://.../page-000/thumb-256.jpg", "argonath:sourceGroup": "pdf"},
        ],
    }
    monkeypatch.setattr(pipeline.fs, "exists", lambda path: True)
    monkeypatch.setattr(pipeline.fs, "read", lambda path: json.dumps(existing_doc).encode())

    new_image_media = [{"@type": "ImageObject", "contentUrl": "cask://.../new-thumb-256.jpg"}]
    doc = pipeline.update_associated_media(ITEM, new_image_media, ["cask:/bronze/g/c/latest/item/photo.tiff"], "image")

    by_group = {e["argonath:sourceGroup"]: e for e in doc["associatedMedia"]}
    assert by_group["image"]["contentUrl"] == "cask://.../new-thumb-256.jpg"
    assert by_group["pdf"]["contentUrl"] == "cask://.../page-000/thumb-256.jpg"
