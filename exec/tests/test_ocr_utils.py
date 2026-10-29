import os
import shutil
import subprocess

import pytest

from digtk.lib import ocr_utils

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None or shutil.which("convert") is None,
    reason="tesseract or ImageMagick not installed",
)


def hocr_with_confidences(confidences):
    """Build a minimal hOCR fragment with one word span per confidence value."""
    words = "".join(
        f'<span class="ocrx_word" title="bbox 1 2 3 4; x_wconf {int(c)}">w</span>'
        for c in confidences
    )
    return f'<html><body><div class="ocr_page"><div class="ocr_carea"><p class="ocr_par">{words}</p></div></div></body></html>'


class FakeCompletedProcess:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


@pytest.fixture
def fake_run(monkeypatch):
    """Replaces subprocess.run with a recorder that also touches Tesseract's
    expected .hocr/.txt outputs (without clobbering ones a test pre-wrote),
    matching run_tesseract()'s post-exit existence check. Pass write_outputs=False
    to simulate Tesseract exiting 0 without writing anything."""
    calls = []

    def _install(raises=None, write_outputs=True):
        def fake(argv, **kwargs):
            calls.append({"argv": argv, "kwargs": kwargs})
            if raises is not None:
                raise raises
            if write_outputs:
                output_base = argv[2]
                for ext in (".hocr", ".txt"):
                    path = output_base + ext
                    if not os.path.exists(path):
                        open(path, "w").close()
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


def test_run_tesseract_limits_openmp_threads(fake_run, tmp_path, monkeypatch):
    monkeypatch.delenv("OMP_THREAD_LIMIT", raising=False)
    calls = fake_run()

    ocr_utils.run_tesseract(str(tmp_path / "img.jpg"), str(tmp_path / "out"))

    assert calls[0]["kwargs"]["env"]["OMP_THREAD_LIMIT"] == "1"


def test_run_tesseract_keeps_an_existing_thread_limit(fake_run, tmp_path, monkeypatch):
    monkeypatch.setenv("OMP_THREAD_LIMIT", "4")
    calls = fake_run()

    ocr_utils.run_tesseract(str(tmp_path / "img.jpg"), str(tmp_path / "out"), {"threads": 2})

    assert calls[0]["kwargs"]["env"]["OMP_THREAD_LIMIT"] == "4"


def test_run_tesseract_retries_a_timeout_once(monkeypatch, tmp_path):
    attempts = []

    def fake(argv, **kwargs):
        attempts.append(argv)
        if len(attempts) == 1:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=120)
        for ext in (".hocr", ".txt"):
            open(argv[2] + ext, "w").close()
        return FakeCompletedProcess()
    monkeypatch.setattr(subprocess, "run", fake)

    result = ocr_utils.run_tesseract(str(tmp_path / "img.jpg"), str(tmp_path / "out"))

    assert len(attempts) == 2
    assert result["hocr_path"] == str(tmp_path / "out") + ".hocr"


def test_run_tesseract_raises_after_the_retry_also_times_out(fake_run, tmp_path):
    calls = fake_run(raises=subprocess.TimeoutExpired(cmd=["tesseract"], timeout=120))

    with pytest.raises(ocr_utils.TesseractError) as exc_info:
        ocr_utils.run_tesseract(str(tmp_path / "img.jpg"), str(tmp_path / "out"))

    assert exc_info.value.timed_out is True
    assert len(calls) == 2


def test_run_tesseract_does_not_retry_a_failed_exit(fake_run, tmp_path):
    calls = fake_run(raises=subprocess.CalledProcessError(1, ["tesseract"], stderr="bad ocr"))

    with pytest.raises(ocr_utils.TesseractError):
        ocr_utils.run_tesseract(str(tmp_path / "img.jpg"), str(tmp_path / "out"))

    assert len(calls) == 1


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


def test_compare_orientations_exposes_hocr_path_per_candidate(monkeypatch):
    stats = {
        "/orig.jpg": {"count": 50, "avg": 60.0, "median": 60.0},
        "/deskew.jpg": {"count": 150, "avg": 90.0, "median": 90.0},
    }
    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda path, base, opts: {"hocr_path": path + ".hocr"})
    monkeypatch.setattr(ocr_utils, "analyze_words", lambda hocr_path, opts: dict(stats[hocr_path[:-5]]))

    result = ocr_utils.compare_orientations({"original": "/orig.jpg", "deskewed": "/deskew.jpg"})
    assert result["stats"]["original"]["hocr_path"] == "/orig.jpg.hocr"
    assert result["stats"]["deskewed"]["hocr_path"] == "/deskew.jpg.hocr"


def test_compare_orientations_keeps_original_when_angle_below_threshold(monkeypatch):
    """Deskewed clearly wins on count/confidence, but the page barely deviates
    from level (angle below threshold) -- not worth correcting, matching the
    reference implementation's angle-threshold gate."""
    stats = {
        "/orig.jpg": {"count": 50, "avg": 60.0, "median": 60.0},
        "/deskew.jpg": {"count": 150, "avg": 90.0, "median": 90.0},
    }
    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda path, base, opts: {"hocr_path": path + ".hocr"})
    monkeypatch.setattr(ocr_utils, "analyze_words", lambda hocr_path, opts: dict(stats[hocr_path[:-5]]))

    result = ocr_utils.compare_orientations(
        {"original": "/orig.jpg", "deskewed": "/deskew.jpg"}, opts={"angle": 0.3, "angle_threshold": 1.1}
    )
    assert result["selected"] == "original"


def test_compare_orientations_picks_deskewed_when_angle_exceeds_threshold(monkeypatch):
    stats = {
        "/orig.jpg": {"count": 50, "avg": 60.0, "median": 60.0},
        "/deskew.jpg": {"count": 150, "avg": 90.0, "median": 90.0},
    }
    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda path, base, opts: {"hocr_path": path + ".hocr"})
    monkeypatch.setattr(ocr_utils, "analyze_words", lambda hocr_path, opts: dict(stats[hocr_path[:-5]]))

    result = ocr_utils.compare_orientations(
        {"original": "/orig.jpg", "deskewed": "/deskew.jpg"}, opts={"angle": -2.5, "angle_threshold": 1.1}
    )
    assert result["selected"] == "deskewed"


def test_compare_orientations_skips_angle_gate_when_angle_not_provided(monkeypatch):
    """A caller that doesn't measure an angle (e.g. a direct/legacy call site)
    gets the old count/confidence-only behavior -- angle=None never blocks."""
    stats = {
        "/orig.jpg": {"count": 50, "avg": 60.0, "median": 60.0},
        "/deskew.jpg": {"count": 150, "avg": 90.0, "median": 90.0},
    }
    monkeypatch.setattr(ocr_utils, "run_tesseract", lambda path, base, opts: {"hocr_path": path + ".hocr"})
    monkeypatch.setattr(ocr_utils, "analyze_words", lambda hocr_path, opts: dict(stats[hocr_path[:-5]]))

    result = ocr_utils.compare_orientations({"original": "/orig.jpg", "deskewed": "/deskew.jpg"})
    assert result["selected"] == "deskewed"


def test_run_tesseract_creates_parent_dirs(fake_run, tmp_path):
    fake_run()
    base = tmp_path / "sub" / "deeper" / "output"
    ocr_utils.run_tesseract("page.tiff", str(base))
    assert base.parent.is_dir()


def test_run_tesseract_raises_on_missing_output(fake_run, tmp_path):
    fake_run(write_outputs=False)
    with pytest.raises(ocr_utils.TesseractError, match="did not write"):
        ocr_utils.run_tesseract(str(tmp_path / "img.jpg"), str(tmp_path / "out"))


def test_extract_confidence_tolerates_bad_bytes(tmp_path):
    hocr = tmp_path / "page.hocr"
    hocr.write_bytes(hocr_with_confidences([80, 90]).encode() + b"\xff\xfe")
    assert ocr_utils.extract_confidence(str(hocr)) == pytest.approx(85.0)


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
    with pytest.raises(ocr_utils.TesseractError):
        ocr_utils.run_tesseract(str(tmp_path / "nope.png"), str(tmp_path / "o"))


# --- correct_words ------------------------------------------------------------

def _words(text):
    return [{"text": t, "bbox": [0, 0, 1, 1]} for t in text.split()]


def _corrected(ocr_text, model_text, opts={}):
    words = _words(ocr_text)
    changes = ocr_utils.correct_words(words, model_text, opts)
    return [w["text"] for w in words], changes


def test_correct_words_fixes_a_misread_word():
    texts, changes = _corrected("The quick hrown fox", "The quick brown fox")
    assert texts == ["The", "quick", "brown", "fox"]
    assert changes == [{"i": 2, "old": "hrown", "new": "brown"}]


def test_correct_words_splits_an_ocr_join():
    texts, _ = _corrected("rising further.Increasing costs", "rising further. Increasing costs")
    assert texts == ["rising", "further. Increasing", "costs"]


def test_correct_words_never_changes_digits():
    texts, changes = _corrected("value 0.7220 here", "value 0.6352 here")
    assert texts == ["value", "0.7220", "here"]
    assert changes == []


def test_correct_words_allows_splitting_numbers():
    texts, _ = _corrected("in 1976.A study", "in 1976. A study")
    assert texts == ["in", "1976. A", "study"]


def test_correct_words_skips_short_tokens():
    texts, _ = _corrected("see WQ there", "see w there")
    assert texts == ["see", "WQ", "there"]


def test_correct_words_keeps_ocr_punctuation_and_typography():
    texts, changes = _corrected("the latter's view", "the latter’s view")
    assert texts == ["the", "latter's", "view"]
    assert changes == []


def test_correct_words_case_only_needs_ocr_signature():
    texts, _ = _corrected("a succesS story COWS", "a success story cows")
    assert texts == ["a", "success", "story", "COWS"]


def test_correct_words_never_drops_letters():
    texts, _ = _corrected("about Slaughtering cattle", "about Slaughter cattle")
    assert texts == ["about", "Slaughtering", "cattle"]


def test_correct_words_rejects_text_outside_cp1252():
    texts, _ = _corrected("the Na2CO3 salt", "the Na₂CO₃ salt")
    assert texts == ["the", "Na2CO3", "salt"]


def test_correct_words_skips_second_half_of_hyphenated_break():
    texts, _ = _corrected("the tem- perature rose", "the temperature rose")
    assert texts == ["the", "tem-", "perature", "rose"]


def test_correct_words_leaves_poorly_aligned_pages_alone():
    texts, changes = _corrected("alpha beta gamma delta", "one two three four five six")
    assert changes == []


def test_correct_words_ignores_blank_or_empty_transcript():
    assert _corrected("hello wrold", "[BLANK]")[1] == []
    assert _corrected("hello wrold", "")[1] == []
    assert ocr_utils.correct_words([], "text") == []


def test_correct_words_min_sim_opt():
    _, strict = _corrected("the hrown fox", "the brown fox", {"min_sim": 0.95})
    assert strict == []
