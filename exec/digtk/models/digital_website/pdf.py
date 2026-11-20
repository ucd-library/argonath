"""
PDF item derivative-generation workflow.

Structurally parallel to digtk.models.digital_website.image, built on the same shared
digtk.models.digital_website.pipeline staging/publishing/lineage plumbing, but with one PDF
page rasterized (via digtk.lib.pdf_render, ImageMagick+Ghostscript) into a local image
before running through the exact same digtk.lib.image_magick/digtk.lib.ocr_utils steps
already used for plain image bronze items — a rasterized page is, by that point, just a
local image file.

One ARK covers the whole PDF; pages are never independently ARK-addressable. Each source
PDF gets its own directory under PUBLIC_FRONTEND_DIR, named after its own bronze
filename (e.g. `public-frontend-derivatives/document.pdf/`) — a multi-PDF item (more
than one PDF binary sharing an ARK) is disambiguated for free this way, no stem-prefix
scheme needed. Inside that, each page gets its own plain 1-indexed subdirectory
(`images/1/`, `images/2/`, ...) holding its own small/medium/large set and manifest,
alongside one aggregate manifest at the `images/` level summarizing every page's
orientation decision. OCR-prep images are unaffected by this and still live under a
zero-padded `ocr/page-NNN` (or `{stem}/ocr/page-NNN` for a multi-PDF item) silver
subdirectory, as before.

See docs/cask-conventions.md for the full silver layout, including how this differs from
the existing "Multi-Part Items" (parts/, per-page ARK) convention.
"""
import json
import os
import tempfile
import time

from digtk.lib import fs, image_magick, items, pdf_render, utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS
from digtk.models.digital_website import pipeline

_LINK_RELATION = {
    "derivatives": f"{ARGONATH_NS}derivativeOf",
    "ocr": f"{ARGONATH_NS}ocrPrepOf",
}


def _rasterize(pdf_path, page_index, tmp_dir, stem, page_tag, opts):
    """Rasterize one PDF page to a local image, named to avoid collisions across
    sources/pages sharing one temp directory.

    :returns: pdf_render.rasterize_page()'s own result — {"dest_path": str,
        "dpi": float, "width_px": int, "height_px": int}. derivatives() uses "dpi"
        for its manifest's "extractDensity" field; other callers just want
        "dest_path".
    :rtype: dict
    """
    raster_format = opts.get("raster_format", CONFIG["pdf"]["raster_format"])
    raster_path = os.path.join(tmp_dir, f"{stem}-{page_tag}-raster.{raster_format}")
    return pdf_render.rasterize_page(pdf_path, page_index, raster_path, opts)


def page_count(target, opts={}):
    """Report each source PDF's page count for an item — ad hoc/debug, no derivatives generated.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: Unused.
    :returns: {"status": "success", "item": str (item path), "pages": {stem: count}}
    :rtype: dict
    """
    item = items.resolve_target(target)
    pdf_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")
    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": pdf_paths})
        pages = {src["stem"]: pdf_render.page_count(src["local_path"], opts) for src in sources}
    return {"status": "success", "item": items.item_id(item), "pages": pages}


def thumbnail(target, opts={}):
    """Generate a small/medium/large set for one page of every source PDF in the item.

    Ad hoc/manual use, mirroring digtk.models.digital_website.image.thumbnails();
    derivatives() is what Dagster's gold_dcs_derivatives actually calls. Publishes into the
    same location derivatives() would for this one page:
    `public-frontend-derivatives/<pdf-filename>/images/<page-number>/` (1-indexed).

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'page' (int, default 0, 0-indexed), 'widths' (comma-separated str,
        default CONFIG["image"]["thumbnail_widths"]), 'quality' (int).
    :returns: {"status": "success", "item": str (item path), "pdfs": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}}
    :rtype: dict
    """
    page = opts.get("page", 0)
    widths = pipeline.parse_widths(opts.get("widths", CONFIG["image"]["thumbnail_widths"]))
    item = items.resolve_target(target)
    pdf_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")
    start = time.monotonic()

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": pdf_paths})
        pdfs = {}
        for src in sources:
            pdf_filename = os.path.basename(src["bronze_path"])
            raster_path = _rasterize(src["local_path"], page, tmp_dir, src["stem"], f"page-{page:03d}", opts)["dest_path"]
            thumb_set = pipeline.generate_thumbnail_set(raster_path, tmp_dir, widths, f"{src['stem']}-{page}", opts)
            local_outputs = [(fn, local_path, src["bronze_path"]) for fn, local_path in thumb_set]

            pdfs[pdf_filename] = pipeline.publish(
                item, f"{pipeline.PUBLIC_FRONTEND_DIR}/{pdf_filename}/images/{page + 1}", local_outputs,
                command=["digtk", "pdf", "thumbnail", items.item_id(item), "--page", str(page), "--widths", ",".join(map(str, widths))],
                params={"page": page, "widths": widths},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION["derivatives"],
                },
            )

    return {"status": "success", "item": items.item_id(item), "pdfs": pdfs}


def convert(target, opts={}):
    """Convert one page of every source PDF's rasterized image to another format.

    Ad hoc — writes standalone `public-frontend-derivatives/page-NNN/converted.<ext>` file(s).

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'page' (int, default 0), 'format' (str, default
        CONFIG["image"]["convert_format"]), 'quality' (int).
    :returns: {"status": "success", "item": str (item path), "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    page = opts.get("page", 0)
    fmt = opts.get("format", CONFIG["image"]["convert_format"])
    ext = pipeline.EXTENSION_BY_FORMAT.get(fmt.lower(), f".{fmt.lower()}")
    item = items.resolve_target(target)
    pdf_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")
    start = time.monotonic()
    multi = len(pdf_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": pdf_paths})
        local_outputs = []
        for src in sources:
            raster_path = _rasterize(src["local_path"], page, tmp_dir, src["stem"], f"page-{page:03d}", opts)["dest_path"]
            filename = pipeline.output_name(src["stem"], f"converted{ext}", multi)
            local_dest = os.path.join(tmp_dir, filename)
            image_magick.convert_format(raster_path, local_dest, opts={"quality": opts.get("quality", CONFIG["image"]["thumbnail_quality"])})
            local_outputs.append((filename, local_dest, src["bronze_path"]))

        result = pipeline.publish(
            item, f"{pipeline.PUBLIC_FRONTEND_DIR}/page-{page:03d}", local_outputs,
            command=["digtk", "pdf", "convert", items.item_id(item), "--page", str(page), "--format", fmt],
            params={"page": page, "format": fmt},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION["derivatives"],
            },
        )

    return {"status": "success", "item": items.item_id(item), **result}


def deskew(target, opts={}):
    """Apply a single deterministic deskew pass to one page of every source PDF.

    Ad hoc — derivatives()'s OCR comparison is what actually decides deskew for the
    published derivative set; this is a fixed single pass for manual testing.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'page' (int, default 0), forwarded to image_magick.deskew()
        (threshold, working_width, output_width, quality).
    :returns: {"status": "success", "item": str (item path), "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    page = opts.get("page", 0)
    item = items.resolve_target(target)
    pdf_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")
    start = time.monotonic()
    multi = len(pdf_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": pdf_paths})
        local_outputs = []
        for src in sources:
            raster_path = _rasterize(src["local_path"], page, tmp_dir, src["stem"], f"page-{page:03d}", opts)["dest_path"]
            filename = pipeline.output_name(src["stem"], "deskewed.jpg", multi)
            local_dest = os.path.join(tmp_dir, filename)
            image_magick.deskew(raster_path, local_dest, opts=opts)
            local_outputs.append((filename, local_dest, src["bronze_path"]))

        result = pipeline.publish(
            item, f"{pipeline.PUBLIC_FRONTEND_DIR}/page-{page:03d}", local_outputs,
            command=["digtk", "pdf", "deskew", items.item_id(item), "--page", str(page)],
            params={k: v for k, v in opts.items() if k in ("page", "threshold", "working_width", "output_width", "quality")},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION["derivatives"],
            },
        )

    return {"status": "success", "item": items.item_id(item), **result}


def prep_ocr(target, opts={}):
    """Generate a single OCR-prep image for one page of every source PDF.

    Ad hoc — derivatives()'s orientation comparison produces the actual published prep
    image(s); this is a fixed single pass (deskewed or not, per opts) for manual testing.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'page' (int, default 0), forwarded to image_magick.prep_ocr()
        (size, dpi, fuzz, quality, deskew, threshold).
    :returns: {"status": "success", "item": str (item path), "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    page = opts.get("page", 0)
    item = items.resolve_target(target)
    pdf_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")
    start = time.monotonic()
    multi = len(pdf_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": pdf_paths})
        local_outputs = []
        for src in sources:
            raster_path = _rasterize(src["local_path"], page, tmp_dir, src["stem"], f"page-{page:03d}", opts)["dest_path"]
            base_name = "prep-deskew.jpg" if opts.get("deskew") else "prep.jpg"
            filename = pipeline.output_name(src["stem"], base_name, multi)
            local_dest = os.path.join(tmp_dir, filename)
            image_magick.prep_ocr(raster_path, local_dest, opts=opts)
            local_outputs.append((filename, local_dest, src["bronze_path"]))

        result = pipeline.publish(
            item, f"ocr/page-{page:03d}", local_outputs,
            command=["digtk", "pdf", "prep-ocr", items.item_id(item), "--page", str(page)],
            params={k: v for k, v in opts.items() if k in ("page", "deskew", "dpi", "fuzz", "quality", "size", "threshold")},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION["ocr"],
            },
        )

    return {"status": "success", "item": items.item_id(item), **result}


def derivatives(target, opts={}):
    """Generate the full derivative set for every page of every source PDF in the
    item — the umbrella `digtk showcase pdf derivatives` calls.

    For each source PDF: rasterizes every page, decides scan orientation independently
    per page via a real OCR comparison (same digtk.models.digital_website.pipeline primitive
    the image pipeline uses), then applies each page's own decision to its own thumbnail
    set and OCR-prep image. Each source PDF gets its own directory under
    PUBLIC_FRONTEND_DIR, named after its own bronze filename (disambiguating a multi-PDF
    item for free — no stem-prefix scheme needed); within that, each page publishes its
    small/medium/large set plus a pyramidal tiled TIFF for IIIF-style deep-zoom serving
    (digtk.lib.image_magick.tile() — generation only, no serving side wired up yet, see
    docs/cask-conventions.md's "Deep-Zoom Tiles" section) together (+ one shared manifest)
    into its own plain 1-indexed `images/<n>/` subdirectory, alongside one aggregate
    manifest at the `images/` level summarizing every page's orientation decision.
    OCR-prep images are unaffected by this restructuring and still publish into a
    zero-padded `ocr/page-NNN` (or `{stem}/ocr/page-NNN` for a multi-PDF item) silver
    subdirectory, with its own aggregate manifest there, exactly as before. All local
    generation happens before anything is published to CaskFS, so a failure at any step
    leaves no partial derivative set behind for that PDF.

    Each page's own images/<n>/ manifest also carries an "extra" block — "page" (this
    codebase's own 1-indexed page number, matching the directory it's published into —
    legacy DAMS/Fedora used a 0-indexed page number matching its own 0-indexed directory
    scheme, so this is a deliberate divergence to stay self-consistent rather than a
    literal port), "extractDensity" (the DPI rasterize_page() actually used for this
    page), and "small"/"medium"/"large"/"tiled"/"ocr" keys shaped exactly like
    digtk.models.digital_website.image.generate()'s own manifest content (see its
    docstring and docs/cask-conventions.md's "Image Manifest Content"/"PDF Manifest
    Content") — no "original" key here, since a PDF page has no separate original
    binary reference the way a plain image derivative does. The aggregate images/
    manifest additionally carries a "pages" array composing every page's own extra
    block, mirroring the legacy pipeline's per-PDF composite manifest.

    Outputs an earlier run already published are skipped step by step (see
    _page_status()); a PDF whose pages are all done isn't even downloaded.
    opts['force'] regenerates everything.

    Per-file mode (opts['files']) — one Dagster step per PDF, see
    dagster/lib/pipeline_fanout.py — processes only the named PDF(s). Everything
    above is already written per PDF, so a per-file run writes exactly the
    paths a whole-item run would (the ocr/ namespacing decision still follows
    the whole item).

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'widths' (comma-separated str, default
        CONFIG["image"]["thumbnail_widths"]); 'files' (iterable of bronze paths or
        filenames, optional — per-file mode, see above; see
        pipeline.select_binaries()); 'force' (bool — regenerate existing
        outputs); forwarded to pdf_render/image_magick/ocr_utils:
        'target_pixel_width', 'default_density', 'quality', 'dpi', 'fuzz', 'threshold', etc.
    :returns: {"status": "success", "item": str (item path), "pdfs": {filename: {"pages": int,
        "derivatives": {"pages": {page_number: {...}}, "manifest": {...}}, "ocr": {...},
        "orientation": {page_number: {...}}}}, "finalized": bool (False in
        per-file mode — run finalize())}
    :rtype: dict
    :raises ValueError: If a 'files' selector matches no PDF, or is ambiguous.
    """
    widths = pipeline.parse_widths(opts.get("widths", CONFIG["image"]["thumbnail_widths"]))
    item = items.resolve_target(target)
    pdf_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")
    files = opts.get("files")
    selected = pipeline.select_binaries(pdf_paths, files) if files else pdf_paths
    start = time.monotonic()
    total_pdfs = len(selected)

    # Look up what earlier runs published, so finished PDFs aren't downloaded.
    published = {}
    for bronze_path in selected:
        pdf_filename = os.path.basename(bronze_path)
        published[bronze_path] = _published_pages(
            item, f"{pipeline.PUBLIC_FRONTEND_DIR}/{pdf_filename}/images",
            pipeline.pdf_ocr_prefix(item.bronze_binary_paths, bronze_path),
            os.path.splitext(pdf_filename)[0], widths, opts,
        )
    pending = [path for path in selected if not _all_complete(published[path])]

    pdfs_result = {}

    with tempfile.TemporaryDirectory() as tmp_dir:
        staged = {}
        if pending:
            staged = {src["bronze_path"]: src for src in pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": pending})}

        for pdf_i, bronze_path in enumerate(selected, start=1):
            pdf_filename = os.path.basename(bronze_path)
            stem = os.path.splitext(pdf_filename)[0]
            src = staged.get(bronze_path)
            # Namespaced by the PDF's own stem whenever anything else in the
            # item could also write to the bare ocr/ — see pipeline.pdf_ocr_prefix().
            ocr_prefix = pipeline.pdf_ocr_prefix(item.bronze_binary_paths, bronze_path)
            images_base = f"{pipeline.PUBLIC_FRONTEND_DIR}/{pdf_filename}/images"
            if src:
                count = pdf_render.page_count(src["local_path"], opts)
                statuses = published[bronze_path]["pages"] if published[bronze_path] and published[bronze_path]["count"] == count else {}
                utils.log(f"[{pdf_i}/{total_pdfs}] {stem}: {count} page(s)")
            else:
                count = published[bronze_path]["count"]
                statuses = published[bronze_path]["pages"]
                utils.log(f"[{pdf_i}/{total_pdfs}] {stem}: all {count} page(s) already generated, skipping")

            orientations = {}
            derivatives_pages = {}
            ocr_pages = []
            ocr_page_dirs = []
            page_manifest_entries = []
            command = ["digtk", "pdf", "derivatives", items.item_id(item), "--widths", ",".join(map(str, widths))]

            for page_index in range(count):
                page_no = page_index + 1
                page_tag = pipeline.page_tag(page_index, count)  # still used for the (unchanged) ocr/ path
                prefix = f"[{pdf_i}/{total_pdfs}] {stem} page {page_no}"
                status = statuses.get(page_no) or _page_status(item, images_base, ocr_prefix, stem, page_no, page_tag, widths, opts)

                if status["complete"]:
                    if src:
                        pipeline.log_skip(prefix, "all derivatives")
                    page_result = _skipped_page(status, bronze_path)
                else:
                    page_result = _generate_page(item, src, tmp_dir, {
                        "page_index": page_index, "page_tag": page_tag, "prefix": prefix, "status": status,
                        "widths": widths, "images_base": images_base, "ocr_prefix": ocr_prefix, "command": command,
                    }, opts)

                orientations[str(page_no)] = page_result["orientation"]
                page_manifest_entries.append(page_result["manifest_extra"])
                derivatives_pages[str(page_no)] = page_result["images"]
                ocr_pages.append(page_result["ocr"])
                ocr_page_dirs.append(f"{page_tag}/")
                utils.log(f"{prefix}: done")

            duration_s = time.monotonic() - start
            agg_images_rel_path = f"{item.item_path}/{images_base}" if item.item_path else images_base
            agg_ocr_rel_path = f"{item.item_path}/{ocr_prefix}ocr" if item.item_path else f"{ocr_prefix}ocr"
            manifest_opts = {"group": item.group, "time": item.time, "duration_s": duration_s}

            derivatives_manifest = utils.write_manifest(
                item.collection, agg_images_rel_path,
                command=command,
                params={"pages": count, "widths": widths, "orientation": {
                    tag: {"deskew_selected": o["deskew"], "stats": o.get("stats")} for tag, o in orientations.items()
                }},
                outputs=[f"{tag}/" for tag in orientations],
                source_files=[bronze_path],
                type="silver",
                opts={**manifest_opts, "extra": {"pages": page_manifest_entries}},
            )
            ocr_manifest = utils.write_manifest(
                item.collection, agg_ocr_rel_path,
                command=["digtk", "pdf", "derivatives", items.item_id(item)],
                params={"pages": count, "orientation": {tag: {"deskew_selected": o["deskew"]} for tag, o in orientations.items()}},
                # The real per-page subdirectories under ocr/ (page-NNN/), not
                # the 1-indexed page numbers the images/ side uses.
                outputs=ocr_page_dirs,
                source_files=[bronze_path],
                opts=manifest_opts,
            )

            pdfs_result[pdf_filename] = {
                "pages": count,
                "derivatives": {"pages": derivatives_pages, "manifest": derivatives_manifest},
                "ocr": {"pages": ocr_pages, "manifest": ocr_manifest},
                "orientation": orientations,
            }

    return {"status": "success", "item": items.item_id(item), "pdfs": pdfs_result, "finalized": not files}


def _published_pages(item, images_base, ocr_prefix, stem, widths, opts):
    """Every page's status for one PDF from an earlier run, read from its
    aggregate images/manifest.json, before the PDF is downloaded.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str images_base: The PDF's images/ subdir.
    :param str ocr_prefix: See pipeline.pdf_ocr_prefix().
    :param str stem: The PDF's filename stem.
    :param list[int] widths: Current thumbnail widths.
    :param dict opts: 'force' (bool).
    :returns: {"count": int, "pages": {page_no: _page_status()}}, or None
        when there's no usable aggregate manifest.
    :rtype: dict|None
    """
    aggregate = pipeline.existing_outputs(item, images_base, {"force": opts.get("force"), "params": {"widths": widths}})
    count = ((aggregate["manifest"] or {}).get("params") or {}).get("pages")
    if not count:
        return None
    pages = {}
    for page_index in range(count):
        page_no = page_index + 1
        pages[page_no] = _page_status(item, images_base, ocr_prefix, stem, page_no, pipeline.page_tag(page_index, count), widths, opts)
    return {"count": count, "pages": pages}


def _all_complete(published):
    """Whether every page of a PDF was already generated.

    :param dict|None published: _published_pages()'s return.
    :rtype: bool
    """
    return bool(published) and all(status["complete"] for status in published["pages"].values())


def _page_status(item, images_base, ocr_prefix, stem, page_no, page_tag, widths, opts):
    """Which of one PDF page's outputs an earlier run already published.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str images_base: The PDF's images/ subdir.
    :param str ocr_prefix: See pipeline.pdf_ocr_prefix().
    :param str stem: The PDF's filename stem.
    :param int page_no: 1-indexed page number.
    :param str page_tag: The page's ocr/ subdir name (pipeline.page_tag()).
    :param list[int] widths: Current thumbnail widths.
    :param dict opts: 'force' (bool) — report nothing as published.
    :returns: pipeline.derivative_set_status()'s return, plus "ocr"
        (pipeline.existing_outputs() for the page's ocr/ dir).
    :rtype: dict
    """
    images = pipeline.existing_outputs(item, f"{images_base}/{page_no}", {"force": opts.get("force"), "params": {"widths": widths}})
    ocr = pipeline.existing_outputs(item, f"{ocr_prefix}ocr/{page_tag}", {"force": opts.get("force")})
    status = pipeline.derivative_set_status(images, ocr["files"], {
        "prep": [f"{stem}-{page_tag}-prep.jpg", f"{stem}-{page_tag}-prep-deskew.jpg"],
        "ocr_layer": ["ocr.djvu", "ocr-stats.json"],
    }, {"required_keys": ["extractDensity"]})
    return {**status, "ocr": ocr}


def _skipped_page(status, bronze_path):
    """A fully generated page's results, rebuilt from its stored manifest.

    :param dict status: _page_status()'s return, with "complete" True.
    :param str bronze_path: The PDF's bronze path.
    :returns: Same shape as _generate_page().
    :rtype: dict
    """
    manifest = status["images"]["manifest"]
    stored = status["orientation"]
    keys = ["page", "extractDensity", *pipeline.THUMBNAIL_SIZE_LABELS, "tiled", "ocr"]
    return {
        "orientation": {"deskew": stored["deskew_selected"], "stats": stored.get("stats"), "skipped": True},
        "manifest_extra": {key: manifest[key] for key in keys if key in manifest},
        "images": {"dir": status["images"]["dir"], "outputs": sorted(status["images"]["files"]), "manifest": manifest, "skipped": True},
        "ocr": {"dir": status["ocr"]["dir"], "outputs": sorted(status["ocr"]["files"]), "manifest": status["ocr"]["manifest"], "skipped": True},
    }


def _generate_page(item, src, tmp_dir, page, opts):
    """Generate and publish one PDF page's missing outputs, skipping any an
    earlier run already published (see _page_status()).

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param dict src: The staged PDF (pipeline.stage_sources() entry).
    :param str tmp_dir: Local scratch directory.
    :param dict page: 'page_index', 'page_tag', 'prefix' (log prefix), 'status'
        (_page_status()), 'widths', 'images_base', 'ocr_prefix', 'command'.
    :param dict opts: derivatives()'s opts.
    :returns: {"orientation", "manifest_extra", "images" (publish result), "ocr" (publish result)}
    :rtype: dict
    """
    status, prefix, widths = page["status"], page["prefix"], page["widths"]
    page_index, page_tag, stem = page["page_index"], page["page_tag"], src["stem"]
    page_no = page_index + 1
    bronze_path = src["bronze_path"]
    stored_manifest = status["images"]["manifest"] or {}

    utils.log(f"{prefix}: rasterizing...")
    raster = _rasterize(src["local_path"], page_index, tmp_dir, stem, page_tag, opts)
    raster_path = raster["dest_path"]

    orientation = pipeline.reused_orientation(status)
    if orientation:
        pipeline.log_skip(prefix, "orientation decision")
    else:
        utils.log(f"{prefix}: deciding orientation (running OCR twice)...")
        orientation = pipeline.select_orientation(raster_path, tmp_dir, opts)
    apply_deskew = orientation["deskew"]

    image_outputs, kept_images = [], []
    if status["thumbnails"]:
        pipeline.log_skip(prefix, f"{len(widths)} thumbnail(s)")
        kept_images += [(f"{label}.jpg", bronze_path) for label in pipeline.THUMBNAIL_SIZE_LABELS]
        size_by_label = {label: pipeline.stored_size(stored_manifest[label]) for label in pipeline.THUMBNAIL_SIZE_LABELS}
    else:
        utils.log(f"{prefix}: generating {len(widths)} thumbnail(s)...")
        thumb_set = pipeline.generate_thumbnail_set(
            raster_path, tmp_dir, widths, f"{stem}-{page_no}", {**opts, "deskew": apply_deskew}
        )
        image_outputs += [(fn, local_path, bronze_path) for fn, local_path in thumb_set]

    tile_dest = None
    if status["tiled"]:
        pipeline.log_skip(prefix, "tiled pyramid TIFF")
        kept_images.append(("tiled.tif", bronze_path))
    else:
        utils.log(f"{prefix}: generating tiled pyramid TIFF...")
        tile_dest = os.path.join(tmp_dir, f"{stem}-{page_no}-tiled.tif")
        image_magick.tile(raster_path, tile_dest, opts)
        image_outputs.append(("tiled.tif", tile_dest, bronze_path))

    ocr_outputs, kept_ocr = [], []
    if status["prep"]:
        pipeline.log_skip(prefix, "OCR-prep image")
        kept_ocr.append((status["prep"], bronze_path))
    else:
        utils.log(f"{prefix}: generating OCR-prep image...")
        base_prep_name = "prep-deskew.jpg" if apply_deskew else "prep.jpg"
        prep_path = os.path.join(tmp_dir, f"{stem}-{page_tag}-{base_prep_name}")
        image_magick.prep_ocr(raster_path, prep_path, opts={**opts, "deskew": apply_deskew})
        ocr_outputs.append((os.path.basename(prep_path), prep_path, bronze_path))

    if not status["thumbnails"] or tile_dest:
        utils.log(f"{prefix}: probing derivative dimensions...")
    if not status["thumbnails"]:
        size_by_label = {label: image_magick.probe_dimensions(local_path) for label, (fn, local_path, _) in zip(pipeline.THUMBNAIL_SIZE_LABELS, image_outputs)}
    tiled_size = (
        [pipeline.size_as_strings(d) for d in image_magick.probe_all_frame_dimensions(tile_dest)]
        if tile_dest else stored_manifest["tiled"]["size"]
    )
    images_dir = pipeline.derivative_dir(item, f"{page['images_base']}/{page_no}")
    ocr_dir = pipeline.derivative_dir(item, f"{page['ocr_prefix']}ocr/{page_tag}")

    if status["ocr_layer"]:
        pipeline.log_skip(prefix, "OCR text layer")
        kept_ocr += [("ocr.djvu", bronze_path), ("ocr-stats.json", bronze_path)]
        djvu_filename = "ocr.djvu"
    else:
        utils.log(f"{prefix}: building OCR text layer...")
        # Each page already publishes into its own page_tag-unique ocr/
        # subdir, so djvu/stats never need output_name()'s multi-source
        # filename prefix -- multi=False always here.
        ocr_products = pipeline.build_ocr_products(
            orientation, tmp_dir, f"{stem}-{page_tag}", False, size_by_label["large"], opts
        )
        djvu_filename = ocr_products[0][0]
        ocr_outputs += [(fn, local_path, bronze_path) for fn, local_path in ocr_products]

    manifest_extra = {
        "page": page_no,
        "extractDensity": raster["dpi"],
        **{
            label: {
                "size": pipeline.size_as_strings(size_by_label[label]),
                "url": pipeline.cask_public_ref(f"{images_dir}/{label}.jpg"),
            }
            for label in pipeline.THUMBNAIL_SIZE_LABELS
        },
        "tiled": {
            "size": tiled_size,
            "url": pipeline.cask_public_ref(f"{images_dir}/tiled.tif"),
        },
        "ocr": {
            "size": pipeline.size_as_strings(size_by_label["large"]),
            "imageSize": "large",
            "url": pipeline.cask_public_ref(f"{ocr_dir}/{djvu_filename}"),
        },
    }

    images_result = pipeline.publish(
        item, f"{page['images_base']}/{page_no}", image_outputs,
        command=page["command"],
        params={"widths": widths, "page": page_index, "orientation": {"deskew_selected": apply_deskew, "stats": orientation["stats"]}},
        opts={
            "manifest_opts": {"extra": manifest_extra},
            "link_relation": _LINK_RELATION["derivatives"],
            "kept_outputs": kept_images,
        },
    )
    ocr_result = pipeline.publish(
        item, f"{page['ocr_prefix']}ocr/{page_tag}", ocr_outputs,
        command=page["command"],
        params={"page": page_index, "orientation": {"deskew_selected": apply_deskew}},
        opts={"link_relation": _LINK_RELATION["ocr"], "kept_outputs": kept_ocr},
    )
    return {"orientation": orientation, "manifest_extra": manifest_extra, "images": images_result, "ocr": ocr_result}


def _read_manifests(item, pdf_paths):
    """Read back each PDF's published aggregate images/manifest.json.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param list[str] pdf_paths: The item's PDF binaries.
    :returns: {bronze path: manifest dict}.
    :rtype: dict
    :raises FileNotFoundError: If any PDF's aggregate manifest is missing (its
        per-PDF run hasn't completed).
    """
    manifests, missing = {}, []
    for path in pdf_paths:
        filename = os.path.basename(path)
        images_dir = pipeline.derivative_dir(item, f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}/images")
        try:
            manifests[path] = json.loads(fs.read(f"{images_dir}/manifest.json"))
        except FileNotFoundError:
            missing.append(filename)
    if missing:
        raise FileNotFoundError(
            f"no derivatives published yet for {len(missing)} PDF(s) of {items.item_id(item)}: {missing}"
        )
    return manifests


def media_objects(item):
    """The item's "pdf" associatedMedia entries — small/medium/large
    ImageObjects for every page of every PDF, each with its 0-indexed
    "position" — rebuilt from each PDF's aggregate images/manifest.json (its
    "pages" array). Read by
    digtk.lib.showcase_index._silver_media().

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: ImageObject entries, in PDF then page order.
    :rtype: list[dict]
    :raises FileNotFoundError: If any PDF's aggregate manifest is missing.
    """
    pdf_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")
    manifests = _read_manifests(item, pdf_paths)
    objects = []
    for path in pdf_paths:
        for page in manifests[path].get("pages", []):
            objects += pipeline.image_objects_from_manifest(page, opts={"position": page["page"] - 1})
    return objects


def finalize(target, opts={}):
    """Check that every PDF of the item has its derivatives published — the
    step after one-per-PDF derivatives() runs (see its 'files' opt). A PDF has
    no item-level outputs of its own to write; its associatedMedia goes into
    the gold item record (see digtk.models.digital_website.item_record).

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: Unused; present for signature symmetry.
    :returns: {"status": "success", "item": str (item path), "pdfs": int}
    :rtype: dict
    :raises FileNotFoundError: If any PDF's aggregate manifest is missing (its
        per-PDF run hasn't completed).
    """
    item = items.resolve_target(target)
    pdf_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")
    _read_manifests(item, pdf_paths)
    return {"status": "success", "item": items.item_id(item), "pdfs": len(pdf_paths)}
