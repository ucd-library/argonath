"""
Searchable PDF assembly — page images with an invisible, position-aligned
text layer built from Tesseract hOCR word geometry.

Each OCR word is placed invisibly (render mode 3) inside its image bounding
box with per-word horizontal scaling: the word is stretched/compressed to
exactly fill its box width, so text selection and search highlights land on
whole words even on monospace/typewriter scans where naive font sizing
drifts badly across a line.

Each page image and each word is inserted as its own content stream — a
downstream accessibility tagger depends on that one-stream-per-element
shape to assign marked-content IDs, so don't batch the inserts.

Words come from the hOCR files digtk.lib.ocr_utils.run_tesseract writes
(hocr_words parses them); callers with word geometry from another source
can pass the same [{'text', 'bbox'}] shape directly.
"""
import html
import os
import re
from html.parser import HTMLParser

import pymupdf

DEFAULT_DPI = 200.0
MIN_FONT_SIZE = 2.0
MIN_HSCALE = 0.2
MAX_HSCALE = 6.0

_BBOX_RE = re.compile(r"bbox (\d+) (\d+) (\d+) (\d+)")


class _HocrWordParser(HTMLParser):
    """Collect {'text', 'bbox'} for every ocrx_word span in an hOCR document."""

    def __init__(self):
        super().__init__()
        self.words = []
        self._depth = 0
        self._bbox = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if self._depth:
            self._depth += 1
            return
        attrs = dict(attrs)
        if "ocrx_word" in (attrs.get("class") or ""):
            m = _BBOX_RE.search(attrs.get("title") or "")
            if m:
                self._depth = 1
                self._bbox = [int(g) for g in m.groups()]
                self._text = []

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1
            if self._depth == 0:
                text = html.unescape("".join(self._text)).strip()
                if text:
                    self.words.append({"text": text, "bbox": self._bbox})

    def handle_data(self, data):
        if self._depth:
            self._text.append(data)


def hocr_words(hocr_path):
    """Extract word geometry from a Tesseract hOCR file.

    :param str hocr_path: Path to an hOCR file (ocr_utils.run_tesseract output).
    :returns: [{'text': str, 'bbox': [x0, y0, x1, y1]}] in image-pixel coords,
        in document order. Words with empty text are skipped.
    :rtype: list[dict]
    """
    parser = _HocrWordParser()
    with open(hocr_path, encoding="utf-8", errors="replace") as f:
        parser.feed(f.read())
    return parser.words


def _insert_words(page, words, page_rect, scale):
    """Place each word's invisible text inside its bounding box.

    :param pymupdf.Page page: Page to write to.
    :param list[dict] words: [{'text', 'bbox'}] in image-pixel coords.
    :param pymupdf.Rect page_rect: Full page rectangle in points.
    :param float scale: Image pixels -> PDF points factor.
    :returns: Number of words placed.
    :rtype: int
    """
    placed = 0
    W, H = page_rect.width, page_rect.height
    for word in words:
        text = (word.get("text") or "").strip()
        box = word.get("bbox")
        if not text or not box or len(box) != 4:
            continue
        x0, y0, x1, y1 = (c * scale for c in box)
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        box_width = x1 - x0
        font_size = max(MIN_FONT_SIZE, (y1 - y0) * 0.85)
        baseline_x = min(max(x0, 0.0), W - 1)
        baseline_y = min(max(y1 - (y1 - y0) * 0.12, font_size), H - 1)
        try:
            natural = pymupdf.get_text_length(text, fontname="helv", fontsize=font_size)
            hscale = (
                max(MIN_HSCALE, min(box_width / natural, MAX_HSCALE))
                if natural > 0.1 else 1.0
            )
            page.insert_text(
                (baseline_x, baseline_y), text,
                fontsize=font_size, fontname="helv", render_mode=3,
                morph=(pymupdf.Point(baseline_x, baseline_y), pymupdf.Matrix(hscale, 1.0)),
            )
            placed += 1
        except Exception:  # noqa: BLE001 — never let one word abort the page
            pass
    return placed


def build_searchable_pdf(image_paths, words_per_page, dest_path, opts={}):
    """Assemble a searchable PDF: one page per image, invisible aligned text.

    :param list[str] image_paths: Page images in page order.
    :param list words_per_page: Per-page word lists aligned with image_paths.
        Each entry is [{'text', 'bbox'}] (see hocr_words), or None/[] for a
        page with no text (image-only page).
    :param str dest_path: Output PDF path (written atomically).
    :param dict opts: 'dpi' (float, default 200 — the pixel->point scale of the
        word geometry), 'title' (str PDF title), 'producer' (str, default 'digtk').
    :returns: Dict with 'pages' and 'words_placed' counts.
    :rtype: dict
    :raises ValueError: If image_paths and words_per_page lengths differ.
    """
    if len(image_paths) != len(words_per_page):
        raise ValueError(
            f"image_paths ({len(image_paths)}) and words_per_page "
            f"({len(words_per_page)}) must align"
        )
    scale = 72.0 / float(opts.get("dpi", DEFAULT_DPI))
    doc = pymupdf.open()
    words_placed = 0
    for image_path, words in zip(image_paths, words_per_page):
        pix = pymupdf.Pixmap(image_path)
        w_pt, h_pt = pix.width * scale, pix.height * scale
        pix = None
        page = doc.new_page(width=w_pt, height=h_pt)
        rect = pymupdf.Rect(0, 0, w_pt, h_pt)
        page.insert_image(rect, filename=image_path)
        if words:
            words_placed += _insert_words(page, words, rect, scale)

    metadata = {"producer": str(opts.get("producer", "digtk"))}
    if opts.get("title"):
        metadata["title"] = str(opts["title"])
    doc.set_metadata(metadata)

    parent = os.path.dirname(os.path.abspath(dest_path))
    os.makedirs(parent, exist_ok=True)
    tmp = dest_path + ".tmp"
    doc.save(tmp, deflate=True, garbage=4)
    doc.close()
    os.replace(tmp, dest_path)
    return {"pages": len(image_paths), "words_placed": words_placed}
