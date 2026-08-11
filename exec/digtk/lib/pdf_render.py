"""
PyMuPDF-based PDF page rasterization.

ARK/CaskFS-agnostic: every function takes and returns local filesystem paths only. Used
by digtk.models.digital_website.pdf to turn a bronze PDF binary into per-page raster images
that then run through the same digtk.lib.image_magick/digtk.lib.ocr_utils pipeline
already used for image bronze items.

Normalizes every page to a fixed output pixel width regardless of the PDF's native page
size — ported from the reference dams/services/image-utils pipeline's
extract-pdf-page.js/config.js (tifResizeWidth: 3072 by default), which computed a
rasterization density from target_width/page_width_inches parsed out of `pdfinfo` text
output. PyMuPDF's page.rect is already in points (1/72 inch), so the same ratio is
computed directly as a render-matrix scale factor with no text parsing involved.
"""
import pymupdf

from digtk.lib.config import CONFIG


class PdfError(RuntimeError):
    """Raised for a PDF that can't be opened, is corrupt, or is password-protected/encrypted.

    Wraps PyMuPDF's own RuntimeError subclasses (FileDataError, EmptyFileError,
    FileNotFoundError, ...) into one typed error for this module, and additionally
    covers the encrypted/needs-password case PyMuPDF itself doesn't raise on open
    (pymupdf.open() succeeds against an encrypted PDF; only reading a page then fails) —
    the reference pipeline lets failures like these propagate as raw, unhandled
    exceptions; this module deliberately doesn't replicate that, matching
    digtk.lib.image_magick's stated ethos of not letting a failed step continue
    silently against a stale or missing result.

    :param str path: The PDF path that failed.
    :param str reason: Human-readable reason.
    """

    def __init__(self, path, reason):
        super().__init__(f"{reason}: {path}")
        self.path = path
        self.reason = reason


def _open(pdf_path):
    """Open a PDF, raising PdfError instead of a raw PyMuPDF exception or an
    unchecked encrypted/needs-password document.

    :param str pdf_path: Local PDF path.
    :returns: Opened document.
    :rtype: pymupdf.Document
    :raises PdfError: If the file can't be opened, or is encrypted/password-protected.
    """
    try:
        doc = pymupdf.open(pdf_path)
    except RuntimeError as e:
        raise PdfError(pdf_path, f"unable to open PDF ({e})") from e
    if doc.needs_pass or doc.is_encrypted:
        doc.close()
        raise PdfError(pdf_path, "PDF is password-protected/encrypted")
    return doc


def page_count(pdf_path, opts={}):
    """Count a PDF's pages.

    :param str pdf_path: Local PDF path.
    :param dict opts: Unused, present for signature symmetry with the rest of this module.
    :returns: Page count.
    :rtype: int
    :raises PdfError: If the PDF can't be opened or is encrypted/password-protected.
    """
    doc = _open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


def page_geometry(pdf_path, page_index, opts={}):
    """Read one page's physical dimensions in points (1/72 inch).

    :param str pdf_path: Local PDF path.
    :param int page_index: 0-indexed page number.
    :param dict opts: Unused, present for signature symmetry with the rest of this module.
    :returns: {"width_pt": float, "height_pt": float}
    :rtype: dict
    :raises PdfError: If the PDF can't be opened or is encrypted/password-protected.
    :raises IndexError: If page_index is out of range.
    """
    doc = _open(pdf_path)
    try:
        rect = doc[page_index].rect
        return {"width_pt": rect.width, "height_pt": rect.height}
    finally:
        doc.close()


def rasterize_page(pdf_path, page_index, dest_path, opts={}):
    """Render one PDF page to a local raster image, normalized to a fixed pixel width.

    Computes `scale = target_pixel_width / width_pt` (falling back to a flat
    `default_density / 72` scale if the page's width can't be read, i.e. is zero or
    missing) and renders via `page.get_pixmap(matrix=pymupdf.Matrix(scale, scale))` — every
    page across every PDF ends up the same output pixel width regardless of its native
    page size, matching the reference pipeline's behavior without any text-parsed
    geometry.

    :param str pdf_path: Local PDF path.
    :param int page_index: 0-indexed page number.
    :param str dest_path: Local destination path (extension drives output format, e.g.
        .png — PyMuPDF's Pixmap.save() supports png/jpg/pnm/psd/ps natively, no tiff).
    :param dict opts: 'target_pixel_width' (int, default CONFIG["pdf"]["target_pixel_width"]),
        'default_density' (int, default CONFIG["pdf"]["default_density"]) — DPI used to
        compute the fallback scale when the page's width can't be read.
    :returns: {"dest_path": str, "dpi": float, "width_px": int, "height_px": int}
    :rtype: dict
    :raises PdfError: If the PDF can't be opened or is encrypted/password-protected.
    :raises IndexError: If page_index is out of range.
    """
    target_width = opts.get("target_pixel_width", CONFIG["pdf"]["target_pixel_width"])
    default_density = opts.get("default_density", CONFIG["pdf"]["default_density"])

    doc = _open(pdf_path)
    try:
        page = doc[page_index]
        width_pt = page.rect.width
        scale = target_width / width_pt if width_pt > 0 else default_density / 72.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale))
        pix.save(dest_path)
        return {
            "dest_path": dest_path, "dpi": scale * 72.0,
            "width_px": pix.width, "height_px": pix.height,
        }
    finally:
        doc.close()
