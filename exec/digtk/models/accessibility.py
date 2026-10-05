"""
Accessibility derivative generation — the Dagster/CLI-facing orchestrators
around the pike-tools accessibility modules (alt_utils, pdf_utils, pdf_tag)
and the ported vision-layout generator (layout), none of which were wired
into any command before this.

generate_alt_text(): AI-generated ADA/WCAG alt text. Resolves the target (an
ARK or a staged workspace item — see digtk.lib.items.resolve_target()), reads
the item's descriptive metadata (title/description/date/creator) for prompt
context when it has a bronze sidecar, and generates alt text for each image
binary and each page of each PDF binary, against a plain resized JPEG built
from the source binary (digtk.models.prep.vision_image()) — not the raw
binary (could be a huge TIFF) or an OCR-prep image (contrast-tuned for
Tesseract, not human/vision-model viewing). Each result is written as its own
derivative, with CaskFS lineage back to the source binary and, if present,
the bronze sidecar the metadata came from.

tag_pdf(): searchable + PDF/UA-1 tagged PDF assembly. For each PDF binary,
rasterizes every page, builds a Tesseract-tuned OCR-prep image per page
(digtk.models.prep.ocr_image()), runs Tesseract for hOCR word geometry,
assembles a searchable PDF, optionally overlays alt text (generate_alt_text's
output, if present) and a vision-generated table/heading layout (layout.py,
cached as its own derivative), tags it PDF/UA-1, validates it with veraPDF,
and publishes the tagged PDF with lineage back to the source PDF and any
alt-text/layout derivatives read.

Neither entry point reads anything `digtk showcase image|pdf derivatives`
publishes — both prepare their own inputs in-process (see digtk.models.prep).
"""
import json
import os
import tempfile

from digtk.lib import derivative, fs, items, ocr_utils, utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS
from digtk.models import alt_utils, layout, pdf_tag, pdf_utils, prep

# Distinguishes this lineage from _LINK_RELATION_BY_SUBDIR's "derivativeOf"/
# "ocrPrepOf" (digtk.models.digital_website.image/.pdf) — these are accessibility
# metadata derived from a source binary, not themselves a viewable/OCR derivative.
ALT_TEXT_LINK_RELATION = f"{ARGONATH_NS}altTextOf"
LAYOUT_LINK_RELATION = f"{ARGONATH_NS}layoutOf"
TAGGED_PDF_LINK_RELATION = f"{ARGONATH_NS}taggedPdfOf"


def _generate_and_write(item, vision_image, source_files, rel_path, metadata, opts):
    """Generate alt text for one image and publish it as a derivative.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str vision_image: Local path to the resized JPEG (prep.vision_image())
        — passed straight through to alt_utils.generate_alt() as the vision input.
    :param list[str] source_files: Lineage sources — the source binary, plus the
        bronze sidecar `metadata` came from when there is one.
    :param str rel_path: rel_path (items.dest_rel()) to publish to.
    :param dict metadata: alt_utils.build_alt_prompt() metadata (title/description/
        date/creator, plus 'page' for a PDF page).
    :param dict opts: alt_utils.generate_alt() opts.
    :returns: {"alt": str, "description": str|None, "flags": [str], "path": "cask:/..."}
    :rtype: dict
    """
    alt, description, flags = alt_utils.generate_alt(vision_image, metadata, opts)
    payload = json.dumps({"alt": alt, "description": description, "flags": flags}, indent=2).encode()
    write_result = derivative.write(
        type=item.dest_type, collection=item.collection, rel_path=rel_path, data=payload,
        source_files=source_files,
        opts={**items.dest_opts(item), "replace": True, "link_relation": ALT_TEXT_LINK_RELATION},
    )
    return {"alt": alt, "description": description, "flags": flags, "path": write_result["path"]}


def _sidecar_sources(item):
    """The bronze sidecar as a lineage source, when the item has one.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: [sidecar_path], or [] for a workspace item.
    :rtype: list[str]
    """
    return [item.sidecar_path] if item.sidecar_path else []


def generate_alt_text(target, opts={}):
    """Generate and publish AI alt text for every image binary, and every page of
    every PDF binary, in an item.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, 'cask:/workspace/...'
        item path, or a resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: alt_utils.build_alt_prompt() opts (collection_context, name_people,
        transcribe_text, tone, language, max_alt_chars, extra_instructions,
        want_description), plus 'model' forwarded to the Samwise chat call —
        see alt_utils.generate_alt() — 'vision_width' (see prep.vision_image()), and
        'files' (set[str], optional — only these source filenames; naming still
        follows the whole item, see digtk.models.ocr.run_ocr()).
    :returns: {"status": "success"|"partial", "ark": str|None, "item": str
        (see items.item_id()),
        "images": {filename: {"alt": str, "description": str|None, "flags":
            [str], "path": "cask:/..."}},
        "pdfs": {filename: {"pages": {page_no_str: {same shape as an image entry}}}},
        "failed": [{"file": str, "page": int|None, "stage": "alt-text", "error": str}]}
        — "images"/"pdfs" are only present when the item has that kind of
        binary. One page failing (e.g. Samwise still unreachable after
        digtk.lib.ai's own retries) doesn't fail the item: that page is left
        out, listed in "failed" (and logged via utils.warn()), and status is
        "partial". Re-running the command redoes every page.
    :rtype: dict
    :raises LookupError: If the target doesn't resolve to an item.
    :raises ValueError: If the item has no image or PDF binaries.
    :raises RuntimeError: If every page failed.
    """
    item = items.resolve_target(target)
    image_paths, pdf_paths = prep.partition_by_format(item.bronze_binary_paths)
    if not image_paths and not pdf_paths:
        raise ValueError(
            f"no image or PDF binaries found among this item's sources: {item.bronze_binary_paths}"
        )
    multi_image = len(image_paths) > 1
    metadata = items.metadata_for(item)

    result = {"status": "success", "ark": item.ark, "item": items.item_id(item)}
    images, pdfs, failed = {}, {}, []

    with tempfile.TemporaryDirectory() as tmp_dir:
        pages = prep.stage_pages(item, tmp_dir, opts={"files": opts.get("files")})
        for page in pages:
            label = (f"{page['filename']} page {page['page_no']}/{page['count']}"
                     if page["kind"] == "pdf" else page["filename"])
            utils.log(f"{label}: alt text...")
            source_files = [page["source_path"]] + _sidecar_sources(item)
            if page["kind"] == "image":
                out_filename = prep.output_name(page["stem"], "alt-text.json", multi_image)
                rel_path = items.dest_rel(item, "accessibility", out_filename)
                page_metadata = metadata
            else:
                rel_path = items.dest_rel(item, "accessibility", page["filename"], str(page["page_no"]), "alt-text.json")
                page_metadata = {**metadata, "page": (page["page_no"], page["count"])}

            try:
                vision_image = prep.vision_image(page, tmp_dir, opts)
                page_result = _generate_and_write(item, vision_image, source_files, rel_path, page_metadata, opts)
            except Exception as e:  # noqa: BLE001 — skip just this page, keep the rest of the item
                utils.warn(f"{label}: alt text failed, skipping page: {e}")
                failed.append({"file": page["filename"], "page": page["page_no"], "stage": "alt-text", "error": str(e)})
                continue

            if page["kind"] == "image":
                images[page["filename"]] = page_result
            else:
                pdfs.setdefault(page["filename"], {"pages": {}})["pages"][str(page["page_no"])] = page_result

    if pages and not images and not pdfs:
        raise RuntimeError(f"alt text failed for all {len(pages)} page(s): {failed[0]['error']}")

    if image_paths:
        result["images"] = images
    if pdf_paths:
        result["pdfs"] = pdfs
    if failed:
        result["status"] = "partial"
    result["failed"] = failed
    return result


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


def tag_pdf(target, opts={}):
    """Assemble a searchable, PDF/UA-1-tagged PDF for every PDF binary in an item.

    Rasterizes each page and builds its own OCR-prep image (prep.ocr_image()),
    then runs Tesseract against it for hOCR word geometry. Alt text
    (generate_alt_text's output) and, with opts['tables'], a vision-generated
    layout (layout.page_layout(), cached as its own derivative) are read back
    if present and used to enrich tagging — neither is a hard prerequisite.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, 'cask:/workspace/...'
        item path, or a resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'lang' (str, default 'en-US' — the *document* language
        for pdf_tag.tag_scan_pdf, e.g. 'en-US'; NOT Tesseract's language code,
        see below), 'strict' (bool, default False), 'lookahead' (int, default
        pdf_tag.DEFAULT_LOOKAHEAD), 'tables' (bool, default False — enables
        the per-page vision layout pass), 'validate' (bool, default True —
        run veraPDF on the tagged PDF), 'dpi' (float, default
        CONFIG["image"]["dpi"] — pixel->point scale for the OCR-prep images'
        word geometry, forwarded to build_searchable_pdf, run_tesseract and
        image_magick.prep_ocr), 'min_conf' (float, default
        CONFIG["accessibility"]["min_word_confidence"] — drop words below
        this hOCR confidence (0-100) from the searchable-PDF text layer
        instead of baking in obviously-wrong low-confidence guesses; see
        pdf_utils.hocr_words()), plus anything else
        digtk.lib.ocr_utils.run_tesseract() accepts (psm/oem/timeout) — its
        own 'lang' (a Tesseract language code like 'eng', a different thing
        from this opts['lang']) is deliberately never forwarded and always
        resolves to CONFIG["ocr"]["lang"] instead. 'files' (set[str], optional)
        limits the run to those PDF filenames.
    :returns: {"status": "success", "ark": str|None, "item": str (see
        items.item_id()), "pdfs": {filename: {**tag_scan_pdf stats,
        **validate_ua result (if opts['validate']), "path": "cask:/..."}}}
    :rtype: dict
    :raises LookupError: If the target doesn't resolve to an item.
    :raises ValueError: If the item has no PDF binaries.
    """
    item = items.resolve_target(target)
    _, pdf_paths = prep.partition_by_format(item.bronze_binary_paths)
    if not pdf_paths:
        raise ValueError(f"no PDF binaries found among this item's sources: {item.bronze_binary_paths}")

    metadata = items.metadata_for(item)

    lang = opts.get("lang", "en-US")
    strict = opts.get("strict", False)
    lookahead = opts.get("lookahead", pdf_tag.DEFAULT_LOOKAHEAD)
    want_tables = bool(opts.get("tables"))
    want_validate = opts.get("validate", True)
    min_conf = opts.get("min_conf", CONFIG["accessibility"]["min_word_confidence"])

    # opts['lang'] above is the *document* language (BCP-47, e.g. "en-US") for
    # pdf_tag.tag_scan_pdf — a different thing from Tesseract's language code
    # (e.g. "eng") that ocr_utils.run_tesseract()'s own opts['lang'] expects.
    # opts['min_conf'] is pdf_utils.hocr_words()'s own filter, not a
    # run_tesseract() option either. Both excluded here so neither collides;
    # run_tesseract() falls back to CONFIG["ocr"]["lang"] on its own.
    # psm/oem/dpi/timeout still pass through for callers that want to
    # override Tesseract's behavior.
    tesseract_opts = {k: v for k, v in opts.items() if k not in ("lang", "min_conf", "files")}

    pdfs = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        pages_by_pdf = {}
        for page in prep.stage_pages(item, tmp_dir, opts={"formats": {"pdf"}, "files": opts.get("files")}):
            pages_by_pdf.setdefault(page["source_path"], []).append(page)

        for source_path, pages in pages_by_pdf.items():
            filename = pages[0]["filename"]
            count = pages[0]["count"]
            title = metadata.get("title") or item.ark or pages[0]["stem"]
            source_files = [source_path] + _sidecar_sources(item)

            local_images, words_per_page = [], []
            for page in pages:
                local_path = prep.ocr_image(page, tmp_dir, tesseract_opts)["local_path"]
                local_images.append(local_path)

                hocr_base = os.path.join(tmp_dir, f"{page['stem']}-{page['page_tag']}")
                tess_result = ocr_utils.run_tesseract(local_path, hocr_base, tesseract_opts)
                words_per_page.append(pdf_utils.hocr_words(tess_result["hocr_path"], min_conf=min_conf))

            searchable_path = os.path.join(tmp_dir, f"{pages[0]['stem']}-searchable.pdf")
            pdf_utils.build_searchable_pdf(local_images, words_per_page, searchable_path, opts={
                "dpi": opts.get("dpi", CONFIG["image"]["dpi"]), "title": title,
            })

            alt = []
            for page_no in range(1, count + 1):
                alt_path = items.dest_dir(item, f"accessibility/{filename}/{page_no}") + "/alt-text.json"
                alt_doc = _read_json_derivative(alt_path)
                if alt_doc:
                    alt.append(alt_doc.get("alt") or None)
                    source_files.append(alt_path)
                else:
                    alt.append(None)

            layouts = None
            if want_tables:
                layouts = []
                for page_no, local_path in zip(range(1, count + 1), local_images):
                    layout_path = items.dest_dir(item, f"accessibility/{filename}/{page_no}") + "/layout.json"
                    lay = _read_json_derivative(layout_path)
                    if lay is not None:
                        source_files.append(layout_path)
                    else:
                        lay = layout.page_layout(local_path, opts)
                        if lay is not None:
                            layout_write = derivative.write(
                                type=item.dest_type, collection=item.collection,
                                rel_path=items.dest_rel(item, "accessibility", filename, str(page_no), "layout.json"),
                                data=json.dumps(lay).encode(), source_files=[source_path],
                                opts={
                                    **items.dest_opts(item), "replace": True,
                                    "link_relation": LAYOUT_LINK_RELATION,
                                },
                            )
                            source_files.append(layout_write["path"])
                    layouts.append(lay)

            tagged_path = os.path.join(tmp_dir, f"{pages[0]['stem']}-tagged.pdf")
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
                type=item.dest_type, collection=item.collection,
                rel_path=items.dest_rel(item, "accessibility", filename, "tagged.pdf"),
                data=tagged_data, source_files=source_files,
                opts={
                    **items.dest_opts(item), "replace": True,
                    "link_relation": TAGGED_PDF_LINK_RELATION,
                },
            )

            pdfs[filename] = {**stats, **compliance, "path": write_result["path"]}

    return {"status": "success", "ark": item.ark, "item": items.item_id(item), "pdfs": pdfs}
