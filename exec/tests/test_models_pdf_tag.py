import pymupdf
import pikepdf
import pytest

from digtk.models import pdf_tag, pdf_utils

# Two prose lines (y in image px, 200 dpi build default)
PROSE_WORDS = [
    {"text": "Monthly", "bbox": [100, 40, 260, 70]},
    {"text": "Report", "bbox": [280, 40, 400, 70]},
    {"text": "hello", "bbox": [100, 120, 220, 150]},
    {"text": "world", "bbox": [240, 120, 380, 150]},
]

# A heading line then a 2x2-body table with a header row
TABLE_WORDS = [
    {"text": "Monthly", "bbox": [100, 40, 260, 70]},
    {"text": "Report", "bbox": [280, 40, 400, 70]},
    {"text": "Name", "bbox": [100, 120, 220, 150]},
    {"text": "Age", "bbox": [400, 120, 470, 150]},
    {"text": "Alice", "bbox": [100, 180, 220, 210]},
    {"text": "30", "bbox": [400, 180, 450, 210]},
    {"text": "Bob", "bbox": [100, 240, 190, 270]},
    {"text": "41", "bbox": [400, 240, 450, 270]},
]

TABLE_LAYOUT = {"blocks": [
    {"type": "heading", "text": "Monthly Report"},
    {"type": "table", "header": ["Name", "Age"], "rows": [["Alice", "30"], ["Bob", "41"]]},
]}


def _build(tmp_path, words_pages, name="src.pdf"):
    images = []
    for i in range(len(words_pages)):
        img = tmp_path / f"page{i}.png"
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 800, 600))
        pix.clear_with(255)
        pix.save(str(img))
        images.append(str(img))
    src = tmp_path / name
    pdf_utils.build_searchable_pdf(images, words_pages, str(src))
    return str(src)


def _doc_elem(pdf):
    return pdf.Root.StructTreeRoot.K


def _kinds(elem):
    return [str(child.S) for child in elem.K]


def test_baseline_lines_become_paragraphs(tmp_path):
    src = _build(tmp_path, [PROSE_WORDS])
    dest = tmp_path / "tagged.pdf"
    stats = pdf_tag.tag_scan_pdf(src, str(dest), "Test Doc")
    assert stats["pages"] == 1
    assert stats["words"] == 4
    assert stats["lines"] == 2
    with pikepdf.open(str(dest)) as pdf:
        doc = _doc_elem(pdf)
        assert str(doc.S) == "/Document"
        assert _kinds(doc) == ["/P", "/P"]


def test_text_still_searchable_after_tagging(tmp_path):
    src = _build(tmp_path, [PROSE_WORDS])
    dest = tmp_path / "tagged.pdf"
    pdf_tag.tag_scan_pdf(src, str(dest), "Test Doc")
    page = pymupdf.open(str(dest))[0]
    assert len(page.search_for("hello")) == 1
    assert "Monthly" in page.get_text()


def test_ua_identification_and_metadata(tmp_path):
    src = _build(tmp_path, [PROSE_WORDS])
    dest = tmp_path / "tagged.pdf"
    pdf_tag.tag_scan_pdf(src, str(dest), "Bodega Report 12", opts={"lang": "en-US"})
    with pikepdf.open(str(dest)) as pdf:
        assert pdf.Root.MarkInfo.Marked
        assert str(pdf.Root.Lang) == "en-US"
        assert pdf.Root.ViewerPreferences.DisplayDocTitle
        assert str(pdf.docinfo["/Title"]) == "Bodega Report 12"
        with pdf.open_metadata() as meta:
            assert meta["pdfuaid:part"] == "1"
            assert meta["dc:title"] == "Bodega Report 12"


def test_alt_text_promotes_scan_to_figure(tmp_path):
    src = _build(tmp_path, [PROSE_WORDS])
    dest = tmp_path / "tagged.pdf"
    pdf_tag.tag_scan_pdf(src, str(dest), "Doc", opts={"alt": "A typed page"})
    with pikepdf.open(str(dest)) as pdf:
        first = _doc_elem(pdf).K[0]
        assert str(first.S) == "/Figure"
        assert str(first.Alt) == "A typed page"


def test_no_alt_keeps_scan_as_artifact(tmp_path):
    src = _build(tmp_path, [PROSE_WORDS])
    dest = tmp_path / "tagged.pdf"
    pdf_tag.tag_scan_pdf(src, str(dest), "Doc")
    with pikepdf.open(str(dest)) as pdf:
        assert "/Figure" not in _kinds(_doc_elem(pdf))


def test_per_page_alt_with_blank_verso(tmp_path):
    src = _build(tmp_path, [PROSE_WORDS, PROSE_WORDS])
    dest = tmp_path / "tagged.pdf"
    pdf_tag.tag_scan_pdf(src, str(dest), "Doc", opts={"alt": ["A typed page", ""]})
    with pikepdf.open(str(dest)) as pdf:
        assert _kinds(_doc_elem(pdf)).count("/Figure") == 1


def test_layout_builds_table_with_caption(tmp_path):
    src = _build(tmp_path, [TABLE_WORDS])
    dest = tmp_path / "tagged.pdf"
    stats = pdf_tag.tag_scan_pdf(
        src, str(dest), "Doc", opts={"layouts": [TABLE_LAYOUT]}
    )
    assert stats["tables"] == 1
    assert stats["captions"] == 1
    with pikepdf.open(str(dest)) as pdf:
        doc = _doc_elem(pdf)
        table = next(c for c in doc.K if str(c.S) == "/Table")
        kinds = _kinds(table)
        assert kinds == ["/Caption", "/THead", "/TBody"]
        thead_tr = table.K[1].K[0]
        assert _kinds(thead_tr) == ["/TH", "/TH"]
        assert str(thead_tr.K[0].A.Scope) == "/Column"
        tbody = table.K[2]
        assert len(tbody.K) == 2
        assert _kinds(tbody.K[0]) == ["/TD", "/TD"]


def test_row_header_marks_leading_body_cells(tmp_path):
    src = _build(tmp_path, [TABLE_WORDS])
    layout = {"blocks": [
        TABLE_LAYOUT["blocks"][0],
        {**TABLE_LAYOUT["blocks"][1], "row_header": True},
    ]}
    dest = tmp_path / "tagged.pdf"
    pdf_tag.tag_scan_pdf(src, str(dest), "Doc", opts={"layouts": [layout]})
    with pikepdf.open(str(dest)) as pdf:
        table = next(c for c in _doc_elem(pdf).K if str(c.S) == "/Table")
        body_tr = table.K[2].K[0]
        assert _kinds(body_tr) == ["/TH", "/TD"]
        assert str(body_tr.K[0].A.Scope) == "/Row"


def test_strict_demotes_unaccounted_lines_to_artifacts(tmp_path):
    words = PROSE_WORDS + [{"text": "zzqxv", "bbox": [600, 500, 700, 530]}]
    src = _build(tmp_path, [words])
    layout = {"blocks": [{"type": "text", "text": "Monthly Report hello world"}]}
    dest = tmp_path / "tagged.pdf"
    stats = pdf_tag.tag_scan_pdf(
        src, str(dest), "Doc", opts={"layouts": [layout], "strict": True}
    )
    assert stats["artifacts"] == 1
    assert stats["lines"] == 2
    # the junk word is gone from the structure but its bytes still render
    with pikepdf.open(str(dest)) as pdf:
        assert _kinds(_doc_elem(pdf)) == ["/P", "/P"]


def test_parent_tree_covers_every_mcid(tmp_path):
    src = _build(tmp_path, [PROSE_WORDS, PROSE_WORDS])
    dest = tmp_path / "tagged.pdf"
    pdf_tag.tag_scan_pdf(src, str(dest), "Doc", opts={"alt": "scan"})
    with pikepdf.open(str(dest)) as pdf:
        nums = pdf.Root.StructTreeRoot.ParentTree.Nums
        assert len(nums) == 4  # (key, parents) per page
        for page in pdf.pages:
            assert int(page.obj.StructParents) in (0, 1)
            assert str(page.obj.Tabs) == "/S"


# --- validate_ua --------------------------------------------------------------

def test_validate_ua_missing_binary_raises(tmp_path):
    with pytest.raises(RuntimeError, match="veraPDF not found"):
        pdf_tag.validate_ua("any.pdf", opts={"verapdf": "definitely-not-installed"})


@pytest.mark.skipif(__import__("shutil").which("verapdf") is None, reason="veraPDF not installed")
def test_validate_ua_passes_tagged_output(tmp_path):
    src = _build(tmp_path, [PROSE_WORDS])
    dest = tmp_path / "tagged.pdf"
    pdf_tag.tag_scan_pdf(src, str(dest), "Validation Sample", opts={"alt": "A typed page"})
    result = pdf_tag.validate_ua(str(dest))
    assert result["compliant"] is True
    assert result["failed_rules"] == 0
    assert result["passed_rules"] > 50


# --- ToUnicode decoding --------------------------------------------------------

def test_parse_tounicode_bfchar_and_bfrange():
    cmap = b"""
    2 beginbfchar
    <0003> <0020>
    <0010> <2019>
    endbfchar
    2 beginbfrange
    <0020> <0022> <0041>
    <0030> <0031> [<00E9> <6771>]
    endbfrange
    """
    assert pdf_tag._parse_tounicode(cmap) == {
        0x03: " ", 0x10: "’",
        0x20: "A", 0x21: "B", 0x22: "C",
        0x30: "é", 0x31: "東",
    }


def test_classify_streams_decodes_embedded_font_words(tmp_path):
    words = [{"text": "CEO’s", "bbox": [100, 40, 260, 70]},
             {"text": "café", "bbox": [280, 40, 400, 70]}]
    src = _build(tmp_path, [words])
    with pikepdf.open(src) as pdf:
        page = pdf.pages[0]
        contents = page.obj.Contents
        streams = [s.read_bytes() for s in (contents if isinstance(contents, pikepdf.Array) else [contents])]
        _imgs, found = pdf_tag._classify_streams(streams, pdf_tag._page_cmaps(page))
    assert [w.text for w in found] == ["CEO’s", "café"]


def test_classify_streams_falls_back_to_cp1252_without_cmap():
    stream = b"q BT /F1 12 Tf 1 0 0 1 10.0 20.0 Tm [<4869>]TJ ET Q"
    _imgs, words = pdf_tag._classify_streams([stream])
    assert [w.text for w in words] == ["Hi"]


def test_layout_heading_matches_typographic_text(tmp_path):
    words = [{"text": "CEO’s", "bbox": [100, 40, 260, 70]},
             {"text": "Report", "bbox": [280, 40, 400, 70]},
             {"text": "hello", "bbox": [100, 120, 220, 150]}]
    src = _build(tmp_path, [words])
    dest = tmp_path / "tagged.pdf"
    stats = pdf_tag.tag_scan_pdf(src, str(dest), "Test Doc", opts={
        "layouts": [{"blocks": [{"type": "heading", "text": "CEO’s Report"}]}],
    })
    assert stats["headings"] == 1
    with pikepdf.open(str(dest)) as pdf:
        assert _kinds(_doc_elem(pdf)) == ["/H1", "/P"]


# --- line grouping, margins, columns ------------------------------------------

def _w(x, y, text="word", size=10.0):
    return pdf_tag._Word(stream_index=0, x=x, y=y, text=text, size=size)


def test_lines_tolerance_grows_with_font_size():
    # 5pt apart: one line at 20pt, two at 8pt
    big = pdf_tag._lines([_w(100, 100, "A", 20), _w(150, 95, "B", 20)])
    small = pdf_tag._lines([_w(100, 100, "A", 8), _w(150, 95, "B", 8)])
    assert [ln.text for ln in big] == ["A B"]
    assert [ln.text for ln in small] == ["A", "B"]


def test_lines_chain_descender_jitter():
    words = [_w(100, 100, "a"), _w(130, 98, "b"), _w(160, 96, "c")]
    assert [ln.text for ln in pdf_tag._lines(words)] == ["a b c"]


def test_split_margins_puts_margin_note_before_body():
    words = []
    for i in range(6):
        y = 700 - 20 * i
        words += [_w(100 + 30 * k, y, f"b{i}{k}") for k in range(5)]
    words.append(_w(10, 700, "Note"))
    lines = pdf_tag._lines(words)
    assert lines[0].text == "Note"
    assert lines[1].text == "b00 b01 b02 b03 b04"


def _two_column_words(n=8):
    words = []
    for i in range(n):
        y = 700 - 20 * i
        words += [_w(x, y, f"L{i}") for x in (50, 80, 110)]
        words += [_w(x, y, f"R{i}") for x in (300, 330, 360)]
    return words


def test_columnize_reads_left_column_then_right():
    lines, _locate, n_gutters = pdf_tag._columnize(pdf_tag._lines(_two_column_words()), 2)
    assert n_gutters == 1
    order = [ln.words[0].text for ln in lines]
    assert order == [f"L{i}" for i in range(8)] + [f"R{i}" for i in range(8)]


def test_columnize_full_width_heading_is_its_own_band():
    words = [_w(x, 900, "Title") for x in (50, 120, 200, 260, 330)]  # spans gutter
    words += _two_column_words()
    lines, _locate, _ = pdf_tag._columnize(pdf_tag._lines(words), 2)
    assert lines[0].words[0].text == "Title"
    assert [w.text for w in lines[1].words] == ["L0", "L0", "L0"]  # left piece only


def test_local_bands_split_two_column_box_without_layout():
    lines, _locate, found = pdf_tag._columnize(pdf_tag._lines(_two_column_words(4)), None)
    assert found == 1
    assert [ln.words[0].text for ln in lines] == ["L0", "L1", "L2", "L3", "R0", "R1", "R2", "R3"]


def test_single_column_keeps_top_to_bottom_order():
    words = [_w(100 + 30 * k, 700 - 20 * i, f"w{i}") for i in range(8) for k in range(5)]
    lines, _locate, n = pdf_tag._columnize(pdf_tag._lines(words), 1)
    assert n == 0
    assert [ln.words[0].text for ln in lines] == [f"w{i}" for i in range(8)]


# --- block helpers ------------------------------------------------------------

def test_level_of_clamps_and_tolerates_junk():
    assert pdf_tag._level_of({"level": 3}) == 3
    assert pdf_tag._level_of({"level": 9}) == 6
    assert pdf_tag._level_of({"level": "x"}) == 0
    assert pdf_tag._level_of({}) == 0


def test_lang_of_requires_bcp47():
    assert pdf_tag._lang_of({"lang": " es "}) == "es"
    assert pdf_tag._lang_of({"lang": "pt-BR"}) == "pt-BR"
    assert pdf_tag._lang_of({"lang": "Spanish!"}) == ""


def test_is_ordered_from_flag_or_labels():
    assert pdf_tag._is_ordered({"ordered": True, "items": ["x"]})
    assert pdf_tag._is_ordered({"items": ["a. one", "b. two", "c. three"]})
    assert not pdf_tag._is_ordered({"items": ["• one", "• two"]})


def test_list_numbering_from_first_label():
    def group(label, ordered=True):
        item = pdf_tag._ListItem(0, [pdf_tag._Line([_w(0, 0, label), _w(20, 0, "x")])])
        return pdf_tag._ListGroup(0, ordered, [item])
    assert pdf_tag._list_numbering(group("1.")) == pikepdf.Name.Decimal
    assert pdf_tag._list_numbering(group("iv)")) == pikepdf.Name.LowerRoman
    assert pdf_tag._list_numbering(group("B.")) == pikepdf.Name.UpperAlpha
    assert pdf_tag._list_numbering(group("•", ordered=False)) == pikepdf.Name.Disc


def test_long_heading_is_tagged_as_text():
    specs = pdf_tag._layout_specs([{"type": "heading", "text": " ".join(["w"] * 21)}])
    assert specs[0][1] == "text"


# --- structure output ---------------------------------------------------------

def _flatten(elem):
    out = [str(elem.S)]
    kids = elem.get("/K")
    if isinstance(kids, pikepdf.Array):
        for k in kids:
            if isinstance(k, pikepdf.Dictionary):
                out += _flatten(k)
    return out


HEADINGS_WORDS = [
    {"text": "Annual", "bbox": [100, 40, 260, 70]},
    {"text": "Report", "bbox": [280, 40, 400, 70]},
    {"text": "Deep", "bbox": [100, 120, 200, 150]},
    {"text": "Section", "bbox": [220, 120, 380, 150]},
    {"text": "body", "bbox": [100, 200, 200, 230]},
    {"text": "text", "bbox": [220, 200, 300, 230]},
]


def test_heading_levels_are_clamped_and_bookmarked(tmp_path):
    src = _build(tmp_path, [HEADINGS_WORDS])
    dest = tmp_path / "tagged.pdf"
    stats = pdf_tag.tag_scan_pdf(src, str(dest), "Doc", opts={"layouts": [{"blocks": [
        {"type": "heading", "level": 1, "text": "Annual Report"},
        {"type": "heading", "level": 3, "text": "Deep Section"},
        {"type": "text", "text": "body text"},
    ]}]})
    assert stats["headings"] == 2
    assert stats["bookmarks"] == 2
    with pikepdf.open(str(dest)) as pdf:
        assert _kinds(_doc_elem(pdf)) == ["/H1", "/H2", "/P"]  # H3 would skip a level
        with pdf.open_outline() as outline:
            top = outline.root[0]
            assert top.title == "Annual Report"
            assert [c.title for c in top.children] == ["Deep Section"]


LIST_WORDS = [
    {"text": "1.", "bbox": [100, 40, 130, 70]},
    {"text": "Apples", "bbox": [150, 40, 300, 70]},
    {"text": "2.", "bbox": [100, 100, 130, 130]},
    {"text": "Pears", "bbox": [150, 100, 280, 130]},
]


def test_list_block_becomes_l_li_lbl_lbody(tmp_path):
    src = _build(tmp_path, [LIST_WORDS])
    dest = tmp_path / "tagged.pdf"
    stats = pdf_tag.tag_scan_pdf(src, str(dest), "Doc", opts={"layouts": [{"blocks": [
        {"type": "list", "ordered": True, "items": ["1. Apples", "2. Pears"]},
    ]}]})
    assert stats["lists"] == 1
    with pikepdf.open(str(dest)) as pdf:
        lst = _doc_elem(pdf).K[0]
        assert str(lst.S) == "/L"
        assert lst.A.ListNumbering == pikepdf.Name.Decimal
        assert [_kinds(li) for li in lst.K] == [["/Lbl", "/LBody"], ["/Lbl", "/LBody"]]


def test_block_in_another_language_gets_its_own_lang(tmp_path):
    src = _build(tmp_path, [PROSE_WORDS])
    dest = tmp_path / "tagged.pdf"
    pdf_tag.tag_scan_pdf(src, str(dest), "Doc", opts={"lang": "en-US", "layouts": [{"blocks": [
        {"type": "text", "text": "Monthly Report", "lang": "en"},
        {"type": "text", "text": "hello world", "lang": "es"},
    ]}]})
    with pikepdf.open(str(dest)) as pdf:
        first, second = _doc_elem(pdf).K
        assert "/Lang" not in first
        assert str(second.Lang) == "es"


PARA_WORDS = [
    {"text": "The", "bbox": [100, 40, 160, 70]},
    {"text": "quick", "bbox": [180, 40, 280, 70]},
    {"text": "brown", "bbox": [100, 80, 200, 110]},
    {"text": "fox", "bbox": [220, 80, 280, 110]},
]
PARA_LAYOUT = [{"blocks": [{"type": "text", "text": "The quick brown fox"}]}]


def test_paragraphs_mode_merges_a_text_block_into_one_p(tmp_path):
    src = _build(tmp_path, [PARA_WORDS])
    merged, split = tmp_path / "merged.pdf", tmp_path / "split.pdf"
    stats = pdf_tag.tag_scan_pdf(src, str(merged), "Doc", opts={"layouts": PARA_LAYOUT, "paragraphs": True})
    pdf_tag.tag_scan_pdf(src, str(split), "Doc", opts={"layouts": PARA_LAYOUT})
    assert stats["paragraphs"] == 1
    with pikepdf.open(str(merged)) as pdf:
        assert _kinds(_doc_elem(pdf)) == ["/P"]
        assert len(_doc_elem(pdf).K[0].K) == 4
    with pikepdf.open(str(split)) as pdf:
        assert _kinds(_doc_elem(pdf)) == ["/P", "/P"]


def test_heading_reads_before_the_block_it_introduces():
    lines = [pdf_tag._Line([_w(100, 700, "Body")]), pdf_tag._Line([_w(10, 690, "Margin"), _w(40, 690, "Head")])]
    para = pdf_tag._Para([lines[0]], block_index=1)
    head = pdf_tag._Heading([lines[1]], 2, block_index=0)
    assert pdf_tag._headings_first([para, head]) == [head, para]


# --- in-page figures ----------------------------------------------------------

FIGURE_WORDS = [
    {"text": "Sales", "bbox": [100, 40, 220, 70]},
    {"text": "rose", "bbox": [240, 40, 330, 70]},
    {"text": "2021", "bbox": [200, 260, 280, 285]},   # axis label, inside the chart
    {"text": "2022", "bbox": [450, 260, 530, 285]},
    {"text": "Figure", "bbox": [100, 320, 220, 350]},  # caption below
    {"text": "1", "bbox": [230, 320, 250, 350]},
    {"text": "Sales", "bbox": [270, 320, 380, 350]},
    {"text": "by", "bbox": [390, 320, 430, 350]},
    {"text": "year", "bbox": [440, 320, 520, 350]},
]
# 800x600 px page; chart at y 100-300 px
CHART_SPEC = {"bbox": [0.1, 0.15, 0.8, 0.5], "alt": "Bar chart of sales by year.",
              "caption": "Figure 1 Sales by year", "kind": "chart"}


def test_figure_region_becomes_figure_with_caption(tmp_path):
    src = _build(tmp_path, [FIGURE_WORDS])
    dest = tmp_path / "tagged.pdf"

    stats = pdf_tag.tag_scan_pdf(src, str(dest), "Doc", opts={"figures": [[CHART_SPEC]], "alt": "Ignored."})

    assert stats["figures"] == 1 and stats["captions"] == 1
    with pikepdf.open(str(dest)) as pdf:
        doc = _doc_elem(pdf)
        assert _kinds(doc) == ["/P", "/Figure"]
        fig = doc.K[1]
        assert str(fig.Alt) == "Bar chart of sales by year."
        assert str(fig.A.O) == "/Layout" and len(fig.A.BBox) == 4
        assert str(fig.K[0].S) == "/Caption"
        # redraw MCID + the two axis labels
        assert len([k for k in fig.K if not isinstance(k, pikepdf.Dictionary)]) == 3


def test_figure_text_stays_searchable(tmp_path):
    src = _build(tmp_path, [FIGURE_WORDS])
    dest = tmp_path / "tagged.pdf"
    pdf_tag.tag_scan_pdf(src, str(dest), "Doc", opts={"figures": [[CHART_SPEC]]})
    assert len(pymupdf.open(str(dest))[0].search_for("2021")) == 1


def test_page_without_figures_keeps_page_alt(tmp_path):
    src = _build(tmp_path, [FIGURE_WORDS])
    dest = tmp_path / "tagged.pdf"
    pdf_tag.tag_scan_pdf(src, str(dest), "Doc", opts={"figures": [[]], "alt": "A page."})
    with pikepdf.open(str(dest)) as pdf:
        assert str(_doc_elem(pdf).K[0].Alt) == "A page."


@pytest.mark.skipif(__import__("shutil").which("verapdf") is None, reason="veraPDF not installed")
def test_figure_output_passes_pdf_ua(tmp_path):
    src = _build(tmp_path, [FIGURE_WORDS])
    dest = tmp_path / "tagged.pdf"
    pdf_tag.tag_scan_pdf(src, str(dest), "Doc", opts={"figures": [[CHART_SPEC]]})
    assert pdf_tag.validate_ua(str(dest))["compliant"] is True


def test_claim_captions_takes_wrapped_caption_lines():
    w = lambda x, y, t: pdf_tag._Word(0, x, y, t, size=10.0)
    l1 = pdf_tag._Line([w(100, 200, "Figure"), w(140, 200, "2"), w(160, 200, "Rainfall")])
    l2 = pdf_tag._Line([w(100, 188, "by"), w(120, 188, "month")])
    body = pdf_tag._Line([w(100, 150, "Unrelated"), w(160, 150, "prose")])
    fig = pdf_tag._Figure([90, 210, 400, 500], "alt", caption="Figure 2 Rainfall by month")
    out = pdf_tag._claim_captions([fig], [l1, l2, body], 792)
    assert out == [body]
    assert fig.caption_line.text == "Figure 2 Rainfall by month"
