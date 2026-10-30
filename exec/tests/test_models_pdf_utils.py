import pikepdf
import pymupdf
import pytest

from digtk.models import pdf_utils

HOCR_SAMPLE = """
<div class='ocr_page' title='bbox 0 0 800 600'>
 <span class='ocr_line' title='bbox 100 200 500 240'>
  <span class='ocrx_word' title='bbox 100 200 300 240; x_wconf 96'>HELLO</span>
  <span class='ocrx_word' title='bbox 320 200 500 240; x_wconf 91'><strong>W&amp;B</strong></span>
  <span class='ocrx_word' title='bbox 520 200 540 240; x_wconf 10'>  </span>
 </span>
</div>
"""


def _make_page_image(path, width=800, height=600):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height))
    pix.clear_with(255)
    pix.save(str(path))


# --- hocr_words ---------------------------------------------------------------

def test_hocr_words_extracts_text_bbox_and_confidence(tmp_path):
    hocr = tmp_path / "page.hocr"
    hocr.write_text(HOCR_SAMPLE)
    words = pdf_utils.hocr_words(str(hocr))
    assert words == [
        {"text": "HELLO", "bbox": [100, 200, 300, 240], "conf": 96.0},
        {"text": "W&B", "bbox": [320, 200, 500, 240], "conf": 91.0},
    ]


def test_hocr_words_min_conf_drops_low_confidence_words(tmp_path):
    hocr = tmp_path / "page.hocr"
    hocr.write_text(HOCR_SAMPLE)
    words = pdf_utils.hocr_words(str(hocr), min_conf=95.0)
    assert words == [{"text": "HELLO", "bbox": [100, 200, 300, 240], "conf": 96.0}]


def test_hocr_words_min_conf_keeps_words_with_no_parseable_confidence(tmp_path):
    hocr = tmp_path / "page.hocr"
    hocr.write_text("""
    <div class='ocr_page' title='bbox 0 0 800 600'>
     <span class='ocrx_word' title='bbox 100 200 300 240'>NOCONF</span>
    </div>
    """)
    words = pdf_utils.hocr_words(str(hocr), min_conf=95.0)
    assert words == [{"text": "NOCONF", "bbox": [100, 200, 300, 240], "conf": None}]


def test_hocr_words_empty_document(tmp_path):
    hocr = tmp_path / "page.hocr"
    hocr.write_text("<div class='ocr_page' title='bbox 0 0 10 10'></div>")
    assert pdf_utils.hocr_words(str(hocr)) == []


def test_hocr_words_tolerates_bad_bytes(tmp_path):
    hocr = tmp_path / "page.hocr"
    hocr.write_bytes(HOCR_SAMPLE.encode() + b"\xff\xfe")
    assert len(pdf_utils.hocr_words(str(hocr))) == 2


# --- build_searchable_pdf -----------------------------------------------------

def test_build_creates_pdf_with_page_per_image(tmp_path):
    images = []
    for i in range(2):
        img = tmp_path / f"page{i}.png"
        _make_page_image(img)
        images.append(str(img))
    dest = tmp_path / "out.pdf"

    result = pdf_utils.build_searchable_pdf(
        images, [[{"text": "HELLO", "bbox": [100, 200, 300, 240]}], None], str(dest)
    )

    assert result == {"pages": 2, "words_placed": 1}
    doc = pymupdf.open(str(dest))
    assert doc.page_count == 2
    # 800x600 px at default 200 dpi -> 288x216 pt
    assert doc[0].rect.width == pytest.approx(288.0)
    assert doc[0].rect.height == pytest.approx(216.0)


def test_text_is_searchable_and_lands_on_its_box(tmp_path):
    img = tmp_path / "page.png"
    _make_page_image(img)
    dest = tmp_path / "out.pdf"
    pdf_utils.build_searchable_pdf(
        [str(img)], [[{"text": "HELLO", "bbox": [100, 200, 300, 240]}]], str(dest)
    )

    doc = pymupdf.open(str(dest))
    hits = doc[0].search_for("HELLO")
    assert len(hits) == 1
    # bbox [100..300] px at 200 dpi -> [36..108] pt: the hscaled word should
    # start at its box and span (close to) its full width
    hit = hits[0]
    assert hit.x0 == pytest.approx(36.0, abs=2.0)
    assert hit.x1 == pytest.approx(108.0, abs=6.0)


def test_dpi_opt_scales_page(tmp_path):
    img = tmp_path / "page.png"
    _make_page_image(img)
    dest = tmp_path / "out.pdf"
    pdf_utils.build_searchable_pdf([str(img)], [None], str(dest), opts={"dpi": 400})
    doc = pymupdf.open(str(dest))
    assert doc[0].rect.width == pytest.approx(144.0)


def test_title_and_producer_metadata(tmp_path):
    img = tmp_path / "page.png"
    _make_page_image(img)
    dest = tmp_path / "out.pdf"
    pdf_utils.build_searchable_pdf(
        [str(img)], [None], str(dest), opts={"title": "Bodega Report 12"}
    )
    doc = pymupdf.open(str(dest))
    assert doc.metadata["title"] == "Bodega Report 12"
    assert doc.metadata["producer"] == "digtk"


def test_misaligned_inputs_raise(tmp_path):
    with pytest.raises(ValueError, match="must align"):
        pdf_utils.build_searchable_pdf(["a.png"], [], str(tmp_path / "out.pdf"))


def test_creates_parent_dirs(tmp_path):
    img = tmp_path / "page.png"
    _make_page_image(img)
    dest = tmp_path / "sub" / "deeper" / "out.pdf"
    pdf_utils.build_searchable_pdf([str(img)], [None], str(dest))
    assert dest.exists()


def test_hocr_to_pdf_round_trip(tmp_path):
    """hOCR from disk -> word geometry -> searchable PDF, end to end."""
    hocr = tmp_path / "page.hocr"
    hocr.write_text(HOCR_SAMPLE)
    img = tmp_path / "page.png"
    _make_page_image(img)
    dest = tmp_path / "out.pdf"

    words = pdf_utils.hocr_words(str(hocr))
    result = pdf_utils.build_searchable_pdf([str(img)], [words], str(dest))

    assert result["words_placed"] == 2
    text = pymupdf.open(str(dest))[0].get_text()
    assert "HELLO" in text
    assert "W&B" in text


# --- text-layer font ----------------------------------------------------------

def test_glyph_safe_keeps_what_the_font_has():
    assert pdf_utils.glyph_safe("CEO’s “best” café — 東京") == \
        "CEO’s “best” café — 東京"


def test_glyph_safe_folds_missing_characters(monkeypatch):
    class _Font:
        def has_glyph(self, code):
            return code < 0x80
    monkeypatch.setattr(pdf_utils, "text_font", lambda: _Font())
    assert pdf_utils.glyph_safe("’ř☃") == "'r?"


def test_glyph_safe_empty():
    assert pdf_utils.glyph_safe("") == ""


def test_text_layer_keeps_typographic_characters(tmp_path):
    img = tmp_path / "page.png"
    _make_page_image(img)
    dest = tmp_path / "out.pdf"
    words = [{"text": "CEO’s", "bbox": [100, 200, 300, 240]},
             {"text": "“quoted”", "bbox": [320, 200, 500, 240]}]

    pdf_utils.build_searchable_pdf([str(img)], [words], str(dest))

    text = pymupdf.open(str(dest))[0].get_text()
    assert "CEO’s" in text
    assert "“quoted”" in text
    assert "·" not in text


def test_text_layer_font_is_embedded_with_cid_to_gid_map(tmp_path):
    img = tmp_path / "page.png"
    _make_page_image(img)
    dest = tmp_path / "out.pdf"
    pdf_utils.build_searchable_pdf([str(img)], [[{"text": "hi", "bbox": [0, 0, 50, 20]}]], str(dest))

    fonts = pymupdf.open(str(dest))[0].get_fonts(full=True)
    assert [f[4] for f in fonts] == [pdf_utils.TEXT_FONT_NAME]
    assert fonts[0][1] != "n/a"  # embedded font file
    with pikepdf.open(str(dest)) as pdf:
        font = next(iter(pdf.pages[0].Resources.Font.values()))
        desc = font.DescendantFonts[0]
        assert str(desc.Subtype) == "/CIDFontType2"
        assert desc.CIDToGIDMap == pikepdf.Name.Identity


def test_fix_cid_fonts_is_a_no_op_without_cid_fonts(tmp_path):
    img = tmp_path / "page.png"
    _make_page_image(img)
    dest = tmp_path / "out.pdf"
    pdf_utils.build_searchable_pdf([str(img)], [None], str(dest))
    assert pdf_utils._fix_cid_fonts(str(dest)) is False
