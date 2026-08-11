import pymupdf
import pytest

from digtk.lib import pdf_render


def _make_pdf(path, page_sizes=((300, 150),)):
    """Build a tiny real PDF with one page per (width_pt, height_pt) pair."""
    doc = pymupdf.open()
    for width, height in page_sizes:
        doc.new_page(width=width, height=height)
    doc.save(str(path))
    doc.close()


def _make_encrypted_pdf(path):
    doc = pymupdf.open()
    doc.new_page(width=300, height=150)
    doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    doc.close()


def test_page_count_single_page(tmp_path):
    pdf = tmp_path / "one.pdf"
    _make_pdf(pdf)
    assert pdf_render.page_count(str(pdf)) == 1


def test_page_count_multi_page(tmp_path):
    pdf = tmp_path / "multi.pdf"
    _make_pdf(pdf, page_sizes=((300, 150), (200, 100), (400, 300)))
    assert pdf_render.page_count(str(pdf)) == 3


def test_page_geometry_returns_points(tmp_path):
    pdf = tmp_path / "sized.pdf"
    _make_pdf(pdf, page_sizes=((612, 792),))
    geometry = pdf_render.page_geometry(str(pdf), 0)
    assert geometry == {"width_pt": 612.0, "height_pt": 792.0}


def test_page_geometry_per_page_for_mixed_sizes(tmp_path):
    pdf = tmp_path / "mixed.pdf"
    _make_pdf(pdf, page_sizes=((612, 792), (300, 150)))
    assert pdf_render.page_geometry(str(pdf), 1) == {"width_pt": 300.0, "height_pt": 150.0}


def test_rasterize_page_normalizes_to_target_pixel_width(tmp_path):
    pdf = tmp_path / "one.pdf"
    _make_pdf(pdf, page_sizes=((300, 150),))
    dest = tmp_path / "page-000.png"

    result = pdf_render.rasterize_page(str(pdf), 0, str(dest), opts={"target_pixel_width": 900})
    assert dest.exists()
    assert result["width_px"] == 900
    assert result["height_px"] == 450  # 300x150 aspect ratio preserved
    assert result["dpi"] == pytest.approx(900 / (300 / 72.0))


def test_rasterize_page_different_native_sizes_hit_the_same_target_width(tmp_path):
    pdf = tmp_path / "mixed.pdf"
    _make_pdf(pdf, page_sizes=((300, 150), (600, 300)))

    dest0 = tmp_path / "page-000.png"
    dest1 = tmp_path / "page-001.png"
    r0 = pdf_render.rasterize_page(str(pdf), 0, str(dest0), opts={"target_pixel_width": 600})
    r1 = pdf_render.rasterize_page(str(pdf), 1, str(dest1), opts={"target_pixel_width": 600})

    assert r0["width_px"] == r1["width_px"] == 600


def test_rasterize_page_uses_configured_defaults(tmp_path):
    pdf = tmp_path / "one.pdf"
    _make_pdf(pdf, page_sizes=((300, 150),))
    dest = tmp_path / "page-000.png"

    result = pdf_render.rasterize_page(str(pdf), 0, str(dest))
    # default target_pixel_width is 3072 (CONFIG["pdf"]["target_pixel_width"])
    assert result["width_px"] == 3072


def test_page_count_raises_pdf_error_for_corrupt_file(tmp_path):
    bad = tmp_path / "garbage.pdf"
    bad.write_bytes(b"not a pdf at all")
    with pytest.raises(pdf_render.PdfError):
        pdf_render.page_count(str(bad))


def test_page_count_raises_pdf_error_for_encrypted_pdf(tmp_path):
    pdf = tmp_path / "locked.pdf"
    _make_encrypted_pdf(pdf)
    with pytest.raises(pdf_render.PdfError, match="password-protected|encrypted"):
        pdf_render.page_count(str(pdf))


def test_rasterize_page_raises_pdf_error_for_encrypted_pdf(tmp_path):
    pdf = tmp_path / "locked.pdf"
    _make_encrypted_pdf(pdf)
    with pytest.raises(pdf_render.PdfError):
        pdf_render.rasterize_page(str(pdf), 0, str(tmp_path / "out.png"))
