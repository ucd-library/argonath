"""
ImageMagick subprocess wrappers for image processing operations.
All operations call `convert` (ImageMagick) via subprocess.
"""


def thumbnail(src_path, dest_path, width, opts={}):
    """Generate a thumbnail JPEG at the specified width, preserving aspect ratio.

    :param str src_path: Source image path.
    :param str dest_path: Destination JPEG path.
    :param int width: Target width in pixels.
    :param dict opts: Additional options (quality, etc.).
    """
    raise NotImplementedError("phase 0.4")


def convert_format(src_path, dest_path, opts={}):
    """Convert an image to a different format via ImageMagick.

    :param str src_path: Source image path.
    :param str dest_path: Destination path (format inferred from extension).
    :param dict opts: Additional options (quality, compression, etc.).
    """
    raise NotImplementedError("phase 0.4")


def deskew(src_path, dest_path, opts={}):
    """Correct image skew using ImageMagick's -deskew operator.

    :param str src_path: Source image path.
    :param str dest_path: Output path for the deskewed image.
    :param dict opts: Additional options (threshold, etc.).
    """
    raise NotImplementedError("phase 0.4")


def prep_ocr(src_path, dest_path, opts={}):
    """Prepare an image for OCR: resize to 2048px long edge, 300 DPI, fuzz whiten background.

    :param str src_path: Source image path.
    :param str dest_path: Output path for the OCR-ready image.
    :param dict opts: Additional options (fuzz threshold, etc.).
    """
    raise NotImplementedError("phase 0.4")
