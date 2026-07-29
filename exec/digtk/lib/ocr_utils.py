"""
OCR processing utilities — Tesseract and ML-based OCR wrappers.
Tesseract is the baseline; ML OCR (surya or docTR) is the fallback
when Tesseract confidence falls below threshold.
"""

CONFIDENCE_THRESHOLD = 60.0


def run_tesseract(image_path, output_base, opts={}):
    """Run Tesseract OCR on an image, producing .hocr and .txt outputs.

    :param str image_path: Path to the prepared (deskewed, 300 DPI) image.
    :param str output_base: Base output path (Tesseract appends .hocr / .txt).
    :param dict opts: Options: lang (str), psm (int), oem (int).
    :returns: Dict with 'hocr_path', 'txt_path', 'confidence' (float 0–100).
    :rtype: dict
    """
    raise NotImplementedError("phase 0.5")


def extract_confidence(hocr_path):
    """Parse a Tesseract hOCR file and return the mean word confidence score.

    :param str hocr_path: Path to the hOCR output file.
    :returns: Mean confidence score (0.0–100.0).
    :rtype: float
    """
    raise NotImplementedError("phase 0.5")


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
