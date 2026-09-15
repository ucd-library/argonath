"""
Shared derivative-generation pipeline plumbing.

Extracted from digtk.models.digital_website.image (the original, still image-specific,
orchestrator) once PDF/video/audio orchestration needed the exact same
staging/publishing/lineage machinery. Every function here is ARK-aware (they all take an
already-resolved digtk.lib.items.ItemLocation) but otherwise media-type-agnostic — no
function assumes its inputs are images specifically. image_magick-specific helpers
(generate_thumbnail_set, select_orientation) are still here rather than in a
format-specific lib module because a rasterized PDF page is itself just a local image
file by the time these run, so PDF derivative generation reuses them unchanged.

This is the only layer any orchestrator (digital_website.image, .pdf, .video, .audio)
should go through to write silver pipeline output — see CLAUDE.md's
digtk.lib.derivative entry. Reads still go through digtk.lib.fs directly.
"""
import json
import os

from digtk.lib import derivative, djvu, fs, image_magick, ocr_utils, utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import SCHEMA_CONTEXT

EXTENSION_BY_FORMAT = {
    "jpeg": ".jpg", "jpg": ".jpg", "png": ".png",
    "tiff": ".tif", "tif": ".tif", "webp": ".webp", "gif": ".gif",
}

# Top-level silver subdirectory for the small/medium/large product set image.py and
# pdf.py publish for frontend consumption — distinct from "derivatives" (the older,
# flatter name) to make clear this is specifically the public-facing product line, not
# every kind of derived artifact (OCR-prep images, AI metadata, etc. still live in
# their own top-level dirs). See docs/cask-conventions.md's Silver Layout section.
PUBLIC_FRONTEND_DIR = "public-frontend-derivatives"

# generate_thumbnail_set() always produces exactly these three, ascending by width —
# a fixed semantic-size product line rather than one file per configured pixel width.
THUMBNAIL_SIZE_LABELS = ["small", "medium", "large"]

# An item's bronze binaries can legitimately span more than one media format — e.g. an
# archival transfer supplying both a compiled PDF and per-page TIFF masters for the same
# physical document (both supplied directly by the archiving team, neither derived from
# the other; see docs/cask-conventions.md). Each format-specific orchestrator
# (digital_website.image, .pdf, .video, .audio, .captions) filters down
# to its own subset via filter_binaries_by_format() rather than assuming
# item.bronze_binary_paths is homogeneous.
FORMAT_BY_EXTENSION = {
    ".tif": "image", ".tiff": "image", ".jpg": "image", ".jpeg": "image",
    ".png": "image", ".gif": "image", ".webp": "image",
    ".pdf": "pdf",
    ".mp4": "video", ".mov": "video", ".avi": "video", ".mkv": "video", ".webm": "video",
    ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".m4a": "audio", ".ogg": "audio",
}


def filter_binaries_by_format(bronze_binary_paths, media_types):
    """Filter an item's bronze binaries down to just the ones matching a media format.

    :param list[str] bronze_binary_paths: CaskFS paths ('cask:/'-prefixed).
    :param str|set[str] media_types: A single format ("image", "pdf", "video", "audio")
        or a set of them — captions.py passes {"video", "audio"} since captions apply
        to either.
    :returns: The subset of paths whose extension matches, in original order.
    :rtype: list[str]
    :raises ValueError: If no binary matches, or a binary's extension isn't recognized
        at all (distinct from just not matching media_types — an unrecognized extension
        means the item has a binary no format-specific pipeline can claim).
    """
    wanted = {media_types} if isinstance(media_types, str) else set(media_types)
    matches = []
    for path in bronze_binary_paths:
        ext = os.path.splitext(path)[1].lower()
        fmt = FORMAT_BY_EXTENSION.get(ext)
        if fmt is None:
            raise ValueError(f"unrecognized media extension {ext!r} in bronze binary: {path}")
        if fmt in wanted:
            matches.append(path)
    if not matches:
        raise ValueError(f"no {'/'.join(sorted(wanted))} binaries found among this item's bronze sources")
    return matches


def partition_by_format(bronze_binary_paths):
    """Split an item's bronze binaries into image and PDF subsets.

    Unlike filter_binaries_by_format(), doesn't raise when a format has zero
    matches (a real item may have only one of the two, or — rarely — both), and
    doesn't raise on a binary of a format the caller just doesn't apply to
    (video/audio, or something unrecognized) — silently skips it rather than
    enforcing every binary be a recognized format. Shared by
    digtk.models.accessibility (alt-text/tagging, image or PDF) and
    digtk.models.ocr (Tesseract + vision OCR, same either-or shape).

    :param list[str] bronze_binary_paths: CaskFS paths ('cask:/'-prefixed).
    :returns: (image_paths, pdf_paths), each a subset of bronze_binary_paths, in order.
    :rtype: tuple[list[str], list[str]]
    """
    image_paths, pdf_paths = [], []
    for path in bronze_binary_paths:
        ext = os.path.splitext(path)[1].lower()
        fmt = FORMAT_BY_EXTENSION.get(ext)
        if fmt == "image":
            image_paths.append(path)
        elif fmt == "pdf":
            pdf_paths.append(path)
    return image_paths, pdf_paths


def parse_widths(widths):
    """Parse a thumbnail-widths spec into a sorted list of ints.

    :param str|list[int] widths: Comma-separated string (e.g. "256,512,1024"), or already a list.
    :returns: Widths sorted ascending.
    :rtype: list[int]
    """
    if isinstance(widths, str):
        widths = [int(w.strip()) for w in widths.split(",")]
    return sorted(widths)


def page_tag(page_index, count):
    """Zero-padded page directory name, wide enough for the item's own page count.

    Shared by digital_website.pdf (naming its ocr/page-NNN publish dirs) and
    any other per-page PDF consumer (e.g. accessibility.tag_pdf, reading those
    same published paths back) that needs to reconstruct the identical name.

    :param int page_index: 0-indexed page number.
    :param int count: Total page count (pad width scales with this, minimum 3 digits).
    :rtype: str
    """
    pad = max(3, len(str(max(count - 1, 0))))
    return f"page-{page_index:0{pad}d}"


def pdf_ocr_prep_images(item, bronze_path, namespace_ocr):
    """Read pdf.derivatives()'s aggregate OCR manifest for one PDF binary and
    return each page's already-published OCR-prep image cask path, in page order.

    Mirrors digtk.models.digital_website.pdf.derivatives()'s own ocr_prefix/
    page_tag/filename construction exactly (ocr_prefix = f"{stem}/" if
    namespace_ocr else "", filename = f"{stem}-{page_tag}-{prep[-deskew]}.jpg"),
    so this reads back precisely what that command published. Shared by
    digtk.models.accessibility.tag_pdf and digtk.models.ocr.run_ocr — both need
    to locate the same published images, just to feed different tools.

    :param ItemLocation item: Resolved item location.
    :param str bronze_path: This PDF's bronze binary cask path.
    :param bool namespace_ocr: True if pdf.derivatives() namespaced this PDF's
        OCR output by its own filename — when the item has more than one PDF
        binary, *or* the item also has non-PDF binaries (a bare "ocr/"
        directory would otherwise collide with e.g. image.py's own
        ocr/manifest.json for a co-located image binary). Callers compute
        this the same way pdf.derivatives() does:
        `len(pdf_paths) > 1 or len(pdf_paths) < len(item.bronze_binary_paths)`.
    :returns: (page image cask paths, in page order; page count)
    :rtype: tuple[list[str], int]
    :raises FileNotFoundError: If `digtk public-dc pdf derivatives` hasn't
        published this PDF's OCR-prep images yet.
    """
    filename = bronze_path.rsplit("/", 1)[-1]
    stem = filename.rsplit(".", 1)[0]
    ocr_prefix = f"{stem}/" if namespace_ocr else ""
    ocr_base_dir = derivative_dir(item, f"{ocr_prefix}ocr")
    manifest = json.loads(fs.read(f"{ocr_base_dir}/manifest.json"))
    count = manifest["params"]["pages"]
    orientation = manifest["params"]["orientation"]

    images = []
    for page_no in range(1, count + 1):
        tag = page_tag(page_no - 1, count)
        deskewed = orientation[str(page_no)]["deskew_selected"]
        base_name = "prep-deskew.jpg" if deskewed else "prep.jpg"
        page_dir = derivative_dir(item, f"{ocr_prefix}ocr/{tag}")
        images.append(f"{page_dir}/{stem}-{tag}-{base_name}")
    return images, count


def image_ocr_prep_image(item, bronze_path, multi_image):
    """Read digital_website.image.generate()'s ocr/manifest.json for one image
    binary and return its already-published OCR-prep image cask path.

    Mirrors generate()'s own ocr_outputs filename construction exactly —
    prep_filename = output_name(stem, "prep[-deskew].jpg", multi) — reading
    the deskew decision back from the manifest params.orientation dict, keyed
    by filename stem (not page number — that's the PDF aggregate manifest's
    convention, see pdf_ocr_prep_images(), which this deliberately parallels
    for the image case).

    :param ItemLocation item: Resolved item location.
    :param str bronze_path: This image's bronze binary cask path.
    :param bool multi_image: True if the item has more than one image binary
        (pipeline.filter_binaries_by_format(item.bronze_binary_paths, "image")).
    :returns: The published OCR-prep image's cask path.
    :rtype: str
    :raises FileNotFoundError: If `digtk public-dc image derivatives` hasn't
        published this image's OCR-prep image yet.
    """
    filename = bronze_path.rsplit("/", 1)[-1]
    stem = filename.rsplit(".", 1)[0]
    ocr_dir = derivative_dir(item, "ocr")
    manifest = json.loads(fs.read(f"{ocr_dir}/manifest.json"))
    deskewed = manifest["params"]["orientation"][stem]["deskew_selected"]
    base_name = "prep-deskew.jpg" if deskewed else "prep.jpg"
    return f"{ocr_dir}/{output_name(stem, base_name, multi_image)}"


def output_name(stem, base_name, multi):
    """Disambiguate an output filename when an item has more than one source binary.

    A single-source item's outputs are named exactly as before (no behavior change, no
    churn for the common case). A multi-source item's outputs are prefixed with the
    source's own filename stem, since they land in the same shared process directory
    (see docs/cask-conventions.md) rather than a per-source subdirectory.

    :param str stem: Source binary's filename stem (no extension).
    :param str base_name: The un-prefixed output filename, e.g. "thumb-256.jpg".
    :param bool multi: True if this item has more than one source binary.
    :rtype: str
    """
    return f"{stem}-{base_name}" if multi else base_name


def stage_sources(item, tmp_dir, opts={}):
    """Stream a set of the item's bronze binaries into local temp files.

    Preserves each binary's original filename (not just extension) — needed for
    TIFF frame-select/format inference by digtk.lib.image_magick, and to derive
    the filename stem used to disambiguate outputs for multi-source items.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str tmp_dir: Local directory to stage into.
    :param dict opts: 'bronze_binary_paths' (list[str], default item.bronze_binary_paths)
        — stage this explicit subset instead of every binary under the item. Callers
        that only want their own format's binaries (see filter_binaries_by_format())
        pass the filtered list here, since an item's bronze binaries can legitimately
        span more than one media format.
    :returns: One entry per binary, in the given order.
    :rtype: list[dict] -- each {"bronze_path": str, "local_path": str, "stem": str}
    """
    bronze_binary_paths = opts.get("bronze_binary_paths", item.bronze_binary_paths)
    sources = []
    total = len(bronze_binary_paths)
    for i, bronze_path in enumerate(bronze_binary_paths, start=1):
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


def derivative_dir(item, subdir):
    """Compute the CaskFS silver directory a publish() call to this subdir would use.

    A pure function of inputs already known before calling publish() — lets a
    caller (e.g. digtk.models.digital_website.image's generate(), building its
    manifest's "extra" content) build cask:// url references into a directory
    before that directory's write/manifest actually happens, without needing
    publish()'s return value first. publish() itself calls this internally.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str subdir: Same 'subdir' publish() would be called with.
    :returns: CaskFS process directory ('cask:/...').
    :rtype: str
    """
    rel_path = f"{item.item_path}/{subdir}" if item.item_path else subdir
    return f"cask:{derivative.dest_path(type='silver', collection=item.collection, rel_path=rel_path, opts={'group': item.group, 'time': item.time})}"


def publish(item, subdir, local_outputs, command, params, opts={}):
    """Write local output files to CaskFS (with lineage) and record a manifest.

    Every write goes through digtk.lib.derivative.write() — the canonical way to
    write a silver pipeline output (see CLAUDE.md) — rather than hand-building
    the CaskFS path and calling fs.write() directly, so each derivative's lineage
    back to the specific bronze binary it was generated from is recorded. CaskFS
    stores link metadata as opaque text (see caskfs's docs/structural-metadata.md),
    so the command/params are JSON-encoded here for structure on our side, not
    because CaskFS interprets it.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str subdir: Process directory under the item's silver location (e.g.
        "derivatives", "ocr", "derivatives/page-000", "stream", "captions").
    :param list[tuple[str, str, str]] local_outputs: (filename, local_path, source_bronze_path)
        triples to publish — source_bronze_path is which of item.bronze_binary_paths this
        specific output was derived from.
    :param list[str] command: Full argv that produced these outputs, for the manifest
        and for each output's derivative-link metadata.
    :param dict params: Parsed/typed params, for the manifest and link metadata.
    :param dict opts: 'manifest_opts' forwarded to utils.write_manifest (e.g. duration_s).
        'link_relation' (str, optional) — forwarded to derivative.write() as every
        output's CaskFS lineage relation. The caller decides this per subdir rather than
        this function inferring it from the subdir string, since subdir naming doesn't map
        1:1 to a relation across every media type this pipeline now serves (e.g.
        "derivatives/page-000" for a PDF page vs. plain "derivatives" for an image).
    :returns: {"dir": CaskFS process directory ('cask:/...'), "outputs": [filenames], "manifest": {...}}
    :rtype: dict
    """
    rel_path = f"{item.item_path}/{subdir}" if item.item_path else subdir
    write_opts = {
        "group": item.group, "time": item.time, "replace": True,
        "link_metadata": json.dumps({"command": command, "params": params}),
    }
    if opts.get("link_relation"):
        write_opts["link_relation"] = opts["link_relation"]

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
    dest_dir = derivative_dir(item, subdir)
    return {"dir": dest_dir, "outputs": outputs, "manifest": manifest}


def size_as_strings(dims):
    """Stringify a {"width", "height"} dict's values, matching the legacy
    DAMS/Fedora manifest.json's "size" field convention (see
    docs/cask-conventions.md's "Image Manifest Content"/"PDF Manifest Content") —
    everywhere else in this codebase (image_magick.probe_dimensions(),
    associatedMedia ImageObject entries) these stay real ints; this formatting is
    specific to generate()'s/derivatives()'s rich manifest "extra" content, shared
    by digital_website.image and .pdf since a rasterized PDF page's manifest needs
    the exact same size formatting an image's does.

    :param dict dims: {"width": int, "height": int}.
    :returns: {"width": str, "height": str}.
    :rtype: dict
    """
    return {"width": str(dims["width"]), "height": str(dims["height"])}


def cask_public_ref(internal_path):
    """Convert an internal 'cask:/...' digtk.lib.fs dispatch path into the
    public 'cask://...' reference URI form used in JSON-LD documents (per
    caskfs's docs/ld.md "Reference Binary File" convention).

    :param str internal_path: e.g. "cask:/silver/g/c/latest/item/derivatives/thumb-256.jpg".
    :returns: e.g. "cask://silver/g/c/latest/item/derivatives/thumb-256.jpg".
    :rtype: str
    """
    return "cask:/" + internal_path[len("cask:"):]


def update_associated_media(item, media_objects, source_files, source_group):
    """Merge schema:associatedMedia refs into the item's silver sidecar, creating
    it if absent. Never touches the bronze sidecar — bronze is immutable/append-only,
    so derivative metadata lives in a new silver/.../[item]/item.jsonld.json instead
    (see docs/cask-conventions.md).

    Merges by `source_group` rather than replacing the whole associatedMedia array:
    an item's bronze binaries can legitimately span more than one media format (e.g.
    an archival transfer supplying both a compiled PDF and per-page TIFF masters for
    the same physical document — see docs/cask-conventions.md), and each format's
    derivatives() umbrella runs independently, as its own CLI invocation. Only the
    entries this call's own `source_group` previously wrote are replaced; entries
    from a different group's earlier run are left untouched, so e.g. running `digtk
    public-dc pdf derivatives` after `digtk public-dc image derivatives` against the
    same item doesn't wipe out the image run's associatedMedia entries.

    Uses a plain dict read/merge/write rather than digtk.lib.schema.ItemMetadata
    (its to_jsonld/from_jsonld round-trip is itself an unimplemented phase-0.2
    stub) — only the @context/@id/ark/associatedMedia fields this needs. Reads
    still go through fs directly (reading isn't restricted); the write goes
    through digtk.lib.derivative, same as every other pipeline output.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param list[dict] media_objects: associatedMedia entries of any schema.org media
        type — ImageObject, VideoObject, AudioObject — e.g. {"@type": "ImageObject",
        "contentUrl": "cask://...", "width": 256, "height": 384}. Each gets an
        "argonath:sourceGroup" field stamped on it (see above).
    :param list[str] source_files: CaskFS paths ('cask:/'-prefixed) this sidecar's
        associatedMedia was derived from — this item's full set of bronze binaries.
    :param str source_group: Which pipeline produced media_objects — "image", "pdf",
        "video", or "audio". Existing entries carrying this same group are replaced;
        entries from other groups are preserved.
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

    for entry in media_objects:
        entry["argonath:sourceGroup"] = source_group
    existing = doc.get("associatedMedia", [])
    doc["associatedMedia"] = [e for e in existing if e.get("argonath:sourceGroup") != source_group] + media_objects

    derivative.write(
        type="silver", collection=item.collection, rel_path=rel_path,
        data=json.dumps(doc, indent=2).encode(), source_files=source_files, opts=write_opts,
    )
    return doc


def generate_thumbnail_set(local_src, tmp_dir, widths, stem, opts={}):
    """Generate small/medium/large JPEGs, all sharing the same orientation.

    Each source now publishes into its own nested directory (see PUBLIC_FRONTEND_DIR
    and docs/cask-conventions.md's Silver Layout section), so the published filename no
    longer needs multi-source disambiguation — it's always exactly "small.jpg",
    "medium.jpg", "large.jpg". `stem` is still used to keep local scratch files apart,
    since multiple sources' generation still shares one `tmp_dir` during a single
    command invocation even though each publishes to its own remote directory.

    :param str local_src: Local path to the staged source image (or a rasterized PDF page).
    :param str tmp_dir: Local directory to write outputs into.
    :param list[int] widths: Exactly 3 widths (ascending -> small, medium, large).
    :param str stem: Disambiguates this source's local scratch filenames from any other
        source sharing tmp_dir; never appears in the returned (published) filename.
    :param dict opts: 'deskew' (bool, default False) applies the deskew recipe to every
        width instead of a plain resize; 'quality' forwarded to image_magick.
    :returns: (filename, local_path) pairs — filename is "small.jpg"/"medium.jpg"/
        "large.jpg", local_path is a stem-disambiguated scratch path.
    :rtype: list[tuple[str, str]]
    :raises ValueError: If widths doesn't have exactly 3 entries.
    """
    if len(widths) != 3:
        raise ValueError(
            f"generate_thumbnail_set() requires exactly 3 widths (small/medium/large), got {widths}"
        )
    quality = opts.get("quality", CONFIG["image"]["thumbnail_quality"])
    apply_deskew = opts.get("deskew", False)
    outputs = []
    for label, width in zip(THUMBNAIL_SIZE_LABELS, widths):
        filename = f"{label}.jpg"
        local_path = os.path.join(tmp_dir, f"{stem}-{label}.jpg")
        if apply_deskew:
            image_magick.deskew(local_src, local_path, opts={**opts, "output_width": width, "quality": quality})
        else:
            image_magick.thumbnail(local_src, local_path, width, opts={"quality": quality})
        outputs.append((filename, local_path))
    return outputs


def select_orientation(local_src, tmp_dir, opts={}):
    """Decide scan orientation for one image via a real OCR comparison.

    Generates both OCR-prep candidates (deskewed and non-deskewed), measures
    the actual skew angle off the non-deskewed one (image_magick's -deskew
    artifact only exists within its own convert invocation — see
    image_magick.detect_skew_angle()'s docstring — so this can't be read back
    from either candidate after the fact), then hands both to
    digtk.lib.ocr_utils.compare_orientations() — the same shared, decoupled
    primitive the (separate, later) OCR phase will call on its own. Runs once
    per source image — a multi-source item decides orientation independently
    for each of its sources, since there's no reliable signal (yet) that
    sources sharing an item are a sequential document rather than independent
    captures that happen to share a folder.

    :param str local_src: Local path to the staged source image (or a rasterized PDF page).
    :param str tmp_dir: Local directory to write candidate images into.
    :param dict opts: Forwarded to image_magick.prep_ocr(),
        image_magick.detect_skew_angle(), and ocr_utils.compare_orientations().
    :returns: {"deskew": bool, "angle": float, "stats": {"original": {...},
        "deskewed": {...}}} — each stats entry also carries "hocr_path" (see
        ocr_utils.compare_orientations()).
    :rtype: dict
    """
    candidates = {}
    for label, apply_deskew in (("original", False), ("deskewed", True)):
        path = os.path.join(tmp_dir, f"orientation-{label}-{os.path.basename(local_src)}.jpg")
        image_magick.prep_ocr(local_src, path, opts={**opts, "deskew": apply_deskew})
        candidates[label] = path

    angle = image_magick.detect_skew_angle(candidates["original"], opts)
    comparison = ocr_utils.compare_orientations(candidates, opts={**opts, "angle": angle})
    return {"deskew": comparison["selected"] == "deskewed", "angle": angle, "stats": comparison["stats"]}


def build_ocr_products(orientation, tmp_dir, stem, multi, large_size, opts={}):
    """Build the real djvu OCR text-layer + stats.json for one source, out of
    the hOCR select_orientation() already produced deciding orientation —
    this is the "for now, replicate legacy" OCR feature (see
    digtk.lib.gcs_sync.py's module docstring); it deliberately reuses that
    already-run Tesseract pass rather than running OCR a third time, which is
    why this only works when called right after select_orientation() in the
    same tmp_dir/process.

    :param dict orientation: select_orientation()'s return -- {"deskew": bool,
        "angle": float, "stats": {"original": {..., "hocr_path": str},
        "deskewed": {..., "hocr_path": str}}}.
    :param str tmp_dir: Local directory to write the djvu/stats files into.
    :param str stem: This source's filename stem — see output_name().
    :param bool multi: True if this item has more than one source binary —
        see output_name(); ocr.djvu/ocr-stats.json land in one "ocr" subdir
        shared by every source in the item, same as the OCR-prep JPEG, so
        they need the same disambiguating prefix.
    :param dict large_size: {"width": int, "height": int} — the published
        "large" product's actual probed dimensions, used to compute the
        word-bbox rescale factor (OCR-prep render width / large's width) —
        computed from real values rather than assumed to always be 2x, in
        case the two independent config defaults (image.ocr_prep_size,
        image.thumbnail_widths) ever drift apart.
    :param dict opts: 'ocr_prep_size' (int, default
        CONFIG["image"]["ocr_prep_size"]) for the scale-factor computation;
        otherwise forwarded to digtk.lib.djvu.build_djvu_xml()/compute_ocr_stats().
    :returns: [(filename, local_path), ...] — exactly the ocr.djvu and
        ocr-stats.json entries (in that order).
    :rtype: list[tuple[str, str]]
    """
    selected = "deskewed" if orientation["deskew"] else "original"
    page_structures = {label: djvu.parse_hocr(stats["hocr_path"]) for label, stats in orientation["stats"].items()}

    ocr_prep_size = opts.get("ocr_prep_size", CONFIG["image"]["ocr_prep_size"])
    scale_factor = ocr_prep_size / large_size["width"]

    djvu_xml = djvu.build_djvu_xml(
        page_structures[selected], scale_factor=scale_factor,
        dpi=opts.get("dpi"), confidence_threshold=opts.get("confidence_threshold"),
    )
    stats = djvu.compute_ocr_stats(page_structures, selected=selected, opts={**opts, "angle": orientation["angle"]})

    djvu_filename = output_name(stem, "ocr.djvu", multi)
    stats_filename = output_name(stem, "ocr-stats.json", multi)
    djvu_path = os.path.join(tmp_dir, djvu_filename)
    stats_path = os.path.join(tmp_dir, stats_filename)

    with open(djvu_path, "w", encoding="utf-8") as f:
        f.write(djvu_xml)
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)

    return [(djvu_filename, djvu_path), (stats_filename, stats_path)]
