import json
from contextlib import contextmanager

import pytest

from digtk.lib.items import ItemLocation
from digtk.models import digital_website as dw

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


@pytest.fixture(autouse=True)
def fake_items(monkeypatch):
    monkeypatch.setattr(dw.items, "resolve", lambda ark, opts={}: ITEM)


def use_multi_item(monkeypatch):
    monkeypatch.setattr(dw.items, "resolve", lambda ark, opts={}: MULTI_ITEM)


@pytest.fixture
def fake_fs(monkeypatch):
    read_stream_calls = []

    @contextmanager
    def fake_read_stream(path, opts={}):
        read_stream_calls.append(path)
        yield iter([b"fake-source-bytes"])

    monkeypatch.setattr(dw.fs, "read_stream", fake_read_stream)
    monkeypatch.setattr(dw.fs, "exists", lambda path: False)
    monkeypatch.setattr(dw.fs, "read", lambda path: b"{}")
    return {"read_stream_calls": read_stream_calls}


@pytest.fixture
def fake_derivative(monkeypatch):
    calls = []

    def fake_write(**kwargs):
        calls.append(kwargs)
        dest = dw.derivative.dest_path(
            type=kwargs["type"], collection=kwargs["collection"],
            rel_path=kwargs["rel_path"], opts=kwargs.get("opts", {}),
        )
        return {"path": f"cask:{dest}", "links": []}

    monkeypatch.setattr(dw.derivative, "write", fake_write)
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

    def probe_dimensions(path, opts={}):
        return {"width": 100, "height": 200}

    monkeypatch.setattr(dw.image_magick, "thumbnail", record_thumbnail())
    monkeypatch.setattr(dw.image_magick, "convert_format", record("convert_format"))
    monkeypatch.setattr(dw.image_magick, "deskew", record("deskew"))
    monkeypatch.setattr(dw.image_magick, "prep_ocr", record("prep_ocr"))
    monkeypatch.setattr(dw.image_magick, "probe_dimensions", probe_dimensions)
    return calls


def fake_compare_orientations(selected):
    def fn(candidates, opts={}):
        return {
            "selected": selected,
            "stats": {"original": {"count": 10}, "deskewed": {"count": 20}},
        }
    return fn


def derivative_calls_for(calls, rel_path_suffix):
    return [c for c in calls if c["rel_path"].endswith(rel_path_suffix)]


# --- standalone verbs (single-image item) ----------------------------------

def test_thumbnails_splits_thumb_and_web_names(fake_fs, fake_derivative, fake_image_magick):
    result = dw.thumbnails(ARK, opts={"widths": "256,512,1024"})
    assert result["status"] == "success"
    assert sorted(result["outputs"]) == ["thumb-256.jpg", "thumb-512.jpg", "web-1024.jpg"]
    resized_widths = {c["width"] for c in fake_image_magick["thumbnail"]}
    assert resized_widths == {256, 512, 1024}
    assert result["dir"] == "cask:/silver/g/c/latest/item/derivatives"

    write_call = fake_derivative[0]
    assert write_call["type"] == "silver"
    assert write_call["collection"] == "c"
    assert write_call["source_files"] == ["cask:/bronze/g/c/latest/item/photo.tiff"]
    assert write_call["opts"]["group"] == "g"
    assert write_call["opts"]["time"] == "latest"
    assert write_call["opts"]["replace"] is True
    assert write_call["opts"]["link_relation"] == "https://lib.ucdavis.edu/argonath/ns#derivativeOf"


def test_thumbnails_stages_source_via_fs_read_stream(fake_fs, fake_derivative, fake_image_magick):
    dw.thumbnails(ARK, opts={"widths": "256"})
    assert fake_fs["read_stream_calls"] == ["cask:/bronze/g/c/latest/item/photo.tiff"]


def test_convert_maps_format_to_extension(fake_fs, fake_derivative, fake_image_magick):
    result = dw.convert(ARK, opts={"format": "png"})
    assert result["outputs"] == ["converted.png"]


def test_convert_defaults_to_configured_format(fake_fs, fake_derivative, fake_image_magick):
    result = dw.convert(ARK)
    assert result["outputs"] == ["converted.jpg"]  # default convert_format is "jpeg"


def test_deskew_standalone_writes_single_output(fake_fs, fake_derivative, fake_image_magick):
    result = dw.deskew(ARK, opts={"threshold": 25})
    assert result["outputs"] == ["deskewed.jpg"]
    assert fake_image_magick["deskew"][0]["opts"]["threshold"] == 25


def test_prep_ocr_standalone_names_by_deskew_flag(fake_fs, fake_derivative, fake_image_magick):
    non_deskew = dw.prep_ocr(ARK)
    assert non_deskew["outputs"] == ["prep.jpg"]
    deskewed = dw.prep_ocr(ARK, opts={"deskew": True})
    assert deskewed["outputs"] == ["prep-deskew.jpg"]
    assert deskewed["dir"] == "cask:/silver/g/c/latest/item/ocr"


# --- multi-image item: standalone verbs -------------------------------------

def test_thumbnails_prefixes_filenames_for_multi_image_item(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    use_multi_item(monkeypatch)
    result = dw.thumbnails(ARK, opts={"widths": "256"})
    assert sorted(result["outputs"]) == ["photo-a-web-256.jpg", "photo-b-web-256.jpg"]


def test_thumbnails_stages_every_binary_for_multi_image_item(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    use_multi_item(monkeypatch)
    dw.thumbnails(ARK, opts={"widths": "256"})
    assert sorted(fake_fs["read_stream_calls"]) == sorted(MULTI_ITEM.bronze_binary_paths)


def test_publish_records_correct_source_file_per_output(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    use_multi_item(monkeypatch)
    dw.thumbnails(ARK, opts={"widths": "256"})
    by_rel_path = {c["rel_path"]: c["source_files"] for c in fake_derivative}
    assert by_rel_path["box-1/folder-3/derivatives/photo-a-web-256.jpg"] == ["cask:/bronze/g/c/latest/box-1/folder-3/photo-a.tiff"]
    assert by_rel_path["box-1/folder-3/derivatives/photo-b-web-256.jpg"] == ["cask:/bronze/g/c/latest/box-1/folder-3/photo-b.tiff"]


# --- orientation selection ------------------------------------------------

def test_select_orientation_generates_both_candidates(tmp_path, fake_image_magick, monkeypatch):
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("deskewed"))
    result = dw._select_orientation("/tmp/src.jpg", str(tmp_path), opts={})
    assert result["deskew"] is True
    assert result["stats"]["deskewed"]["count"] == 20

    prep_calls = fake_image_magick["prep_ocr"]
    assert len(prep_calls) == 2
    assert {c["opts"]["deskew"] for c in prep_calls} == {False, True}


def test_select_orientation_original_when_comparison_picks_original(tmp_path, fake_image_magick, monkeypatch):
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    result = dw._select_orientation("/tmp/src.jpg", str(tmp_path), opts={})
    assert result["deskew"] is False


# --- generate() umbrella: single-image item ---------------------------------

def test_generate_stages_source_exactly_once(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    dw.generate(ARK, opts={"widths": "256,512,1024"})
    assert len(fake_fs["read_stream_calls"]) == 1


def test_generate_calls_compare_orientations_exactly_once(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    calls = []

    def spy(candidates, opts={}):
        calls.append(candidates)
        return {"selected": "original", "stats": {"original": {}, "deskewed": {}}}

    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", spy)
    dw.generate(ARK, opts={"widths": "256,512"})
    assert len(calls) == 1


def test_generate_applies_deskew_decision_to_every_width(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("deskewed"))
    dw.generate(ARK, opts={"widths": "256,512,1024"})

    assert len(fake_image_magick["thumbnail"]) == 0
    deskewed_widths = {c["opts"]["output_width"] for c in fake_image_magick["deskew"]}
    assert deskewed_widths == {256, 512, 1024}
    assert len(fake_image_magick["prep_ocr"]) == 3  # 2 orientation candidates + 1 final chosen prep image


def test_generate_uses_resize_when_original_orientation_wins(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    dw.generate(ARK, opts={"widths": "256,512"})
    assert len(fake_image_magick["deskew"]) == 0
    assert {c["width"] for c in fake_image_magick["thumbnail"]} == {256, 512}


def test_generate_publishes_derivatives_and_ocr_as_separate_manifests(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("deskewed"))
    result = dw.generate(ARK, opts={"widths": "256,512"})

    assert result["derivatives"]["dir"] == "cask:/silver/g/c/latest/item/derivatives"
    assert sorted(result["derivatives"]["outputs"]) == ["thumb-256.jpg", "web-512.jpg"]
    assert result["ocr"]["dir"] == "cask:/silver/g/c/latest/item/ocr"
    assert result["ocr"]["outputs"] == ["prep-deskew.jpg"]
    assert result["orientation"]["photo"]["deskew"] is True


def test_generate_records_orientation_decision_in_derivatives_manifest_params(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("deskewed"))
    dw.generate(ARK, opts={"widths": "256"})
    derivatives_manifest_call = next(c for c in fake_derivative if c["rel_path"].endswith("derivatives/manifest.json"))
    manifest = json.loads(derivatives_manifest_call["data"])
    assert manifest["params"]["orientation"]["photo"]["deskew_selected"] is True
    assert manifest["params"]["orientation"]["photo"]["stats"]["deskewed"]["count"] == 20


def test_generate_fails_before_any_publish_when_a_thumbnail_fails(fake_fs, fake_derivative, monkeypatch):
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    monkeypatch.setattr(dw.image_magick, "prep_ocr", lambda src, dest, opts={}: {"dest_path": dest})

    def failing_thumbnail(src, dest, width, opts={}):
        raise RuntimeError("convert exploded")

    monkeypatch.setattr(dw.image_magick, "thumbnail", failing_thumbnail)

    with pytest.raises(RuntimeError, match="convert exploded"):
        dw.generate(ARK, opts={"widths": "256,512"})

    assert fake_derivative == []


def test_generate_cleans_up_temp_directory(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    captured_dirs = []
    original_stage = dw._stage_sources

    def spy_stage(item, tmp_dir):
        captured_dirs.append(tmp_dir)
        return original_stage(item, tmp_dir)

    monkeypatch.setattr(dw, "_stage_sources", spy_stage)
    dw.generate(ARK, opts={"widths": "256"})

    import os
    assert not os.path.exists(captured_dirs[0])


def test_generate_writes_associated_media_sidecar_with_dimensions(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    dw.generate(ARK, opts={"widths": "256,512"})

    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    doc = json.loads(sidecar_write["data"])
    assert doc["@id"] == ARK
    assert doc["ark"] == ARK
    assert len(doc["associatedMedia"]) == 2
    for entry in doc["associatedMedia"]:
        assert entry["@type"] == "ImageObject"
        assert entry["width"] == 100 and entry["height"] == 200
        assert entry["contentUrl"].startswith("cask://silver/g/c/latest/item/derivatives/")


def test_generate_merges_into_existing_sidecar(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("original"))
    monkeypatch.setattr(dw.fs, "exists", lambda path: True)
    monkeypatch.setattr(dw.fs, "read", lambda path: b'{"@id": "' + ARK.encode() + b'", "ark": "' + ARK.encode() + b'", "name": "Existing Title"}')

    dw.generate(ARK, opts={"widths": "256"})

    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    doc = json.loads(sidecar_write["data"])
    assert doc["name"] == "Existing Title"
    assert len(doc["associatedMedia"]) == 1


# --- generate() umbrella: multi-image item ----------------------------------

def test_generate_decides_orientation_independently_per_image(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    use_multi_item(monkeypatch)
    calls = []

    def selective_compare(candidates, opts={}):
        # pick "deskewed" for whichever candidate set is currently being compared,
        # alternating by call order to prove each image gets its own decision
        calls.append(candidates)
        selected = "deskewed" if len(calls) == 1 else "original"
        return {"selected": selected, "stats": {"original": {"count": 1}, "deskewed": {"count": 2}}}

    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", selective_compare)
    result = dw.generate(ARK, opts={"widths": "256"})

    assert len(calls) == 2  # one comparison per source image
    assert result["orientation"]["photo-a"]["deskew"] is True
    assert result["orientation"]["photo-b"]["deskew"] is False


def test_generate_combines_multi_image_outputs_into_shared_directories(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    use_multi_item(monkeypatch)
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("original"))

    result = dw.generate(ARK, opts={"widths": "256"})

    assert result["derivatives"]["dir"] == "cask:/silver/g/c/latest/box-1/folder-3/derivatives"
    assert sorted(result["derivatives"]["outputs"]) == ["photo-a-web-256.jpg", "photo-b-web-256.jpg"]
    assert result["ocr"]["dir"] == "cask:/silver/g/c/latest/box-1/folder-3/ocr"
    assert sorted(result["ocr"]["outputs"]) == ["photo-a-prep.jpg", "photo-b-prep.jpg"]


def test_generate_associated_media_covers_every_image_for_multi_image_item(fake_fs, fake_derivative, fake_image_magick, monkeypatch):
    use_multi_item(monkeypatch)
    monkeypatch.setattr(dw.ocr_utils, "compare_orientations", fake_compare_orientations("original"))

    dw.generate(ARK, opts={"widths": "256"})

    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    assert sorted(sidecar_write["source_files"]) == sorted(MULTI_ITEM.bronze_binary_paths)
    doc = json.loads(sidecar_write["data"])
    assert len(doc["associatedMedia"]) == 2
