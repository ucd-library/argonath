import fitz
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
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 800, 600))
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
    page = fitz.open(str(dest))[0]
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
