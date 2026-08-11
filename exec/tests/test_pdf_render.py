import subprocess

import pytest

from digtk.lib import pdf_render

PDFINFO_OK = "Title:          Sample\nPages:          2\nPage size:      612 x 792 pts (letter)\n"


class FakeCompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def fake_run(monkeypatch):
    """Replaces subprocess.run with a recorder that dispatches by binary name.

    pdf_render.py's own `pdfinfo` calls and image_magick's `convert`/`identify`
    calls (used by rasterize_page()) all go through subprocess.run, so one fake
    handles all three shapes rather than needing separate patches per module.
    """
    calls = []

    def install(pdfinfo_stdout=PDFINFO_OK, pdfinfo_returncode=0, pdfinfo_stderr="",
                identify_stdout="3072x3976 ", raises=None):
        def fake(argv, **kwargs):
            calls.append({"argv": argv, "kwargs": kwargs})
            if raises is not None:
                raise raises
            if "pdfinfo" in argv[0]:
                return FakeCompletedProcess(stdout=pdfinfo_stdout, stderr=pdfinfo_stderr, returncode=pdfinfo_returncode)
            if "identify" in argv:
                return FakeCompletedProcess(stdout=identify_stdout, returncode=0)
            return FakeCompletedProcess(stdout="", returncode=0)  # convert/magick
        monkeypatch.setattr(subprocess, "run", fake)
        return calls

    return install


# --- page_count -----------------------------------------------------------

def test_page_count_parses_pages_line(fake_run):
    fake_run(pdfinfo_stdout="Title: x\nPages:          7\n")
    assert pdf_render.page_count("/tmp/doc.pdf") == 7


def test_page_count_calls_pdfinfo_with_no_page_flags(fake_run):
    calls = fake_run()
    pdf_render.page_count("/tmp/doc.pdf")
    assert calls[0]["argv"] == ["pdfinfo", "/tmp/doc.pdf"]


def test_page_count_honors_configured_pdfinfo_bin(fake_run, monkeypatch):
    monkeypatch.setitem(pdf_render.CONFIG["pdf"], "pdfinfo_bin", "/opt/poppler/bin/pdfinfo")
    calls = fake_run()
    pdf_render.page_count("/tmp/doc.pdf")
    assert calls[0]["argv"][0] == "/opt/poppler/bin/pdfinfo"


def test_page_count_raises_when_no_pages_line(fake_run):
    fake_run(pdfinfo_stdout="Title: x\n")
    with pytest.raises(pdf_render.PdfError, match="could not parse page count"):
        pdf_render.page_count("/tmp/doc.pdf")


def test_page_count_raises_on_nonzero_exit(fake_run):
    fake_run(pdfinfo_returncode=1, pdfinfo_stderr="Syntax Error: broken pdf")
    with pytest.raises(pdf_render.PdfError, match="pdfinfo failed"):
        pdf_render.page_count("/tmp/bad.pdf")


def test_page_count_raises_on_missing_binary(fake_run):
    fake_run(raises=FileNotFoundError())
    with pytest.raises(pdf_render.PdfError, match="pdfinfo not found"):
        pdf_render.page_count("/tmp/doc.pdf")


def test_page_count_raises_on_timeout(fake_run):
    fake_run(raises=subprocess.TimeoutExpired(cmd=["pdfinfo"], timeout=5))
    with pytest.raises(pdf_render.PdfError, match="timed out"):
        pdf_render.page_count("/tmp/doc.pdf")


# --- page_geometry ----------------------------------------------------------

def test_page_geometry_parses_page_size(fake_run):
    fake_run(pdfinfo_stdout="Page size:      612 x 792 pts (letter)\n")
    assert pdf_render.page_geometry("/tmp/doc.pdf", 0) == {"width_pt": 612.0, "height_pt": 792.0}


def test_page_geometry_scopes_to_one_indexed_page(fake_run):
    calls = fake_run()
    pdf_render.page_geometry("/tmp/doc.pdf", 2)
    assert calls[0]["argv"] == ["pdfinfo", "-f", "3", "-l", "3", "/tmp/doc.pdf"]


def test_page_geometry_raises_when_no_page_size_line(fake_run):
    fake_run(pdfinfo_stdout="Pages: 1\n")
    with pytest.raises(pdf_render.PdfError, match="could not parse page size"):
        pdf_render.page_geometry("/tmp/doc.pdf", 0)


# --- rasterize_page -----------------------------------------------------------
# Legacy fidelity: convert -density {density} {pdf}[{page}] -resize {width}x {dest},
# no -compress/-depth/-colorspace flags. See docs on digtk.lib.pdf_render for the
# reference dams/services/image-utils pipeline this replicates.

def test_rasterize_page_builds_exact_legacy_convert_argv(fake_run, tmp_path):
    calls = fake_run(pdfinfo_stdout="Page size:      612 x 792 pts (letter)\n")
    dest = tmp_path / "page.tif"

    pdf_render.rasterize_page("/tmp/doc.pdf", 2, str(dest), opts={"target_pixel_width": 3072})

    convert_call = next(c for c in calls if c["argv"][0] == "convert")
    expected_density = str(3072 / (612 / 72.0))
    assert convert_call["argv"] == [
        "convert", "-density", expected_density, "/tmp/doc.pdf[2]",
        "-resize", "3072x", str(dest),
    ]


def test_rasterize_page_uses_page_specific_width_via_dash_f_dash_l(fake_run, tmp_path):
    calls = fake_run(pdfinfo_stdout="Page size:      612 x 792 pts (letter)\n")
    dest = tmp_path / "page.tif"

    pdf_render.rasterize_page("/tmp/doc.pdf", 4, str(dest))

    pdfinfo_call = next(c for c in calls if "pdfinfo" in c["argv"][0])
    assert pdfinfo_call["argv"] == ["pdfinfo", "-f", "5", "-l", "5", "/tmp/doc.pdf"]


def test_rasterize_page_no_compression_or_depth_flags(fake_run, tmp_path):
    calls = fake_run(pdfinfo_stdout="Page size:      612 x 792 pts (letter)\n")
    dest = tmp_path / "page.tif"

    pdf_render.rasterize_page("/tmp/doc.pdf", 0, str(dest))

    convert_call = next(c for c in calls if c["argv"][0] == "convert")
    forbidden = {"-compress", "-depth", "-colorspace", "-type"}
    assert not (forbidden & set(convert_call["argv"]))


def test_rasterize_page_falls_back_to_default_density_when_width_unparseable(fake_run, tmp_path):
    fake_run(pdfinfo_stdout="Pages: 1\n")  # no "Page size:" line at all
    dest = tmp_path / "page.tif"

    result = pdf_render.rasterize_page("/tmp/doc.pdf", 0, str(dest), opts={"default_density": 450})
    assert result["dpi"] == 450


def test_rasterize_page_uses_configured_defaults(fake_run, tmp_path):
    fake_run(pdfinfo_stdout="Pages: 1\n")
    dest = tmp_path / "page.tif"

    result = pdf_render.rasterize_page("/tmp/doc.pdf", 0, str(dest))
    assert result["dpi"] == pdf_render.CONFIG["pdf"]["default_density"]


def test_rasterize_page_returns_actual_probed_dimensions(fake_run, tmp_path):
    fake_run(pdfinfo_stdout="Page size:      612 x 792 pts (letter)\n", identify_stdout="3072x3976 ")
    dest = tmp_path / "page.tif"

    result = pdf_render.rasterize_page("/tmp/doc.pdf", 0, str(dest))
    assert result["width_px"] == 3072
    assert result["height_px"] == 3976
    assert result["dest_path"] == str(dest)


def test_rasterize_page_propagates_pdfinfo_subprocess_failure(fake_run, tmp_path):
    fake_run(raises=FileNotFoundError())
    with pytest.raises(pdf_render.PdfError, match="pdfinfo not found"):
        pdf_render.rasterize_page("/tmp/doc.pdf", 0, str(tmp_path / "page.tif"))
