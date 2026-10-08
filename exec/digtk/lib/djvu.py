"""
hOCR -> "djvu" text-layer conversion, replicating the legacy DAMS/fin
`image-utils` OCR product.

Despite the name, the output isn't a real DjVu binary -- it's a hand-built XML
document mimicking the Internet Archive djvu-text-layer schema
(OBJECT/PARAM/HIDDENTEXT/PAGECOLUMN/REGION/PARAGRAPH/LINE/WORD) that legacy's
image-utils service produces and dams/fin's client reads back as `ocr.djvu`.
This is a deliberate legacy-compatibility feature (see
digtk/lib/gcs_sync.py's module docstring) -- functional compatibility with
that schema, not byte-for-byte replication of legacy's exact serializer
output (same tags/nesting/attributes, not necessarily identical whitespace or
attribute ordering).

Kept separate from digtk.lib.ocr_utils, which deliberately avoids a
structural hOCR parse (regex-extracting just x_wconf values is enough for
orientation comparison) -- this module reverses that decision because
building real djvu output needs each word's bounding box and text, not just
its confidence.

ARK/CaskFS-agnostic, like ocr_utils.py and image_magick.py: every function
takes local file paths / already-parsed structures and returns plain data.
"""
import re
import xml.etree.ElementTree as ET

from digtk.lib.config import CONFIG

# hOCR word/line/carea/par spans carry title='bbox x0 y0 x1 y1; ...'.
_BBOX_RE = re.compile(r"bbox (-?\d+) (-?\d+) (-?\d+) (-?\d+)")
# Word spans additionally carry '; x_wconf NN' in the same title attribute.
_X_WCONF_RE = re.compile(r"x_wconf\s+(\d+)")


def _parse_bbox(title):
    """
    :param str|None title: An hOCR element's `title` attribute.
    :returns: [x0, y0, x1, y1], or None if the title has no bbox.
    :rtype: list[int]|None
    """
    match = _BBOX_RE.search(title or "")
    return [int(v) for v in match.groups()] if match else None


def _parse_confidence(title):
    """
    :param str|None title: An hOCR word span's `title` attribute.
    :returns: Word confidence (0.0-100.0), or 0.0 if the title has no x_wconf.
    :rtype: float
    """
    match = _X_WCONF_RE.search(title or "")
    return float(match.group(1)) if match else 0.0


def _elements_by_class(root, class_name):
    """Every descendant of `root` (root included) with an exact `class` match.

    hOCR nests classes strictly (ocr_page > ocr_carea > ocr_par > ocr_line >
    ocrx_word), so scoping this search to a subtree's direct owner (e.g.
    calling this on a carea element to find its ocr_par children) never
    crosses into a sibling carea's paragraphs.

    :param xml.etree.ElementTree.Element root:
    :param str class_name:
    :rtype: list[xml.etree.ElementTree.Element]
    """
    return [el for el in root.iter() if el.get("class") == class_name]


# hOCR 1.1's line-level classes -- all structurally identical (a direct
# container of ocrx_word spans), differing only in semantic role. Tesseract
# emits ocr_caption for large/stylized text near a photo or graphic (e.g. a
# yearbook title-page caption) instead of the generic ocr_line -- confirmed
# against a real derivatives run (2026-08-28) where a page mixing a photo
# with a caption produced zero words entirely because only "ocr_line" was
# recognized, silently dropping legible, high-confidence text. ocr_header/
# ocr_footer/ocr_textfloat are the same class of miss for other page layouts
# (running headers/footers, floating text blocks) even though none have been
# observed directly yet.
_LINE_CLASSES = {"ocr_line", "ocr_caption", "ocr_header", "ocr_footer", "ocr_textfloat"}


def _elements_by_classes(root, class_names):
    """Same as _elements_by_class(), but matching any of several classes.

    :param xml.etree.ElementTree.Element root:
    :param set[str] class_names:
    :rtype: list[xml.etree.ElementTree.Element]
    """
    return [el for el in root.iter() if el.get("class") in class_names]


def parse_hocr(hocr_path):
    """Parse an hOCR file's full word-level structure (bbox + confidence +
    text for every word, nested under its line/paragraph/region), for
    building a real text-layer output from -- unlike
    ocr_utils._word_confidences(), which only regex-extracts confidence
    values for the orientation A/B comparison.

    hOCR is XHTML; parsed via xml.etree.ElementTree with the default XHTML
    namespace stripped first (ElementTree's namespace-qualified tag/XPath
    handling adds friction for no benefit here, since we only ever match on
    the `class` attribute, not tag names).

    :param str hocr_path: Path to the hOCR output file.
    :returns: {"page_bbox": [x0,y0,x1,y1]|None, "careas": [{"bbox": [...],
        "paragraphs": [{"bbox": [...], "lines": [{"bbox": [...], "words":
        [{"bbox": [...], "confidence": float, "text": str}, ...]}, ...]},
        ...]}, ...]}
    :rtype: dict
    """
    with open(hocr_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    text = text.replace(' xmlns="http://www.w3.org/1999/xhtml"', "")
    root = ET.fromstring(text)

    page_elements = _elements_by_class(root, "ocr_page")
    page_bbox = _parse_bbox(page_elements[0].get("title")) if page_elements else None

    careas = []
    for carea_el in _elements_by_class(root, "ocr_carea"):
        paragraphs = []
        for par_el in _elements_by_class(carea_el, "ocr_par"):
            lines = []
            for line_el in _elements_by_classes(par_el, _LINE_CLASSES):
                words = [
                    {
                        "bbox": _parse_bbox(word_el.get("title")),
                        "confidence": _parse_confidence(word_el.get("title")),
                        "text": "".join(word_el.itertext()).strip(),
                    }
                    for word_el in _elements_by_class(line_el, "ocrx_word")
                ]
                lines.append({"bbox": _parse_bbox(line_el.get("title")), "words": words})
            paragraphs.append({"bbox": _parse_bbox(par_el.get("title")), "lines": lines})
        careas.append({"bbox": _parse_bbox(carea_el.get("title")), "paragraphs": paragraphs})

    return {"page_bbox": page_bbox, "careas": careas}


def _rescale_bbox(bbox, scale_factor):
    return [round(v / scale_factor) for v in bbox]


def build_djvu_xml(page_structure, *, scale_factor, dpi=None, confidence_threshold=None, page_size=None):
    """Build a djvu-text-layer XML document from a parsed hOCR structure,
    matching legacy image-utils' OBJECT/PARAM/HIDDENTEXT/PAGECOLUMN/REGION/
    PARAGRAPH/LINE/WORD schema. Words below `confidence_threshold` are
    dropped entirely (matching legacy's OCR-noise filtering); empty
    lines/paragraphs/regions left behind by that filtering are dropped too,
    rather than emitted as empty elements.

    Word coordinates are divided by `scale_factor` before being written --
    the hOCR was produced against an OCR-prep render at 2x the published
    "large" image's width (see digtk.models.digital_website.pipeline's
    build_ocr_products()), so word boxes need rescaling into the "large"
    image's own coordinate space, matching what a client-side reader
    displaying the "large" image expects to overlay text onto.

    :param dict page_structure: parse_hocr()'s return value.
    :param float scale_factor: OCR-prep render width / "large" image width
        (computed by the caller from actual values, not assumed to be a
        fixed constant -- see build_ocr_products()).
    :param int dpi: DPI value written into the PARAM element (default
        CONFIG["image"]["dpi"]) -- legacy always writes a fixed 300 here
        regardless of the OCR render's own DPI; this defaults to argonath's
        own dpi config, which is also 300 by default.
    :param float confidence_threshold: Words below this are omitted (default
        CONFIG["ocr"]["orientation_word_confidence_threshold"]).
    :param dict page_size: {"width", "height"} of the image the word coordinates
        are in (the "large" image), written as OBJECT's width/height attributes
        like legacy's -- readers use them to scale word boxes (e.g. dams' page
        search highlight boxes). Omitted when None.
    :returns: Serialized XML document (unicode string, no XML declaration).
    :rtype: str
    """
    dpi = dpi if dpi is not None else CONFIG["image"]["dpi"]
    confidence_threshold = (
        confidence_threshold
        if confidence_threshold is not None
        else CONFIG["ocr"]["orientation_word_confidence_threshold"]
    )

    object_attrs = {}
    if page_size:
        object_attrs = {"width": str(page_size["width"]), "height": str(page_size["height"])}
    object_el = ET.Element("OBJECT", object_attrs)
    ET.SubElement(object_el, "PARAM", {"name": "DPI", "value": str(dpi)})
    hiddentext_el = ET.SubElement(object_el, "HIDDENTEXT")
    pagecolumn_el = ET.SubElement(hiddentext_el, "PAGECOLUMN")

    for carea in page_structure.get("careas", []):
        region_el = ET.Element("REGION")
        for paragraph in carea.get("paragraphs", []):
            paragraph_el = ET.Element("PARAGRAPH")
            for line in paragraph.get("lines", []):
                surviving_words = [w for w in line.get("words", []) if w["confidence"] >= confidence_threshold]
                if not surviving_words:
                    continue
                line_el = ET.SubElement(paragraph_el, "LINE")
                for word in surviving_words:
                    x0, y0, x1, y1 = _rescale_bbox(word["bbox"], scale_factor)
                    # dams/fin's BookReaderModel.js (and the page-search ES
                    # indexer) both destructure this attribute as
                    # [left, bottom, right, top] -- i.e. descending y order
                    # (the larger/lower value in slot 2, the smaller/upper
                    # value in slot 4), the classic Internet Archive
                    # djvu.xml word-box convention. hOCR's own bbox is
                    # ascending (y0=top/small, y1=bottom/large), so the pair
                    # must be swapped here or the consumer's top/bottom
                    # cross over into a collapsed/negative-height box.
                    word_el = ET.SubElement(line_el, "WORD", {"coords": f"{x0},{y1},{x1},{y0}"})
                    word_el.text = word["text"]
            if len(paragraph_el):
                region_el.append(paragraph_el)
        if len(region_el):
            pagecolumn_el.append(region_el)

    return ET.tostring(object_el, encoding="unicode")


def _all_word_confidences(page_structure):
    return [
        word["confidence"]
        for carea in page_structure.get("careas", [])
        for paragraph in carea.get("paragraphs", [])
        for line in paragraph.get("lines", [])
        for word in line.get("words", [])
    ]


def _decile_histogram(confidences):
    """Bucket confidence values (0.0-100.0) into 10 equal-width deciles.

    :param list[float] confidences:
    :returns: 10 counts, index 0 = [0,10), index 9 = [90,100].
    :rtype: list[int]
    """
    buckets = [0] * 10
    for c in confidences:
        buckets[min(int(c // 10), 9)] += 1
    return buckets


def _variant_stats(page_structure, confidence_threshold):
    confidences = sorted(c for c in _all_word_confidences(page_structure) if c >= confidence_threshold)
    if not confidences:
        return {"count": 0, "avg": 0.0, "median": 0.0, "buckets": [0] * 10}
    count = len(confidences)
    return {
        "count": count,
        "avg": sum(confidences) / count,
        "median": confidences[count // 2],
        "buckets": _decile_histogram(confidences),
    }


def compute_ocr_stats(page_structures, *, selected, opts={}):
    """Build the legacy-shaped `ocr-stats.json` content: word-confidence
    count/avg/median/decile-histogram for both orientation candidates (not
    just the selected one -- legacy records both), plus which one was
    selected and the threshold config used to decide.

    Stats are computed over the same confidence-filtered word set that
    build_djvu_xml() actually publishes (words below the threshold are
    excluded from both), so these numbers describe what's really in the
    djvu file, not raw unfiltered OCR noise.

    :param dict page_structures: {"original": <parse_hocr() result>,
        "deskewed": <parse_hocr() result>}.
    :param str selected: "original"|"deskewed" -- which candidate's djvu was
        actually published.
    :param dict opts: 'confidence_threshold' (float, default
        CONFIG["ocr"]["orientation_word_confidence_threshold"]),
        'word_count_threshold' (int, default
        CONFIG["ocr"]["orientation_word_count_threshold"]), 'angle_threshold'
        (float, default CONFIG["ocr"]["orientation_angle_threshold"]),
        'angle' (float|None, default None) -- the measured skew angle, if
        known, recorded here as 'deskewAngle'.
    :returns: {"original": {count,avg,median,buckets}, "deskewed": {...},
        "selected": str, "config": {wordCountThreshold,
        wordConfidenceThreshold, angleThreshold, deskewAngle}}
    :rtype: dict
    """
    confidence_threshold = opts.get("confidence_threshold", CONFIG["ocr"]["orientation_word_confidence_threshold"])
    return {
        "original": _variant_stats(page_structures["original"], confidence_threshold),
        "deskewed": _variant_stats(page_structures["deskewed"], confidence_threshold),
        "selected": selected,
        "config": {
            "wordCountThreshold": opts.get("word_count_threshold", CONFIG["ocr"]["orientation_word_count_threshold"]),
            "wordConfidenceThreshold": confidence_threshold,
            "angleThreshold": opts.get("angle_threshold", CONFIG["ocr"]["orientation_angle_threshold"]),
            "deskewAngle": opts.get("angle"),
        },
    }
