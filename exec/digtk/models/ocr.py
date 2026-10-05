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
import json
import os
import tempfile

from digtk.lib import ai, derivative, items, ocr_utils, utils
from digtk.lib.schema import ARGONATH_NS
from digtk.models import prep

_OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text on this page verbatim, preserving "
    "reading order and line breaks. Output only the transcription. If the page has "
    "no readable text, output exactly: [BLANK]"
)

# Distinct from image.py/pdf.py's own "derivativeOf"/"ocrPrepOf" relations —
# these describe OCR output derived from a source binary, not an image derivative.
HOCR_LINK_RELATION = f"{ARGONATH_NS}ocrGeometryOf"
TRANSCRIPT_LINK_RELATION = f"{ARGONATH_NS}transcriptOf"


# Output subdirectory for every OCR product — kept separate from showcase's own
# ocr/ directory (its OCR-prep images, djvu text layer, manifest.json).
TEXT_DIR = "text"


def _run_both_engines(item, dir_parts, name_stem, multi, ocr_image, display_image,
                       source_files, opts, want_vision):
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
        data=json.dumps({"tesseract_confidence": tess_result["confidence"]}).encode(),
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
        orientation comparison and the final Tesseract pass. 'files' (set[str],
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
    multi_image = len(image_paths) > 1
    # A PDF's per-page text/ directories are namespaced by its own filename stem
    # whenever anything else in the item also writes into text/ — more than one
    # PDF, or the item also has image binaries.
    namespace_pdf = len(pdf_paths) > 1 or len(pdf_paths) < len(item.bronze_binary_paths)
    want_vision = opts.get("vision", True)
    tesseract_opts = {k: v for k, v in opts.items() if k not in ("vision", "vision_width", "files")}

    result = {"status": "success", "ark": item.ark, "item": items.item_id(item)}
    images, pdfs, failed = {}, {}, []

    with tempfile.TemporaryDirectory() as tmp_dir:
        pages = prep.stage_pages(item, tmp_dir, opts={"files": opts.get("files")})
        for page in pages:
            label = _page_label(page)
            utils.log(f"{label}: ocr{' + transcript' if want_vision else ''}...")
            try:
                ocr_image = prep.ocr_image(page, tmp_dir, tesseract_opts)
                display_image = prep.vision_image(page, tmp_dir, opts) if want_vision else None
                if page["kind"] == "image":
                    dir_parts, stem, multi = (TEXT_DIR,), page["stem"], multi_image
                else:
                    text_dir = f"{page['stem']}/{TEXT_DIR}" if namespace_pdf else TEXT_DIR
                    dir_parts, stem, multi = (text_dir, page["page_tag"]), page["stem"], False
                page_result = _run_both_engines(
                    item, dir_parts, stem, multi, ocr_image["local_path"],
                    display_image, [page["source_path"]], tesseract_opts, want_vision,
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

            page_result = {**page_result, "deskew": ocr_image["deskew"]}
            if page["kind"] == "image":
                images[page["filename"]] = page_result
            else:
                pdfs.setdefault(page["filename"], {"pages": {}})["pages"][str(page["page_no"])] = page_result

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
