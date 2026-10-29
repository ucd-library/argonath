import shutil

import pikepdf
import pymupdf
import pytest

from digtk.models import pdf_inplace, pdf_tag, pdf_utils

HEADING_LAYOUT = {"blocks": [
    {"type": "heading", "level": 1, "text": "Annual Report"},
    {"type": "text", "text": "Revenue grew in every quarter. Costs fell as the plant opened."},
]}


def _born_digital(path, link=False):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 80), "Annual Report", fontsize=20)
    page.insert_text((72, 120), "Revenue grew in every quarter.", fontsize=11)
    page.insert_text((72, 136), "Costs fell as the plant opened.", fontsize=11)
    page.draw_rect(pymupdf.Rect(72, 150, 300, 160), color=(0, 0, 0))
    if link:
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 170, 200, 185),
                          "uri": "https://example.org"})
    doc.save(str(path))
    return str(path)


def _scan_page(doc, tmp_path):
    img = tmp_path / "scan.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 500))
    pix.clear_with(250)
    pix.save(str(img))
    page = doc.new_page()
    page.insert_image(page.rect, filename=str(img))
    return page


def _doc(pdf):
    return pdf.Root.StructTreeRoot.K


def _kinds(elem):
    return [str(k.S) for k in elem.K]


def test_tags_born_digital_page_and_keeps_its_text(tmp_path):
    src = _born_digital(tmp_path / "born.pdf", link=True)
    before = pymupdf.open(src)[0].get_text()
    dest = tmp_path / "tagged.pdf"

    stats = pdf_inplace.tag_pdf_inplace(src, str(dest), "Report", {"layouts": [HEADING_LAYOUT], "paragraphs": True})

    assert stats["headings"] == 1 and stats["paragraphs"] == 1
    assert stats["annotations"] == 1 and stats["bookmarks"] == 1
    assert stats["unmatched_spans"] == 0
    assert pymupdf.open(str(dest))[0].get_text() == before
    with pikepdf.open(str(dest)) as pdf:
        assert _kinds(_doc(pdf)) == ["/H1", "/P", "/Link"]
        assert pdf.Root.MarkInfo.Marked is True
        assert str(pdf.Root.Lang) == "en-US"


def test_every_marked_sequence_has_a_parent(tmp_path):
    src = _born_digital(tmp_path / "born.pdf")
    dest = tmp_path / "tagged.pdf"
    pdf_inplace.tag_pdf_inplace(src, str(dest), "Report")
    with pikepdf.open(str(dest)) as pdf:
        page = pdf.pages[0]
        ops = pikepdf.parse_content_stream(page)
        mcids = [op[0][1].MCID for op in ops if str(op[1]) == "BDC" and "/MCID" in op[0][1]]
        nums = pdf.Root.StructTreeRoot.ParentTree.Nums
        row = dict(zip([int(n) for n in nums[::2]], nums[1::2]))[int(page.obj.StructParents)]
        assert len(row) == len(mcids) == 3  # one per text operator


def test_retagging_replaces_old_marks(tmp_path):
    src = _born_digital(tmp_path / "born.pdf")
    once, twice = tmp_path / "once.pdf", tmp_path / "twice.pdf"
    pdf_inplace.tag_pdf_inplace(src, str(once), "Report")
    pdf_inplace.tag_pdf_inplace(str(once), str(twice), "Report")
    with pikepdf.open(str(once)) as a, pikepdf.open(str(twice)) as b:
        assert _kinds(_doc(a)) == _kinds(_doc(b))
        ops = [str(op) for _, op in pikepdf.parse_content_stream(b.pages[0])]
        assert ops.count("BDC") + ops.count("BMC") == ops.count("EMC")


def test_text_in_a_form_xobject_is_inlined(tmp_path):
    inner = _born_digital(tmp_path / "inner.pdf")
    outer = pymupdf.open()
    page = outer.new_page()
    page.show_pdf_page(page.rect, pymupdf.open(inner), 0)  # draws it as a form
    src = tmp_path / "outer.pdf"
    outer.save(str(src))
    dest = tmp_path / "tagged.pdf"

    stats = pdf_inplace.tag_pdf_inplace(str(src), str(dest), "Report")

    assert stats["words"] == 13
    assert stats["lines"] == 3
    with pikepdf.open(str(dest)) as pdf:
        ops = [str(op) for _, op in pikepdf.parse_content_stream(pdf.pages[0])]
        assert "Tj" in ops or "TJ" in ops


def test_whole_page_scan_becomes_figure_with_alt(tmp_path):
    doc = pymupdf.open()
    _scan_page(doc, tmp_path)
    src = tmp_path / "scan.pdf"
    doc.save(str(src))
    dest = tmp_path / "tagged.pdf"

    pdf_inplace.tag_pdf_inplace(str(src), str(dest), "Scan", {"alt": "A blank form."})

    with pikepdf.open(str(dest)) as pdf:
        fig = _doc(pdf).K[0]
        assert str(fig.S) == "/Figure"
        assert str(fig.Alt) == "A blank form."


def test_scan_without_alt_stays_an_artifact(tmp_path):
    doc = pymupdf.open()
    _scan_page(doc, tmp_path)
    src = tmp_path / "scan.pdf"
    doc.save(str(src))
    dest = tmp_path / "tagged.pdf"

    pdf_inplace.tag_pdf_inplace(str(src), str(dest), "Scan")

    with pikepdf.open(str(dest)) as pdf:
        assert len(_doc(pdf).K) == 0


def test_text_pages_and_page_texts(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Hello there")
    _scan_page(doc, tmp_path)
    src = tmp_path / "mixed.pdf"
    doc.save(str(src))
    assert pdf_inplace.text_pages(str(src)) == [True, False]
    assert pdf_inplace.page_texts(str(src)) == ["Hello there", ""]
    assert pdf_inplace.page_widths(str(src)) == [595.0, 595.0]


def test_unembedded_fonts_finds_base14(tmp_path):
    src = _born_digital(tmp_path / "born.pdf")
    assert pdf_inplace.unembedded_fonts(src) == ["Helvetica"]


def test_embed_fonts_without_ghostscript_returns_false(tmp_path):
    src = _born_digital(tmp_path / "born.pdf")
    assert pdf_inplace.embed_fonts(src, str(tmp_path / "out.pdf"), {"gs": "no-such-gs"}) is False


def test_ocr_layer_on_a_scan_page_is_tagged(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Cover letter")
    _scan_page(doc, tmp_path)
    src = tmp_path / "mixed.pdf"
    doc.save(str(src))
    searchable = tmp_path / "searchable.pdf"
    words = [{"text": "Scanned", "bbox": [200, 300, 600, 360]}, {"text": "page’s", "bbox": [640, 300, 900, 360]}]

    result = pdf_utils.add_text_layer(str(src), str(searchable), {1: {"words": words, "scale": 595 / 2048}})

    assert result == {"pages": 1, "words_placed": 2}
    assert pymupdf.open(str(searchable))[1].get_text().strip() == "Scanned page’s"
    dest = tmp_path / "tagged.pdf"
    stats = pdf_inplace.tag_pdf_inplace(str(searchable), str(dest), "Mixed")
    assert stats["words"] == 4
    assert stats["lines"] == 2


needs_tools = pytest.mark.skipif(
    shutil.which("gs") is None or shutil.which("verapdf") is None, reason="Ghostscript and veraPDF needed")


@needs_tools
def test_embedded_in_place_output_passes_pdf_ua(tmp_path):
    src = _born_digital(tmp_path / "born.pdf", link=True)
    embedded = tmp_path / "embedded.pdf"
    assert pdf_inplace.embed_fonts(src, str(embedded)) is True
    assert pdf_inplace.unembedded_fonts(str(embedded)) == []
    dest = tmp_path / "tagged.pdf"
    pdf_inplace.tag_pdf_inplace(str(embedded), str(dest), "Report", {"layouts": [HEADING_LAYOUT]})
    assert pdf_tag.validate_ua(str(dest))["compliant"] is True


@needs_tools
def test_retagging_drops_marks_ghostscript_moved_to_properties(tmp_path):
    src = _born_digital(tmp_path / "born.pdf")
    once, embedded, twice = tmp_path / "once.pdf", tmp_path / "embedded.pdf", tmp_path / "twice.pdf"
    pdf_inplace.tag_pdf_inplace(src, str(once), "Report")
    assert pdf_inplace.embed_fonts(str(once), str(embedded)) is True
    with pikepdf.open(str(embedded)) as pdf:  # the marks now point into /Properties
        assert any(str(op) == "BDC" and isinstance(operands[1], pikepdf.Name)
                   for operands, op in pikepdf.parse_content_stream(pdf.pages[0]))

    pdf_inplace.tag_pdf_inplace(str(embedded), str(twice), "Report")

    with pikepdf.open(str(twice)) as pdf:
        depth = deepest = 0
        mcids = []
        for operands, op in pikepdf.parse_content_stream(pdf.pages[0]):
            o = str(op)
            if o in ("BDC", "BMC"):
                if o == "BDC":
                    assert isinstance(operands[1], pikepdf.Dictionary)
                    mcids.append(int(operands[1].MCID))
                depth += 1
                deepest = max(deepest, depth)
            elif o == "EMC":
                depth -= 1
        assert deepest == 1
        assert mcids == list(range(len(mcids)))


@needs_tools
def test_unembedded_in_place_output_fails_pdf_ua(tmp_path):
    src = _born_digital(tmp_path / "born.pdf")
    dest = tmp_path / "tagged.pdf"
    pdf_inplace.tag_pdf_inplace(src, str(dest), "Report")
    assert pdf_tag.validate_ua(str(dest))["compliant"] is False


# --- figures ------------------------------------------------------------------

def _chart_page(path):
    """Born-digital page: heading, a vector chart with a label, a caption."""
    doc = pymupdf.open()
    page = doc.new_page()  # 595 x 842
    page.insert_text((72, 80), "Annual Report", fontsize=20)
    page.draw_rect(pymupdf.Rect(100, 200, 160, 400), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
    page.draw_rect(pymupdf.Rect(200, 260, 260, 400), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
    page.insert_text((110, 420), "2021", fontsize=9)
    page.insert_text((100, 470), "Figure 1 Sales by year", fontsize=10)
    doc.save(str(path))
    return str(path)


# fractions of the 595 x 842 page; chart from (90, 190) to (300, 430) points
CHART = {"bbox": [90 / 595, 190 / 842, 300 / 595, 430 / 842], "alt": "Bars rising from 2021.",
         "caption": "Figure 1 Sales by year", "kind": "chart"}


def test_vector_chart_becomes_figure_with_its_paths(tmp_path):
    src = _chart_page(tmp_path / "chart.pdf")
    dest = tmp_path / "tagged.pdf"

    stats = pdf_inplace.tag_pdf_inplace(src, str(dest), "Report", {"figures": [[CHART]]})

    assert stats["figures"] == 1 and stats["captions"] == 1
    with pikepdf.open(str(dest)) as pdf:
        kinds = _kinds(_doc(pdf))
        assert kinds == ["/P", "/Figure"]
        fig = _doc(pdf).K[1]
        assert str(fig.Alt) == "Bars rising from 2021."
        assert str(fig.K[0].S) == "/Caption"
        # two bar paths + the "2021" label
        assert len([k for k in fig.K if not isinstance(k, pikepdf.Dictionary)]) == 3


def test_figure_in_wrapped_scan_gets_a_clipped_redraw(tmp_path):
    doc = pymupdf.open()
    _scan_page(doc, tmp_path)
    src = tmp_path / "scan.pdf"
    doc.save(str(src))
    dest = tmp_path / "tagged.pdf"
    spec = {"bbox": [0.2, 0.2, 0.6, 0.5], "alt": "A stamp.", "caption": ""}

    stats = pdf_inplace.tag_pdf_inplace(str(src), str(dest), "Scan", {"figures": [[spec]], "alt": "Ignored"})

    assert stats["figures"] == 1
    with pikepdf.open(str(dest)) as pdf:
        assert _kinds(_doc(pdf)) == ["/Figure"]
        assert str(_doc(pdf).K[0].Alt) == "A stamp."
        ops = [str(op) for _, op in pikepdf.parse_content_stream(pdf.pages[0])]
        assert ops.count("Do") == 2  # the scan, then its clipped copy
    scan, copy = [i["bbox"] for i in pymupdf.open(str(dest))[0].get_image_info()]
    assert copy == pytest.approx(scan, abs=0.01)  # same pixels, same place


def _wrap_scaled(src, dest):
    """Rewrite a scan page as Ghostscript does: under q 0.5 cm, with the image's cm doubled."""
    with pikepdf.open(src) as pdf:
        page = pdf.pages[0]
        ops = [([float(v) * 2 for v in operands], op) if str(op) == "cm" else (operands, op)
               for operands, op in pikepdf.parse_content_stream(page)]
        ops = [([], pikepdf.Operator("q")), ([0.5, 0, 0, 0.5, 0, 0], pikepdf.Operator("cm"))] + ops \
            + [([], pikepdf.Operator("Q"))]
        page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(ops))
        pdf.save(dest)


def test_figure_redraw_in_a_nested_scan_stays_in_place(tmp_path):
    doc = pymupdf.open()
    _scan_page(doc, tmp_path)
    plain = tmp_path / "plain.pdf"
    doc.save(str(plain))
    src = tmp_path / "scan.pdf"
    _wrap_scaled(str(plain), str(src))
    dest = tmp_path / "tagged.pdf"
    spec = {"bbox": [0.0, 0.0, 1.0, 1.0], "alt": "A photo.", "caption": ""}

    pdf_inplace.tag_pdf_inplace(str(src), str(dest), "Scan", {"figures": [[spec]]})

    page = pymupdf.open(str(dest))[0]
    scan, copy = [i["bbox"] for i in page.get_image_info()]
    assert copy == pytest.approx(scan, abs=0.01)
    assert page.rect.contains(pymupdf.Rect(copy))


def test_rotate_specs_maps_rendered_boxes_back():
    spec = [{"bbox": [0.1, 0.2, 0.3, 0.4], "alt": "x"}]
    assert pdf_inplace._rotate_specs(spec, 0) is spec
    assert pdf_inplace._rotate_specs(spec, 180)[0]["bbox"] == pytest.approx([0.7, 0.6, 0.9, 0.8])
    assert pdf_inplace._rotate_specs(spec, 90)[0]["bbox"] == pytest.approx([0.2, 0.7, 0.4, 0.9])


@needs_tools
def test_in_place_figure_output_passes_pdf_ua(tmp_path):
    src = _chart_page(tmp_path / "chart.pdf")
    embedded = tmp_path / "embedded.pdf"
    assert pdf_inplace.embed_fonts(src, str(embedded))
    dest = tmp_path / "tagged.pdf"
    pdf_inplace.tag_pdf_inplace(str(embedded), str(dest), "Report", {"figures": [[CHART]]})
    assert pdf_tag.validate_ua(str(dest))["compliant"] is True
