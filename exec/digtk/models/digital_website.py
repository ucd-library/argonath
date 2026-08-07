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

No SystemExit/utils.result()/utils.error() calls here — functions return a
plain dict on success and let exceptions propagate; digtk.commands.image is
the only layer that translates those into the CLI's last-line-JSON convention.
"""
import json
import os
import tempfile
import time

from digtk.lib import derivative, fs, image_magick, items, ocr_utils, utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS, SCHEMA_CONTEXT

_EXTENSION_BY_FORMAT = {
    "jpeg": ".jpg", "jpg": ".jpg", "png": ".png",
    "tiff": ".tif", "tif": ".tif", "webp": ".webp", "gif": ".gif",
}

# Derivative-link relation per process subdir, distinguishing what an output
# actually is for (a viewable thumbnail vs. an OCR-engine input) rather than
# leaving every link at CaskFS's generic default (schema:source) — lets
# `cask lineage derivatives <bronze-file>` filter by which kind of derivative
# it wants. See caskfs's docs/structural-metadata.md for the lineage model.
_LINK_RELATION_BY_SUBDIR = {
    "derivatives": f"{ARGONATH_NS}derivativeOf",
    "ocr": f"{ARGONATH_NS}ocrPrepOf",
}


def _parse_widths(widths):
    """Parse a thumbnail-widths spec into a sorted list of ints.

    :param str|list[int] widths: Comma-separated string (e.g. "256,512,1024"), or already a list.
    :returns: Widths sorted ascending.
    :rtype: list[int]
    """
    if isinstance(widths, str):
        widths = [int(w.strip()) for w in widths.split(",")]
    return sorted(widths)


def _thumbnail_filename(width, widths):
    """The largest configured width is the "web" derivative; the rest are "thumb"s.

    :param int width: Width to name.
    :param list[int] widths: Full set of widths being generated.
    :rtype: str
    """
    return f"web-{width}.jpg" if width == max(widths) else f"thumb-{width}.jpg"


def _output_name(stem, base_name, multi):
    """Disambiguate an output filename when an item has more than one source image.

    A single-image item's outputs are named exactly as before (no behavior change,
    no churn for the common case). A multi-image item's outputs are prefixed with
    the source image's own filename stem, since they land in the same shared
    derivatives/ocr directory (see docs/cask-conventions.md) rather than a
    per-image subdirectory.

    :param str stem: Source image's filename stem (no extension).
    :param str base_name: The un-prefixed output filename, e.g. "thumb-256.jpg".
    :param bool multi: True if this item has more than one source image.
    :rtype: str
    """
    return f"{stem}-{base_name}" if multi else base_name


def _stage_sources(item, tmp_dir):
    """Stream every one of the item's bronze binaries into local temp files.

    Preserves each binary's original filename (not just extension) — needed for
    TIFF frame-select/format inference by digtk.lib.image_magick, and to derive
    the filename stem used to disambiguate outputs for multi-image items.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str tmp_dir: Local directory to stage into.
    :returns: One entry per binary, in item.bronze_binary_paths order.
    :rtype: list[dict] -- each {"bronze_path": str, "local_path": str, "stem": str}
    """
    sources = []
    total = len(item.bronze_binary_paths)
    for i, bronze_path in enumerate(item.bronze_binary_paths, start=1):
        filename = bronze_path.rsplit("/", 1)[-1]
        stem = os.path.splitext(filename)[0]
        utils.log(f"[{i}/{total}] Staging {filename}...")
        local_path = os.path.join(tmp_dir, filename)
        with fs.read_stream(bronze_path) as chunks:
            with open(local_path, "wb") as f:
                for chunk in chunks:
                    f.write(chunk)
        sources.append({"bronze_path": bronze_path, "local_path": local_path, "stem": stem})
    return sources


def _publish(item, subdir, local_outputs, command, params, opts={}):
    """Write local output files to CaskFS (with lineage) and record a manifest.

    Every write goes through digtk.lib.derivative.write() — the canonical way to
    write a silver pipeline output (see CLAUDE.md) — rather than hand-building
    the CaskFS path and calling fs.write() directly, so each derivative's lineage
    back to the specific bronze binary it was generated from is recorded. Each
    link also carries a relation distinguishing what kind of derivative it is
    (_LINK_RELATION_BY_SUBDIR) and metadata recording the exact command/params
    that produced it — CaskFS stores that metadata as opaque text (see caskfs's
    docs/structural-metadata.md), so it's JSON-encoded here for structure on our
    side, not because CaskFS interprets it.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str subdir: Process directory under the item's silver location (e.g. "derivatives", "ocr").
    :param list[tuple[str, str, str]] local_outputs: (filename, local_path, source_bronze_path)
        triples to publish — source_bronze_path is which of item.bronze_binary_paths this
        specific output was derived from.
    :param list[str] command: Full argv that produced these outputs, for the manifest
        and for each output's derivative-link metadata.
    :param dict params: Parsed/typed params, for the manifest and link metadata.
    :param dict opts: 'manifest_opts' forwarded to utils.write_manifest (e.g. duration_s).
    :returns: {"dir": CaskFS process directory ('cask:/...'), "outputs": [filenames], "manifest": {...}}
    :rtype: dict
    """
    rel_path = f"{item.item_path}/{subdir}" if item.item_path else subdir
    write_opts = {
        "group": item.group, "time": item.time, "replace": True,
        "link_metadata": json.dumps({"command": command, "params": params}),
    }
    relation = _LINK_RELATION_BY_SUBDIR.get(subdir)
    if relation:
        write_opts["link_relation"] = relation

    outputs = []
    source_files = []
    for filename, local_path, source_bronze_path in local_outputs:
        with open(local_path, "rb") as f:
            data = f.read()
        derivative.write(
            type="silver", collection=item.collection, rel_path=f"{rel_path}/{filename}",
            data=data, source_files=[source_bronze_path], opts=write_opts,
        )
        outputs.append(filename)
        if source_bronze_path not in source_files:
            source_files.append(source_bronze_path)

    manifest = utils.write_manifest(
        item.collection, rel_path, command, params, outputs,
        source_files=source_files,
        opts={**opts.get("manifest_opts", {}), "group": item.group, "time": item.time},
    )
    dest_dir = f"cask:{derivative.dest_path(type='silver', collection=item.collection, rel_path=rel_path, opts={'group': item.group, 'time': item.time})}"
    return {"dir": dest_dir, "outputs": outputs, "manifest": manifest}


def _cask_public_ref(internal_path):
    """Convert an internal 'cask:/...' digtk.lib.fs dispatch path into the
    public 'cask://...' reference URI form used in JSON-LD documents (per
    caskfs's docs/ld.md "Reference Binary File" convention).

    :param str internal_path: e.g. "cask:/silver/g/c/latest/item/derivatives/thumb-256.jpg".
    :returns: e.g. "cask://silver/g/c/latest/item/derivatives/thumb-256.jpg".
    :rtype: str
    """
    return "cask:/" + internal_path[len("cask:"):]


def _update_associated_media(item, image_objects, source_files):
    """Merge schema:associatedMedia ImageObject refs into the item's silver
    sidecar, creating it if absent. Never touches the bronze sidecar — bronze
    is immutable/append-only, so derivative metadata lives in a new
    silver/.../[item]/item.jsonld.json instead (see docs/cask-conventions.md).

    Uses a plain dict read/merge/write rather than digtk.lib.schema.ItemMetadata
    (its to_jsonld/from_jsonld round-trip is itself an unimplemented phase-0.2
    stub) — only the @context/@id/ark/associatedMedia fields this needs. Reads
    still go through fs directly (reading isn't restricted); the write goes
    through digtk.lib.derivative, same as every other pipeline output.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param list[dict] image_objects: ImageObject entries, e.g.
        {"@type": "ImageObject", "contentUrl": "cask://...", "width": 256, "height": 384}.
    :param list[str] source_files: CaskFS paths ('cask:/'-prefixed) this sidecar's
        associatedMedia was derived from — this item's full set of bronze binaries.
    :returns: The full sidecar dict that was written.
    :rtype: dict
    """
    rel_path = f"{item.item_path}/item.jsonld.json" if item.item_path else "item.jsonld.json"
    write_opts = {"group": item.group, "time": item.time, "replace": True}
    dest = derivative.dest_path(type="silver", collection=item.collection, rel_path=rel_path, opts=write_opts)
    sidecar_path = f"cask:{dest}"

    if fs.exists(sidecar_path):
        doc = json.loads(fs.read(sidecar_path))
    else:
        doc = {"@context": SCHEMA_CONTEXT, "@id": item.ark, "ark": item.ark}

    doc["associatedMedia"] = image_objects
    derivative.write(
        type="silver", collection=item.collection, rel_path=rel_path,
        data=json.dumps(doc, indent=2).encode(), source_files=source_files, opts=write_opts,
    )
    return doc


def _generate_thumbnail_set(local_src, tmp_dir, widths, stem, multi, opts={}):
    """Generate one resized JPEG per configured width, all sharing the same orientation.

    :param str local_src: Local path to the staged source image.
    :param str tmp_dir: Local directory to write outputs into.
    :param list[int] widths: Widths to generate.
    :param str stem: Source image's filename stem, for multi-image disambiguation.
    :param bool multi: True if this item has more than one source image.
    :param dict opts: 'deskew' (bool, default False) applies the deskew recipe to every
        width instead of a plain resize; 'quality' forwarded to image_magick.
    :returns: (filename, local_path) pairs, one per width.
    :rtype: list[tuple[str, str]]
    """
    quality = opts.get("quality", CONFIG["image"]["thumbnail_quality"])
    apply_deskew = opts.get("deskew", False)
    outputs = []
    for width in widths:
        filename = _output_name(stem, _thumbnail_filename(width, widths), multi)
        local_path = os.path.join(tmp_dir, filename)
        if apply_deskew:
            image_magick.deskew(local_src, local_path, opts={**opts, "output_width": width, "quality": quality})
        else:
            image_magick.thumbnail(local_src, local_path, width, opts={"quality": quality})
        outputs.append((filename, local_path))
    return outputs


def _select_orientation(local_src, tmp_dir, opts={}):
    """Decide scan orientation for one image via a real OCR comparison.

    Generates both OCR-prep candidates (deskewed and non-deskewed), then hands
    them to digtk.lib.ocr_utils.compare_orientations() — the same shared,
    decoupled primitive the (separate, later) OCR phase will call on its own.
    Runs once per source image — a multi-image item decides orientation
    independently for each of its images, since there's no reliable signal
    (yet) that images sharing an item are a sequential document rather than
    independent photographs that happen to share a folder.

    :param str local_src: Local path to the staged source image.
    :param str tmp_dir: Local directory to write candidate images into.
    :param dict opts: Forwarded to image_magick.prep_ocr() and ocr_utils.compare_orientations().
    :returns: {"deskew": bool, "stats": {"original": {...}, "deskewed": {...}}}
    :rtype: dict
    """
    candidates = {}
    for label, apply_deskew in (("original", False), ("deskewed", True)):
        path = os.path.join(tmp_dir, f"orientation-{label}-{os.path.basename(local_src)}.jpg")
        image_magick.prep_ocr(local_src, path, opts={**opts, "deskew": apply_deskew})
        candidates[label] = path

    comparison = ocr_utils.compare_orientations(candidates, opts=opts)
    return {"deskew": comparison["selected"] == "deskewed", "stats": comparison["stats"]}


def thumbnails(ark, opts={}):
    """Generate thumbnail (+ web) JPEGs at each configured width, for every
    source image in the item.

    Standalone, deterministic — no orientation comparison. For ad-hoc/manual
    use; generate() is what Dagster's derivatives_job actually calls.

    :param str ark: Item ARK identifier.
    :param dict opts: 'widths' (comma-separated str, default
        CONFIG["image"]["thumbnail_widths"]), 'quality' (int).
    :returns: {"status": "success", "ark": str, "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    widths = _parse_widths(opts.get("widths", CONFIG["image"]["thumbnail_widths"]))
    item = items.resolve(ark)
    start = time.monotonic()
    multi = len(item.bronze_binary_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = _stage_sources(item, tmp_dir)
        total = len(sources)
        local_outputs = []
        for i, src in enumerate(sources, start=1):
            utils.log(f"[{i}/{total}] {src['stem']}: generating {len(widths)} thumbnail(s)...")
            thumb_set = _generate_thumbnail_set(src["local_path"], tmp_dir, widths, src["stem"], multi, opts)
            local_outputs += [(filename, local_path, src["bronze_path"]) for filename, local_path in thumb_set]

        result = _publish(
            item, "derivatives", local_outputs,
            command=["digtk", "image", "thumbnail", ark, "--widths", ",".join(map(str, widths))],
            params={"widths": widths},
            opts={"manifest_opts": {"duration_s": time.monotonic() - start}},
        )

    return {"status": "success", "ark": ark, **result}


def convert(ark, opts={}):
    """Convert every source image's binary to another format.

    Ad hoc — writes standalone `derivatives/converted.<ext>` file(s), not part of
    generate()'s standard derivative set.

    :param str ark: Item ARK identifier.
    :param dict opts: 'format' (str, default CONFIG["image"]["convert_format"]), 'quality' (int).
    :returns: {"status": "success", "ark": str, "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    fmt = opts.get("format", CONFIG["image"]["convert_format"])
    ext = _EXTENSION_BY_FORMAT.get(fmt.lower(), f".{fmt.lower()}")
    item = items.resolve(ark)
    start = time.monotonic()
    multi = len(item.bronze_binary_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = _stage_sources(item, tmp_dir)
        total = len(sources)
        local_outputs = []
        for i, src in enumerate(sources, start=1):
            utils.log(f"[{i}/{total}] {src['stem']}: converting to {fmt}...")
            filename = _output_name(src["stem"], f"converted{ext}", multi)
            local_dest = os.path.join(tmp_dir, filename)
            image_magick.convert_format(src["local_path"], local_dest, opts={"quality": opts.get("quality", CONFIG["image"]["thumbnail_quality"])})
            local_outputs.append((filename, local_dest, src["bronze_path"]))

        result = _publish(
            item, "derivatives", local_outputs,
            command=["digtk", "image", "convert", ark, "--format", fmt],
            params={"format": fmt},
            opts={"manifest_opts": {"duration_s": time.monotonic() - start}},
        )

    return {"status": "success", "ark": ark, **result}


def deskew(ark, opts={}):
    """Apply a single deterministic deskew pass to every source image.

    Ad hoc — generate()'s OCR comparison is what actually decides deskew for
    the published derivative set; this is a fixed single pass for manual testing.

    :param str ark: Item ARK identifier.
    :param dict opts: Forwarded to image_magick.deskew() (threshold, working_width,
        output_width, quality).
    :returns: {"status": "success", "ark": str, "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    item = items.resolve(ark)
    start = time.monotonic()
    multi = len(item.bronze_binary_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = _stage_sources(item, tmp_dir)
        total = len(sources)
        local_outputs = []
        for i, src in enumerate(sources, start=1):
            utils.log(f"[{i}/{total}] {src['stem']}: deskewing...")
            filename = _output_name(src["stem"], "deskewed.jpg", multi)
            local_dest = os.path.join(tmp_dir, filename)
            image_magick.deskew(src["local_path"], local_dest, opts=opts)
            local_outputs.append((filename, local_dest, src["bronze_path"]))

        result = _publish(
            item, "derivatives", local_outputs,
            command=["digtk", "image", "deskew", ark],
            params={k: v for k, v in opts.items() if k in ("threshold", "working_width", "output_width", "quality")},
            opts={"manifest_opts": {"duration_s": time.monotonic() - start}},
        )

    return {"status": "success", "ark": ark, **result}


def prep_ocr(ark, opts={}):
    """Generate a single OCR-prep image for every source image in the item.

    Ad hoc — generate()'s orientation comparison produces the actual published
    prep image(s); this is a fixed single pass (deskewed or not, per opts) for
    manual testing.

    :param str ark: Item ARK identifier.
    :param dict opts: Forwarded to image_magick.prep_ocr() (size, dpi, fuzz, quality,
        deskew, threshold).
    :returns: {"status": "success", "ark": str, "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    item = items.resolve(ark)
    start = time.monotonic()
    multi = len(item.bronze_binary_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = _stage_sources(item, tmp_dir)
        total = len(sources)
        local_outputs = []
        for i, src in enumerate(sources, start=1):
            utils.log(f"[{i}/{total}] {src['stem']}: preparing for OCR...")
            base_name = "prep-deskew.jpg" if opts.get("deskew") else "prep.jpg"
            filename = _output_name(src["stem"], base_name, multi)
            local_dest = os.path.join(tmp_dir, filename)
            image_magick.prep_ocr(src["local_path"], local_dest, opts=opts)
            local_outputs.append((filename, local_dest, src["bronze_path"]))

        result = _publish(
            item, "ocr", local_outputs,
            command=["digtk", "image", "prep-ocr", ark],
            params={k: v for k, v in opts.items() if k in ("deskew", "dpi", "fuzz", "quality", "size", "threshold")},
            opts={"manifest_opts": {"duration_s": time.monotonic() - start}},
        )

    return {"status": "success", "ark": ark, **result}


def generate(ark, opts={}):
    """Generate the full derivative set for an item — the Dagster-facing umbrella.

    Stages every source image once, decides scan orientation independently per
    image via a real OCR comparison (_select_orientation), then applies each
    image's own decision to its own thumbnail set and OCR-prep image. All
    images' outputs accumulate into one combined derivatives/ publish and one
    combined ocr/ publish (matching the item's single derivatives/ and ocr/
    directories — no per-image subdirectory nesting). All local generation
    happens before anything is published to CaskFS, so a failure at any step
    leaves no partial derivative set behind.

    :param str ark: Item ARK identifier.
    :param dict opts: 'widths' (comma-separated str, default
        CONFIG["image"]["thumbnail_widths"]); forwarded to image_magick/ocr_utils:
        'quality', 'dpi', 'fuzz', 'threshold', etc.
    :returns: {"status": "success", "ark": str, "derivatives": {...}, "ocr": {...},
        "orientation": {stem: {"deskew": bool, "stats": {...}}, ...}}
    :rtype: dict
    """
    widths = _parse_widths(opts.get("widths", CONFIG["image"]["thumbnail_widths"]))
    item = items.resolve(ark)
    start = time.monotonic()
    multi = len(item.bronze_binary_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = _stage_sources(item, tmp_dir)
        total = len(sources)

        thumb_outputs = []
        ocr_outputs = []
        orientations = {}

        for i, src in enumerate(sources, start=1):
            stem = src["stem"]
            utils.log(f"[{i}/{total}] {stem}: deciding orientation (running OCR twice)...")
            orientation = _select_orientation(src["local_path"], tmp_dir, opts)
            apply_deskew = orientation["deskew"]
            orientations[stem] = orientation
            utils.log(f"[{i}/{total}] {stem}: orientation={'deskewed' if apply_deskew else 'original'}")

            utils.log(f"[{i}/{total}] {stem}: generating {len(widths)} thumbnail(s)...")
            thumb_set = _generate_thumbnail_set(
                src["local_path"], tmp_dir, widths, stem, multi, {**opts, "deskew": apply_deskew}
            )
            thumb_outputs += [(filename, local_path, src["bronze_path"]) for filename, local_path in thumb_set]

            utils.log(f"[{i}/{total}] {stem}: generating OCR-prep image...")
            base_prep_name = "prep-deskew.jpg" if apply_deskew else "prep.jpg"
            prep_filename = _output_name(stem, base_prep_name, multi)
            prep_path = os.path.join(tmp_dir, prep_filename)
            image_magick.prep_ocr(src["local_path"], prep_path, opts={**opts, "deskew": apply_deskew})
            ocr_outputs.append((prep_filename, prep_path, src["bronze_path"]))
            utils.log(f"[{i}/{total}] {stem}: done")

        duration_s = time.monotonic() - start
        command = ["digtk", "image", "derivatives", ark, "--widths", ",".join(map(str, widths))]

        utils.log(f"Publishing {len(thumb_outputs)} derivative file(s)...")
        derivatives_result = _publish(
            item, "derivatives", thumb_outputs,
            command=command,
            params={
                "widths": widths,
                "orientation": {
                    stem: {"deskew_selected": o["deskew"], "stats": o["stats"]}
                    for stem, o in orientations.items()
                },
            },
            opts={"manifest_opts": {"duration_s": duration_s}},
        )
        utils.log(f"Publishing {len(ocr_outputs)} OCR-prep file(s)...")
        ocr_result = _publish(
            item, "ocr", ocr_outputs,
            command=command,
            params={
                "orientation": {
                    stem: {"deskew_selected": o["deskew"]}
                    for stem, o in orientations.items()
                },
            },
            opts={"manifest_opts": {"duration_s": duration_s}},
        )

        utils.log("Updating associated-media sidecar...")
        image_objects = []
        for filename, local_path, _source_bronze_path in thumb_outputs:
            dims = image_magick.probe_dimensions(local_path)
            image_objects.append({
                "@type": "ImageObject",
                "contentUrl": _cask_public_ref(f"{derivatives_result['dir']}/{filename}"),
                "encodingFormat": "image/jpeg",
                **dims,
            })
        _update_associated_media(item, image_objects, [src["bronze_path"] for src in sources])

    return {
        "status": "success",
        "ark": ark,
        "derivatives": derivatives_result,
        "ocr": ocr_result,
        "orientation": orientations,
    }
