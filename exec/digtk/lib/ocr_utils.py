"""
OCR processing utilities — Tesseract and ML-based OCR wrappers.
Tesseract is the baseline; ML OCR (surya or docTR) is the fallback
when Tesseract confidence falls below threshold.
"""
import os
import re
import subprocess

from digtk.lib.config import CONFIG

CONFIDENCE_THRESHOLD = CONFIG["ocr"]["confidence_threshold"]

DEFAULT_TIMEOUT_S = 180

# hOCR word spans carry title='bbox ...; x_wconf NN' — x_wconf appears nowhere else.
_WCONF_RE = re.compile(r"x_wconf\s+(\d+)")


def run_tesseract(image_path, output_base, opts={}):
    """Run Tesseract OCR on an image, producing .hocr and .txt outputs.

    Output is captured as bytes and decoded with errors="replace" — Tesseract
    can emit non-UTF-8 bytes, and text=True would turn that into an uncaught
    UnicodeDecodeError.

    :param str image_path: Path to the prepared (deskewed, 300 DPI) image.
    :param str output_base: Base output path (Tesseract appends .hocr / .txt).
    :param dict opts: Options: lang (str), psm (int), oem (int) — default from
        CONFIG["ocr"] — and timeout (seconds, default 180).
    :returns: Dict with 'hocr_path', 'txt_path', 'confidence' (float 0–100).
    :rtype: dict
    :raises RuntimeError: If Tesseract fails, times out, or writes no output.
    """
    parent = os.path.dirname(os.path.abspath(output_base))
    os.makedirs(parent, exist_ok=True)

    cmd = [
        "tesseract", image_path, output_base,
        "-l", str(opts.get("lang", CONFIG["ocr"]["lang"])),
        "--psm", str(opts.get("psm", CONFIG["ocr"]["psm"])),
        "--oem", str(opts.get("oem", CONFIG["ocr"]["oem"])),
        "hocr", "txt",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=opts.get("timeout", DEFAULT_TIMEOUT_S)
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"tesseract timed out after {e.timeout}s on {image_path}") from e
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"tesseract failed ({proc.returncode}) on {image_path}: {stderr}")

    hocr_path = output_base + ".hocr"
    txt_path = output_base + ".txt"
    for path in (hocr_path, txt_path):
        if not os.path.exists(path):
            raise RuntimeError(f"tesseract exited 0 but did not write {path}")

    return {
        "hocr_path": hocr_path,
        "txt_path": txt_path,
        "confidence": extract_confidence(hocr_path),
    }


def extract_confidence(hocr_path):
    """Parse a Tesseract hOCR file and return the mean word confidence score.

    A page with no recognized words scores 0.0 — downstream that reads as
    "below threshold", which is the right signal for both genuinely unreadable
    pages and blank ones.

    :param str hocr_path: Path to the hOCR output file.
    :returns: Mean confidence score (0.0–100.0).
    :rtype: float
    """
    with open(hocr_path, encoding="utf-8", errors="replace") as f:
        confidences = [int(m) for m in _WCONF_RE.findall(f.read())]
    if not confidences:
        return 0.0
    return sum(confidences) / len(confidences)


def run_ml_ocr(image_path, output_base, opts={}):
    """Run ML-based OCR (surya or docTR) on an image.

    Used automatically when Tesseract confidence is below CONFIDENCE_THRESHOLD.

    :param str image_path: Path to the source image.
    :param str output_base: Base output path for hOCR and text outputs.
    :param dict opts: Additional model options.
    :returns: Dict with 'hocr_path', 'txt_path'.
    :rtype: dict
    """
    raise NotImplementedError("phase 0.5")
