"""
PDF page rasterization via ImageMagick + Ghostscript, matching the reference
dams/services/image-utils pipeline's extract-pdf-page.js exactly: `pdfinfo`
(poppler-utils) for page count and page width, ImageMagick's `convert` (whose PDF
delegate shells out to Ghostscript) for the actual rasterization. Used by
digtk.models.digital_website.pdf to turn a bronze PDF binary into per-page raster
images that then run through the same digtk.lib.image_magick/digtk.lib.ocr_utils
pipeline already used for image bronze items.

Density is computed as target_pixel_width / page_width_inches (falling back to a
flat default_density DPI if the width can't be determined), then the same `convert`
call's `-resize {target_pixel_width}x` snaps the actual output to that exact pixel
width regardless of how precisely the density calculation landed — matching the
reference pipeline's `convert -density D file.pdf[N] -resize Wx out.tif` exactly,
including writing an unflagged TIFF (no -compress/-depth/-colorspace: whatever this
ImageMagick+Ghostscript install's defaults produce).

Deliberately diverges from the reference pipeline in one place: it looks up EACH
page's own width via `pdfinfo -f N -l N` rather than reusing page 1's width for
every page (the reference pipeline's actual behavior — a real quirk that produces
the wrong density for any page after the first in a mixed-page-size PDF).
"""
import re
import subprocess

from digtk.lib import image_magick
from digtk.lib.config import CONFIG

_PAGES_RE = re.compile(r"^Pages:\s+(\d+)", re.MULTILINE)
_PAGE_SIZE_RE = re.compile(r"^Page size:\s+(\d+\.?\d+?)\s+x\s+(\d+\.?\d+?)+\s+pts", re.MULTILINE)
PTS_TO_INCH = 72.0


class PdfError(RuntimeError):
    """Raised when `pdfinfo` can't be run against a PDF (missing binary, timeout,
    non-zero exit — e.g. a corrupt or encrypted file it can't open), or when its
    output doesn't contain the field being looked up.

    :param str path: The PDF path that failed.
    :param str reason: Human-readable reason.
    """

    def __init__(self, path, reason):
        super().__init__(f"{reason}: {path}")
        self.path = path
        self.reason = reason


def _pdfinfo(pdf_path, opts={}, page=None):
    """Run `pdfinfo`, optionally scoped to a single page.

    :param str pdf_path: Local PDF path.
    :param dict opts: 'pdfinfo_bin' (str, default CONFIG["pdf"]["pdfinfo_bin"]),
        'timeout' (float, default CONFIG["image"]["timeout_s"]).
    :param int|None page: 0-indexed page to scope to (poppler's `-f`/`-l` flags are
        1-indexed, so this is translated internally), or None for the whole file.
    :returns: pdfinfo's stdout.
    :rtype: str
    :raises PdfError: If the binary is missing, times out, or exits non-zero.
    """
    binary = opts.get("pdfinfo_bin", CONFIG["pdf"]["pdfinfo_bin"])
    timeout = opts.get("timeout", CONFIG["image"]["timeout_s"])
    argv = [binary]
    if page is not None:
        argv += ["-f", str(page + 1), "-l", str(page + 1)]
    argv.append(pdf_path)
    try:
        result = subprocess.run(argv, shell=False, timeout=timeout, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise PdfError(pdf_path, f"pdfinfo not found ({binary!r})") from e
    except subprocess.TimeoutExpired as e:
        raise PdfError(pdf_path, "pdfinfo timed out") from e
    if result.returncode != 0:
        raise PdfError(pdf_path, f"pdfinfo failed: {result.stderr.strip()}")
    return result.stdout


def page_count(pdf_path, opts={}):
    """Count a PDF's pages via `pdfinfo`.

    :param str pdf_path: Local PDF path.
    :param dict opts: See _pdfinfo().
    :returns: Page count.
    :rtype: int
    :raises PdfError: If pdfinfo fails, or its output has no `Pages:` line.
    """
    stdout = _pdfinfo(pdf_path, opts)
    m = _PAGES_RE.search(stdout)
    if not m:
        raise PdfError(pdf_path, "could not parse page count from pdfinfo output")
    return int(m.group(1))


def page_geometry(pdf_path, page_index, opts={}):
    """Read one page's physical dimensions in points, via `pdfinfo -f -l`.

    :param str pdf_path: Local PDF path.
    :param int page_index: 0-indexed page number.
    :param dict opts: See _pdfinfo().
    :returns: {"width_pt": float, "height_pt": float}
    :rtype: dict
    :raises PdfError: If pdfinfo fails, or its output has no `Page size:` line.
    """
    stdout = _pdfinfo(pdf_path, opts, page=page_index)
    m = _PAGE_SIZE_RE.search(stdout)
    if not m:
        raise PdfError(pdf_path, f"could not parse page size for page {page_index}")
    return {"width_pt": float(m.group(1)), "height_pt": float(m.group(2))}


def _page_width_inches(pdf_path, page_index, opts):
    """One page's width in inches, or None if pdfinfo ran but its output didn't
    contain a parseable `Page size:` line — the signal rasterize_page() falls back
    to a flat default density on, matching the reference pipeline's own NaN-checked
    fallback. A pdfinfo subprocess failure (missing binary, timeout, non-zero exit)
    still raises PdfError and propagates, rather than falling back — the reference
    pipeline doesn't treat those as recoverable either.

    :param str pdf_path: Local PDF path.
    :param int page_index: 0-indexed page number.
    :param dict opts: See _pdfinfo().
    :rtype: float|None
    :raises PdfError: If the pdfinfo subprocess itself fails.
    """
    stdout = _pdfinfo(pdf_path, opts, page=page_index)
    m = _PAGE_SIZE_RE.search(stdout)
    return float(m.group(1)) / PTS_TO_INCH if m else None


def rasterize_page(pdf_path, page_index, dest_path, opts={}):
    """Render one PDF page to a local TIFF, normalized to a fixed pixel width.

    :param str pdf_path: Local PDF path.
    :param int page_index: 0-indexed page number.
    :param str dest_path: Local destination path (extension drives ImageMagick's
        output format — CONFIG["pdf"]["raster_format"] defaults to "tif").
    :param dict opts: 'target_pixel_width' (int, default
        CONFIG["pdf"]["target_pixel_width"]), 'default_density' (int, default
        CONFIG["pdf"]["default_density"]) — DPI used when the page's width can't be
        read; forwarded to image_magick.convert_argv()/run() ('magick', 'convert_bin',
        'timeout').
    :returns: {"dest_path": str, "dpi": float, "width_px": int, "height_px": int}
    :rtype: dict
    :raises PdfError: If the pdfinfo subprocess itself fails.
    :raises digtk.lib.image_magick.ImageMagickError: If the `convert` invocation fails.
    """
    target_width = opts.get("target_pixel_width", CONFIG["pdf"]["target_pixel_width"])
    default_density = opts.get("default_density", CONFIG["pdf"]["default_density"])

    width_inches = _page_width_inches(pdf_path, page_index, opts)
    density = target_width / width_inches if width_inches and width_inches > 0 else default_density

    argv = image_magick.convert_argv(opts) + [
        "-density", str(density), f"{pdf_path}[{page_index}]",
        "-resize", f"{target_width}x", dest_path,
    ]
    image_magick.run(argv, opts)

    dims = image_magick.probe_dimensions(dest_path, opts)
    return {
        "dest_path": dest_path, "dpi": density,
        "width_px": dims["width"], "height_px": dims["height"],
    }
