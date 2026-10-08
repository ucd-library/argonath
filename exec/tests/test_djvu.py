import pytest

from digtk.lib import djvu

HOCR_TEMPLATE = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div class='ocr_page' id='page_1' title='image "p.png"; bbox 0 0 800 400; ppageno 0'>
<div class='ocr_carea' id='block_1_1' title="bbox 50 60 450 110">
<p class='ocr_par' id='par_1_1' lang='eng' title="bbox 50 60 450 110">
<span class='ocr_line' id='line_1_1' title="bbox 50 60 450 110; baseline 0 -8">
{words}
</span>
</p>
</div>
</div>
</body></html>"""


def word_span(word_id, bbox, confidence, text):
    x0, y0, x1, y1 = bbox
    return (
        f"<span class='ocrx_word' id='{word_id}' "
        f"title='bbox {x0} {y0} {x1} {y1}; x_wconf {confidence}'>{text}</span>"
    )


def write_hocr(tmp_path, words_xml, name="page.hocr"):
    path = tmp_path / name
    path.write_text(HOCR_TEMPLATE.format(words=words_xml), encoding="utf-8")
    return str(path)


def write_hocr_with_line_class(tmp_path, line_class, words_xml, name="page.hocr"):
    """Same shape as HOCR_TEMPLATE, but with a caller-chosen line-level span
    class -- for testing hOCR line classes other than the generic ocr_line
    (ocr_caption, ocr_header, etc.)."""
    html = f"""<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div class='ocr_page' id='page_1' title='image "p.png"; bbox 0 0 800 400; ppageno 0'>
<div class='ocr_carea' id='block_1_1' title="bbox 50 60 450 110">
<p class='ocr_par' id='par_1_1' lang='eng' title="bbox 50 60 450 110">
<span class='{line_class}' id='line_1_1' title="bbox 50 60 450 110; baseline 0 -8">
{words_xml}
</span>
</p>
</div>
</div>
</body></html>"""
    path = tmp_path / name
    path.write_text(html, encoding="utf-8")
    return str(path)


def test_parse_hocr_extracts_page_bbox_and_nested_words(tmp_path):
    words_xml = (
        word_span("word_1_1", [50, 60, 150, 100], 96, "Hello")
        + word_span("word_1_2", [160, 60, 250, 100], 88, "World")
    )
    hocr_path = write_hocr(tmp_path, words_xml)

    structure = djvu.parse_hocr(hocr_path)

    assert structure["page_bbox"] == [0, 0, 800, 400]
    assert len(structure["careas"]) == 1
    carea = structure["careas"][0]
    assert carea["bbox"] == [50, 60, 450, 110]
    assert len(carea["paragraphs"]) == 1
    line = carea["paragraphs"][0]["lines"][0]
    assert line["bbox"] == [50, 60, 450, 110]
    assert line["words"] == [
        {"bbox": [50, 60, 150, 100], "confidence": 96.0, "text": "Hello"},
        {"bbox": [160, 60, 250, 100], "confidence": 88.0, "text": "World"},
    ]


@pytest.mark.parametrize("line_class", ["ocr_line", "ocr_caption", "ocr_header", "ocr_footer", "ocr_textfloat"])
def test_parse_hocr_recognizes_all_hocr_line_classes(tmp_path, line_class):
    # Confirmed via a real derivatives run (2026-08-28): Tesseract emits
    # ocr_caption (not ocr_line) for large/stylized text near a photo or
    # graphic (a yearbook title-page caption) -- a version of this function
    # that only recognized "ocr_line" silently dropped legible,
    # high-confidence text entirely for that page.
    words_xml = word_span("word_1_1", [50, 60, 150, 100], 96, "Hello")
    hocr_path = write_hocr_with_line_class(tmp_path, line_class, words_xml)

    structure = djvu.parse_hocr(hocr_path)

    line = structure["careas"][0]["paragraphs"][0]["lines"][0]
    assert line["words"] == [{"bbox": [50, 60, 150, 100], "confidence": 96.0, "text": "Hello"}]


def test_parse_hocr_handles_no_words(tmp_path):
    hocr_path = write_hocr(tmp_path, "")
    structure = djvu.parse_hocr(hocr_path)
    assert structure["careas"][0]["paragraphs"][0]["lines"][0]["words"] == []


def test_build_djvu_xml_has_expected_tag_structure():
    structure = {
        "careas": [{
            "paragraphs": [{
                "lines": [{
                    "words": [{"bbox": [100, 200, 300, 240], "confidence": 96.0, "text": "Hello"}],
                }],
            }],
        }],
    }
    xml_out = djvu.build_djvu_xml(structure, scale_factor=1.0, dpi=300, confidence_threshold=50)

    assert xml_out.startswith("<OBJECT>")
    assert '<PARAM name="DPI" value="300"' in xml_out
    assert "<HIDDENTEXT>" in xml_out
    assert "<PAGECOLUMN>" in xml_out
    assert "<REGION>" in xml_out
    assert "<PARAGRAPH>" in xml_out
    assert "<LINE>" in xml_out
    assert '<WORD coords="100,240,300,200">Hello</WORD>' in xml_out


def test_build_djvu_xml_rescales_word_bboxes_by_scale_factor():
    structure = {
        "careas": [{
            "paragraphs": [{
                "lines": [{
                    "words": [{"bbox": [100, 200, 300, 400], "confidence": 96.0, "text": "Hello"}],
                }],
            }],
        }],
    }
    xml_out = djvu.build_djvu_xml(structure, scale_factor=2.0, confidence_threshold=50)
    assert '<WORD coords="50,200,150,100">Hello</WORD>' in xml_out


def test_build_djvu_xml_writes_coords_in_left_bottom_right_top_order():
    # Confirmed live against a real fin/BookReaderModel.js render
    # (2026-08-28): the consumer destructures WORD's `coords` as
    # [left, bottom, right, top] -- descending y, the larger/lower value in
    # slot 2 and the smaller/upper value in slot 4 (Internet Archive
    # djvu.xml convention). hOCR's own bbox is ascending (y0=top/small,
    # y1=bottom/large); writing that order straight through crossed the
    # consumer's top/bottom, collapsing every highlight box to zero height.
    structure = {
        "careas": [{
            "paragraphs": [{
                "lines": [{
                    "words": [{"bbox": [10, 20, 30, 40], "confidence": 96.0, "text": "Hello"}],
                }],
            }],
        }],
    }
    xml_out = djvu.build_djvu_xml(structure, scale_factor=1.0, confidence_threshold=50)
    assert '<WORD coords="10,40,30,20">Hello</WORD>' in xml_out


def test_build_djvu_xml_drops_words_below_confidence_threshold():
    structure = {
        "careas": [{
            "paragraphs": [{
                "lines": [{
                    "words": [
                        {"bbox": [0, 0, 10, 10], "confidence": 30.0, "text": "noisy"},
                        {"bbox": [20, 0, 30, 10], "confidence": 90.0, "text": "clean"},
                    ],
                }],
            }],
        }],
    }
    xml_out = djvu.build_djvu_xml(structure, scale_factor=1.0, confidence_threshold=50)
    assert "noisy" not in xml_out
    assert "clean" in xml_out


def test_build_djvu_xml_drops_empty_regions_and_paragraphs_after_filtering():
    """A carea/paragraph whose only words all fail the confidence threshold
    should not leave behind an empty REGION/PARAGRAPH in the output."""
    structure = {
        "careas": [{
            "paragraphs": [{
                "lines": [{
                    "words": [{"bbox": [0, 0, 10, 10], "confidence": 10.0, "text": "noise"}],
                }],
            }],
        }],
    }
    xml_out = djvu.build_djvu_xml(structure, scale_factor=1.0, confidence_threshold=50)
    assert "<REGION>" not in xml_out
    assert "<PARAGRAPH>" not in xml_out


def test_build_djvu_xml_defaults_dpi_and_confidence_threshold_from_config():
    from digtk.lib.config import CONFIG
    structure = {
        "careas": [{
            "paragraphs": [{
                "lines": [{
                    "words": [{"bbox": [0, 0, 10, 10], "confidence": CONFIG["ocr"]["orientation_word_confidence_threshold"], "text": "w"}],
                }],
            }],
        }],
    }
    xml_out = djvu.build_djvu_xml(structure, scale_factor=1.0)
    assert f'value="{CONFIG["image"]["dpi"]}"' in xml_out
    assert "<WORD" in xml_out  # survives the default threshold (>=, not >)


def _page_structure(confidences):
    return {
        "careas": [{
            "paragraphs": [{
                "lines": [{
                    "words": [{"bbox": [0, 0, 10, 10], "confidence": c, "text": "w"} for c in confidences],
                }],
            }],
        }],
    }


def test_compute_ocr_stats_reports_both_variants_and_selected():
    stats = djvu.compute_ocr_stats(
        {"original": _page_structure([60, 70]), "deskewed": _page_structure([90, 95, 96])},
        selected="deskewed",
    )
    assert stats["selected"] == "deskewed"
    assert stats["original"]["count"] == 2
    assert stats["deskewed"]["count"] == 3
    assert stats["deskewed"]["avg"] == pytest.approx((90 + 95 + 96) / 3)


def test_compute_ocr_stats_filters_by_confidence_threshold():
    stats = djvu.compute_ocr_stats(
        {"original": _page_structure([10, 20, 90]), "deskewed": _page_structure([])},
        selected="original",
        opts={"confidence_threshold": 50},
    )
    assert stats["original"]["count"] == 1
    assert stats["original"]["avg"] == 90.0


def test_compute_ocr_stats_builds_decile_histogram():
    stats = djvu.compute_ocr_stats(
        {"original": _page_structure([5, 15, 95, 99]), "deskewed": _page_structure([])},
        selected="original",
        opts={"confidence_threshold": 0},
    )
    buckets = stats["original"]["buckets"]
    assert len(buckets) == 10
    assert buckets[0] == 1  # 5 -> [0,10)
    assert buckets[1] == 1  # 15 -> [10,20)
    assert buckets[9] == 2  # 95, 99 -> [90,100]


def test_compute_ocr_stats_handles_no_surviving_words():
    stats = djvu.compute_ocr_stats(
        {"original": _page_structure([]), "deskewed": _page_structure([])},
        selected="original",
    )
    assert stats["original"] == {"count": 0, "avg": 0.0, "median": 0.0, "buckets": [0] * 10}


def test_compute_ocr_stats_includes_threshold_config_and_angle():
    stats = djvu.compute_ocr_stats(
        {"original": _page_structure([]), "deskewed": _page_structure([])},
        selected="original",
        opts={"word_count_threshold": 100, "confidence_threshold": 50, "angle_threshold": 1.1, "angle": -2.4},
    )
    assert stats["config"] == {
        "wordCountThreshold": 100,
        "wordConfidenceThreshold": 50,
        "angleThreshold": 1.1,
        "deskewAngle": -2.4,
    }


def test_build_djvu_xml_writes_page_size_on_object_when_given():
    structure = {"careas": [{"paragraphs": [{"lines": [{
        "words": [{"bbox": [100, 200, 300, 240], "confidence": 96.0, "text": "Hello"}],
    }]}]}]}

    xml_out = djvu.build_djvu_xml(
        structure, scale_factor=1.0, confidence_threshold=50, page_size={"width": 1024, "height": 1325},
    )

    assert xml_out.startswith('<OBJECT width="1024" height="1325">')
