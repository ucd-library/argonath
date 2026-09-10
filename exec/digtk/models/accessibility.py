"""
Accessibility derivative generation — the Dagster/CLI-facing orchestrators
around the pike-tools accessibility modules (alt_utils, pdf_utils, pdf_tag)
and the ported vision-layout generator (layout), none of which were wired
into any command before this.

generate_alt_text(): AI-generated ADA/WCAG alt text. Resolves an ARK to its
bronze item location, reads the item's existing bronze descriptive metadata
(title/description/date/creator) for prompt context, and generates alt text
for each of the item's image binaries and each page of each PDF binary,
against the "large" display JPEG generate_derivatives (image items) or
`digtk public-dc pdf derivatives` (PDF items) already publishes — not the raw
bronze binary (could be a huge TIFF) or the OCR-prep image (contrast-tuned
for Tesseract, not human/vision-model viewing). Each result is written as
its own silver derivative, with CaskFS lineage back to both the display
image and the bronze sidecar the metadata came from.

tag_pdf(): searchable + PDF/UA-1 tagged PDF assembly. For each PDF binary,
stages the already-published per-page OCR-prep images, runs Tesseract fresh
for hOCR word geometry (pdf.derivatives() computes this internally too, but
only persists a converted djvu text layer, not the raw hOCR pdf_utils needs),
assembles a searchable PDF, optionally overlays alt text (generate_alt_text's
output, if present) and a vision-generated table/heading layout (layout.py,
cached as its own derivative), tags it PDF/UA-1, validates it with veraPDF,
and publishes the tagged PDF with lineage back to the bronze PDF, the
OCR-prep images used, and any alt-text/layout derivatives read.

Both entry points deliberately depend on `digtk public-dc image|pdf
derivatives` having already run — they read back published derivatives
rather than regenerating/rasterizing anything themselves, so neither needs
the still-unimplemented `digtk ocr tesseract` (phase 0.5) or AI metadata
draft (phase 0.6) commands.
"""
import json
import os
import tempfile

from digtk.lib import derivative, fs, items, ocr_utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS
from digtk.models import alt_utils, layout, pdf_tag, pdf_utils
from digtk.models.digital_website import pipeline

# Distinguishes this lineage from _LINK_RELATION_BY_SUBDIR's "derivativeOf"/
# "ocrPrepOf" (digtk.models.digital_website.image/.pdf) — these are derived
# from a display/OCR-prep image, not themselves a viewable/OCR derivative.
ALT_TEXT_LINK_RELATION = f"{ARGONATH_NS}altTextOf"
LAYOUT_LINK_RELATION = f"{ARGONATH_NS}layoutOf"
TAGGED_PDF_LINK_RELATION = f"{ARGONATH_NS}taggedPdfOf"


def _silver_rel_path(item, *parts):
    """Silver rel_path (relative to the collection/time root) under this item's
    own location — the item_path-prefixing every derivative.write() call in
    this module needs, factored out so it's written in exactly one place.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str parts: Path segments to join after the item's own item_path.
    :rtype: str
    """
    subdir = "/".join(parts)
    return f"{item.item_path}/{subdir}" if item.item_path else subdir


def _partition_by_format(bronze_binary_paths):
    """Split an item's bronze binaries into image and PDF subsets for alt-text purposes.

    Unlike pipeline.filter_binaries_by_format(), doesn't raise when a format has zero
    matches (a real item may have only one of the two, or — rarely — both), and
    doesn't raise on a binary of a format alt-text just doesn't apply to (video/audio,
    or something unrecognized) — silently skips it rather than enforcing every binary
    be a recognized format, which isn't this module's concern.

    :param list[str] bronze_binary_paths: CaskFS paths ('cask:/'-prefixed).
    :returns: (image_paths, pdf_paths), each a subset of bronze_binary_paths, in order.
    :rtype: tuple[list[str], list[str]]
    """
    image_paths, pdf_paths = [], []
    for path in bronze_binary_paths:
        ext = os.path.splitext(path)[1].lower()
        fmt = pipeline.FORMAT_BY_EXTENSION.get(ext)
        if fmt == "image":
            image_paths.append(path)
        elif fmt == "pdf":
            pdf_paths.append(path)
    return image_paths, pdf_paths


def _generate_and_write(item, display_image, sidecar_path, rel_path, metadata, opts):
    """Generate alt text for one display image and publish it as a silver derivative.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str display_image: 'cask:/...' path to the published display JPEG —
        passed straight through to alt_utils.generate_alt() as the vision input.
    :param str sidecar_path: 'cask:/...' path to the bronze sidecar `metadata` came from.
    :param str rel_path: Silver rel_path (relative to the collection/time root) to publish to.
    :param dict metadata: alt_utils.build_alt_prompt() metadata (title/description/
        date/creator, plus 'page' for a PDF page).
    :param dict opts: alt_utils.generate_alt() opts.
    :returns: {"alt": str, "description": str|None, "flags": [str], "path": "cask:/..."}
    :rtype: dict
    """
    alt, description, flags = alt_utils.generate_alt(display_image, metadata, opts)
    payload = json.dumps({"alt": alt, "description": description, "flags": flags}, indent=2).encode()
    write_result = derivative.write(
        type="silver", collection=item.collection, rel_path=rel_path, data=payload,
        source_files=[display_image, sidecar_path],
        opts={
            "group": item.group, "time": item.time, "replace": True,
            "link_relation": ALT_TEXT_LINK_RELATION,
        },
    )
    return {"alt": alt, "description": description, "flags": flags, "path": write_result["path"]}


def generate_alt_text(ark, opts={}):
    """Generate and publish AI alt text for every image binary, and every page of
    every PDF binary, in an item.

    :param str ark: Item ARK identifier.
    :param dict opts: alt_utils.build_alt_prompt() opts (collection_context,
        name_people, transcribe_text, tone, language, max_alt_chars,
        extra_instructions, want_description), plus 'model' forwarded to the
        Samwise chat call — see alt_utils.generate_alt().
    :returns: {"status": "success", "ark": str,
        "images": {filename: {"alt": str, "description": str|None, "flags":
            [str], "path": "cask:/..."}},
        "pdfs": {filename: {"pages": {page_no_str: {same shape as an image entry}}}}}
        — "images"/"pdfs" are only present when the item has that kind of binary.
    :rtype: dict
    :raises LookupError: If the ARK doesn't resolve to a bronze item (see items.resolve()).
    :raises ValueError: If the item has no image or PDF binaries.
    :raises FileNotFoundError: If a display image (from `digtk public-dc image
        derivatives`) or a PDF's aggregate images/ manifest.json (from `digtk
        public-dc pdf derivatives`) hasn't been published yet — see this module's
        docstring.
    """
    item = items.resolve(ark)
    image_paths, pdf_paths = _partition_by_format(item.bronze_binary_paths)
    if not image_paths and not pdf_paths:
        raise ValueError(
            f"no image or PDF binaries found among this item's bronze sources: {item.bronze_binary_paths}"
        )
    multi_image = len(image_paths) > 1

    sidecar_path = item.bronze_binary_paths[0] + ".jsonld.json"
    metadata = items.metadata(ark)

    result = {"status": "success", "ark": ark}

    if image_paths:
        images = {}
        for bronze_path in image_paths:
            filename = bronze_path.rsplit("/", 1)[-1]
            stem = filename.rsplit(".", 1)[0]
            images_dir = pipeline.derivative_dir(item, f"{pipeline.PUBLIC_FRONTEND_DIR}/images/{filename}/images")
            display_image = f"{images_dir}/large.jpg"

            out_filename = pipeline.output_name(stem, "alt-text.json", multi_image)
            rel_path = _silver_rel_path(item, "accessibility", out_filename)
            images[filename] = _generate_and_write(item, display_image, sidecar_path, rel_path, metadata, opts)
        result["images"] = images

    if pdf_paths:
        pdfs = {}
        for bronze_path in pdf_paths:
            filename = bronze_path.rsplit("/", 1)[-1]
            # Mirrors digtk.models.digital_website.pdf.derivatives()'s own
            # images_base/images_dir construction exactly, so this reads back
            # precisely what that command published — see its module docstring.
            images_base = f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}/images"
            images_base_dir = pipeline.derivative_dir(item, images_base)
            manifest = json.loads(fs.read(f"{images_base_dir}/manifest.json"))
            count = manifest["params"]["pages"]

            pages = {}
            for page_no in range(1, count + 1):
                page_dir = pipeline.derivative_dir(item, f"{images_base}/{page_no}")
                display_image = f"{page_dir}/large.jpg"
                page_metadata = {**metadata, "page": (page_no, count)}

                rel_path = _silver_rel_path(item, "accessibility", filename, str(page_no), "alt-text.json")
                pages[str(page_no)] = _generate_and_write(
                    item, display_image, sidecar_path, rel_path, page_metadata, opts
                )
            pdfs[filename] = {"pages": pages}
        result["pdfs"] = pdfs

    return result


def _pdf_ocr_prep_images(item, bronze_path, multi_pdf):
    """Read pdf.derivatives()'s aggregate OCR manifest for one PDF binary and
    return each page's already-published OCR-prep image cask path, in page order.

    Mirrors digtk.models.digital_website.pdf.derivatives()'s own ocr_prefix/
    page_tag/filename construction exactly (ocr_prefix = f"{stem}/" if
    multi_pdf else "", filename = f"{stem}-{page_tag}-{prep[-deskew]}.jpg"),
    so this reads back precisely what that command published.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str bronze_path: This PDF's bronze binary cask path.
    :param bool multi_pdf: True if the item has more than one PDF binary
        (pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")) —
        matches pdf.derivatives()'s own ocr_prefix convention exactly.
    :returns: (page image cask paths, in page order; page count)
    :rtype: tuple[list[str], int]
    :raises FileNotFoundError: If `digtk public-dc pdf derivatives` hasn't
        published this PDF's OCR-prep images yet.
    """
    filename = bronze_path.rsplit("/", 1)[-1]
    stem = filename.rsplit(".", 1)[0]
    ocr_prefix = f"{stem}/" if multi_pdf else ""
    ocr_base_dir = pipeline.derivative_dir(item, f"{ocr_prefix}ocr")
    manifest = json.loads(fs.read(f"{ocr_base_dir}/manifest.json"))
    count = manifest["params"]["pages"]
    orientation = manifest["params"]["orientation"]

    images = []
    for page_no in range(1, count + 1):
        tag = pipeline.page_tag(page_no - 1, count)
        deskewed = orientation[str(page_no)]["deskew_selected"]
        base_name = "prep-deskew.jpg" if deskewed else "prep.jpg"
        page_dir = pipeline.derivative_dir(item, f"{ocr_prefix}ocr/{tag}")
        images.append(f"{page_dir}/{stem}-{tag}-{base_name}")
    return images, count


def _read_json_derivative(path):
    """Best-effort read of an optional JSON derivative.

    :param str path: 'cask:/...' path.
    :returns: Parsed JSON, or None if the file doesn't exist.
    :rtype: dict|None
    """
    try:
        return json.loads(fs.read(path))
    except FileNotFoundError:
        return None


def tag_pdf(ark, opts={}):
    """Assemble a searchable, PDF/UA-1-tagged PDF for every PDF binary in an item.

    Requires `digtk public-dc pdf derivatives` to have already run (reads back
    its published per-page OCR-prep images). Re-runs Tesseract against those
    images for hOCR word geometry (pdf.derivatives() computes this internally
    too but only persists a converted djvu text layer). Alt text
    (generate_alt_text's output) and, with opts['tables'], a vision-generated
    layout (layout.page_layout(), cached as its own derivative) are read back
    if present and used to enrich tagging — neither is a hard prerequisite.

    :param str ark: Item ARK identifier.
    :param dict opts: 'lang' (str, default 'en-US' — the *document* language
        for pdf_tag.tag_scan_pdf, e.g. 'en-US'; NOT Tesseract's language code,
        see below), 'strict' (bool, default False), 'lookahead' (int, default
        pdf_tag.DEFAULT_LOOKAHEAD), 'tables' (bool, default False — enables
        the per-page vision layout pass), 'validate' (bool, default True —
        run veraPDF on the tagged PDF), 'dpi' (float, default
        CONFIG["image"]["dpi"] — pixel->point scale for the OCR-prep images'
        word geometry, forwarded to both build_searchable_pdf and
        run_tesseract), plus anything else digtk.lib.ocr_utils.run_tesseract()
        accepts (psm/oem/timeout) — its own 'lang' (a Tesseract language code
        like 'eng', a different thing from this opts['lang']) is deliberately
        never forwarded and always resolves to CONFIG["ocr"]["lang"] instead.
    :returns: {"status": "success", "ark": str, "pdfs": {filename: {**tag_scan_pdf
        stats, **validate_ua result (if opts['validate']), "path": "cask:/..."}}}
    :rtype: dict
    :raises LookupError: If the ARK doesn't resolve to a bronze item.
    :raises ValueError: If the item has no PDF binaries.
    :raises FileNotFoundError: If `digtk public-dc pdf derivatives` hasn't
        published this item's OCR-prep images yet.
    """
    item = items.resolve(ark)
    pdf_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")
    multi_pdf = len(pdf_paths) > 1

    sidecar_path = item.bronze_binary_paths[0] + ".jsonld.json"
    metadata = items.metadata(ark)
    title = metadata.get("title") or ark

    lang = opts.get("lang", "en-US")
    strict = opts.get("strict", False)
    lookahead = opts.get("lookahead", pdf_tag.DEFAULT_LOOKAHEAD)
    want_tables = bool(opts.get("tables"))
    want_validate = opts.get("validate", True)

    # opts['lang'] above is the *document* language (BCP-47, e.g. "en-US") for
    # pdf_tag.tag_scan_pdf — a different thing from Tesseract's language code
    # (e.g. "eng") that ocr_utils.run_tesseract()'s own opts['lang'] expects.
    # Excluded here so the two never collide; run_tesseract() falls back to
    # CONFIG["ocr"]["lang"] on its own. psm/oem/dpi/timeout still pass through
    # for callers that want to override Tesseract's behavior.
    tesseract_opts = {k: v for k, v in opts.items() if k != "lang"}

    pdfs = {}
    for bronze_path in pdf_paths:
        filename = bronze_path.rsplit("/", 1)[-1]
        page_images, count = _pdf_ocr_prep_images(item, bronze_path, multi_pdf)
        source_files = [bronze_path, sidecar_path]

        with tempfile.TemporaryDirectory() as tmp_dir:
            local_images, words_per_page = [], []
            for page_no, cask_image in enumerate(page_images, start=1):
                local_path = os.path.join(tmp_dir, f"page-{page_no:04d}.jpg")
                with open(local_path, "wb") as f:
                    f.write(fs.read(cask_image))
                local_images.append(local_path)
                source_files.append(cask_image)

                hocr_base = os.path.join(tmp_dir, f"page-{page_no:04d}")
                tess_result = ocr_utils.run_tesseract(local_path, hocr_base, tesseract_opts)
                words_per_page.append(pdf_utils.hocr_words(tess_result["hocr_path"]))

            searchable_path = os.path.join(tmp_dir, "searchable.pdf")
            pdf_utils.build_searchable_pdf(local_images, words_per_page, searchable_path, opts={
                "dpi": opts.get("dpi", CONFIG["image"]["dpi"]), "title": title,
            })

            alt = []
            for page_no in range(1, count + 1):
                alt_path = pipeline.derivative_dir(item, f"accessibility/{filename}/{page_no}") + "/alt-text.json"
                alt_doc = _read_json_derivative(alt_path)
                if alt_doc:
                    alt.append(alt_doc.get("alt") or None)
                    source_files.append(alt_path)
                else:
                    alt.append(None)

            layouts = None
            if want_tables:
                layouts = []
                for page_no, local_path, cask_image in zip(range(1, count + 1), local_images, page_images):
                    layout_path = pipeline.derivative_dir(item, f"accessibility/{filename}/{page_no}") + "/layout.json"
                    lay = _read_json_derivative(layout_path)
                    if lay is not None:
                        source_files.append(layout_path)
                    else:
                        lay = layout.page_layout(cask_image, opts)
                        if lay is not None:
                            derivative.write(
                                type="silver", collection=item.collection,
                                rel_path=_silver_rel_path(item, "accessibility", filename, str(page_no), "layout.json"),
                                data=json.dumps(lay).encode(), source_files=[cask_image],
                                opts={
                                    "group": item.group, "time": item.time, "replace": True,
                                    "link_relation": LAYOUT_LINK_RELATION,
                                },
                            )
                            source_files.append(cask_image)
                    layouts.append(lay)

            tagged_path = os.path.join(tmp_dir, "tagged.pdf")
            stats = pdf_tag.tag_scan_pdf(searchable_path, tagged_path, title, opts={
                "alt": alt, "layouts": layouts, "lang": lang,
                "strict": strict, "lookahead": lookahead,
            })

            compliance = {}
            if want_validate:
                compliance = pdf_tag.validate_ua(tagged_path, opts={
                    "verapdf": CONFIG["accessibility"]["verapdf_bin"],
                })

            with open(tagged_path, "rb") as f:
                tagged_data = f.read()
            write_result = derivative.write(
                type="silver", collection=item.collection,
                rel_path=_silver_rel_path(item, "accessibility", filename, "tagged.pdf"),
                data=tagged_data, source_files=source_files,
                opts={
                    "group": item.group, "time": item.time, "replace": True,
                    "link_relation": TAGGED_PDF_LINK_RELATION,
                },
            )

        pdfs[filename] = {**stats, **compliance, "path": write_result["path"]}

    return {"status": "success", "ark": ark, "pdfs": pdfs}
