import shutil
import subprocess
from types import SimpleNamespace

import pytest

from digtk.lib import ocr_utils

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None or shutil.which("convert") is None,
    reason="tesseract or ImageMagick not installed",
)

HOCR_TWO_WORDS = """
<div class='ocr_page'>
 <span class='ocrx_word' title='bbox 0 0 10 10; x_wconf 90'>hello</span>
 <span class='ocrx_word' title='bbox 12 0 30 10; x_wconf 70'>world</span>
</div>
"""

HOCR_NO_WORDS = "<div class='ocr_page' title='bbox 0 0 100 100'></div>"


@pytest.fixture
def fake_tesseract(monkeypatch, tmp_path):
    """Capture the tesseract command without running it; writes empty outputs."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        base = cmd[2]
        for ext in (".hocr", ".txt"):
            with open(base + ext, "w") as f:
                f.write(HOCR_TWO_WORDS if ext == ".hocr" else "")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(ocr_utils.subprocess, "run", fake_run)
    return calls


# --- argument assembly / behavior (no tesseract required) ---------------------

def test_run_tesseract_defaults_from_config(fake_tesseract, tmp_path):
    base = str(tmp_path / "output")
    result = ocr_utils.run_tesseract("page.tiff", base)
    cmd = fake_tesseract[0]
    assert cmd[:3] == ["tesseract", "page.tiff", base]
    assert cmd[cmd.index("-l") + 1] == "eng"
    assert cmd[cmd.index("--psm") + 1] == "1"
    assert cmd[cmd.index("--oem") + 1] == "3"
    assert cmd[-2:] == ["hocr", "txt"]
    assert result["hocr_path"] == base + ".hocr"
    assert result["txt_path"] == base + ".txt"
    assert result["confidence"] == 80.0


def test_run_tesseract_opts_override(fake_tesseract, tmp_path):
    ocr_utils.run_tesseract("page.tiff", str(tmp_path / "o"), opts={"lang": "spa", "psm": 6, "oem": 1})
    cmd = fake_tesseract[0]
    assert cmd[cmd.index("-l") + 1] == "spa"
    assert cmd[cmd.index("--psm") + 1] == "6"
    assert cmd[cmd.index("--oem") + 1] == "1"


def test_run_tesseract_creates_parent_dirs(fake_tesseract, tmp_path):
    base = tmp_path / "sub" / "deeper" / "output"
    ocr_utils.run_tesseract("page.tiff", str(base))
    assert base.parent.is_dir()


def test_run_tesseract_nonzero_exit_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ocr_utils.subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom \xff"),
    )
    with pytest.raises(RuntimeError, match="tesseract failed"):
        ocr_utils.run_tesseract("page.tiff", str(tmp_path / "o"))


def test_run_tesseract_timeout_raises(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(ocr_utils.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out"):
        ocr_utils.run_tesseract("page.tiff", str(tmp_path / "o"), opts={"timeout": 5})


def test_run_tesseract_missing_output_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ocr_utils.subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    with pytest.raises(RuntimeError, match="did not write"):
        ocr_utils.run_tesseract("page.tiff", str(tmp_path / "o"))


def test_extract_confidence_means_word_scores(tmp_path):
    hocr = tmp_path / "page.hocr"
    hocr.write_text(HOCR_TWO_WORDS)
    assert ocr_utils.extract_confidence(str(hocr)) == 80.0


def test_extract_confidence_no_words_is_zero(tmp_path):
    hocr = tmp_path / "page.hocr"
    hocr.write_text(HOCR_NO_WORDS)
    assert ocr_utils.extract_confidence(str(hocr)) == 0.0


def test_extract_confidence_tolerates_bad_bytes(tmp_path):
    hocr = tmp_path / "page.hocr"
    hocr.write_bytes(HOCR_TWO_WORDS.encode() + b"\xff\xfe")
    assert ocr_utils.extract_confidence(str(hocr)) == 80.0


def test_run_ml_ocr_still_stubbed(tmp_path):
    with pytest.raises(NotImplementedError):
        ocr_utils.run_ml_ocr("page.tiff", str(tmp_path / "o"))


# --- end-to-end against real tesseract ----------------------------------------

@needs_tesseract
def test_run_tesseract_reads_rendered_text(tmp_path):
    src = tmp_path / "page.png"
    subprocess.run(
        ["convert", "-size", "600x150", "xc:white", "-pointsize", "48",
         "-fill", "black", "-annotate", "+40+90", "HELLO WORLD", str(src)],
        capture_output=True, check=True,
    )
    result = ocr_utils.run_tesseract(str(src), str(tmp_path / "output"))
    text = open(result["txt_path"], encoding="utf-8", errors="replace").read()
    assert "HELLO" in text
    assert result["confidence"] > 50.0


@needs_tesseract
def test_run_tesseract_missing_image_raises(tmp_path):
    with pytest.raises(RuntimeError, match="tesseract failed"):
        ocr_utils.run_tesseract(str(tmp_path / "nope.png"), str(tmp_path / "o"))
