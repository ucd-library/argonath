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

from digtk.lib import derivative, fs, image_magick, items, ocr_utils, utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS
from digtk.models import alt_utils, figures, layout, ocr, pdf_inplace, pdf_tag, pdf_utils, prep

# Distinguishes this lineage from _LINK_RELATION_BY_SUBDIR's "derivativeOf"/
# "ocrPrepOf" (digtk.models.digital_website.image/.pdf) — these are accessibility
# metadata derived from a source binary, not themselves a viewable/OCR derivative.
ALT_TEXT_LINK_RELATION = f"{ARGONATH_NS}altTextOf"
LAYOUT_LINK_RELATION = f"{ARGONATH_NS}layoutOf"
TAGGED_PDF_LINK_RELATION = f"{ARGONATH_NS}taggedPdfOf"
SEARCHABLE_PDF_LINK_RELATION = f"{ARGONATH_NS}searchablePdfOf"
DOCUMENT_TEXT_LINK_RELATION = f"{ARGONATH_NS}documentTextOf"

# Not for Tesseract ('lang' is BCP-47 here).
_NON_TESSERACT_OPTS = {"lang", "min_conf", "files", "reuse_ocr", "correct", "skip_existing",
                       "name", "title", "base_sources", "alt_paths", "mode", "embed_fonts",
                       "figures", "geometry"}
TAG_MODES = ("auto", "inplace", "rebuild")
GEOMETRIES = ("tesseract", "qwen")


def _check_geometry(opts):
    """Raise on an unknown text-layer geometry.

    :raises ValueError: If opts['geometry'] isn't one of GEOMETRIES.
    """
    geometry = opts.get("geometry", "tesseract")
    if geometry not in GEOMETRIES:
        raise ValueError(f"unknown geometry {geometry!r} (use one of {', '.join(GEOMETRIES)})")


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
    :returns: {"alt": str, "description": str|None, "long_description": str|None,
        "flags": [str], "path": "cask:/..."}
    :rtype: dict
    """
    alt, description, long_description, flags = alt_utils.generate_alt(vision_image, metadata, opts)
    doc = {"alt": alt, "description": description, "long_description": long_description, "flags": flags}
    payload = json.dumps(doc, indent=2).encode()
    write_result = derivative.write(
        type=item.dest_type, collection=item.collection, rel_path=rel_path, data=payload,
        source_files=source_files,
        opts={**items.dest_opts(item), "replace": True, "link_relation": ALT_TEXT_LINK_RELATION},
    )
    return {**doc, "path": write_result["path"]}


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
        want_description, want_long), 'skip_existing' (bool — skip pages
        whose alt-text.json exists), plus 'model' forwarded to the Samwise chat call —
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
                out_dir = ("accessibility",)
                out_filename = prep.output_name(page["stem"], "alt-text.json", multi_image)
                page_metadata = metadata
            else:
                out_dir = ("accessibility", page["filename"], str(page["page_no"]))
                out_filename = "alt-text.json"
                page_metadata = {**metadata, "page": (page["page_no"], page["count"])}
            rel_path = items.dest_rel(item, *out_dir, out_filename)

            existing = items.dest_dir(item, "/".join(out_dir)) + "/" + out_filename
            if opts.get("skip_existing") and fs.exists(existing):
                utils.log(f"{label}: alt text exists, skipping")
                page_result = {"skipped": "outputs exist", "path": existing}
                if page["kind"] == "image":
                    images[page["filename"]] = page_result
                else:
                    pdfs.setdefault(page["filename"], {"pages": {}})["pages"][str(page["page_no"])] = page_result
                continue

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


def _page_words(item, page, tmp_dir, opts):
    """Word geometry, display image and text for one PDF page.

    Reuses the `ocr` step's hOCR when it was made with the same geometry
    settings; otherwise runs orientation and Tesseract here.

    :param ItemLocation item: Resolved item location.
    :param dict page: A prep.stage_pages() entry.
    :param str tmp_dir: Local working directory.
    :param dict opts: 'tesseract_opts' (dict), 'reuse_ocr' (bool),
        'correct' (bool), 'flat' (bool — never deskew and skip the display
        image, for OCR laid over an original page), 'geometry' ('tesseract'
        default, or 'qwen' — see _vision_page_words()).
    :returns: {'display' (None when flat), 'words', 'text', 'sources',
        'reused' (bool), 'changes' (int|None — None when not corrected)}
    :rtype: dict
    """
    tesseract_opts = opts["tesseract_opts"]
    flat = opts.get("flat", False)
    if opts.get("geometry") == "qwen":
        return _vision_page_words(item, page, tmp_dir, opts)
    page_ocr = ocr.read_page_ocr(item, page) if opts.get("reuse_ocr", True) else None
    record = page_ocr["record"] if page_ocr else {}
    reused = ("deskew" in record and record.get("prep") == prep.geometry_opts(tesseract_opts)
              and not (flat and record["deskew"]))
    sources = []
    if reused:
        deskew, words = record["deskew"], page_ocr["words"]
        sources.append(page_ocr["hocr_path"])
    else:
        hocr_base = os.path.join(tmp_dir, f"{page['stem']}-{page['page_tag']}")
        if flat:
            deskew, ocr_path = False, f"{hocr_base}-flat.jpg"
            image_magick.prep_ocr(page["local_path"], ocr_path, {**tesseract_opts, "deskew": False})
        else:
            prepped = prep.ocr_image(page, tmp_dir, tesseract_opts)
            deskew, ocr_path = prepped["deskew"], prepped["local_path"]
        tess_result = ocr_utils.run_tesseract(ocr_path, hocr_base, tesseract_opts)
        words = pdf_utils.hocr_words(tess_result["hocr_path"])

    changes = None
    if opts.get("correct") and reused and page_ocr["transcript"]:
        corrected = ocr.corrected_words(item, page, page_ocr)
        words, changes = corrected["words"], len(corrected["changes"])
        sources.append(corrected["path"])

    transcript = page_ocr["transcript"] if reused else None
    return {
        "display": None if flat else prep.display_image(page, tmp_dir, {**tesseract_opts, "deskew": deskew}),
        "words": words,
        "text": transcript.strip() if transcript else " ".join(w["text"] for w in words),
        "sources": sources,
        "reused": reused,
        "changes": changes,
    }


def _vision_page_words(item, page, tmp_dir, opts):
    """Text layer for one page from Qwen's line boxes instead of Tesseract (handwriting).

    The page is never deskewed, so the same render serves as the display image
    and as the image the boxes refer to.

    :param ItemLocation item: Resolved item location.
    :param dict page: A prep.stage_pages() entry.
    :param str tmp_dir: Local working directory.
    :param dict opts: _page_words() opts.
    :returns: _page_words()'s shape; 'changes' is 0 with 'correct' (the text
        is already the model's reading).
    :rtype: dict
    """
    tesseract_opts = opts["tesseract_opts"]
    display = prep.display_image(page, tmp_dir, {**tesseract_opts, "deskew": False})
    vision = ocr.vision_lines(item, page, display, tmp_dir, tesseract_opts)
    return {
        "display": None if opts.get("flat") else display,
        "words": vision["lines"],
        "text": "\n".join(line["text"] for line in vision["lines"]),
        "sources": [vision["path"]] if vision["path"] else [],
        "reused": False,
        "changes": 0 if opts.get("correct") else None,
    }


def _publish_file(item, local_path, rel_parts, source_files, link_relation):
    """Publish a local file as an item derivative.

    :param ItemLocation item: Resolved item location.
    :param str local_path: File to publish.
    :param tuple rel_parts: Path segments under the item.
    :param list[str] source_files: Lineage sources.
    :param str link_relation: Lineage link relation.
    :returns: Published 'cask:/...' path.
    :rtype: str
    """
    with open(local_path, "rb") as f:
        data = f.read()
    return derivative.write(
        type=item.dest_type, collection=item.collection,
        rel_path=items.dest_rel(item, *rel_parts), data=data, source_files=source_files,
        opts={**items.dest_opts(item), "replace": True, "link_relation": link_relation},
    )["path"]


def _document_outputs(item, name):
    """CaskFS paths of a document's published outputs.

    :param ItemLocation item: Resolved item location.
    :param str name: Output directory name under accessibility/.
    :rtype: dict
    """
    base = items.dest_dir(item, f"accessibility/{name}")
    return {key: f"{base}/{key}.{ext}" for key, ext in
            (("tagged", "pdf"), ("searchable", "pdf"), ("text", "txt"))}


def _skip_if_built(item, name, opts):
    """With 'skip_existing', a skip result when a document's outputs all exist.

    :param ItemLocation item: Resolved item location.
    :param str name: Output directory name under accessibility/.
    :param dict opts: Step opts.
    :returns: {"skipped": reason, ...output paths}, or None to build it.
    :rtype: dict|None
    """
    if not opts.get("skip_existing"):
        return None
    outputs = _document_outputs(item, name)
    if not all(fs.exists(p) for p in outputs.values()):
        return None
    utils.log(f"{name}: outputs exist, skipping")
    return {"skipped": "outputs exist", "path": outputs["tagged"],
            "searchable_path": outputs["searchable"], "text_path": outputs["text"]}


def _build_document(item, pages, tmp_dir, opts):
    """Build, tag and publish one searchable, tagged document from staged pages.

    Shared by tag_pdf() (one document per PDF) and build_pdf() (an item's
    images as one document). Publishes tagged.pdf, searchable.pdf and text.txt.

    :param ItemLocation item: Resolved item location.
    :param list[dict] pages: prep.stage_pages() entries, in page order.
    :param str tmp_dir: Local working directory.
    :param dict opts: tag_pdf() opts, plus 'name' (output dir under
        accessibility/), 'title', 'base_sources' (lineage), 'alt_paths'
        (per-page alt-text.json paths).
    :returns: tag_pdf()'s per-PDF result shape.
    :rtype: dict
    """
    name, title = opts["name"], opts["title"]
    min_conf = opts.get("min_conf", CONFIG["accessibility"]["min_word_confidence"])
    # Tesseract gets its own 'lang' from CONFIG
    tesseract_opts = {k: v for k, v in opts.items() if k not in _NON_TESSERACT_OPTS}
    page_opts = {"tesseract_opts": tesseract_opts, "reuse_ocr": opts.get("reuse_ocr", True),
                 "correct": bool(opts.get("correct")), "geometry": opts.get("geometry", "tesseract")}
    source_files = list(opts["base_sources"])

    local_images, words_per_page, texts = [], [], []
    ocr_stats = {"reused_ocr_pages": 0, "corrections": 0}
    uncorrected = []
    for page_no, page in enumerate(pages, start=1):
        page_words = _page_words(item, page, tmp_dir, page_opts)
        local_images.append(page_words["display"])
        words_per_page.append(pdf_utils.filter_min_conf(page_words["words"], min_conf))
        texts.append(page_words["text"])
        source_files += page_words["sources"]
        ocr_stats["reused_ocr_pages"] += int(page_words["reused"])
        if page_words["changes"] is None:
            uncorrected.append(page_no)
        else:
            ocr_stats["corrections"] += page_words["changes"]
    if page_opts["correct"]:
        ocr_stats["uncorrected_pages"] = uncorrected
        if uncorrected:
            utils.warn(f"{name}: no reusable OCR + transcript for page(s) {uncorrected}; left uncorrected")

    searchable_path = os.path.join(tmp_dir, f"{pages[0]['stem']}-searchable.pdf")
    pdf_utils.build_searchable_pdf(local_images, words_per_page, searchable_path, opts={
        "dpi": opts.get("dpi", CONFIG["image"]["dpi"]), "title": title,
    })
    text_path = os.path.join(tmp_dir, f"{pages[0]['stem']}-text.txt")
    with open(text_path, "w", encoding="utf-8") as f:
        f.write("\f".join(texts) + "\n")  # form feed between pages
    searchable_sources = list(source_files)

    tag_opts = _alt_and_layouts(item, pages, tmp_dir, source_files, opts)
    return _tag_and_publish(item, pdf_tag.tag_scan_pdf, {
        **opts, **tag_opts, "tmp_dir": tmp_dir, "searchable_path": searchable_path,
        "text_path": text_path, "source_files": source_files,
        "searchable_sources": searchable_sources, "stats": ocr_stats,
    })


def _vision_images(pages, tmp_dir, opts):
    """Resized JPEGs of each page for vision calls (prep.vision_image()).

    Full rasters are too large to send; boxes are fractions, so size doesn't matter.

    :param list[dict] pages: The document's pages; PDF pages are rasterized if needed.
    :param str tmp_dir: Local working directory.
    :param dict opts: prep.vision_image() and rasterize opts.
    :rtype: list[str]
    """
    images = []
    for page in pages:
        if page["kind"] == "pdf":
            prep.rasterize(page, tmp_dir, opts)
        images.append(prep.vision_image(page, tmp_dir, opts))
    return images


def _alt_and_layouts(item, pages, tmp_dir, source_files, opts):
    """Read each page's alt text, plus (with 'tables') its layout and (with 'figures') its figures.

    Missing layouts and figures are made and cached, from resized page images.

    :param ItemLocation item: Resolved item location.
    :param list[dict] pages: The document's pages.
    :param str tmp_dir: Local working directory.
    :param list[str] source_files: Lineage list; read derivatives are appended.
    :param dict opts: 'name', 'alt_paths', 'tables', 'figures', plus
        layout.page_layout() and figures.describe() opts.
    :returns: {'alt': [str|None], 'layouts': list|None, 'figures': list|None}
    :rtype: dict
    """
    name = opts["name"]
    images = _vision_images(pages, tmp_dir, opts) if opts.get("tables") or opts.get("figures") else []
    alt = []
    for alt_path in opts["alt_paths"]:
        alt_doc = _read_json_derivative(alt_path)
        alt.append((alt_doc or {}).get("alt") or None)
        if alt_doc:
            source_files.append(alt_path)

    layouts = None
    if opts.get("tables"):
        layouts = []
        for page_no, (page, local_path) in enumerate(zip(pages, images), start=1):
            layout_path = items.dest_dir(item, f"accessibility/{name}/{page_no}") + "/layout.json"
            lay = _read_json_derivative(layout_path)
            if layout.is_current(lay):
                source_files.append(layout_path)
            else:
                lay = layout.page_layout(local_path, opts)
                if lay is not None:
                    layout_write = derivative.write(
                        type=item.dest_type, collection=item.collection,
                        rel_path=items.dest_rel(item, "accessibility", name, str(page_no), "layout.json"),
                        data=json.dumps(lay).encode(), source_files=[page["source_path"]],
                        opts={**items.dest_opts(item), "replace": True, "link_relation": LAYOUT_LINK_RELATION},
                    )
                    source_files.append(layout_write["path"])
            layouts.append(lay)
    specs = _figure_specs(item, pages, images, alt, opts) if opts.get("figures") else None
    return {"alt": alt, "layouts": layouts, "figures": specs}


def _figure_specs(item, pages, images, alt, opts):
    """Per-page figure specs for the tagger, detecting pages not yet done.

    Newly detected figures are appended to each source file's review CSV.

    :param ItemLocation item: Resolved item location.
    :param list[dict] pages: The document's pages.
    :param list[str] images: Local page images.
    :param list alt: Per-page alt text (one page: reused for its main figure).
    :param dict opts: figures.describe() opts.
    :rtype: list[list[dict]]
    """
    fig_opts = {k: v for k, v in opts.items() if k not in ("figures", "alt", "layouts")}
    reviews, new_rows, specs = {}, {}, []
    for page, image in zip(pages, images):
        filename = page["filename"]
        if filename not in reviews:
            reviews[filename] = figures.read_review(item, filename)
        page_alt = alt[0] if len(pages) == 1 else None
        figs, fresh = figures.page_figures(item, page, image, {
            **fig_opts, "review": reviews[filename], "page_alt": page_alt})
        if fresh:
            new_rows.setdefault(filename, (page["source_path"], []))[1].extend(
                dict(f, page=page["page_no"] or 1) for f in figs)
        specs.append(figures.for_tagger(figs))
    for filename, (source_path, rows) in new_rows.items():
        figures.append_review(item, filename, source_path, rows)
    return specs


def _tag_and_publish(item, tagger, opts):
    """Tag a searchable PDF, validate it, and publish the document's three outputs.

    :param ItemLocation item: Resolved item location.
    :param tagger: pdf_tag.tag_scan_pdf or pdf_inplace.tag_pdf_inplace.
    :param dict opts: Step opts plus 'tmp_dir', 'searchable_path', 'text_path',
        'source_files', 'searchable_sources', 'stats', 'alt', 'layouts',
        'figures' (per-page specs or None).
    :rtype: dict
    """
    name = opts["name"]
    tagged_path = os.path.splitext(opts["searchable_path"])[0] + "-tagged.pdf"
    stats = tagger(opts["searchable_path"], tagged_path, opts["title"], {
        "alt": opts["alt"], "layouts": opts["layouts"], "figures": opts.get("figures"),
        "lang": opts.get("lang", "en-US"),
        "strict": opts.get("strict", False),
        "lookahead": opts.get("lookahead", pdf_tag.DEFAULT_LOOKAHEAD),
        "paragraphs": bool(opts.get("paragraphs")),
    })

    compliance = {}
    if opts.get("validate", True):
        compliance = pdf_tag.validate_ua(tagged_path, opts={"verapdf": CONFIG["accessibility"]["verapdf_bin"]})

    out_dir = ("accessibility", name)
    searchable_sources = opts["searchable_sources"]
    return {
        **stats, **compliance, **opts["stats"],
        "path": _publish_file(item, tagged_path, (*out_dir, "tagged.pdf"),
                              opts["source_files"], TAGGED_PDF_LINK_RELATION),
        "searchable_path": _publish_file(item, opts["searchable_path"], (*out_dir, "searchable.pdf"),
                                         searchable_sources, SEARCHABLE_PDF_LINK_RELATION),
        "text_path": _publish_file(item, opts["text_path"], (*out_dir, "text.txt"),
                                   searchable_sources, DOCUMENT_TEXT_LINK_RELATION),
    }


def _embed_fonts(pdf_path, tmp_dir, stats):
    """Rewrite a PDF with Ghostscript when it uses unembedded fonts.

    :param str pdf_path: Local PDF.
    :param str tmp_dir: Local working directory.
    :param dict stats: Result stats; gets 'fonts_embedded' or 'fonts_not_embedded'.
    :returns: The PDF to tag (the rewrite, or the original).
    :rtype: str
    """
    missing = pdf_inplace.unembedded_fonts(pdf_path)
    if not missing:
        return pdf_path
    embedded = os.path.splitext(pdf_path)[0] + "-embedded.pdf"
    if pdf_inplace.embed_fonts(pdf_path, embedded, {"gs": CONFIG["accessibility"]["gs_bin"]}):
        stats["fonts_embedded"] = missing
        return embedded
    utils.warn(f"{os.path.basename(pdf_path)}: fonts not embedded (fails PDF/UA): {', '.join(missing)}")
    stats["fonts_not_embedded"] = missing
    return pdf_path


def _build_inplace_document(item, pages, tmp_dir, opts):
    """Tag one PDF in place: original pages kept, OCR text added to scan pages.

    Pages with extractable text keep it; pages without get an invisible OCR
    layer from a non-deskewed render so its boxes match the page.

    :param ItemLocation item: Resolved item location.
    :param list[dict] pages: stage_pages() entries of one PDF, unrasterized.
    :param str tmp_dir: Local working directory.
    :param dict opts: _build_document() opts plus 'text_flags' (one bool per
        page) and 'embed_fonts' (bool, default True).
    :returns: tag_pdf()'s per-PDF result shape, plus 'mode': 'inplace' and
        'ocr_pages' (pages given an OCR layer).
    :rtype: dict
    """
    name, title = opts["name"], opts["title"]
    min_conf = opts.get("min_conf", CONFIG["accessibility"]["min_word_confidence"])
    tesseract_opts = {k: v for k, v in opts.items() if k not in _NON_TESSERACT_OPTS | {"text_flags"}}
    page_opts = {"tesseract_opts": tesseract_opts, "reuse_ocr": opts.get("reuse_ocr", True),
                 "correct": bool(opts.get("correct")), "flat": True,
                 "geometry": opts.get("geometry", "tesseract")}
    stats = {"mode": "inplace", "ocr_pages": 0, "reused_ocr_pages": 0, "corrections": 0}
    source_files = list(opts["base_sources"])

    working = pages[0]["pdf_path"]
    if opts.get("embed_fonts", True):
        working = _embed_fonts(working, tmp_dir, stats)

    texts = pdf_inplace.page_texts(working)
    widths = pdf_inplace.page_widths(working)
    scale = 1.0 / prep.geometry_opts(tesseract_opts)["size"]
    layers, uncorrected = {}, []
    for index, (page, has_text) in enumerate(zip(pages, opts["text_flags"])):
        if has_text:
            continue
        prep.rasterize(page, tmp_dir, tesseract_opts)
        page_words = _page_words(item, page, tmp_dir, page_opts)
        layers[index] = {"words": pdf_utils.filter_min_conf(page_words["words"], min_conf),
                         "scale": widths[index] * scale}
        texts[index] = page_words["text"]
        source_files += page_words["sources"]
        stats["ocr_pages"] += 1
        stats["reused_ocr_pages"] += int(page_words["reused"])
        if page_words["changes"] is None:
            uncorrected.append(index + 1)
        else:
            stats["corrections"] += page_words["changes"]
    if page_opts["correct"]:
        stats["uncorrected_pages"] = uncorrected

    searchable_path = os.path.join(tmp_dir, f"{pages[0]['stem']}-searchable.pdf")
    pdf_utils.add_text_layer(working, searchable_path, layers, {"title": title})
    text_path = os.path.join(tmp_dir, f"{pages[0]['stem']}-text.txt")
    with open(text_path, "w", encoding="utf-8") as f:
        f.write("\f".join(texts) + "\n")  # form feed between pages
    searchable_sources = list(source_files)

    tag_opts = _alt_and_layouts(item, pages, tmp_dir, source_files, opts)
    return _tag_and_publish(item, pdf_inplace.tag_pdf_inplace, {
        **opts, **tag_opts, "tmp_dir": tmp_dir, "searchable_path": searchable_path,
        "text_path": text_path, "source_files": source_files,
        "searchable_sources": searchable_sources, "stats": stats,
    })


def tag_pdf(target, opts={}):
    """Assemble a searchable, PDF/UA-1-tagged PDF for every PDF binary in an item.

    Reuses the `ocr` step's hOCR when it was made with the same geometry
    settings, else builds its own OCR-prep image and runs Tesseract. The PDF
    embeds the unbinarized render (prep.display_image()). Publishes
    tagged.pdf, searchable.pdf and text.txt under accessibility/<pdf>/. Alt text
    (generate_alt_text's output) and, with opts['tables'], a vision-generated
    layout (layout.page_layout(), cached as its own derivative) are read back
    if present and used to enrich tagging — neither is a hard prerequisite.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, 'cask:/workspace/...'
        item path, or a resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'lang' (str, default 'en-US' — the *document* language
        for pdf_tag.tag_scan_pdf, e.g. 'en-US'; NOT Tesseract's language code,
        see below), 'strict' (bool, default False), 'lookahead' (int, default
        pdf_tag.DEFAULT_LOOKAHEAD), 'tables' (bool, default False — enables
        the per-page vision layout pass), 'paragraphs' (bool, default False —
        one /P per layout text block; needs 'tables'), 'validate' (bool, default True —
        run veraPDF on the tagged PDF), 'dpi' (float, default
        CONFIG["image"]["dpi"] — pixel->point scale for the OCR-prep images'
        word geometry, forwarded to build_searchable_pdf, run_tesseract and
        image_magick.prep_ocr), 'enhance' (None|'auto'|'gray' — contrast
        boost for the OCR render, see image_magick.prep_ocr()), 'min_conf' (float, default
        CONFIG["accessibility"]["min_word_confidence"] — drop words below
        this hOCR confidence (0-100) from the searchable-PDF text layer
        instead of baking in obviously-wrong low-confidence guesses; see
        pdf_utils.hocr_words()), plus anything else
        digtk.lib.ocr_utils.run_tesseract() accepts (psm/oem/timeout) — its
        own 'lang' (a Tesseract language code like 'eng', a different thing
        from this opts['lang']) is deliberately never forwarded and always
        resolves to CONFIG["ocr"]["lang"] instead. 'files' (set[str], optional)
        limits the run to those PDF filenames. 'reuse_ocr' (bool, default
        True); 'correct' (bool, default False — use ocr.corrected_words()
        on pages with reused OCR and a transcript). 'figures' (bool — tag
        in-page figures, detecting pages not yet done; see figures.py);
        'geometry' ('tesseract' default, or 'qwen' for handwriting — the
        text layer from vision line boxes). 'mode' ('auto' default —
        in place when any page has extractable text, else rebuild; 'inplace';
        'rebuild'); 'embed_fonts' (bool, default True — in place only).
    :returns: {"status": "success", "ark": str|None, "item": str (see
        items.item_id()), "pdfs": {filename: {**tag_scan_pdf stats,
        **validate_ua result (if opts['validate']), "path": "cask:/...",
        "searchable_path", "text_path", "reused_ocr_pages" (int),
        "corrections" (int), "uncorrected_pages" ([int], with 'correct')}}}
    :rtype: dict
    :raises LookupError: If the target doesn't resolve to an item.
    :raises ValueError: If the item has no PDF binaries.
    """
    item = items.resolve_target(target)
    _, pdf_paths = prep.partition_by_format(item.bronze_binary_paths)
    if not pdf_paths:
        raise ValueError(f"no PDF binaries found among this item's sources: {item.bronze_binary_paths}")

    metadata = items.metadata_for(item)

    files = opts.get("files")
    pdfs = {}
    for path in pdf_paths:
        filename = os.path.basename(path)
        if not files or filename in files:
            skipped = _skip_if_built(item, filename, opts)
            if skipped:
                pdfs[filename] = skipped
    todo = {os.path.basename(p) for p in pdf_paths if not files or os.path.basename(p) in files} - set(pdfs)
    if not todo:
        return {"status": "success", "ark": item.ark, "item": items.item_id(item), "pdfs": pdfs}

    _check_geometry(opts)
    mode = opts.get("mode", "auto")
    if mode not in TAG_MODES:
        raise ValueError(f"unknown tag-pdf mode {mode!r} (use one of {', '.join(TAG_MODES)})")
    with tempfile.TemporaryDirectory() as tmp_dir:
        pages_by_pdf = {}
        stage_opts = {"formats": {"pdf"}, "files": todo if pdfs else files, "rasterize": False}
        for page in prep.stage_pages(item, tmp_dir, opts=stage_opts):
            pages_by_pdf.setdefault(page["source_path"], []).append(page)

        for source_path, pages in pages_by_pdf.items():
            filename = pages[0]["filename"]
            alt_paths = [items.dest_dir(item, f"accessibility/{filename}/{page['page_no']}") + "/alt-text.json"
                         for page in pages]
            doc_opts = {
                **opts, "name": filename, "title": metadata.get("title") or item.ark or pages[0]["stem"],
                "base_sources": [source_path] + _sidecar_sources(item), "alt_paths": alt_paths,
            }
            text_flags = pdf_inplace.text_pages(pages[0]["pdf_path"]) if mode != "rebuild" else []
            # auto: in place if any text
            if mode == "inplace" or (mode == "auto" and any(text_flags)):
                utils.log(f"{filename}: tagging in place ({sum(text_flags)}/{len(pages)} page(s) with text)")
                pdfs[filename] = _build_inplace_document(item, pages, tmp_dir, {**doc_opts, "text_flags": text_flags})
                continue
            for page in pages:
                prep.rasterize(page, tmp_dir, opts)
            pdfs[filename] = {**_build_document(item, pages, tmp_dir, doc_opts), "mode": "rebuild"}

    return {"status": "success", "ark": item.ark, "item": items.item_id(item), "pdfs": pdfs}


# build_pdf()'s output dir under accessibility/.
DOCUMENT_NAME = "document"


def build_pdf(target, opts={}):
    """Build an item's page images into one searchable, PDF/UA-1-tagged PDF.

    Pages are the item's image binaries in natural filename order. Each page
    goes through the same OCR reuse, correction and tagging as tag_pdf().

    :param str|digtk.lib.items.ItemLocation target: Item ARK, 'cask:/workspace/...'
        item path, or a resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: tag_pdf() opts ('files' is ignored: the document is
        always the whole item), plus 'skip_existing' (bool).
    :returns: {"status": "success", "ark", "item", "document": {tag_pdf()'s
        per-PDF result shape, or {"skipped": reason, ...paths}}}
    :rtype: dict
    :raises LookupError: If the target doesn't resolve to an item.
    :raises ValueError: If the item has no image binaries.
    """
    _check_geometry(opts)
    item = items.resolve_target(target)
    image_paths, _ = prep.partition_by_format(item.bronze_binary_paths)
    if not image_paths:
        raise ValueError(f"no image binaries found among this item's sources: {item.bronze_binary_paths}")
    result = {"status": "success", "ark": item.ark, "item": items.item_id(item)}
    skipped = _skip_if_built(item, DOCUMENT_NAME, opts)
    if skipped:
        return {**result, "document": skipped}

    metadata = items.metadata_for(item)
    multi = len(image_paths) > 1
    with tempfile.TemporaryDirectory() as tmp_dir:
        pages = prep.stage_pages(item, tmp_dir, opts={"formats": {"image"}})
        pages.sort(key=lambda p: prep.natural_key(p["filename"]))
        alt_dir = items.dest_dir(item, "accessibility")
        alt_paths = [f"{alt_dir}/{prep.output_name(p['stem'], 'alt-text.json', multi)}" for p in pages]
        utils.log(f"Building {len(pages)} page(s) into one PDF")
        result["document"] = _build_document(item, pages, tmp_dir, {
            **{k: v for k, v in opts.items() if k != "files"},
            "name": DOCUMENT_NAME, "title": metadata.get("title") or item.ark or items.item_id(item),
            "base_sources": [p["source_path"] for p in pages] + _sidecar_sources(item),
            "alt_paths": alt_paths,
        })
    return result


def detect_figures(target, opts={}):
    """Detect and describe the figures on every image and PDF page of an item.

    Writes figures.json and a crop per figure for each page, and appends new
    pages to each source file's figure-review.csv. Pages already cached or
    reviewed cost nothing.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, 'cask:/workspace/...'
        item path, or a resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: figures.describe() opts (collection_context,
        transcribe_text, max_alt_chars, ...), 'vision_width' (see
        prep.vision_image()), 'files' (set[str], optional).
    :returns: {"status": "success"|"partial", "ark", "item",
        "files": {filename: {"pages": {page_no_str: {"figures": int,
            "tagged": int, "fresh": bool}}, "review_path": str|None}},
        "failed": [{"file", "page", "stage": "figures", "error"}]}
    :rtype: dict
    :raises LookupError: If the target doesn't resolve to an item.
    :raises ValueError: If the item has no image or PDF binaries.
    :raises RuntimeError: If every page failed.
    """
    item = items.resolve_target(target)
    image_paths, pdf_paths = prep.partition_by_format(item.bronze_binary_paths)
    if not image_paths and not pdf_paths:
        raise ValueError(f"no image or PDF binaries found among this item's sources: {item.bronze_binary_paths}")
    metadata = items.metadata_for(item)
    fig_opts = {**opts, "title": metadata.get("title") or item.ark or items.item_id(item)}

    files, failed, new_rows = {}, [], {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        pages = prep.stage_pages(item, tmp_dir, opts={"files": opts.get("files")})
        reviews = {}
        for page in pages:
            filename = page["filename"]
            page_no = page["page_no"] or 1
            if filename not in reviews:
                reviews[filename] = figures.read_review(item, filename)
            try:
                image = prep.vision_image(page, tmp_dir, opts)
                figs, fresh = figures.page_figures(item, page, image, {**fig_opts, "review": reviews[filename]})
            except Exception as e:  # noqa: BLE001 — skip just this page
                utils.warn(f"{filename} page {page_no}: figures failed, skipping page: {e}")
                failed.append({"file": filename, "page": page["page_no"], "stage": "figures", "error": str(e)})
                continue
            utils.log(f"{filename} page {page_no}: {len(figs)} figure(s){'' if fresh else ' (cached)'}")
            if fresh:
                new_rows.setdefault(filename, (page["source_path"], []))[1].extend(
                    dict(f, page=page_no) for f in figs)
            files.setdefault(filename, {"pages": {}, "review_path": None})["pages"][str(page_no)] = {
                "figures": len(figs), "tagged": len(figures.for_tagger(figs)), "fresh": fresh,
            }

    for filename, (source_path, rows) in new_rows.items():
        files[filename]["review_path"] = figures.append_review(item, filename, source_path, rows)
    if pages and not files:
        raise RuntimeError(f"figures failed for all {len(pages)} page(s): {failed[0]['error']}")
    return {"status": "partial" if failed else "success", "ark": item.ark,
            "item": items.item_id(item), "files": files, "failed": failed}
