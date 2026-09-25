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

Reads back the already-published OCR-prep image (Tesseract input) and
display image (vision input) that `digtk showcase image|pdf derivatives`
publish — doesn't rasterize or stage anything from bronze itself, so it
needs those commands to have already run.
"""
import json
import os
import tempfile

from digtk.lib import ai, derivative, fs, items, ocr_utils
from digtk.lib.schema import ARGONATH_NS
from digtk.models.digital_website import pipeline

_OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text on this page verbatim, preserving "
    "reading order and line breaks. Output only the transcription. If the page has "
    "no readable text, output exactly: [BLANK]"
)

# Distinct from image.py/pdf.py's own "derivativeOf"/"ocrPrepOf" relations —
# these describe a *further* derivative of the OCR-prep/display image, not
# the image itself.
HOCR_LINK_RELATION = f"{ARGONATH_NS}ocrGeometryOf"
TRANSCRIPT_LINK_RELATION = f"{ARGONATH_NS}transcriptOf"


def _silver_rel_path(item, *parts):
    """Silver rel_path (relative to the collection/time root) under this item's
    own location — see digtk.models.accessibility's identical helper.

    :param ItemLocation item: Resolved item location.
    :param str parts: Path segments to join after the item's own item_path.
    :rtype: str
    """
    subdir = "/".join(parts)
    return f"{item.item_path}/{subdir}" if item.item_path else subdir


def _run_both_engines(item, dir_parts, name_stem, multi, ocr_image, display_image,
                       source_files, opts, want_vision):
    """Run Tesseract (always) and vision OCR (unless want_vision is False)
    against one page/image, publishing hOCR + transcript + a small confidence
    record.

    :param ItemLocation item: Resolved item location.
    :param tuple dir_parts: Silver rel_path segments for the containing
        directory (before item.item_path prefixing) — e.g. ("ocr",) for a
        plain image, or (f"{ocr_prefix}ocr", page_tag) for one PDF page
        (that per-page directory already disambiguates, so `multi` is always
        False and `name_stem` unused for PDF pages — see pdf.py/
        pipeline.build_ocr_products()'s identical convention for the
        existing djvu/stats outputs in the same directory).
    :param str name_stem: output_name() stem — the bronze image's own
        filename stem for a plain image; ignored when multi is False.
    :param bool multi: Forwarded to pipeline.output_name().
    :param str ocr_image: cask path to the Tesseract-tuned OCR-prep image.
    :param str display_image: cask path to the display image for vision OCR
        (not the OCR-prep image — that's contrast-tuned for Tesseract, not a
        vision model or a human reader, same reasoning as accessibility.py's
        alt-text/layout image choice).
    :param list[str] source_files: Lineage sources beyond the images
        themselves (the bronze binary path).
    :param dict opts: run_ocr()'s own opts, forwarded to
        ocr_utils.run_tesseract() (lang/psm/oem/dpi/timeout).
    :param bool want_vision: Whether to also run vision OCR.
    :returns: {"tesseract_confidence": float, "hocr_path": "cask:/...",
        "transcript_path": "cask:/..."|None}
    :rtype: dict
    """
    write_opts = {"group": item.group, "time": item.time, "replace": True}

    with tempfile.TemporaryDirectory() as tmp_dir:
        local_image = os.path.join(tmp_dir, "page.jpg")
        with open(local_image, "wb") as f:
            f.write(fs.read(ocr_image))

        tess_result = ocr_utils.run_tesseract(local_image, os.path.join(tmp_dir, "page"), opts)
        with open(tess_result["hocr_path"], "rb") as f:
            hocr_data = f.read()

        hocr_rel = _silver_rel_path(item, *dir_parts, pipeline.output_name(name_stem, "ocr.hocr", multi))
        hocr_write = derivative.write(
            type="silver", collection=item.collection, rel_path=hocr_rel, data=hocr_data,
            source_files=[ocr_image] + source_files,
            opts={**write_opts, "link_relation": HOCR_LINK_RELATION},
        )

        transcript_path = None
        if want_vision:
            transcript = ai.chat_completion(_OCR_PROMPT, {"image": display_image})
            transcript_rel = _silver_rel_path(item, *dir_parts, pipeline.output_name(name_stem, "transcript.txt", multi))
            transcript_write = derivative.write(
                type="silver", collection=item.collection, rel_path=transcript_rel,
                data=transcript.encode("utf-8"), source_files=[display_image] + source_files,
                opts={**write_opts, "link_relation": TRANSCRIPT_LINK_RELATION},
            )
            transcript_path = transcript_write["path"]

        result_rel = _silver_rel_path(item, *dir_parts, pipeline.output_name(name_stem, "ocr-result.json", multi))
        derivative.write(
            type="silver", collection=item.collection, rel_path=result_rel,
            data=json.dumps({"tesseract_confidence": tess_result["confidence"]}).encode(),
            source_files=[ocr_image] + source_files,
            opts={**write_opts, "link_relation": HOCR_LINK_RELATION},
        )

    return {
        "tesseract_confidence": tess_result["confidence"],
        "hocr_path": hocr_write["path"],
        "transcript_path": transcript_path,
    }


def run_ocr(ark, opts={}):
    """Run Tesseract (word geometry) and, unless opts['vision'] is False, Qwen
    vision OCR (transcript) for every image binary, and every page of every
    PDF binary, in an item.

    :param str ark: Item ARK identifier.
    :param dict opts: 'vision' (bool, default True — set False to skip the
        Samwise vision call entirely: fast local dev without
        SAMWISE_API_KEY, or deliberate cost control on a large batch; not a
        quality gate, the default is always both), plus anything
        digtk.lib.ocr_utils.run_tesseract() accepts (lang/psm/oem/dpi/timeout).
    :returns: {"status": "success", "ark": str,
        "images": {filename: {"tesseract_confidence": float, "hocr_path":
            "cask:/...", "transcript_path": "cask:/..."|None}},
        "pdfs": {filename: {"pages": {page_no_str: {same shape as an image entry}}}}}
        — "images"/"pdfs" are only present when the item has that kind of binary.
    :rtype: dict
    :raises LookupError: If the ARK doesn't resolve to a bronze item.
    :raises ValueError: If the item has no image or PDF binaries.
    :raises FileNotFoundError: If `digtk showcase image derivatives` or
        `digtk showcase pdf derivatives` hasn't published this item's
        OCR-prep/display images yet.
    """
    item = items.resolve(ark)
    image_paths, pdf_paths = pipeline.partition_by_format(item.bronze_binary_paths)
    if not image_paths and not pdf_paths:
        raise ValueError(
            f"no image or PDF binaries found among this item's bronze sources: {item.bronze_binary_paths}"
        )
    multi_image = len(image_paths) > 1
    # Matches pdf.derivatives()'s own namespace_ocr condition exactly — see
    # pipeline.pdf_ocr_prep_images()'s docstring for why "mixed item" (also
    # has non-PDF binaries, e.g. this item's own image binaries) needs this
    # alongside "more than one PDF".
    namespace_pdf_ocr = len(pdf_paths) > 1 or len(pdf_paths) < len(item.bronze_binary_paths)
    want_vision = opts.get("vision", True)

    result = {"status": "success", "ark": ark}

    if image_paths:
        images = {}
        for bronze_path in image_paths:
            filename = bronze_path.rsplit("/", 1)[-1]
            stem = filename.rsplit(".", 1)[0]
            ocr_image = pipeline.image_ocr_prep_image(item, bronze_path, multi_image)
            images_dir = pipeline.derivative_dir(
                item, f"{pipeline.PUBLIC_FRONTEND_DIR}/images/{filename}/images",
            )
            display_image = f"{images_dir}/large.jpg"
            images[filename] = _run_both_engines(
                item, ("ocr",), stem, multi_image, ocr_image, display_image,
                [bronze_path], opts, want_vision,
            )
        result["images"] = images

    if pdf_paths:
        pdfs = {}
        for bronze_path in pdf_paths:
            filename = bronze_path.rsplit("/", 1)[-1]
            stem = filename.rsplit(".", 1)[0]
            page_images, count = pipeline.pdf_ocr_prep_images(item, bronze_path, namespace_pdf_ocr)
            ocr_prefix = f"{stem}/" if namespace_pdf_ocr else ""
            images_base = f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}/images"

            pages = {}
            for page_no, ocr_image in enumerate(page_images, start=1):
                tag = pipeline.page_tag(page_no - 1, count)
                page_display_dir = pipeline.derivative_dir(item, f"{images_base}/{page_no}")
                display_image = f"{page_display_dir}/large.jpg"
                pages[str(page_no)] = _run_both_engines(
                    item, (f"{ocr_prefix}ocr", tag), stem, False, ocr_image, display_image,
                    [bronze_path], opts, want_vision,
                )
            pdfs[filename] = {"pages": pages}
        result["pdfs"] = pdfs

    return result
