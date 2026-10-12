"""
Digital-website item derivative-generation workflow.

Orchestrates phase 0.4 (image derivatives): resolves an ARK to its CaskFS
bronze location (one item can hold more than one binary — e.g. an archival
box/folder containing several photographs; see docs/cask-conventions.md and
digtk.lib.items), stages each bronze binary to a local temp file, generates
the derivative image set via digtk.lib.image_magick, decides scan orientation
independently per image via digtk.lib.ocr_utils, and publishes results back
to CaskFS through digtk.lib.derivative — the canonical way to write any
silver/gold/workspace pipeline output and record its CaskFS lineage back to
the source binary it came from (see CLAUDE.md).

This is the only layer that knows about ARKs, CaskFS paths, and the silver
directory layout — digtk.lib.image_magick and digtk.lib.ocr_utils only ever
see local file paths. Reads still go through digtk.lib.fs directly (its own
'cask:/' prefix check already decides local-vs-CaskFS transparently); writes
of pipeline outputs go through digtk.lib.derivative instead. The only reason
a local temp directory appears here at all is that ImageMagick and Tesseract
are subprocess binaries that require real filesystem paths.

Staging/publishing/lineage plumbing shared with the PDF/video/audio
orchestrators lives in digtk.models.digital_website.pipeline; only the
image-specific top-level commands and the derivatives/ocr link-relation
mapping stay here.

No SystemExit/utils.result()/utils.error() calls here — functions return a
plain dict on success and let exceptions propagate; digtk.commands.image is
the only layer that translates those into the CLI's last-line-JSON convention.
"""
import json
import os
import tempfile
import time

from digtk.lib import fs, image_magick, items, utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS
from digtk.models.digital_website import pipeline

# Derivative-link relation per process subdir, distinguishing what an output
# actually is for (a viewable thumbnail vs. an OCR-engine input) rather than
# leaving every link at CaskFS's generic default (schema:source) — lets
# `cask lineage derivatives <bronze-file>` filter by which kind of derivative
# it wants. See caskfs's docs/structural-metadata.md for the lineage model.
_LINK_RELATION_BY_SUBDIR = {
    "derivatives": f"{ARGONATH_NS}derivativeOf",
    "ocr": f"{ARGONATH_NS}ocrPrepOf",
}


def thumbnails(target, opts={}):
    """Generate small/medium/large JPEGs, for every source image in the item.

    Standalone, deterministic — no orientation comparison. For ad-hoc/manual
    use; generate() is what Dagster's derivatives_job actually calls.

    Each source publishes into its own nested directory, keyed by its own bronze
    filename (see PUBLIC_FRONTEND_DIR and docs/cask-conventions.md's Silver Layout
    section) — e.g. `public-frontend-derivatives/images/page_100.tif/images/`.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'widths' (comma-separated str, default
        CONFIG["image"]["thumbnail_widths"]) — must resolve to exactly 3 widths
        (small/medium/large), 'quality' (int).
    :returns: {"status": "success", "item": str (item path), "images": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}}
    :rtype: dict
    """
    widths = pipeline.parse_widths(opts.get("widths", CONFIG["image"]["thumbnail_widths"]))
    item = items.resolve_target(target)
    image_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "image")
    start = time.monotonic()

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": image_paths})
        total = len(sources)
        images = {}
        for i, src in enumerate(sources, start=1):
            filename = os.path.basename(src["bronze_path"])
            utils.log(f"[{i}/{total}] {filename}: generating {len(widths)} thumbnail(s)...")
            thumb_set = pipeline.generate_thumbnail_set(src["local_path"], tmp_dir, widths, src["stem"], opts)
            local_outputs = [(fn, local_path, src["bronze_path"]) for fn, local_path in thumb_set]

            images[filename] = pipeline.publish(
                item, f"{pipeline.PUBLIC_FRONTEND_DIR}/images/{filename}/images", local_outputs,
                command=["digtk", "image", "thumbnail", items.item_id(item), "--widths", ",".join(map(str, widths))],
                params={"widths": widths},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION_BY_SUBDIR["derivatives"],
                },
            )

    return {"status": "success", "item": items.item_id(item), "images": images}


def convert(target, opts={}):
    """Convert every source image's binary to another format.

    Ad hoc — writes standalone `public-frontend-derivatives/converted.<ext>` file(s),
    not part of generate()'s standard derivative set.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'format' (str, default CONFIG["image"]["convert_format"]),
        'quality' (int).
    :returns: {"status": "success", "item": str (item path), "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    fmt = opts.get("format", CONFIG["image"]["convert_format"])
    ext = pipeline.EXTENSION_BY_FORMAT.get(fmt.lower(), f".{fmt.lower()}")
    item = items.resolve_target(target)
    image_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "image")
    start = time.monotonic()
    multi = len(image_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": image_paths})
        total = len(sources)
        local_outputs = []
        for i, src in enumerate(sources, start=1):
            utils.log(f"[{i}/{total}] {src['stem']}: converting to {fmt}...")
            filename = pipeline.output_name(src["stem"], f"converted{ext}", multi)
            local_dest = os.path.join(tmp_dir, filename)
            image_magick.convert_format(src["local_path"], local_dest, opts={"quality": opts.get("quality", CONFIG["image"]["thumbnail_quality"])})
            local_outputs.append((filename, local_dest, src["bronze_path"]))

        result = pipeline.publish(
            item, pipeline.PUBLIC_FRONTEND_DIR, local_outputs,
            command=["digtk", "image", "convert", items.item_id(item), "--format", fmt],
            params={"format": fmt},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION_BY_SUBDIR["derivatives"],
            },
        )

    return {"status": "success", "item": items.item_id(item), **result}


def deskew(target, opts={}):
    """Apply a single deterministic deskew pass to every source image.

    Ad hoc — generate()'s OCR comparison is what actually decides deskew for
    the published derivative set; this is a fixed single pass for manual testing.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: Forwarded to image_magick.deskew() (threshold, working_width,
        output_width, quality).
    :returns: {"status": "success", "item": str (item path), "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    item = items.resolve_target(target)
    image_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "image")
    start = time.monotonic()
    multi = len(image_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": image_paths})
        total = len(sources)
        local_outputs = []
        for i, src in enumerate(sources, start=1):
            utils.log(f"[{i}/{total}] {src['stem']}: deskewing...")
            filename = pipeline.output_name(src["stem"], "deskewed.jpg", multi)
            local_dest = os.path.join(tmp_dir, filename)
            image_magick.deskew(src["local_path"], local_dest, opts=opts)
            local_outputs.append((filename, local_dest, src["bronze_path"]))

        result = pipeline.publish(
            item, pipeline.PUBLIC_FRONTEND_DIR, local_outputs,
            command=["digtk", "image", "deskew", items.item_id(item)],
            params={k: v for k, v in opts.items() if k in ("threshold", "working_width", "output_width", "quality")},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION_BY_SUBDIR["derivatives"],
            },
        )

    return {"status": "success", "item": items.item_id(item), **result}


def prep_ocr(target, opts={}):
    """Generate a single OCR-prep image for every source image in the item.

    Ad hoc — generate()'s orientation comparison produces the actual published
    prep image(s); this is a fixed single pass (deskewed or not, per opts) for
    manual testing.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: Forwarded to image_magick.prep_ocr() (size, dpi, fuzz, quality,
        deskew, threshold).
    :returns: {"status": "success", "item": str (item path), "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    item = items.resolve_target(target)
    image_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "image")
    start = time.monotonic()
    multi = len(image_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": image_paths})
        total = len(sources)
        local_outputs = []
        for i, src in enumerate(sources, start=1):
            utils.log(f"[{i}/{total}] {src['stem']}: preparing for OCR...")
            base_name = "prep-deskew.jpg" if opts.get("deskew") else "prep.jpg"
            filename = pipeline.output_name(src["stem"], base_name, multi)
            local_dest = os.path.join(tmp_dir, filename)
            image_magick.prep_ocr(src["local_path"], local_dest, opts=opts)
            local_outputs.append((filename, local_dest, src["bronze_path"]))

        result = pipeline.publish(
            item, "ocr", local_outputs,
            command=["digtk", "image", "prep-ocr", items.item_id(item)],
            params={k: v for k, v in opts.items() if k in ("deskew", "dpi", "fuzz", "quality", "size", "threshold")},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION_BY_SUBDIR["ocr"],
            },
        )

    return {"status": "success", "item": items.item_id(item), **result}


def generate(target, opts={}):
    """Generate the full derivative set for an item — the Dagster-facing umbrella.

    Stages every source image once, decides scan orientation independently per
    image via a real OCR comparison (pipeline.select_orientation), then applies
    each image's own decision to its own thumbnail set and OCR-prep image. Each
    image's small/medium/large set, plus a pyramidal tiled TIFF for IIIF-style
    deep-zoom serving (digtk.lib.image_magick.tile() — generation only, no serving
    side wired up yet, see docs/cask-conventions.md's "Deep-Zoom Tiles" section),
    publish together into one nested directory, keyed by its own bronze filename
    (see PUBLIC_FRONTEND_DIR and docs/cask-conventions.md's Silver Layout section);
    OCR-prep images still accumulate into one combined ocr/ publish, unaffected by
    this. All local generation happens before anything is published to CaskFS, so
    a failure at any step leaves no partial derivative set behind.

    That per-source images/ manifest also carries an "extra" block — "original"/
    "small"/"medium"/"large"/"tiled"/"ocr" keys, each a {"size", "url"} pair (size
    is a {"width", "height"} dict of strings, or a list of them for "tiled"'s
    multi-level pyramid — stringified via pipeline.size_as_strings() to match the legacy
    manifest's own convention) — simulating the equivalent content the legacy
    DAMS/Fedora manifest carried, but with cask:// reference URIs in place of
    Fin-proxy/GCS/IIIF-server paths (serving URLs are derived from them when the
    item is indexed — see digtk.lib.showcase_index) and "ocr" pointing at the
    page's DjVu text layer, with "imageSize": "large" like legacy's (see
    docs/cask-conventions.md's "Image Manifest Content").

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    Per-file mode (opts['files']) — one Dagster step per image, see
    dagster/lib/pipeline_fanout.py — processes only the named image(s) and
    writes only what belongs to them: their own images/ set and manifest, and
    their files in the shared ocr/ directory. It skips the item-level
    ocr/manifest.json that combines every image; finalize() writes it once
    every image is done. Output
    naming still follows the whole item (e.g. the multi-image stem prefix), so a
    per-file run writes exactly the paths a whole-item run would.

    :param dict opts: 'widths' (comma-separated str, default
        CONFIG["image"]["thumbnail_widths"]) — must resolve to exactly 3 widths
        (small/medium/large); 'files' (iterable of bronze paths or filenames,
        optional — per-file mode, see above; see pipeline.select_binaries());
        forwarded to image_magick/ocr_utils: 'quality', 'dpi', 'fuzz', 'threshold', etc.
    :returns: {"status": "success", "item": str (item path), "derivatives": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}, "ocr": {...} ("manifest" None in
        per-file mode), "orientation": {stem: {"deskew": bool, "stats": {...}}, ...},
        "finalized": bool (False in per-file mode — run finalize())}
    :rtype: dict
    :raises ValueError: If a 'files' selector matches no image, or is ambiguous.
    """
    widths = pipeline.parse_widths(opts.get("widths", CONFIG["image"]["thumbnail_widths"]))
    item = items.resolve_target(target)
    image_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "image")
    files = opts.get("files")
    selected = pipeline.select_binaries(image_paths, files) if files else image_paths
    start = time.monotonic()
    multi = len(image_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": selected})
        total = len(sources)

        derivatives_results = {}
        ocr_outputs = []
        orientations = {}
        command = ["digtk", "image", "derivatives", items.item_id(item), "--widths", ",".join(map(str, widths))]
        for path in (selected if files else []):
            command += ["--file", path]

        for i, src in enumerate(sources, start=1):
            stem = src["stem"]
            filename = os.path.basename(src["bronze_path"])
            utils.log(f"[{i}/{total}] {stem}: deciding orientation (running OCR twice)...")
            orientation = pipeline.select_orientation(src["local_path"], tmp_dir, opts)
            apply_deskew = orientation["deskew"]
            orientations[stem] = orientation
            utils.log(f"[{i}/{total}] {stem}: orientation={'deskewed' if apply_deskew else 'original'}")

            utils.log(f"[{i}/{total}] {stem}: generating {len(widths)} thumbnail(s)...")
            thumb_set = pipeline.generate_thumbnail_set(
                src["local_path"], tmp_dir, widths, stem, {**opts, "deskew": apply_deskew}
            )
            thumb_outputs = [(fn, local_path, src["bronze_path"]) for fn, local_path in thumb_set]

            utils.log(f"[{i}/{total}] {stem}: generating tiled pyramid TIFF...")
            tile_dest = os.path.join(tmp_dir, f"{stem}-tiled.tif")
            image_magick.tile(src["local_path"], tile_dest, opts)
            thumb_outputs.append(("tiled.tif", tile_dest, src["bronze_path"]))

            utils.log(f"[{i}/{total}] {stem}: generating OCR-prep image...")
            base_prep_name = "prep-deskew.jpg" if apply_deskew else "prep.jpg"
            prep_filename = pipeline.output_name(stem, base_prep_name, multi)
            prep_path = os.path.join(tmp_dir, prep_filename)
            image_magick.prep_ocr(src["local_path"], prep_path, opts={**opts, "deskew": apply_deskew})
            ocr_outputs.append((prep_filename, prep_path, src["bronze_path"]))

            utils.log(f"[{i}/{total}] {stem}: probing derivative dimensions...")
            size_by_label = {label: image_magick.probe_dimensions(local_path) for label, (fn, local_path) in zip(pipeline.THUMBNAIL_SIZE_LABELS, thumb_set)}
            images_dir = pipeline.derivative_dir(item, f"{pipeline.PUBLIC_FRONTEND_DIR}/images/{filename}/images")
            ocr_dir = pipeline.derivative_dir(item, "ocr")

            utils.log(f"[{i}/{total}] {stem}: building OCR text layer...")
            ocr_products = pipeline.build_ocr_products(orientation, tmp_dir, stem, multi, size_by_label["large"], opts)
            for ocr_filename, ocr_local_path in ocr_products:
                ocr_outputs.append((ocr_filename, ocr_local_path, src["bronze_path"]))
            djvu_filename = ocr_products[0][0]

            manifest_extra = {
                "original": {
                    "size": pipeline.size_as_strings(image_magick.probe_dimensions(src["local_path"])),
                    "url": pipeline.cask_public_ref(src["bronze_path"]),
                },
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

            utils.log(f"[{i}/{total}] {stem}: publishing derivative set...")
            derivatives_result = pipeline.publish(
                item, f"{pipeline.PUBLIC_FRONTEND_DIR}/images/{filename}/images", thumb_outputs,
                command=command,
                params={
                    "widths": widths,
                    "orientation": {"deskew_selected": apply_deskew, "stats": orientation["stats"]},
                },
                opts={
                    "manifest_opts": {"extra": manifest_extra},
                    "link_relation": _LINK_RELATION_BY_SUBDIR["derivatives"],
                },
            )
            derivatives_results[filename] = derivatives_result

            utils.log(f"[{i}/{total}] {stem}: done")

        duration_s = time.monotonic() - start
        utils.log(f"Publishing {len(ocr_outputs)} OCR-prep file(s)...")
        ocr_result = pipeline.publish(
            item, "ocr", ocr_outputs,
            command=command,
            params={
                "orientation": {
                    stem: {"deskew_selected": o["deskew"]}
                    for stem, o in orientations.items()
                },
            },
            opts={
                "manifest_opts": {"duration_s": duration_s},
                "link_relation": _LINK_RELATION_BY_SUBDIR["ocr"],
                # Per-file runs share ocr/ with the item's other images; its
                # manifest is written once, by finalize().
                "manifest": not files,
            },
        )

    return {
        "status": "success",
        "item": items.item_id(item),
        "derivatives": derivatives_results,
        "ocr": ocr_result,
        "orientation": orientations,
        "finalized": not files,
    }


def _read_manifests(item, image_paths):
    """Read back each image's published images/manifest.json.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param list[str] image_paths: The item's image binaries.
    :returns: {bronze path: manifest dict}.
    :rtype: dict
    :raises FileNotFoundError: If any image's derivatives manifest is missing
        (its per-image run hasn't completed).
    """
    manifests, missing = {}, []
    for path in image_paths:
        filename = os.path.basename(path)
        images_dir = pipeline.derivative_dir(item, f"{pipeline.PUBLIC_FRONTEND_DIR}/images/{filename}/images")
        try:
            manifests[path] = json.loads(fs.read(f"{images_dir}/manifest.json"))
        except FileNotFoundError:
            missing.append(filename)
    if missing:
        raise FileNotFoundError(
            f"no derivatives published yet for {len(missing)} image(s) of {items.item_id(item)}: {missing}"
        )
    return manifests


def media_objects(item):
    """The item's "image" associatedMedia entries — small/medium/large
    ImageObjects for every image — rebuilt from each image's published
    images/manifest.json. Read by
    digtk.models.digital_website.item_record.build().

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: ImageObject entries, in image order.
    :rtype: list[dict]
    :raises FileNotFoundError: If any image's derivatives manifest is missing.
    """
    image_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "image")
    manifests = _read_manifests(item, image_paths)
    objects = []
    for path in image_paths:
        objects += pipeline.image_objects_from_manifest(manifests[path])
    return objects


def finalize(target, opts={}):
    """Write an image item's item-level outputs from its already-published
    per-image derivatives — the step after one-per-image generate() runs (see
    its 'files' opt).

    Reads each image's own images/manifest.json back (orientation decision)
    rather than regenerating anything, then writes ocr/manifest.json
    (per-stem orientation, listing each image's prep/djvu/stats files) — the
    same content a whole-item generate() writes.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: Unused; present for signature symmetry.
    :returns: {"status": "success", "item": str (item path), "images": int, "ocr_manifest": {...}}
    :rtype: dict
    :raises FileNotFoundError: If any image's derivatives manifest is missing
        (its per-image run hasn't completed) — nothing is written in that case.
    """
    item = items.resolve_target(target)
    image_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "image")
    multi = len(image_paths) > 1
    manifests = _read_manifests(item, image_paths)

    orientation, outputs = {}, []
    for path in image_paths:
        manifest = manifests[path]
        stem = os.path.splitext(os.path.basename(path))[0]
        deskew = manifest["params"]["orientation"]["deskew_selected"]
        orientation[stem] = {"deskew_selected": deskew}
        outputs += [
            pipeline.output_name(stem, "prep-deskew.jpg" if deskew else "prep.jpg", multi),
            pipeline.output_name(stem, "ocr.djvu", multi),
            pipeline.output_name(stem, "ocr-stats.json", multi),
        ]

    utils.log(f"Writing ocr/manifest.json for {len(image_paths)} image(s)...")
    ocr_rel_path = f"{item.item_path}/ocr" if item.item_path else "ocr"
    ocr_manifest = utils.write_manifest(
        item.collection, ocr_rel_path, ["digtk", "showcase", "finalize", items.item_id(item)],
        {"orientation": orientation}, outputs, source_files=image_paths,
        opts={"group": item.group, "time": item.time},
    )
    return {"status": "success", "item": items.item_id(item), "images": len(image_paths), "ocr_manifest": ocr_manifest}
