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
import os
import tempfile
import time

from digtk.lib import image_magick, items, pdf_render, utils
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


def page_count(ark, opts={}):
    """Report each source PDF's page count for an item — ad hoc/debug, no derivatives generated.

    :param str ark: Item ARK identifier.
    :param dict opts: Unused.
    :returns: {"status": "success", "ark": str, "pages": {stem: count}}
    :rtype: dict
    """
    item = items.resolve(ark)
    pdf_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")
    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": pdf_paths})
        pages = {src["stem"]: pdf_render.page_count(src["local_path"], opts) for src in sources}
    return {"status": "success", "ark": ark, "pages": pages}


def thumbnail(ark, opts={}):
    """Generate a small/medium/large set for one page of every source PDF in the item.

    Ad hoc/manual use, mirroring digtk.models.digital_website.image.thumbnails();
    derivatives() is what Dagster's derivatives_job actually calls. Publishes into the
    same location derivatives() would for this one page:
    `public-frontend-derivatives/<pdf-filename>/images/<page-number>/` (1-indexed).

    :param str ark: Item ARK identifier.
    :param dict opts: 'page' (int, default 0, 0-indexed), 'widths' (comma-separated str,
        default CONFIG["image"]["thumbnail_widths"]), 'quality' (int).
    :returns: {"status": "success", "ark": str, "pdfs": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}}
    :rtype: dict
    """
    page = opts.get("page", 0)
    widths = pipeline.parse_widths(opts.get("widths", CONFIG["image"]["thumbnail_widths"]))
    item = items.resolve(ark)
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
                command=["digtk", "pdf", "thumbnail", ark, "--page", str(page), "--widths", ",".join(map(str, widths))],
                params={"page": page, "widths": widths},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION["derivatives"],
                },
            )

    return {"status": "success", "ark": ark, "pdfs": pdfs}


def convert(ark, opts={}):
    """Convert one page of every source PDF's rasterized image to another format.

    Ad hoc — writes standalone `public-frontend-derivatives/page-NNN/converted.<ext>` file(s).

    :param str ark: Item ARK identifier.
    :param dict opts: 'page' (int, default 0), 'format' (str, default
        CONFIG["image"]["convert_format"]), 'quality' (int).
    :returns: {"status": "success", "ark": str, "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    page = opts.get("page", 0)
    fmt = opts.get("format", CONFIG["image"]["convert_format"])
    ext = pipeline.EXTENSION_BY_FORMAT.get(fmt.lower(), f".{fmt.lower()}")
    item = items.resolve(ark)
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
            command=["digtk", "pdf", "convert", ark, "--page", str(page), "--format", fmt],
            params={"page": page, "format": fmt},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION["derivatives"],
            },
        )

    return {"status": "success", "ark": ark, **result}


def deskew(ark, opts={}):
    """Apply a single deterministic deskew pass to one page of every source PDF.

    Ad hoc — derivatives()'s OCR comparison is what actually decides deskew for the
    published derivative set; this is a fixed single pass for manual testing.

    :param str ark: Item ARK identifier.
    :param dict opts: 'page' (int, default 0), forwarded to image_magick.deskew()
        (threshold, working_width, output_width, quality).
    :returns: {"status": "success", "ark": str, "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    page = opts.get("page", 0)
    item = items.resolve(ark)
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
            command=["digtk", "pdf", "deskew", ark, "--page", str(page)],
            params={k: v for k, v in opts.items() if k in ("page", "threshold", "working_width", "output_width", "quality")},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION["derivatives"],
            },
        )

    return {"status": "success", "ark": ark, **result}


def prep_ocr(ark, opts={}):
    """Generate a single OCR-prep image for one page of every source PDF.

    Ad hoc — derivatives()'s orientation comparison produces the actual published prep
    image(s); this is a fixed single pass (deskewed or not, per opts) for manual testing.

    :param str ark: Item ARK identifier.
    :param dict opts: 'page' (int, default 0), forwarded to image_magick.prep_ocr()
        (size, dpi, fuzz, quality, deskew, threshold).
    :returns: {"status": "success", "ark": str, "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    page = opts.get("page", 0)
    item = items.resolve(ark)
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
            command=["digtk", "pdf", "prep-ocr", ark, "--page", str(page)],
            params={k: v for k, v in opts.items() if k in ("page", "deskew", "dpi", "fuzz", "quality", "size", "threshold")},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION["ocr"],
            },
        )

    return {"status": "success", "ark": ark, **result}


def derivatives(ark, opts={}):
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

    :param str ark: Item ARK identifier.
    :param dict opts: 'widths' (comma-separated str, default
        CONFIG["image"]["thumbnail_widths"]); forwarded to pdf_render/image_magick/ocr_utils:
        'target_pixel_width', 'default_density', 'quality', 'dpi', 'fuzz', 'threshold', etc.
    :returns: {"status": "success", "ark": str, "pdfs": {filename: {"pages": int,
        "derivatives": {"pages": {page_number: {...}}, "manifest": {...}}, "ocr": {...},
        "orientation": {page_number: {...}}}}}
    :rtype: dict
    """
    widths = pipeline.parse_widths(opts.get("widths", CONFIG["image"]["thumbnail_widths"]))
    item = items.resolve(ark)
    pdf_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")
    start = time.monotonic()
    multi_pdf = len(pdf_paths) > 1
    # Namespace the OCR output by the PDF's own filename whenever anything else
    # could also publish to the item's bare "ocr/" directory — not just when
    # there's more than one PDF. image.py's generate() publishes its own
    # ocr/manifest.json (keyed by filename stem) to that exact same bare path
    # for any co-located image binaries; without this, whichever of image/pdf
    # derivatives ran most recently silently overwrites the other's manifest.
    # (The "images_base" display-image path below has always been namespaced
    # by pdf_filename regardless of multi_pdf — this brings ocr/ in line with
    # that, only for the case that actually needs it, so a pure single-PDF
    # item's existing ocr/ layout is unaffected.)
    mixed_item = len(pdf_paths) < len(item.bronze_binary_paths)

    pdfs_result = {}
    image_objects = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": pdf_paths})
        total_pdfs = len(sources)

        for pdf_i, src in enumerate(sources, start=1):
            stem = src["stem"]
            pdf_path = src["local_path"]
            pdf_filename = os.path.basename(src["bronze_path"])
            ocr_prefix = f"{stem}/" if (multi_pdf or mixed_item) else ""
            images_base = f"{pipeline.PUBLIC_FRONTEND_DIR}/{pdf_filename}/images"
            count = pdf_render.page_count(pdf_path, opts)
            utils.log(f"[{pdf_i}/{total_pdfs}] {stem}: {count} page(s)")

            orientations = {}
            derivatives_pages = {}
            ocr_pages = []
            page_manifest_entries = []

            for page_index in range(count):
                page_no = page_index + 1
                page_tag = pipeline.page_tag(page_index, count)  # still used for the (unchanged) ocr/ path
                utils.log(f"[{pdf_i}/{total_pdfs}] {stem} page {page_no}: rasterizing...")
                raster = _rasterize(pdf_path, page_index, tmp_dir, stem, page_tag, opts)
                raster_path = raster["dest_path"]
                density = raster["dpi"]

                utils.log(f"[{pdf_i}/{total_pdfs}] {stem} page {page_no}: deciding orientation (running OCR twice)...")
                orientation = pipeline.select_orientation(raster_path, tmp_dir, opts)
                apply_deskew = orientation["deskew"]
                orientations[str(page_no)] = orientation

                utils.log(f"[{pdf_i}/{total_pdfs}] {stem} page {page_no}: generating {len(widths)} thumbnail(s)...")
                thumb_set = pipeline.generate_thumbnail_set(
                    raster_path, tmp_dir, widths, f"{stem}-{page_no}", {**opts, "deskew": apply_deskew}
                )
                thumb_outputs = [(fn, local_path, src["bronze_path"]) for fn, local_path in thumb_set]

                utils.log(f"[{pdf_i}/{total_pdfs}] {stem} page {page_no}: generating tiled pyramid TIFF...")
                tile_dest = os.path.join(tmp_dir, f"{stem}-{page_no}-tiled.tif")
                image_magick.tile(raster_path, tile_dest, opts)
                thumb_outputs.append(("tiled.tif", tile_dest, src["bronze_path"]))

                utils.log(f"[{pdf_i}/{total_pdfs}] {stem} page {page_no}: generating OCR-prep image...")
                base_prep_name = "prep-deskew.jpg" if apply_deskew else "prep.jpg"
                prep_path = os.path.join(tmp_dir, f"{stem}-{page_tag}-{base_prep_name}")
                image_magick.prep_ocr(raster_path, prep_path, opts={**opts, "deskew": apply_deskew})

                utils.log(f"[{pdf_i}/{total_pdfs}] {stem} page {page_no}: probing derivative dimensions...")
                size_by_label = {label: image_magick.probe_dimensions(local_path) for label, (fn, local_path) in zip(pipeline.THUMBNAIL_SIZE_LABELS, thumb_set)}
                images_dir = pipeline.derivative_dir(item, f"{images_base}/{page_no}")
                ocr_dir = pipeline.derivative_dir(item, f"{ocr_prefix}ocr/{page_tag}")
                ocr_filename = os.path.basename(prep_path)

                utils.log(f"[{pdf_i}/{total_pdfs}] {stem} page {page_no}: building OCR text layer...")
                # Each page already publishes into its own page_tag-unique ocr/
                # subdir, so djvu/stats never need output_name()'s multi-source
                # filename prefix -- multi=False always here.
                ocr_products = pipeline.build_ocr_products(
                    orientation, tmp_dir, f"{stem}-{page_tag}", False, size_by_label["large"], opts
                )
                djvu_filename = ocr_products[0][0]

                page_manifest_extra = {
                    "page": page_no,
                    "extractDensity": density,
                    **{
                        label: {
                            "size": pipeline.size_as_strings(size_by_label[label]),
                            "url": pipeline.cask_public_ref(f"{images_dir}/{label}.jpg"),
                        }
                        for label in pipeline.THUMBNAIL_SIZE_LABELS
                    },
                    "tiled": {
                        "size": [pipeline.size_as_strings(d) for d in image_magick.probe_all_frame_dimensions(tile_dest)],
                        "url": pipeline.cask_public_ref(f"{images_dir}/tiled.tif"),
                    },
                    "ocr": {
                        "size": pipeline.size_as_strings(size_by_label["large"]),
                        "imageSize": "large",
                        "url": pipeline.cask_public_ref(f"{ocr_dir}/{djvu_filename}"),
                    },
                }
                page_manifest_entries.append(page_manifest_extra)

                command = ["digtk", "pdf", "derivatives", ark, "--widths", ",".join(map(str, widths))]
                page_result = pipeline.publish(
                    item, f"{images_base}/{page_no}", thumb_outputs,
                    command=command,
                    params={"widths": widths, "page": page_index, "orientation": {"deskew_selected": apply_deskew, "stats": orientation["stats"]}},
                    opts={
                        "manifest_opts": {"extra": page_manifest_extra},
                        "link_relation": _LINK_RELATION["derivatives"],
                    },
                )
                ocr_outputs = [(ocr_filename, prep_path, src["bronze_path"])] + [
                    (fn, local_path, src["bronze_path"]) for fn, local_path in ocr_products
                ]
                ocr_result = pipeline.publish(
                    item, f"{ocr_prefix}ocr/{page_tag}", ocr_outputs,
                    command=command,
                    params={"page": page_index, "orientation": {"deskew_selected": apply_deskew}},
                    opts={"link_relation": _LINK_RELATION["ocr"]},
                )

                for label, (fn, local_path) in zip(pipeline.THUMBNAIL_SIZE_LABELS, thumb_set):
                    image_objects.append({
                        "@type": "ImageObject",
                        "contentUrl": pipeline.cask_public_ref(f"{images_dir}/{fn}"),
                        "encodingFormat": "image/jpeg",
                        "position": page_index,
                        **size_by_label[label],
                    })
                derivatives_pages[str(page_no)] = page_result
                ocr_pages.append(ocr_result)
                utils.log(f"[{pdf_i}/{total_pdfs}] {stem} page {page_no}: done")

            duration_s = time.monotonic() - start
            agg_images_rel_path = f"{item.item_path}/{images_base}" if item.item_path else images_base
            agg_ocr_rel_path = f"{item.item_path}/{ocr_prefix}ocr" if item.item_path else f"{ocr_prefix}ocr"
            manifest_opts = {"group": item.group, "time": item.time, "duration_s": duration_s}

            derivatives_manifest = utils.write_manifest(
                item.collection, agg_images_rel_path,
                command=["digtk", "pdf", "derivatives", ark, "--widths", ",".join(map(str, widths))],
                params={"pages": count, "widths": widths, "orientation": {
                    tag: {"deskew_selected": o["deskew"], "stats": o["stats"]} for tag, o in orientations.items()
                }},
                outputs=[f"{tag}/" for tag in orientations],
                source_files=[src["bronze_path"]],
                type="silver",
                opts={**manifest_opts, "extra": {"pages": page_manifest_entries}},
            )
            ocr_manifest = utils.write_manifest(
                item.collection, agg_ocr_rel_path,
                command=["digtk", "pdf", "derivatives", ark],
                params={"pages": count, "orientation": {tag: {"deskew_selected": o["deskew"]} for tag, o in orientations.items()}},
                outputs=[f"{tag}/" for tag in orientations],
                source_files=[src["bronze_path"]],
                opts=manifest_opts,
            )

            pdfs_result[pdf_filename] = {
                "pages": count,
                "derivatives": {"pages": derivatives_pages, "manifest": derivatives_manifest},
                "ocr": {"pages": ocr_pages, "manifest": ocr_manifest},
                "orientation": orientations,
            }

        utils.log("Updating associated-media sidecar...")
        pipeline.update_associated_media(item, image_objects, [src["bronze_path"] for src in sources], "pdf")

    return {"status": "success", "ark": ark, "pdfs": pdfs_result}
