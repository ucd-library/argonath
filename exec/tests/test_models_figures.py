import json

import pymupdf
import pytest

from digtk.lib import ai, derivative, fs
from digtk.lib.items import ItemLocation
from digtk.models import figures

ITEM = ItemLocation(
    ark="ark:/87287/d7abc1234", group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/report.pdf"],
)
PAGE = {"source_path": "cask:/bronze/g/c/latest/item/report.pdf", "filename": "report.pdf",
        "stem": "report", "kind": "pdf", "page_no": 2, "count": 3, "page_tag": "page-001"}
BASE = "cask:/silver/g/c/latest/item/accessibility/report.pdf"


@pytest.fixture
def page_image(tmp_path):
    path = tmp_path / "page.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 1000, 800))
    pix.clear_with(200)
    pix.save(str(path))
    return str(path)


@pytest.fixture
def written(monkeypatch):
    out = []

    def fake_write(*, type, collection, rel_path, data, source_files, opts={}):
        out.append({"rel_path": rel_path, "data": data, "source_files": source_files, "opts": opts})
        return {"path": f"cask:/silver/g/c/latest/{rel_path}"}
    monkeypatch.setattr(derivative, "write", fake_write)
    return out


def _serve(monkeypatch, table):
    def fake_read(path):
        if path in table:
            return table[path]
        raise FileNotFoundError(path)
    monkeypatch.setattr(fs, "read", fake_read)


def _chat(answers):
    seen = []
    answers = iter(answers)

    def fake(message, opts={}):
        seen.append((message, opts))
        return next(answers)
    return fake, seen


# --- parsing ------------------------------------------------------------------

def test_clamp_box_converts_pads_and_drops_slivers():
    assert figures._clamp_box([100, 200, 500, 600]) == [0.092, 0.192, 0.508, 0.608]
    assert figures._clamp_box([500, 600, 100, 200]) == [0.092, 0.192, 0.508, 0.608]
    assert figures._clamp_box([0, 0, 1000, 5]) is None  # a rule
    assert figures._clamp_box(["x", 0, 1, 1]) is None


@pytest.mark.parametrize("raw, count", [
    ('{"figures": [{"bbox": [1, 2, 3, 4]}]}', 1),
    ('[{"bbox_2d": [1, 2, 3, 4]}, {"bbox": [5, 6, 7, 8]}]', 2),
    ('```json\n{"bbox": [1, 2, 3, 4], "kind": "photo"}\n```', 1),
    ('{"figures": []}', 0),
])
def test_parse_figure_list_shapes(raw, count):
    assert len(figures._parse_figure_list(raw)) == count


def test_parse_figure_list_none_on_garbage():
    assert figures._parse_figure_list("I see a chart.") is None


def test_detect_normalizes_kinds_and_decorative(monkeypatch):
    fake, seen = _chat(['{"figures": [{"bbox": [100, 100, 600, 500], "kind": "Chart", "caption": " Fig 1 "},'
                        '{"bbox": [0, 0, 100, 100], "kind": "ornament"},'
                        '{"bbox": [0, 0, 100, 100], "kind": "weird"}]}'])
    monkeypatch.setattr(ai, "chat_completion", fake)
    found = figures.detect("p.jpg")
    assert [f["kind"] for f in found] == ["chart", "ornament", "drawing"]
    assert found[0]["caption"] == "Fig 1" and found[0]["decorative"] is False
    assert found[1]["decorative"] is True
    assert seen[0][1]["temperature"] == 0.0


# --- describe -----------------------------------------------------------------

def test_describe_chart_asks_for_long_description(monkeypatch):
    fake, seen = _chat(['{"alt": "Sales rose.", "long_description": "Bars for 2021-2023."}'])
    monkeypatch.setattr(ai, "chat_completion", fake)
    alt, long_desc, flags = figures.describe("img", "chart", "Fig 1", {"title": "Report"})
    assert (alt, long_desc, flags) == ("Sales rose.", "Bars for 2021-2023.", [])
    assert '"long_description": 2-5 sentences' in seen[0][0]


def test_describe_flags_missing_long_description(monkeypatch):
    fake, _ = _chat(['{"alt": "A map.", "long_description": null}'])
    monkeypatch.setattr(ai, "chat_completion", fake)
    assert figures.describe("img", "map", "", {})[2] == ["NO_LONG_DESCRIPTION"]


def test_describe_photo_gets_no_long_description_ask(monkeypatch):
    fake, seen = _chat(['{"alt": "A barn."}'])
    monkeypatch.setattr(ai, "chat_completion", fake)
    figures.describe("img", "photo", "", {})
    assert '"long_description" to null' in seen[0][0]


def test_describe_shortens_long_alt(monkeypatch):
    fake, _ = _chat(['{"alt": "' + "x" * 200 + '"}', '{"alt": "Short."}'])
    monkeypatch.setattr(ai, "chat_completion", fake)
    assert figures.describe("img", "photo", "", {})[:1] == ("Short.",)


def test_describe_unparseable_is_a_flag(monkeypatch):
    fake, _ = _chat(["no"])
    monkeypatch.setattr(ai, "chat_completion", fake)
    assert figures.describe("img", "photo", "", {}) == ("", None, ["ERROR:unparseable-model-output"])


def test_crop_returns_jpeg_of_the_region(page_image):
    cropped = figures.crop(page_image, [0.1, 0.25, 0.6, 0.75])
    assert cropped.mimetype == "image/jpeg"
    pix = pymupdf.Pixmap(cropped.data)
    assert (pix.width, pix.height) == (500, 400)


def test_crop_limits_size(page_image):
    pix = pymupdf.Pixmap(figures.crop(page_image, [0, 0, 1, 1], {"max_px": 250}).data)
    assert max(pix.width, pix.height) == 250


# --- for_tagger / review CSV --------------------------------------------------

def test_for_tagger_skips_decorative_rejected_and_unspoken():
    figs = [
        {"bbox": [0, 0, 1, 1], "alt": "A chart.", "long_description": "Rises.", "kind": "chart"},
        {"bbox": [0, 0, 1, 1], "alt": "Logo.", "decorative": True},
        {"bbox": [0, 0, 1, 1], "alt": "Bad.", "flag": "NO_LONG_DESCRIPTION;reject"},
        {"bbox": [0, 0, 1, 1], "alt": ""},
    ]
    assert figures.for_tagger(figs) == [
        {"bbox": [0, 0, 1, 1], "alt": "A chart. Rises.", "caption": "", "kind": "chart"}]


def test_review_csv_round_trip_with_edits():
    rows = [{"page": 2, "n": 1, "kind": "chart", "bbox": [0.1, 0.2, 0.3, 0.4], "caption": "Fig 1",
             "alt": "Sales.", "long_description": None, "flags": ["NO_LONG_DESCRIPTION"]}]
    text = figures.review_rows_text(rows)
    edited = "﻿" + text.replace("Sales.", "Sales by year.").replace(",,NO_LONG", ",y,NO_LONG")
    parsed = figures.parse_review(edited)
    assert list(parsed) == [2]
    rec = parsed[2][0]
    assert rec["bbox"] == [0.1, 0.2, 0.3, 0.4]
    assert rec["alt"] == "Sales by year."
    assert rec["flag"] == "NO_LONG_DESCRIPTION"


def test_parse_review_reads_decorative_and_skips_bad_rows():
    text = ("page,n,kind,decorative,bbox,caption,alt,long_description,flag,notes,crop\n"
            "1,1,logo,yes,0 0 0.5 0.5,,Logo,,,,\n"
            "x,1,logo,,0 0 1 1,,,,,,\n"
            "1,2,photo,,0 0 1,,,,,,\n")
    parsed = figures.parse_review(text)
    assert len(parsed[1]) == 1 and parsed[1][0]["decorative"] is True


def test_append_review_adds_header_once(monkeypatch, written):
    _serve(monkeypatch, {})
    figures.append_review(ITEM, "report.pdf", PAGE["source_path"], [
        {"page": 1, "n": 1, "bbox": [0, 0, 1, 1], "alt": "A."}])
    first = written[-1]["data"].decode()
    assert first.startswith("page,n,kind")
    _serve(monkeypatch, {f"{BASE}/figure-review.csv": written[-1]["data"]})
    figures.append_review(ITEM, "report.pdf", PAGE["source_path"], [
        {"page": 2, "n": 1, "bbox": [0, 0, 1, 1], "alt": "B."}])
    second = written[-1]["data"].decode()
    assert second.count("page,n,kind") == 1
    assert written[-1]["rel_path"] == "item/accessibility/report.pdf/figure-review.csv"
    assert figures.append_review(ITEM, "report.pdf", PAGE["source_path"], []) is None


# --- page_figures -------------------------------------------------------------

def test_page_figures_prefers_review_rows(monkeypatch, page_image):
    monkeypatch.setattr(ai, "chat_completion", lambda *a, **k: pytest.fail("no model calls"))
    rows = [{"bbox": [0, 0, 1, 1], "alt": "Reviewed."}]
    assert figures.page_figures(ITEM, PAGE, page_image, {"review": {2: rows}}) == (rows, False)


def test_page_figures_uses_cache(monkeypatch, page_image):
    monkeypatch.setattr(ai, "chat_completion", lambda *a, **k: pytest.fail("no model calls"))
    cached = [{"n": 1, "bbox": [0, 0, 1, 1], "alt": "Cached."}]
    _serve(monkeypatch, {f"{BASE}/2/figures.json": json.dumps(cached).encode()})
    assert figures.page_figures(ITEM, PAGE, page_image) == (cached, False)


def test_page_figures_detects_describes_and_publishes(monkeypatch, page_image, written):
    _serve(monkeypatch, {})
    fake, seen = _chat([
        '{"figures": [{"bbox": [100, 100, 600, 500], "kind": "chart", "caption": "Fig 1"},'
        '{"bbox": [700, 700, 900, 900], "kind": "ornament"}]}',
        '{"alt": "Sales rose.", "long_description": "Three bars."}',
    ])
    monkeypatch.setattr(ai, "chat_completion", fake)

    figs, fresh = figures.page_figures(ITEM, PAGE, page_image, {"title": "Report"})

    assert fresh is True
    assert len(seen) == 2  # detect + one describe (ornament skipped)
    assert "page 2 of 3" in seen[1][0]
    assert figs[0]["alt"] == "Sales rose." and figs[1]["alt"] == ""
    assert figs[0]["crop"] == "report.pdf/2/figure-1.jpg"
    paths = [w["rel_path"] for w in written]
    assert paths == ["item/accessibility/report.pdf/2/figure-1.jpg",
                     "item/accessibility/report.pdf/2/figure-2.jpg",
                     "item/accessibility/report.pdf/2/figures.json"]
    assert json.loads(written[-1]["data"])[0]["long_description"] == "Three bars."


def test_page_figures_reuses_page_alt_for_main_figure(monkeypatch, page_image, written):
    _serve(monkeypatch, {})
    fake, seen = _chat([
        '{"figures": [{"bbox": [0, 0, 900, 900], "kind": "photo"}, {"bbox": [0, 0, 200, 200], "kind": "photo"}]}',
        '{"alt": "Small photo."}',
    ])
    monkeypatch.setattr(ai, "chat_completion", fake)
    figs, _ = figures.page_figures(ITEM, PAGE, page_image, {"page_alt": "Curated alt."})
    assert [f["alt"] for f in figs] == ["Curated alt.", "Small photo."]
    assert len(seen) == 2


def test_page_figures_does_not_cache_unusable_detection(monkeypatch, page_image, written):
    _serve(monkeypatch, {})
    fake, _ = _chat(["sorry"])
    monkeypatch.setattr(ai, "chat_completion", fake)
    assert figures.page_figures(ITEM, PAGE, page_image) == ([], False)
    assert written == []


def test_image_pages_use_the_file_dir():
    image_page = dict(PAGE, kind="image", filename="photo.tif", page_no=None)
    assert figures.page_dir(image_page) == ("accessibility", "photo.tif")
    assert figures.page_dir(PAGE) == ("accessibility", "report.pdf", "2")
