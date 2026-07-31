"""
ImageMagick subprocess wrappers for image processing operations.
All operations call `convert` (ImageMagick) via subprocess.

Sources deeper than 8 bits per channel (16-bit archival TIFF masters) are
auto-leveled before JPEG output — a linear 16->8 scale leaves scans that only
use a narrow band of the tonal range looking flat/dark, so the tonal range is
stretched to full scale first (same behavior for float/32-bit sources).
"""
import os
import subprocess

DEFAULT_JPEG_QUALITY = 85
DEFAULT_DESKEW_THRESHOLD = "40%"
DEFAULT_PREP_FUZZ = "10%"
PREP_LONG_EDGE = 2048
PREP_DENSITY = 300


def _run_convert(args):
    """Run `convert` with the given arguments, raising on failure.

    Output is captured as bytes and decoded with errors="replace" — ImageMagick
    can emit non-UTF-8 bytes in error paths, and text=True would turn that into
    an uncaught UnicodeDecodeError.

    :param list[str] args: Arguments to `convert` (everything after the program name).
    :raises RuntimeError: If convert exits non-zero, with its stderr in the message.
    """
    proc = subprocess.run(["convert"] + args, capture_output=True)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"convert failed ({proc.returncode}) for {args[0]}: {stderr}")


def _bit_depth(src_path):
    """Read an image's bits-per-channel via `identify`.

    :param str src_path: Source image path.
    :returns: Bit depth of the first frame, or 8 if identify fails (the
        subsequent convert call will surface the real error).
    :rtype: int
    """
    proc = subprocess.run(
        ["identify", "-format", "%[bit-depth]", src_path + "[0]"], capture_output=True
    )
    if proc.returncode != 0:
        return 8
    try:
        return int(proc.stdout.decode("utf-8", errors="replace").strip())
    except ValueError:
        return 8


def _prepare_dest(dest_path):
    """Create the destination's parent directory if needed (convert won't).

    :param str dest_path: Destination file path.
    """
    parent = os.path.dirname(os.path.abspath(dest_path))
    os.makedirs(parent, exist_ok=True)


def thumbnail(src_path, dest_path, width, opts={}):
    """Generate a thumbnail JPEG at the specified width, preserving aspect ratio.

    Uses the first frame of multi-frame sources, applies EXIF orientation, and
    never upscales — a source narrower than `width` keeps its size.

    :param str src_path: Source image path.
    :param str dest_path: Destination JPEG path.
    :param int width: Target width in pixels.
    :param dict opts: 'quality' (int JPEG quality, default 85).
    :raises RuntimeError: If ImageMagick fails.
    """
    _prepare_dest(dest_path)
    args = [src_path + "[0]", "-auto-orient"]
    if _bit_depth(src_path) > 8:
        args += ["-auto-level"]
    args += [
        "-thumbnail", f"{width}x>",
        "-colorspace", "sRGB",
        "-quality", str(opts.get("quality", DEFAULT_JPEG_QUALITY)),
        dest_path,
    ]
    _run_convert(args)


def convert_format(src_path, dest_path, opts={}):
    """Convert an image to a different format via ImageMagick.

    All frames are kept (multi-page sources stay multi-page where the target
    format supports it).

    :param str src_path: Source image path.
    :param str dest_path: Destination path (format inferred from extension).
    :param dict opts: 'quality' (int), 'compress' (str, e.g. 'LZW', 'JPEG').
    :raises RuntimeError: If ImageMagick fails.
    """
    _prepare_dest(dest_path)
    args = [src_path]
    if "quality" in opts:
        args += ["-quality", str(opts["quality"])]
    if "compress" in opts:
        args += ["-compress", str(opts["compress"])]
    args.append(dest_path)
    _run_convert(args)


def deskew(src_path, dest_path, opts={}):
    """Correct image skew using ImageMagick's -deskew operator.

    :param str src_path: Source image path.
    :param str dest_path: Output path for the deskewed image.
    :param dict opts: 'threshold' (str, default '40%' — fraction of pixels that
        must agree on a rotation before it is applied).
    :raises RuntimeError: If ImageMagick fails.
    """
    _prepare_dest(dest_path)
    _run_convert([
        src_path,
        "-background", "white",
        "-deskew", str(opts.get("threshold", DEFAULT_DESKEW_THRESHOLD)),
        "+repage",
        dest_path,
    ])


def prep_ocr(src_path, dest_path, opts={}):
    """Prepare an image for OCR: resize to 2048px long edge, 300 DPI, fuzz whiten background.

    Uses the first frame, applies EXIF orientation, never upscales, and
    replaces near-white pixels with pure white (background cleanup for
    typewriter/archival scans).

    :param str src_path: Source image path.
    :param str dest_path: Output path for the OCR-ready image.
    :param dict opts: 'fuzz' (str, default '10%' — how near white a pixel must
        be to be whitened).
    :raises RuntimeError: If ImageMagick fails.
    """
    _prepare_dest(dest_path)
    args = [src_path + "[0]", "-auto-orient"]
    if _bit_depth(src_path) > 8:
        args += ["-auto-level"]
    args += [
        "-resize", f"{PREP_LONG_EDGE}x{PREP_LONG_EDGE}>",
        "-units", "PixelsPerInch",
        "-density", str(PREP_DENSITY),
        "-fuzz", str(opts.get("fuzz", DEFAULT_PREP_FUZZ)),
        "-fill", "white",
        "-opaque", "white",
        dest_path,
    ]
    _run_convert(args)
