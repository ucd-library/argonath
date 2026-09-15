import json

import pytest

from digtk.lib import ai, derivative, fs, image_magick, items
from digtk.lib.items import ItemLocation
from digtk.models import split

ARK = "ark:/87287/d7abc1234"

ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo-a.tiff"],
)

MULTI_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/photo-a.tiff",
        "cask:/bronze/g/c/latest/item/photo-b.tiff",
    ],
)

VIDEO_ONLY_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/reel.mp4"],
)

GUTTERS_PATH = "cask:/silver/g/c/latest/item/split-spreads/gutters.json"


def _stub_resolve(monkeypatch, item):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: item)


def _stub_fs_read(monkeypatch, table):
    def fake_read(path):
        if path in table:
            return table[path]
        if path.startswith("cask:/bronze/"):
            return b"RAWBYTES"
        raise FileNotFoundError(path)
    monkeypatch.setattr(fs, "read", fake_read)


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
def fake_image_magick(monkeypatch):
    calls = {"thumbnail": [], "probe_dimensions": [], "crop": [], "run": []}

    def fake_thumbnail(src_path, dest_path, width, opts={}):
        calls["thumbnail"].append({"src_path": src_path, "dest_path": dest_path, "width": width})
        with open(dest_path, "wb") as f:
            f.write(b"JPEGBYTES")
        return {"argv": [], "dest_path": dest_path}

    def fake_probe_dimensions(path, opts={}):
        calls["probe_dimensions"].append(path)
        return {"width": 1000, "height": 500}

    def fake_crop(src_path, dest_path, box, opts={}):
        calls["crop"].append({"src_path": src_path, "dest_path": dest_path, "box": box})
        with open(dest_path, "wb") as f:
            f.write(b"CROPPED")
        return {"argv": [], "dest_path": dest_path}

    def fake_run(argv, opts={}):
        calls["run"].append(argv)
        with open(argv[-1], "wb") as f:
            f.write(b"DRAWN")
        return ""

    monkeypatch.setattr(image_magick, "thumbnail", fake_thumbnail)
    monkeypatch.setattr(image_magick, "probe_dimensions", fake_probe_dimensions)
    monkeypatch.setattr(image_magick, "crop", fake_crop)
    monkeypatch.setattr(image_magick, "run", fake_run)
    return calls


@pytest.fixture
def fake_vision(monkeypatch):
    calls = []

    def fake_chat_completion(message, opts={}):
        calls.append(opts)
        return json.dumps({"gutter_x": 512, "is_spread": True, "confidence": "high"})

    monkeypatch.setattr(ai, "chat_completion", fake_chat_completion)
    return calls


# --- detect --------------------------------------------------------------------

def test_detect_raises_when_no_image_binaries(monkeypatch):
    _stub_resolve(monkeypatch, VIDEO_ONLY_ITEM)
    with pytest.raises(ValueError, match="no image binaries"):
        split.detect(ARK)


def test_detect_skips_already_present_stems_and_preserves_edits(
    monkeypatch, fake_derivative, fake_image_magick, fake_vision,
):
    _stub_resolve(monkeypatch, MULTI_ITEM)
    existing = {"photo-a": {"gutter_x": 400, "is_spread": True, "confidence": "low"}}
    _stub_fs_read(monkeypatch, {
        GUTTERS_PATH: json.dumps(existing).encode(),
        # photo-a's review strip lives in CaskFS from an earlier detect() run —
        # this run never touches it, so _build_contact_sheets must fs.read it back.
        "cask:/silver/g/c/latest/item/split-spreads/review/photo-a.jpg": b"OLDREVIEW",
    })

    result = split.detect(ARK)

    assert result == {"status": "success", "ark": ARK, "detected": 1, "total": 2}
    assert len(fake_vision) == 1  # only photo-b was newly detected

    gutters_write = next(w for w in fake_derivative if w["rel_path"] == "item/split-spreads/gutters.json")
    written = json.loads(gutters_write["data"])
    assert written["photo-a"] == {"gutter_x": 400, "is_spread": True, "confidence": "low"}  # untouched
    assert written["photo-b"] == {"gutter_x": 512, "is_spread": True, "confidence": "high"}
    assert set(gutters_write["source_files"]) == set(MULTI_ITEM.bronze_binary_paths)


def test_detect_publishes_review_strip_and_contact_sheet(
    monkeypatch, fake_derivative, fake_image_magick, fake_vision,
):
    _stub_resolve(monkeypatch, ITEM)
    _stub_fs_read(monkeypatch, {})

    split.detect(ARK)

    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert "item/split-spreads/review/photo-a.jpg" in rel_paths
    assert "item/split-spreads/review-sheets/sheet-01.jpg" in rel_paths
    assert "item/split-spreads/gutters.json" in rel_paths


def test_detect_forwards_max_px_to_thumbnail(monkeypatch, fake_derivative, fake_image_magick, fake_vision):
    _stub_resolve(monkeypatch, ITEM)
    _stub_fs_read(monkeypatch, {})

    split.detect(ARK, opts={"max_px": 800})

    assert fake_image_magick["thumbnail"][0]["width"] == 800


def test_detect_draws_border_not_gutter_line_for_non_spreads(monkeypatch, fake_derivative, fake_image_magick):
    def fake_chat_completion(message, opts={}):
        return json.dumps({"gutter_x": 0, "is_spread": False, "confidence": "high"})
    monkeypatch.setattr(ai, "chat_completion", fake_chat_completion)
    _stub_resolve(monkeypatch, ITEM)
    _stub_fs_read(monkeypatch, {})

    split.detect(ARK)

    draw_argv = fake_image_magick["run"][0]
    assert "-stroke" in draw_argv
    assert split._GUTTER_LINE_COLOR not in draw_argv


# --- apply ---------------------------------------------------------------------

def test_apply_raises_when_gutters_json_missing(monkeypatch):
    _stub_resolve(monkeypatch, ITEM)
    _stub_fs_read(monkeypatch, {})
    with pytest.raises(FileNotFoundError):
        split.apply(ARK)


def test_apply_splits_spread_with_correct_pixel_math(monkeypatch, fake_derivative, fake_image_magick):
    _stub_resolve(monkeypatch, ITEM)
    gutters = {"photo-a": {"gutter_x": 250, "is_spread": True, "confidence": "high"}}
    _stub_fs_read(monkeypatch, {GUTTERS_PATH: json.dumps(gutters).encode()})

    result = split.apply(ARK)

    assert result["split"] == 1
    assert result["singles"] == 0
    assert result["skipped"] == []

    # width=1000 (fake probe_dimensions), gutter_x=250 -> x=250
    boxes = {c["dest_path"].split("/")[-1]: c["box"] for c in fake_image_magick["crop"]}
    assert boxes["photo-a_L.tiff"] == (0, 0, 250, 500)
    assert boxes["photo-a_R.tiff"] == (250, 0, 750, 500)

    rel_paths = {w["rel_path"] for w in fake_derivative}
    assert rel_paths == {"item/split-spreads/pages/photo-a_L.tiff", "item/split-spreads/pages/photo-a_R.tiff"}
    for w in fake_derivative:
        assert GUTTERS_PATH in w["source_files"]


def test_apply_publishes_non_spread_unchanged(monkeypatch, fake_derivative, fake_image_magick):
    _stub_resolve(monkeypatch, ITEM)
    gutters = {"photo-a": {"gutter_x": 0, "is_spread": False, "confidence": "high"}}
    _stub_fs_read(monkeypatch, {GUTTERS_PATH: json.dumps(gutters).encode()})

    result = split.apply(ARK)

    assert result["singles"] == 1
    assert result["split"] == 0
    assert fake_image_magick["crop"] == []  # never cropped

    write = next(w for w in fake_derivative if w["rel_path"] == "item/split-spreads/pages/photo-a.tiff")
    assert write["data"] == b"RAWBYTES"


def test_apply_skips_stem_missing_from_gutters_without_aborting(monkeypatch, fake_derivative, fake_image_magick):
    _stub_resolve(monkeypatch, MULTI_ITEM)
    gutters = {"photo-a": {"gutter_x": 500, "is_spread": True, "confidence": "high"}}
    _stub_fs_read(monkeypatch, {GUTTERS_PATH: json.dumps(gutters).encode()})

    result = split.apply(ARK)

    assert result["skipped"] == ["photo-b.tiff"]
    assert result["split"] == 1  # photo-a still processed
