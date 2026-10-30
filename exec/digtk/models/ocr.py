"""
Two-engine OCR — Tesseract for word geometry, Qwen vision for the
authoritative transcript. Orchestration pattern ported from the digitization
toolkit's reference implementation: "the tool always uses both: the vision
model for the words people read and search, Tesseract for where those words
sit on the page." Both engines run unconditionally for every image binary,
and every page of every PDF binary, in an item — this is not a
confidence-gated fallback. Tesseract's own word geometry is the only source
of it there is; Qwen's transcript is the authoritative "what does this page
say" answer regardless of Tesseract's confidence, which is recorded purely
as a diagnostic value, not a gate.

Prepares its own inputs from the item's source binaries (digtk.models.prep):
stages each image, rasterizes each PDF page, picks orientation and builds a
Tesseract-tuned OCR-prep image, plus a plain resized image for the vision
model — nothing from `digtk showcase image|pdf derivatives` is read or
required. Outputs land in the item's own text/ directory (silver for an ARK,
workspace for a staged workspace item — see digtk.lib.items.resolve_target()),
with lineage straight back to the source binary.
"""
import copy
import hashlib
import json
import os
import tempfile

import pymupdf

from digtk.lib import ai, derivative, fs, image_magick, items, ocr_utils, utils
from digtk.lib.schema import ARGONATH_NS
from digtk.models import pdf_inplace, pdf_utils, prep

_OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text on this page verbatim, preserving "
    "reading order and line breaks. Output only the transcription. If the page has "
    "no readable text, output exactly: [BLANK]"
)

_LINES_PROMPT = (
    "Perform OCR with layout on this page. Return ONLY a JSON array of text lines in "
    "natural reading order; each element is {\"text\": <verbatim line text>, "
    "\"bbox\": [x0, y0, x1, y1]} where bbox is the line's bounding box in 0-1000 "
    "normalized page coordinates (0,0 = top-left). Transcribe handwriting as written. "
    "If the page has no readable text, return []. No commentary."
)
_MIN_LINES_OK = 3  # fewer lines: re-ask at another size
_LINE_RETRY_PX = (1400, 2600)

# Distinct from image.py/pdf.py's own "derivativeOf"/"ocrPrepOf" relations —
# these describe OCR output derived from a source binary, not an image derivative.
HOCR_LINK_RELATION = f"{ARGONATH_NS}ocrGeometryOf"
LINES_LINK_RELATION = f"{ARGONATH_NS}visionLinesOf"
TRANSCRIPT_LINK_RELATION = f"{ARGONATH_NS}transcriptOf"
CORRECTED_LINK_RELATION = f"{ARGONATH_NS}correctedOcrOf"


# Output subdirectory for every OCR product — kept separate from showcase's own
# ocr/ directory (its OCR-prep images, djvu text layer, manifest.json).
TEXT_DIR = "text"


def page_location(item, page):
    """Where one page's OCR outputs live, following the whole item's naming.

    A PDF's text/ dir is namespaced by its stem when anything else in the
    item also writes text/; several images get stem-prefixed names.

    :param ItemLocation item: Resolved item location.
    :param dict page: A prep.stage_pages() entry.
    :returns: (dir_parts, name_stem, multi) — see _run_both_engines().
    :rtype: tuple
    """
    image_paths, pdf_paths = prep.partition_by_format(item.bronze_binary_paths)
    if page["kind"] == "image":
        return (TEXT_DIR,), page["stem"], len(image_paths) > 1
    namespace_pdf = len(pdf_paths) > 1 or len(pdf_paths) < len(item.bronze_binary_paths)
    text_dir = f"{page['stem']}/{TEXT_DIR}" if namespace_pdf else TEXT_DIR
    return (text_dir, page["page_tag"]), page["stem"], False


def page_output_path(item, page, base_name):
    """CaskFS path of one of a page's OCR outputs.

    :param ItemLocation item: Resolved item location.
    :param dict page: A prep.stage_pages() entry.
    :param str base_name: e.g. 'ocr.hocr', 'transcript.txt'.
    :rtype: str
    """
    dir_parts, stem, multi = page_location(item, page)
    return items.dest_dir(item, "/".join(dir_parts)) + "/" + prep.output_name(stem, base_name, multi)


def _read_optional(path):
    """Read a file, or return None if it doesn't exist.

    :param str path: 'cask:/...' path.
    :rtype: bytes|None
    """
    try:
        return fs.read(path)
    except FileNotFoundError:
        return None


def read_page_ocr(item, page):
    """Read back what `ocr tesseract` published for one page.

    :param ItemLocation item: Resolved item location.
    :param dict page: A prep.stage_pages() entry.
    :returns: {'words', 'transcript' (str|None), 'record' (ocr-result.json,
        or {}), 'hocr_path', 'hocr_sha1'}, or None when there's no hOCR.
    :rtype: dict|None
    """
    hocr_path = page_output_path(item, page, "ocr.hocr")
    hocr = _read_optional(hocr_path)
    if hocr is None:
        return None
    transcript = _read_optional(page_output_path(item, page, "transcript.txt"))
    record = _read_optional(page_output_path(item, page, "ocr-result.json"))
    return {
        "words": pdf_utils.parse_hocr_words(hocr.decode("utf-8", errors="replace")),
        "transcript": transcript.decode("utf-8", errors="replace") if transcript is not None else None,
        "record": json.loads(record) if record else {},
        "hocr_path": hocr_path,
        "hocr_sha1": hashlib.sha1(hocr).hexdigest(),
    }


def corrected_words(item, page, page_ocr, opts={}):
    """Get a page's corrected words, reusing a current ocr-corrected.json or making one.

    A cached correction counts only if it was made from the same hOCR.

    :param ItemLocation item: Resolved item location.
    :param dict page: A prep.stage_pages() entry.
    :param dict page_ocr: read_page_ocr() result with a transcript.
    :param dict opts: ocr_utils.correct_words() opts.
    :returns: {'words', 'changes', 'path', 'cached' (bool)}
    :rtype: dict
    """
    path = page_output_path(item, page, "ocr-corrected.json")
    cached = _read_optional(path)
    if cached is not None:
        doc = json.loads(cached)
        if doc.get("hocr_sha1") == page_ocr["hocr_sha1"]:
            return {"words": doc["words"], "changes": doc["changes"], "path": path, "cached": True}

    words = copy.deepcopy(page_ocr["words"])
    changes = ocr_utils.correct_words(words, page_ocr["transcript"], opts)
    dir_parts, stem, multi = page_location(item, page)
    sources = [page_ocr["hocr_path"], page_output_path(item, page, "transcript.txt")]
    write = derivative.write(
        type=item.dest_type, collection=item.collection,
        rel_path=items.dest_rel(item, *dir_parts, prep.output_name(stem, "ocr-corrected.json", multi)),
        data=json.dumps({"hocr_sha1": page_ocr["hocr_sha1"], "words": words, "changes": changes}).encode(),
        source_files=sources,
        opts={**items.dest_opts(item), "replace": True, "link_relation": CORRECTED_LINK_RELATION},
    )
    return {"words": words, "changes": changes, "path": write["path"], "cached": False}


def _run_both_engines(item, dir_parts, name_stem, multi, ocr_image, display_image,
                       source_files, opts, want_vision, record={}):
    """Run Tesseract (always) and vision OCR (unless want_vision is False)
    against one page/image, publishing hOCR + transcript + a small confidence
    record.

    :param ItemLocation item: Resolved item location.
    :param tuple dir_parts: rel_path segments for the containing directory
        (before items.dest_rel()'s item prefixing) — e.g. ("text",) for a plain
        image, or (f"{stem}/text", page_tag) for one PDF page (that per-page
        directory already disambiguates, so `multi` is always False and
        `name_stem` unused for PDF pages).
    :param str name_stem: output_name() stem — the source image's own
        filename stem for a plain image; ignored when multi is False.
    :param bool multi: Forwarded to prep.output_name().
    :param str ocr_image: Local path to the Tesseract-tuned OCR-prep image.
    :param str|None display_image: Local path to the vision-model image (not
        the OCR-prep image — that's contrast-tuned for Tesseract, not a vision
        model or a human reader). None when want_vision is False.
    :param list[str] source_files: Lineage sources (the source binary path).
    :param dict opts: run_ocr()'s own opts, forwarded to
        ocr_utils.run_tesseract() (lang/psm/oem/dpi/timeout).
    :param bool want_vision: Whether to also run vision OCR.
    :param dict record: Extra fields for ocr-result.json (deskew, prep).
    :returns: {"tesseract_confidence": float, "hocr_path": "cask:/...",
        "transcript_path": "cask:/..."|None, "transcript_error": str|None} —
        a failed vision call (e.g. Samwise still unreachable after
        digtk.lib.ai's own retries) doesn't fail the page: its Tesseract
        output is still published, and the error is reported here instead.
    :rtype: dict
    """
    write_opts = {**items.dest_opts(item), "replace": True}

    with tempfile.TemporaryDirectory() as tmp_dir:
        tess_result = ocr_utils.run_tesseract(ocr_image, os.path.join(tmp_dir, "page"), opts)
        with open(tess_result["hocr_path"], "rb") as f:
            hocr_data = f.read()

    hocr_write = derivative.write(
        type=item.dest_type, collection=item.collection,
        rel_path=items.dest_rel(item, *dir_parts, prep.output_name(name_stem, "ocr.hocr", multi)),
        data=hocr_data, source_files=source_files,
        opts={**write_opts, "link_relation": HOCR_LINK_RELATION},
    )

    transcript_path = transcript_error = None
    if want_vision:
        try:
            transcript = ai.chat_completion(_OCR_PROMPT, {"image": display_image})
        except Exception as e:  # noqa: BLE001 — any vision failure just leaves this page without a transcript
            transcript_error = str(e)
        else:
            transcript_write = derivative.write(
                type=item.dest_type, collection=item.collection,
                rel_path=items.dest_rel(item, *dir_parts, prep.output_name(name_stem, "transcript.txt", multi)),
                data=transcript.encode("utf-8"), source_files=source_files,
                opts={**write_opts, "link_relation": TRANSCRIPT_LINK_RELATION},
            )
            transcript_path = transcript_write["path"]

    derivative.write(
        type=item.dest_type, collection=item.collection,
        rel_path=items.dest_rel(item, *dir_parts, prep.output_name(name_stem, "ocr-result.json", multi)),
        data=json.dumps({"tesseract_confidence": tess_result["confidence"], **record}).encode(),
        source_files=source_files,
        opts={**write_opts, "link_relation": HOCR_LINK_RELATION},
    )

    return {
        "tesseract_confidence": tess_result["confidence"],
        "hocr_path": hocr_write["path"],
        "transcript_path": transcript_path,
        "transcript_error": transcript_error,
    }


def _page_label(page):
    """
    :param dict page: A prep.stage_pages() entry.
    :returns: e.g. 'photo.tiff' or 'document.pdf page 3/21', for logs and failure records.
    :rtype: str
    """
    if page["kind"] == "pdf":
        return f"{page['filename']} page {page['page_no']}/{page['count']}"
    return page["filename"]

def _store_page(images, pdfs, page, page_result):
    """File a page's result under images or pdfs.

    :param dict images: {filename: result}, updated in place.
    :param dict pdfs: {filename: {"pages": {...}}}, updated in place.
    :param dict page: A prep.stage_pages() entry.
    :param dict page_result: The page's result.
    """
    if page["kind"] == "image":
        images[page["filename"]] = page_result
    else:
        pdfs.setdefault(page["filename"], {"pages": {}})["pages"][str(page["page_no"])] = page_result


def _existing_ocr(item, page, want_vision):
    """A skip result when a page's OCR outputs already exist.

    :param ItemLocation item: Resolved item location.
    :param dict page: A prep.stage_pages() entry.
    :param bool want_vision: Whether a transcript is also required.
    :returns: {"skipped", "hocr_path", "transcript_path"}, or None.
    :rtype: dict|None
    """
    hocr_path = page_output_path(item, page, "ocr.hocr")
    transcript_path = page_output_path(item, page, "transcript.txt")
    if not fs.exists(hocr_path) or (want_vision and not fs.exists(transcript_path)):
        return None
    return {"skipped": "outputs exist", "hocr_path": hocr_path,
            "transcript_path": transcript_path if want_vision else None}


def run_ocr(target, opts={}):
    """Run Tesseract (word geometry) and, unless opts['vision'] is False, Qwen
    vision OCR (transcript) for every image binary, and every page of every
    PDF binary, in an item.

    :param str|ItemLocation target: Item ARK, 'cask:/workspace/...' item path,
        or a resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'vision' (bool, default True — set False to skip the
        Samwise vision call entirely: fast local dev without
        SAMWISE_API_KEY, or deliberate cost control on a large batch; not a
        quality gate, the default is always both), 'vision_width' (see
        prep.vision_image()), plus anything digtk.lib.ocr_utils.run_tesseract()
        accepts (lang/psm/oem/dpi/timeout) and image_magick.prep_ocr() accepts
        (size/fuzz/quality/threshold/enhance) — forwarded to both the
        orientation comparison and the final Tesseract pass. 'skip_existing'
        (bool) skips pages whose hOCR (and transcript) already exist.
        PDF pages with extractable text are skipped unless 'ocr_text_pages'. 'files' (set[str],
        optional) limits the run to those source filenames; output naming
        still follows the whole item, so a per-file run writes exactly the
        paths a whole-item run would.
    :returns: {"status": "success"|"partial", "ark": str|None, "item": str
        (see items.item_id()),
        "images": {filename: {"tesseract_confidence": float, "hocr_path":
            "cask:/...", "transcript_path": "cask:/..."|None,
            "transcript_error": str|None, "deskew": bool}},
        "pdfs": {filename: {"pages": {page_no_str: {same shape as an image entry}}}},
        "failed": [{"file": str, "page": int|None, "stage": "ocr"|"transcript",
            "error": str}]}
        — "images"/"pdfs" are only present when the item has that kind of
        binary. One page failing doesn't fail the item: a page whose
        Tesseract/prep step raised is left out of images/pdfs, a page whose
        vision call failed keeps its Tesseract output but no transcript, and
        each is listed in "failed" (and logged via utils.warn()), with status
        "partial". Re-running the command redoes every page.
    :rtype: dict
    :raises LookupError: If the target doesn't resolve to an item.
    :raises ValueError: If the item has no image or PDF binaries.
    :raises RuntimeError: If every page failed outright (nothing was produced).
    """
    item = items.resolve_target(target)
    image_paths, pdf_paths = prep.partition_by_format(item.bronze_binary_paths)
    if not image_paths and not pdf_paths:
        raise ValueError(
            f"no image or PDF binaries found among this item's sources: {item.bronze_binary_paths}"
        )
    want_vision = opts.get("vision", True)
    tesseract_opts = {k: v for k, v in opts.items()
                      if k not in ("vision", "vision_width", "files", "skip_existing", "ocr_text_pages")}
    text_flags = {}  # staged PDF -> pages with text

    result = {"status": "success", "ark": item.ark, "item": items.item_id(item)}
    images, pdfs, failed = {}, {}, []

    with tempfile.TemporaryDirectory() as tmp_dir:
        pages = prep.stage_pages(item, tmp_dir, opts={"files": opts.get("files"), "rasterize": False})
        for page in pages:
            label = _page_label(page)
            if page["kind"] == "pdf" and not opts.get("ocr_text_pages"):
                if page["pdf_path"] not in text_flags:
                    text_flags[page["pdf_path"]] = pdf_inplace.text_pages(page["pdf_path"])
                if text_flags[page["pdf_path"]][page["page_no"] - 1]:
                    utils.log(f"{label}: page has text, skipping OCR")
                    _store_page(images, pdfs, page, {"skipped": "page has text"})
                    continue
            if opts.get("skip_existing"):
                done = _existing_ocr(item, page, want_vision)
                if done:
                    utils.log(f"{label}: OCR outputs exist, skipping")
                    _store_page(images, pdfs, page, done)
                    continue
            utils.log(f"{label}: ocr{' + transcript' if want_vision else ''}...")
            try:
                prep.rasterize(page, tmp_dir, tesseract_opts)
                ocr_image = prep.ocr_image(page, tmp_dir, tesseract_opts)
                display_image = prep.vision_image(page, tmp_dir, opts) if want_vision else None
                dir_parts, stem, multi = page_location(item, page)
                page_result = _run_both_engines(
                    item, dir_parts, stem, multi, ocr_image["local_path"],
                    display_image, [page["source_path"]], tesseract_opts, want_vision,
                    record={"deskew": ocr_image["deskew"], "prep": prep.geometry_opts(tesseract_opts)},
                )
            except Exception as e:  # noqa: BLE001 — skip just this page, keep the rest of the item
                utils.warn(f"{label}: OCR failed, skipping page: {e}")
                failed.append({"file": page["filename"], "page": page["page_no"], "stage": "ocr", "error": str(e)})
                continue

            if page_result["transcript_error"]:
                utils.warn(f"{label}: transcript failed, kept Tesseract output: {page_result['transcript_error']}")
                failed.append({
                    "file": page["filename"], "page": page["page_no"], "stage": "transcript",
                    "error": page_result["transcript_error"],
                })

            _store_page(images, pdfs, page, {**page_result, "deskew": ocr_image["deskew"]})

    if pages and not images and not pdfs:
        raise RuntimeError(f"OCR failed for all {len(pages)} page(s): {failed[0]['error']}")

    if image_paths:
        result["images"] = images
    if pdf_paths:
        result["pdfs"] = pdfs
    if failed:
        result["status"] = "partial"
    result["failed"] = failed
    return result


def correct_ocr(target, opts={}):
    """Correct every page's OCR words with its vision transcript and publish ocr-corrected.json.

    Reads what `ocr tesseract` already published; no Tesseract or vision
    calls. PDF pages with real text and pages without a transcript are
    skipped; scan pages without hOCR are listed in "failed".

    :param str|ItemLocation target: Item ARK, 'cask:/workspace/...' item path,
        or a resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: ocr_utils.correct_words() opts (min_sim,
        min_page_ratio), plus 'files' (set[str], optional) — only these
        source filenames.
    :returns: {"status": "success"|"partial", "ark", "item",
        "images": {filename: {"changes": int, "path": "cask:/..."} or {"skipped": reason}},
        "pdfs": {filename: {"pages": {page_no_str: {same shape}}}},
        "failed": [{"file", "page", "stage": "ocr-correct", "error"}]}
    :rtype: dict
    :raises LookupError: If the target doesn't resolve to an item.
    :raises ValueError: If the item has no image or PDF binaries.
    :raises RuntimeError: If every page failed (skips don't count).
    """
    item = items.resolve_target(target)
    image_paths, pdf_paths = prep.partition_by_format(item.bronze_binary_paths)
    if not image_paths and not pdf_paths:
        raise ValueError(
            f"no image or PDF binaries found among this item's sources: {item.bronze_binary_paths}"
        )
    correct_opts = {k: v for k, v in opts.items() if k in ("min_sim", "min_page_ratio")}

    result = {"status": "success", "ark": item.ark, "item": items.item_id(item)}
    images, pdfs, failed = {}, {}, []
    text_flags = {}  # staged PDF -> pages with text
    with tempfile.TemporaryDirectory() as tmp_dir:
        pages = prep.stage_pages(item, tmp_dir, opts={"files": opts.get("files"), "rasterize": False})
        for page in pages:
            label = _page_label(page)
            if page["kind"] == "pdf":
                if page["pdf_path"] not in text_flags:
                    text_flags[page["pdf_path"]] = pdf_inplace.text_pages(page["pdf_path"])
                if text_flags[page["pdf_path"]][page["page_no"] - 1]:
                    _store_page(images, pdfs, page, {"skipped": "page has text"})
                    continue
            try:
                page_ocr = read_page_ocr(item, page)
                if page_ocr is None:
                    raise LookupError("no OCR output; run `digtk ocr tesseract` first")
                if not page_ocr["transcript"]:
                    utils.log(f"{label}: no vision transcript, skipping correction")
                    _store_page(images, pdfs, page, {"skipped": "no vision transcript (ocr ran without vision)"})
                    continue
                corrected = corrected_words(item, page, page_ocr, correct_opts)
            except Exception as e:  # noqa: BLE001 — skip just this page
                utils.warn(f"{label}: OCR correction failed: {e}")
                failed.append({"file": page["filename"], "page": page["page_no"], "stage": "ocr-correct", "error": str(e)})
                continue
            utils.log(f"{label}: {len(corrected['changes'])} correction(s)")
            _store_page(images, pdfs, page, {"changes": len(corrected["changes"]), "path": corrected["path"]})

    if failed and len(failed) == len(pages):
        raise RuntimeError(f"OCR correction failed for all {len(pages)} page(s): {failed[0]['error']}")
    if image_paths:
        result["images"] = images
    if pdf_paths:
        result["pdfs"] = pdfs
    if failed:
        result["status"] = "partial"
    result["failed"] = failed
    return result


def _parse_lines(raw, width, height):
    """Parse a line-OCR answer into pixel-space lines.

    :param str raw: Model output.
    :param int width: Width of the page image the boxes refer to.
    :param int height: Its height.
    :returns: [{'text', 'bbox'}], or None when unusable.
    :rtype: list[dict]|None
    """
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    # boxes without text: unusable
    n_box, n_txt = raw.count('"bbox'), raw.count('"text"')
    if n_box >= 3 and n_txt * 2 < n_box:
        return None
    lines = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        box = it.get("bbox") or it.get("bbox_2d")
        txt = str(it.get("text") or "").strip()
        if not txt or not isinstance(box, list) or len(box) != 4:
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in box)
        except (TypeError, ValueError):
            continue
        lines.append({"text": txt, "bbox": [x0 * width / 1000.0, y0 * height / 1000.0,
                                            x1 * width / 1000.0, y1 * height / 1000.0]})
    return lines


def ocr_page_lines(image_path, tmp_dir, opts={}):
    """Vision OCR with line boxes, for handwriting and faint typescript.

    The answer depends on image size, so a thin one is re-asked at other
    sizes; the fullest wins. Boxes are pixels of image_path.

    :param str image_path: Local page image.
    :param str tmp_dir: Local directory for resized copies.
    :param dict opts: 'model' passed to the chat call.
    :returns: [{'text', 'bbox'}], or None when every answer was unusable.
    :rtype: list[dict]|None
    """
    pix = pymupdf.Pixmap(image_path)
    width, height = pix.width, pix.height
    pix = None
    chat_opts = {"max_tokens": 4096, "temperature": 0.0}
    if opts.get("model"):
        chat_opts["model"] = opts["model"]
    best = None
    stem = os.path.splitext(os.path.basename(image_path))[0]
    for px in (None, *_LINE_RETRY_PX):
        image = image_path
        if px is not None:
            image = os.path.join(tmp_dir, f"{stem}-lines-{px}.jpg")
            image_magick.thumbnail(image_path, image, px)
        lines = _parse_lines(ai.chat_completion(_LINES_PROMPT, {**chat_opts, "image": image}), width, height)
        if lines is not None and (best is None or len(lines) > len(best)):
            best = lines
        if best is not None and len(best) >= _MIN_LINES_OK:
            break
    return best


def vision_lines(item, page, image_path, tmp_dir, opts={}):
    """A page's vision line OCR, cached as qwen-lines.json next to its OCR output.

    The cache counts only when made with the same image geometry.

    :param ItemLocation item: Resolved item location.
    :param dict page: A prep.stage_pages() entry.
    :param str image_path: The non-deskewed page render the boxes refer to.
    :param str tmp_dir: Local working directory.
    :param dict opts: Geometry opts (prep.geometry_opts()) plus 'model'.
    :returns: {'lines', 'path' (cache path, or None when unusable)}
    :rtype: dict
    """
    geometry = prep.geometry_opts(opts)
    path = page_output_path(item, page, "qwen-lines.json")
    cached = _read_optional(path)
    if cached is not None:
        doc = json.loads(cached)
        if doc.get("prep") == geometry:
            return {"lines": doc["lines"], "path": path}
    lines = ocr_page_lines(image_path, tmp_dir, opts)
    if lines is None:  # don't cache; rerun asks again
        utils.warn(f"{_page_label(page)}: line OCR output unusable; page left without text")
        return {"lines": [], "path": None}
    dir_parts, stem, multi = page_location(item, page)
    derivative.write(
        type=item.dest_type, collection=item.collection,
        rel_path=items.dest_rel(item, *dir_parts, prep.output_name(stem, "qwen-lines.json", multi)),
        data=json.dumps({"prep": geometry, "lines": lines}, ensure_ascii=False).encode("utf-8"),
        source_files=[page["source_path"]],
        opts={**items.dest_opts(item), "replace": True, "link_relation": LINES_LINK_RELATION},
    )
    return {"lines": lines, "path": path}
