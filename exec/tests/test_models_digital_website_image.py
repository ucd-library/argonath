import json
import os
from contextlib import contextmanager

import pytest

from digtk.lib.items import ItemLocation
from digtk.models.digital_website import pipeline
from digtk.models.digital_website import image as dw

ARK = "ark:/87287/d7abc1234"
WIDTHS = "256,512,1024"

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

# An archival transfer supplying both a compiled PDF and a per-page TIFF for the same
# physical document, both supplied directly by the archiving team (see
# docs/cask-conventions.md) — digital_website/image.py must only ever touch the TIFF.
MIXED_FORMAT_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/document.pdf",
        "cask:/bronze/g/c/latest/item/photo.tiff",
    ],
)


@pytest.fixture(autouse=True)
def fake_items(monkeypatch):
    monkeypatch.setattr(dw.items, "resolve", lambda ark, opts={}: ITEM)


def use_multi_item(monkeypatch):
    monkeypatch.setattr(dw.items, "resolve", lambda ark, opts={}: MULTI_ITEM)


def use_mixed_format_item(monkeypatch):
    monkeypatch.setattr(dw.items, "resolve", lambda ark, opts={}: MIXED_FORMAT_ITEM)


@pytest.fixture
def fake_fs(monkeypatch):
    read_stream_calls = []

    @contextmanager
    def fake_read_stream(path, opts={}):
        read_stream_calls.append(path)
        yield iter([b"fake-source-bytes"])

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
    calls = {"thumbnail": [], "convert_format": [], "deskew": [], "prep_ocr": [], "tile": [], "detect_skew_angle": []}

    def record(kind):
        def fn(src, dest, opts={}):
            calls[kind].append({"src": src, "dest": dest, "opts": opts})
            with open(dest, "wb") as f:
                f.write(b"fake-image-bytes")
            return {"dest_path": dest}
        return fn

    def fake_detect_skew_angle(src, opts={}):
        calls["detect_skew_angle"].append({"src": src, "opts": opts})
        return 0.0

    def record_thumbnail():
        def fn(src, dest, width, opts={}):
            calls["thumbnail"].append({"src": src, "dest": dest, "width": width, "opts": opts})
            with open(dest, "wb") as f:
                f.write(b"fake-image-bytes")
            return {"dest_path": dest}
        return fn

    def probe_dimensions(path, opts={}):
        return {"width": 100, "height": 200}

    def probe_all_frame_dimensions(path, opts={}):
        return [{"width": 3072, "height": 3911}, {"width": 1536, "height": 1955}]

    monkeypatch.setattr(dw.image_magick, "thumbnail", record_thumbnail())
    monkeypatch.setattr(dw.image_magick, "convert_format", record("convert_format"))
    monkeypatch.setattr(dw.image_magick, "deskew", record("deskew"))
    monkeypatch.setattr(dw.image_magick, "prep_ocr", record("prep_ocr"))
    monkeypatch.setattr(dw.image_magick, "tile", record("tile"))
    monkeypatch.setattr(dw.image_magick, "detect_skew_angle", fake_detect_skew_angle)
    monkeypatch.setattr(dw.image_magick, "probe_dimensions", probe_dimensions)
    monkeypatch.setattr(dw.image_magick, "probe_all_frame_dimensions", probe_all_frame_dimensions)
    return calls


@pytest.fixture
def fake_djvu(monkeypatch):
    """Stubs out digtk.lib.djvu (via pipeline.build_ocr_products()'s own
    reference to it) so tests don't need real hOCR files on disk."""
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


def derivative_calls_for(calls, rel_path_suffix):
    return [c for c in calls if c["rel_path"].endswith(rel_path_suffix)]


# --- standalone verbs (single-image item) ----------------------------------

def test_thumbnails_publishes_small_medium_large(fake_fs, fake_derivative, fake_image_magick):
    result = dw.thumbnails(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})
    assert result["status"] == "success"
    image_result = result["images"]["photo.tiff"]
    assert sorted(image_result["outputs"]) == ["large.jpg", "medium.jpg", "small.jpg"]
    resized_widths = {c["width"] for c in fake_image_magick["thumbnail"]}
    assert resized_widths == {256, 512, 1024}
    assert image_result["dir"] == (
        "cask:/gold/digital-dev/c/item/public-frontend-derivatives/images/photo.tiff/images"
    )

    write_call = fake_derivative[0]
    assert write_call["type"] == "gold"
    assert write_call["collection"] == "c"
    assert write_call["source_files"] == ["cask:/bronze/g/c/latest/item/photo.tiff"]
    assert write_call["opts"]["app"] == "digital-dev"
    assert write_call["opts"]["replace"] is True
    assert write_call["opts"]["link_relation"] == "https://lib.ucdavis.edu/argonath/ns#derivativeOf"


def test_thumbnails_stages_source_via_fs_read_stream(fake_fs, fake_derivative, fake_image_magick):
    dw.thumbnails(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})
    assert fake_fs["read_stream_calls"] == ["cask:/bronze/g/c/latest/item/photo.tiff"]


def test_convert_maps_format_to_extension(fake_fs, fake_derivative, fake_image_magick):
    result = dw.convert(ARK, opts={"format": "png", "app": "digital-dev"})
    assert result["outputs"] == ["converted.png"]
    assert result["dir"] == "cask:/gold/digital-dev/c/item/public-frontend-derivatives"


def test_convert_defaults_to_configured_format(fake_fs, fake_derivative, fake_image_magick):
    result = dw.convert(ARK, opts={"app": "digital-dev"})
    assert result["outputs"] == ["converted.jpg"]  # default convert_format is "jpeg"


def test_deskew_standalone_writes_single_output(fake_fs, fake_derivative, fake_image_magick):
    result = dw.deskew(ARK, opts={"threshold": 25, "app": "digital-dev"})
    assert result["outputs"] == ["deskewed.jpg"]
    assert result["dir"] == "cask:/gold/digital-dev/c/item/public-frontend-derivatives"
    assert fake_image_magick["deskew"][0]["opts"]["threshold"] == 25


def test_prep_ocr_standalone_names_by_deskew_flag(fake_fs, fake_derivative, fake_image_magick):
    non_deskew = dw.prep_ocr(ARK)
    assert non_deskew["outputs"] == ["prep.jpg"]
    deskewed = dw.prep_ocr(ARK, opts={"deskew": True})
    assert deskewed["outputs"] == ["prep-deskew.jpg"]
    assert deskewed["dir"] == "cask:/silver/g/c/latest/item/ocr"


# --- multi-image item: standalone verbs -------------------------------------

def test_thumbnails_nests_per_source_for_multi_image_item(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    use_multi_item(monkeypatch)
    result = dw.thumbnails(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})
    assert set(result["images"].keys()) == {"photo-a.tiff", "photo-b.tiff"}
    for filename in ("photo-a.tiff", "photo-b.tiff"):
        assert sorted(result["images"][filename]["outputs"]) == ["large.jpg", "medium.jpg", "small.jpg"]
        assert result["images"][filename]["dir"] == (
            f"cask:/gold/digital-dev/c/box-1/folder-3/public-frontend-derivatives/images/{filename}/images"
        )


def test_thumbnails_stages_every_binary_for_multi_image_item(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    use_multi_item(monkeypatch)
    dw.thumbnails(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})
    assert sorted(fake_fs["read_stream_calls"]) == sorted(MULTI_ITEM.bronze_binary_paths)


def test_publish_records_correct_source_file_per_output(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    use_multi_item(monkeypatch)
    dw.thumbnails(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})
    by_rel_path = {c["rel_path"]: c["source_files"] for c in fake_derivative}
    assert by_rel_path["box-1/folder-3/public-frontend-derivatives/images/photo-a.tiff/images/small.jpg"] == [
        "cask:/bronze/g/c/latest/box-1/folder-3/photo-a.tiff"
    ]
    assert by_rel_path["box-1/folder-3/public-frontend-derivatives/images/photo-b.tiff/images/small.jpg"] == [
        "cask:/bronze/g/c/latest/box-1/folder-3/photo-b.tiff"
    ]


# --- mixed-format item: PDF + TIFF supplied for the same document -----------

def test_thumbnails_ignores_pdf_sibling_and_stages_only_the_tiff(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    use_mixed_format_item(monkeypatch)
    result = dw.thumbnails(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    assert fake_fs["read_stream_calls"] == ["cask:/bronze/g/c/latest/item/photo.tiff"]
    assert set(result["images"].keys()) == {"photo.tiff"}


def test_thumbnails_raises_when_item_has_no_image_binaries(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    pdf_only_item = ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=["cask:/bronze/g/c/latest/item/document.pdf"],
    )
    monkeypatch.setattr(dw.items, "resolve", lambda ark, opts={}: pdf_only_item)

    with pytest.raises(ValueError, match="no image binaries found"):
        dw.thumbnails(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})


# --- orientation selection ------------------------------------------------

def test_select_orientation_generates_both_candidates(tmp_path, fake_image_magick, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("deskewed"))
    result = pipeline.select_orientation("/tmp/src.jpg", str(tmp_path), opts={})
    assert result["deskew"] is True
    assert result["stats"]["deskewed"]["count"] == 20

    prep_calls = fake_image_magick["prep_ocr"]
    assert len(prep_calls) == 2
    assert {c["opts"]["deskew"] for c in prep_calls} == {False, True}


def test_select_orientation_measures_angle_off_the_original_candidate(tmp_path, fake_image_magick, monkeypatch):
    captured_opts = {}

    def fake_compare(candidates, opts={}):
        captured_opts.update(opts)
        return {"selected": "original", "stats": {"original": {"count": 10}, "deskewed": {"count": 5}}}

    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare)
    pipeline.select_orientation("/tmp/src.jpg", str(tmp_path), opts={})

    angle_calls = fake_image_magick["detect_skew_angle"]
    assert len(angle_calls) == 1
    assert angle_calls[0]["src"].endswith("orientation-original-src.jpg.jpg")
    assert captured_opts["angle"] == 0.0


def test_select_orientation_original_when_comparison_picks_original(tmp_path, fake_image_magick, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    result = pipeline.select_orientation("/tmp/src.jpg", str(tmp_path), opts={})
    assert result["deskew"] is False


# --- generate() umbrella: single-image item ---------------------------------

def test_generate_stages_source_exactly_once(fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})
    assert len(fake_fs["read_stream_calls"]) == 1


def test_generate_calls_compare_orientations_exactly_once(fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    calls = []

    def spy(candidates, opts={}):
        calls.append(candidates)
        return {
            "selected": "original",
            "stats": {"original": {"hocr_path": "/tmp/orig.hocr"}, "deskewed": {"hocr_path": "/tmp/deskew.hocr"}},
        }

    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", spy)
    dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})
    assert len(calls) == 1


def test_generate_applies_deskew_decision_to_every_width(fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("deskewed"))
    dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    assert len(fake_image_magick["thumbnail"]) == 0
    deskewed_widths = {c["opts"]["output_width"] for c in fake_image_magick["deskew"]}
    assert deskewed_widths == {256, 512, 1024}
    assert len(fake_image_magick["prep_ocr"]) == 3  # 2 orientation candidates + 1 final chosen prep image


def test_generate_uses_resize_when_original_orientation_wins(fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})
    assert len(fake_image_magick["deskew"]) == 0
    assert {c["width"] for c in fake_image_magick["thumbnail"]} == {256, 512, 1024}


def test_generate_publishes_derivatives_and_ocr_as_separate_manifests(fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("deskewed"))
    result = dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    image_result = result["derivatives"]["photo.tiff"]
    assert image_result["dir"] == (
        "cask:/gold/digital-dev/c/item/public-frontend-derivatives/images/photo.tiff/images"
    )
    assert sorted(image_result["outputs"]) == ["large.jpg", "medium.jpg", "small.jpg", "tiled.tif"]
    assert result["ocr"]["dir"] == "cask:/silver/g/c/latest/item/ocr"
    assert sorted(result["ocr"]["outputs"]) == ["ocr-stats.json", "ocr.djvu", "prep-deskew.jpg"]
    assert result["orientation"]["photo"]["deskew"] is True


def test_generate_records_orientation_decision_in_derivatives_manifest_params(fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("deskewed"))
    dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})
    derivatives_manifest_call = next(
        c for c in fake_derivative
        if c["rel_path"] == "item/public-frontend-derivatives/images/photo.tiff/images/manifest.json"
    )
    manifest = json.loads(derivatives_manifest_call["data"])
    assert manifest["params"]["orientation"]["deskew_selected"] is True
    assert manifest["params"]["orientation"]["stats"]["deskewed"]["count"] == 20


def test_generate_records_rich_manifest_content_for_original_small_medium_large_tiled_ocr(
    fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch
):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})
    derivatives_manifest_call = next(
        c for c in fake_derivative
        if c["rel_path"] == "item/public-frontend-derivatives/images/photo.tiff/images/manifest.json"
    )
    manifest = json.loads(derivatives_manifest_call["data"])

    assert manifest["original"] == {
        "size": {"width": "100", "height": "200"},
        "url": "cask://bronze/g/c/latest/item/photo.tiff",
    }
    for label in ("small", "medium", "large"):
        assert manifest[label] == {
            "size": {"width": "100", "height": "200"},
            "url": f"cask://gold/digital-dev/c/item/public-frontend-derivatives/images/photo.tiff/images/{label}.jpg",
        }
    assert manifest["tiled"] == {
        "size": [{"width": "3072", "height": "3911"}, {"width": "1536", "height": "1955"}],
        "url": "cask://gold/digital-dev/c/item/public-frontend-derivatives/images/photo.tiff/images/tiled.tif",
    }
    assert manifest["ocr"] == {
        "size": {"width": "100", "height": "200"},
        "imageSize": "large",
        "url": "cask://silver/g/c/latest/item/ocr/ocr.djvu",
    }


def test_generate_fails_before_any_publish_when_a_thumbnail_fails(fake_fs, fake_derivative, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    monkeypatch.setattr(dw.image_magick, "prep_ocr", lambda src, dest, opts={}: {"dest_path": dest})
    monkeypatch.setattr(dw.image_magick, "detect_skew_angle", lambda src, opts={}: 0.0)

    def failing_thumbnail(src, dest, width, opts={}):
        raise RuntimeError("convert exploded")

    monkeypatch.setattr(dw.image_magick, "thumbnail", failing_thumbnail)

    with pytest.raises(RuntimeError, match="convert exploded"):
        dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    assert fake_derivative == []


def test_generate_cleans_up_temp_directory(fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    captured_dirs = []
    original_stage = pipeline.stage_sources

    def spy_stage(item, tmp_dir, opts={}):
        captured_dirs.append(tmp_dir)
        return original_stage(item, tmp_dir, opts)

    monkeypatch.setattr(pipeline, "stage_sources", spy_stage)
    dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    assert not os.path.exists(captured_dirs[0])


def test_generate_writes_associated_media_sidecar_with_dimensions(fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    doc = json.loads(sidecar_write["data"])
    assert doc["@id"] == ARK
    assert doc["ark"] == ARK
    assert len(doc["associatedMedia"]) == 3
    for entry in doc["associatedMedia"]:
        assert entry["@type"] == "ImageObject"
        assert entry["width"] == 100 and entry["height"] == 200
        assert entry["contentUrl"].startswith(
            "cask://gold/digital-dev/c/item/public-frontend-derivatives/images/photo.tiff/images/"
        )


def test_generate_merges_into_existing_sidecar(fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    monkeypatch.setattr(pipeline.fs, "exists", lambda path: True)
    monkeypatch.setattr(pipeline.fs, "read", lambda path: b'{"@id": "' + ARK.encode() + b'", "ark": "' + ARK.encode() + b'", "name": "Existing Title"}')

    dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    doc = json.loads(sidecar_write["data"])
    assert doc["name"] == "Existing Title"
    assert len(doc["associatedMedia"]) == 3


# --- generate() umbrella: multi-image item ----------------------------------

def test_generate_decides_orientation_independently_per_image(fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    use_multi_item(monkeypatch)
    calls = []

    def selective_compare(candidates, opts={}):
        # pick "deskewed" for whichever candidate set is currently being compared,
        # alternating by call order to prove each image gets its own decision
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
    result = dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    assert len(calls) == 2  # one comparison per source image
    assert result["orientation"]["photo-a"]["deskew"] is True
    assert result["orientation"]["photo-b"]["deskew"] is False


def test_generate_nests_multi_image_derivatives_by_source_filename(fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    use_multi_item(monkeypatch)
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))

    result = dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    assert set(result["derivatives"].keys()) == {"photo-a.tiff", "photo-b.tiff"}
    for filename in ("photo-a.tiff", "photo-b.tiff"):
        assert result["derivatives"][filename]["dir"] == (
            f"cask:/gold/digital-dev/c/box-1/folder-3/public-frontend-derivatives/images/{filename}/images"
        )
        assert sorted(result["derivatives"][filename]["outputs"]) == ["large.jpg", "medium.jpg", "small.jpg", "tiled.tif"]
    assert result["ocr"]["dir"] == "cask:/silver/g/c/latest/box-1/folder-3/ocr"
    assert sorted(result["ocr"]["outputs"]) == [
        "photo-a-ocr-stats.json", "photo-a-ocr.djvu", "photo-a-prep.jpg",
        "photo-b-ocr-stats.json", "photo-b-ocr.djvu", "photo-b-prep.jpg",
    ]


def test_generate_associated_media_covers_every_image_for_multi_image_item(fake_fs, fake_derivative, fake_image_magick, fake_djvu, monkeypatch):
    use_multi_item(monkeypatch)
    monkeypatch.setattr(pipeline.ocr_utils, "compare_orientations", fake_compare_orientations("original"))

    dw.generate(ARK, opts={"widths": WIDTHS, "app": "digital-dev"})

    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    assert sorted(sidecar_write["source_files"]) == sorted(MULTI_ITEM.bronze_binary_paths)
    doc = json.loads(sidecar_write["data"])
    assert len(doc["associatedMedia"]) == 6  # 2 images x 3 sizes each
