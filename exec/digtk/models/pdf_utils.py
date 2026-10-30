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
import unicodedata
from html.parser import HTMLParser

import pikepdf
import pymupdf

DEFAULT_DPI = 200.0
MIN_FONT_SIZE = 2.0
MIN_HSCALE = 0.2
MAX_HSCALE = 6.0

_BBOX_RE = re.compile(r"bbox (\d+) (\d+) (\d+) (\d+)")
# hOCR word spans carry title='bbox ...; x_wconf NN' — same convention
# digtk.lib.ocr_utils's own regex parsing relies on.
_WCONF_RE = re.compile(r"x_wconf (\d+)")

# Embedded text-layer font (PDF/UA-1 7.21.4.1).
TEXT_FONT_NAME = "digtk"
_text_font = None

# Typographic characters folded to ASCII.
_TYPOGRAPHIC = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-",
    "\u2026": "...", "\u2022": "*", "\u2023": "*", "\u00a0": " ", "\u2009": " ",
    "\u202f": " ", "\u2122": "TM", "\u20ac": "EUR", "\u2039": "<", "\u203a": ">",
}
_TYPO_TABLE = str.maketrans(_TYPOGRAPHIC)


def text_font():
    """Return the shared text-layer font (MuPDF's Droid Sans Fallback).

    It covers Latin, Greek, Cyrillic and CJK, and is subset on save.

    :returns: The cached font.
    :rtype: pymupdf.Font
    """
    global _text_font
    if _text_font is None:
        _text_font = pymupdf.Font("cjk")
    return _text_font


def _fold_candidates(ch):
    """Yield replacement candidates for a character the font can't show.

    :param str ch: A single character.
    :returns: Typographic fold, NFKC form, then base letter.
    :rtype: tuple[str]
    """
    stripped = "".join(c for c in unicodedata.normalize("NFKD", ch)
                       if not unicodedata.combining(c))
    return (ch.translate(_TYPO_TABLE), unicodedata.normalize("NFKC", ch), stripped)


def glyph_safe(text):
    """Return text using only characters the text-layer font has.

    Missing characters fold to ASCII or a base letter, else '?'.
    PDF/UA-1 7.21.8 forbids .notdef glyphs.

    :param str text: Text to clean.
    :returns: Text the font can fully render.
    :rtype: str
    """
    if not text:
        return text
    font = text_font()
    out = []
    for ch in text:
        if font.has_glyph(ord(ch)):
            out.append(ch)
            continue
        for cand in _fold_candidates(ch):
            if cand and cand != ch and all(font.has_glyph(ord(c)) for c in cand):
                out.append(cand)
                break
        else:
            out.append("?")
    return "".join(out)


def _ensure_font(page):
    """Register the text-layer font on a page once.

    :param pymupdf.Page page: Page to register the font on.
    :returns: None
    """
    if TEXT_FONT_NAME not in {f[4] for f in page.get_fonts(full=True)}:
        page.insert_font(fontname=TEXT_FONT_NAME, fontbuffer=text_font().buffer)


def _fix_cid_fonts(path):
    """Add the /CIDToGIDMap PyMuPDF omits from embedded CID fonts.

    ISO 32000 9.7.4 and PDF/UA-1 7.21.3.2 require it.

    :param str path: PDF to fix in place.
    :returns: True if the file was changed.
    :rtype: bool
    """
    changed = False
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            fonts = (page.obj.get("/Resources") or {}).get("/Font")
            if fonts is None:
                continue
            for _name, font in fonts.items():
                for desc in font.get("/DescendantFonts") or []:
                    if str(desc.get("/Subtype")) == "/CIDFontType2" and "/CIDToGIDMap" not in desc:
                        desc.CIDToGIDMap = pikepdf.Name.Identity
                        changed = True
        if changed:
            pdf.save(path)
    return changed


class _HocrWordParser(HTMLParser):
    """Collect {'text', 'bbox', 'conf'} for every ocrx_word span in an hOCR document."""

    def __init__(self):
        super().__init__()
        self.words = []
        self._depth = 0
        self._bbox = None
        self._conf = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if self._depth:
            self._depth += 1
            return
        attrs = dict(attrs)
        if "ocrx_word" in (attrs.get("class") or ""):
            title = attrs.get("title") or ""
            m = _BBOX_RE.search(title)
            if m:
                self._depth = 1
                self._bbox = [int(g) for g in m.groups()]
                wconf_m = _WCONF_RE.search(title)
                self._conf = float(wconf_m.group(1)) if wconf_m else None
                self._text = []

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1
            if self._depth == 0:
                text = html.unescape("".join(self._text)).strip()
                if text:
                    self.words.append({"text": text, "bbox": self._bbox, "conf": self._conf})

    def handle_data(self, data):
        if self._depth:
            self._text.append(data)


def hocr_words(hocr_path, min_conf=0.0):
    """Extract word geometry from a Tesseract hOCR file.

    Ported from the reference toolkit's words.py `--min-conf` filter — drops
    obviously-wrong low-confidence OCR guesses (e.g. over handwriting or
    marginalia) before they're baked into a searchable PDF's invisible text
    layer, where they'd otherwise surface as misleading search hits or
    misaligned highlight boxes.

    :param str hocr_path: Path to an hOCR file (ocr_utils.run_tesseract output).
    :param float min_conf: Drop words whose hOCR x_wconf (0-100) is below
        this (default 0.0 — keep everything). A word with no parseable
        x_wconf is always kept, since that's a parsing gap, not evidence of
        low quality.
    :returns: [{'text': str, 'bbox': [x0, y0, x1, y1], 'conf': float|None}]
        in image-pixel coords, in document order. Words with empty text are
        skipped.
    :rtype: list[dict]
    """
    with open(hocr_path, encoding="utf-8", errors="replace") as f:
        return filter_min_conf(parse_hocr_words(f.read()), min_conf)


def parse_hocr_words(hocr_text):
    """Extract word geometry from hOCR markup.

    :param str hocr_text: hOCR document text.
    :returns: [{'text', 'bbox', 'conf'}] in document order.
    :rtype: list[dict]
    """
    parser = _HocrWordParser()
    parser.feed(hocr_text)
    return parser.words


def filter_min_conf(words, min_conf=0.0):
    """Drop words below a confidence floor; words without one are kept.

    :param list[dict] words: [{'text', 'bbox', 'conf'}].
    :param float min_conf: Confidence floor (0-100); 0 keeps everything.
    :rtype: list[dict]
    """
    if min_conf <= 0.0:
        return words
    return [w for w in words if w.get("conf") is None or w["conf"] >= min_conf]


def _insert_words(page, words, page_rect, scale):
    """Place each word's invisible text inside its bounding box.

    :param pymupdf.Page page: Page to write to.
    :param list[dict] words: [{'text', 'bbox'}] in image-pixel coords.
    :param pymupdf.Rect page_rect: Full page rectangle in points.
    :param float scale: Image pixels -> PDF points factor.
    :returns: Number of words placed.
    :rtype: int
    """
    _ensure_font(page)
    font = text_font()
    placed = 0
    W, H = page_rect.width, page_rect.height
    for word in words:
        text = glyph_safe((word.get("text") or "").strip())
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
            natural = font.text_length(text, fontsize=font_size)
            hscale = (
                max(MIN_HSCALE, min(box_width / natural, MAX_HSCALE))
                if natural > 0.1 else 1.0
            )
            page.insert_text(
                (baseline_x, baseline_y), text,
                fontsize=font_size, fontname=TEXT_FONT_NAME, render_mode=3,
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
    _save(doc, dest_path)
    return {"pages": len(image_paths), "words_placed": words_placed}


def add_text_layer(src_path, dest_path, pages, opts={}):
    """Add an invisible OCR text layer to some pages of an existing PDF.

    Every page's own content is kept; the layer goes on top.

    :param str src_path: Source PDF.
    :param str dest_path: Output PDF path (written atomically).
    :param dict pages: {page_index: {'words': [{'text', 'bbox'}], 'scale':
        float}} — scale turns the words' pixel boxes into page points.
    :param dict opts: 'title' (str).
    :returns: {'pages': int (pages given a layer), 'words_placed': int}
    :rtype: dict
    """
    doc = pymupdf.open(src_path)
    placed = 0
    for index, layer in pages.items():
        page = doc[index]
        placed += _insert_words(page, layer["words"], page.rect, layer["scale"])
    if opts.get("title"):
        doc.set_metadata({**(doc.metadata or {}), "title": str(opts["title"])})
    _save(doc, dest_path)
    return {"pages": len(pages), "words_placed": placed}


def _save(doc, dest_path):
    """Save atomically, subsetting the text-layer font and fixing its CID map.

    :param pymupdf.Document doc: Document to save; closed afterwards.
    :param str dest_path: Output path.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    tmp = dest_path + ".tmp"
    try:
        doc.subset_fonts()
    except Exception:  # noqa: BLE001 — full font still valid
        pass
    doc.save(tmp, deflate=True, garbage=4)
    doc.close()
    _fix_cid_fonts(tmp)
    os.replace(tmp, dest_path)
