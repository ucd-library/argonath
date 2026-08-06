import subprocess

import pytest

from digtk.lib import ocr_utils


def hocr_with_confidences(confidences):
    """Build a minimal hOCR fragment with one word span per confidence value."""
    words = "".join(
        f'<span class="ocrx_word" title="bbox 1 2 3 4; x_wconf {int(c)}">w</span>'
        for c in confidences
    )
    return f'<html><body><div class="ocr_page"><div class="ocr_carea"><p class="ocr_par">{words}</p></div></div></body></html>'


class FakeCompletedProcess:
    def __init__(self, stdout=""):
        self.stdout = stdout


@pytest.fixture
def fake_run(monkeypatch):
    calls = []

    def _install(raises=None):
        def fake(argv, **kwargs):
            calls.append({"argv": argv, "kwargs": kwargs})
            if raises is not None:
                raise raises
            return FakeCompletedProcess()
        monkeypatch.setattr(subprocess, "run", fake)
        return calls

    return _install


def test_extract_confidence_computes_mean(tmp_path):
    hocr = tmp_path / "output.hocr"
    hocr.write_text(hocr_with_confidences([80, 90, 100]))
    assert ocr_utils.extract_confidence(str(hocr)) == pytest.approx(90.0)


def test_extract_confidence_returns_zero_for_no_words(tmp_path):
    hocr = tmp_path / "output.hocr"
    hocr.write_text(hocr_with_confidences([]))
    assert ocr_utils.extract_confidence(str(hocr)) == 0.0


def test_analyze_words_counts_avg_median(tmp_path):
    hocr = tmp_path / "output.hocr"
    hocr.write_text(hocr_with_confidences([60, 70, 80, 90]))
    result = ocr_utils.analyze_words(str(hocr))
    assert result["count"] == 4
    assert result["avg"] == pytest.approx(75.0)
    assert result["median"] == 80  # sorted [60,70,80,90], index len//2 == 2


def test_analyze_words_drops_words_below_threshold(tmp_path):
    hocr = tmp_path / "output.hocr"
    hocr.write_text(hocr_with_confidences([10, 20, 95]))
    result = ocr_utils.analyze_words(str(hocr), opts={"confidence_threshold": 50})
    assert result["count"] == 1
    assert result["avg"] == 95.0


def test_analyze_words_uses_configured_default_threshold(tmp_path):
    hocr = tmp_path / "output.hocr"
    hocr.write_text(hocr_with_confidences([10, 95]))  # default threshold is 50
    result = ocr_utils.analyze_words(str(hocr))
    assert result["count"] == 1


def test_run_tesseract_builds_expected_argv(fake_run, tmp_path):
    calls = fake_run()
    hocr = tmp_path / "out.hocr"
    hocr.write_text(hocr_with_confidences([90]))
    result = ocr_utils.run_tesseract(str(tmp_path / "img.jpg"), str(tmp_path / "out"))
    assert calls[0]["argv"] == [
        "tesseract", str(tmp_path / "img.jpg"), str(tmp_path / "out"),
        "--dpi", "300", "-l", "eng", "--psm", "1", "--oem", "3", "hocr", "txt",
    ]
    assert result["hocr_path"] == str(tmp_path / "out.hocr")
    assert result["txt_path"] == str(tmp_path / "out.txt")
    assert result["confidence"] == 90.0


def test_run_tesseract_never_uses_shell(fake_run, tmp_path):
    calls = fake_run()
    (tmp_path / "out.hocr").write_text(hocr_with_confidences([90]))
    ocr_utils.run_tesseract(str(tmp_path / "img.jpg"), str(tmp_path / "out"))
    assert calls[0]["kwargs"].get("shell", False) is False


def test_run_tesseract_raises_tesseract_error_on_failure(fake_run, tmp_path):
    fake_run(raises=subprocess.CalledProcessError(1, ["tesseract"], stderr="bad ocr"))
    with pytest.raises(ocr_utils.TesseractError) as exc_info:
        ocr_utils.run_tesseract(str(tmp_path / "img.jpg"), str(tmp_path / "out"))
    assert exc_info.value.returncode == 1
    assert exc_info.value.timed_out is False


def test_run_tesseract_raises_tesseract_error_on_timeout(fake_run, tmp_path):
    fake_run(raises=subprocess.TimeoutExpired(cmd=["tesseract"], timeout=120))
    with pytest.raises(ocr_utils.TesseractError) as exc_info:
        ocr_utils.run_tesseract(str(tmp_path / "img.jpg"), str(tmp_path / "out"))
    assert exc_info.value.timed_out is True


def test_compare_orientations_rejects_wrong_keys():
    with pytest.raises(ValueError, match="original.*deskewed"):
        ocr_utils.compare_orientations({"foo": "a", "bar": "b"})


def test_compare_orientations_picks_deskewed_when_clearly_better(monkeypatch):
    stats = {
        "/orig.jpg": {"count": 50, "avg": 60.0, "median": 60.0},
        "/deskew.jpg": {"count": 150, "avg": 90.0, "median": 90.0},
    }
    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda path, base, opts: {"hocr_path": path + ".hocr"})
    monkeypatch.setattr(ocr_utils, "analyze_words", lambda hocr_path, opts: stats[hocr_path[:-5]])

    result = ocr_utils.compare_orientations({"original": "/orig.jpg", "deskewed": "/deskew.jpg"})
    assert result["selected"] == "deskewed"
    assert result["stats"]["deskewed"]["count"] == 150


def test_compare_orientations_keeps_original_when_deskew_is_worse(monkeypatch):
    stats = {
        "/orig.jpg": {"count": 150, "avg": 90.0, "median": 90.0},
        "/deskew.jpg": {"count": 50, "avg": 60.0, "median": 60.0},
    }
    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda path, base, opts: {"hocr_path": path + ".hocr"})
    monkeypatch.setattr(ocr_utils, "analyze_words", lambda hocr_path, opts: stats[hocr_path[:-5]])

    result = ocr_utils.compare_orientations({"original": "/orig.jpg", "deskewed": "/deskew.jpg"})
    assert result["selected"] == "original"


def test_compare_orientations_keeps_original_when_not_enough_evidence(monkeypatch):
    """Deskewed looks 'better' but total word count is too low to trust it."""
    stats = {
        "/orig.jpg": {"count": 2, "avg": 60.0, "median": 60.0},
        "/deskew.jpg": {"count": 3, "avg": 90.0, "median": 90.0},
    }
    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda path, base, opts: {"hocr_path": path + ".hocr"})
    monkeypatch.setattr(ocr_utils, "analyze_words", lambda hocr_path, opts: stats[hocr_path[:-5]])

    result = ocr_utils.compare_orientations({"original": "/orig.jpg", "deskewed": "/deskew.jpg"})
    assert result["selected"] == "original"


def test_compare_orientations_honors_custom_word_count_threshold(monkeypatch):
    stats = {
        "/orig.jpg": {"count": 2, "avg": 60.0, "median": 60.0},
        "/deskew.jpg": {"count": 3, "avg": 90.0, "median": 90.0},
    }
    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda path, base, opts: {"hocr_path": path + ".hocr"})
    monkeypatch.setattr(ocr_utils, "analyze_words", lambda hocr_path, opts: stats[hocr_path[:-5]])

    result = ocr_utils.compare_orientations(
        {"original": "/orig.jpg", "deskewed": "/deskew.jpg"}, opts={"word_count_threshold": 2}
    )
    assert result["selected"] == "deskewed"
