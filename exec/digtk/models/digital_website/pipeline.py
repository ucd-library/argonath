"""
Shared derivative-generation pipeline plumbing.

Extracted from digtk.models.digital_website.image (the original, still image-specific,
orchestrator) once PDF/video/audio orchestration needed the exact same
staging/publishing/lineage machinery. Every function here is ARK-aware (they all take an
already-resolved digtk.lib.items.ItemLocation) but otherwise media-type-agnostic — no
function assumes its inputs are images specifically. image_magick-specific helpers
(generate_thumbnail_set) are still here rather than in a format-specific lib module
because a rasterized PDF page is itself just a local image file by the time these run,
so PDF derivative generation reuses them unchanged. The format/staging/orientation
helpers the root-level tools also need (select_orientation, stage_sources, etc.) live
in digtk.models.prep and are re-exported below.

This is the only layer any orchestrator (digital_website.image, .pdf, .video, .audio)
should go through to write silver/gold pipeline output — see CLAUDE.md's
digtk.lib.derivative entry. Reads still go through digtk.lib.fs directly.
"""
import json
import os

from digtk.lib import derivative, djvu, fs, image_magick, ocr_utils, utils
from digtk.lib.config import CONFIG
# Format detection, source staging, output naming and orientation selection live in
# digtk.models.prep so the root-level tools (ocr, accessibility, split-spreads) can
# share them without depending on this showcase-only module. Re-exported here under
# their original names so every showcase orchestrator (and test) that already
# reaches them as pipeline.X keeps working unchanged.
from digtk.models.prep import (  # noqa: F401
    FORMAT_BY_EXTENSION,
    output_name,
    page_tag,
    parse_widths,
    partition_by_format,
    select_orientation,
    stage_sources,
)

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


# Preservica tree/ items can hold the same page twice: an access copy (e.g.
# Access/page_001.jpg) and its preservation master (Preservation/page_001.tif).
# The showcase uses the master — see drop_access_duplicates().
ACCESS_DIR = "Access"
PRESERVATION_DIR = "Preservation"


def drop_access_duplicates(bronze_binary_paths):
    """Drop access copies that have a preservation master: a file in an item's
    Access/ folder is left out when the same item's Preservation/ folder has a
    file with the same name stem (page_001.jpg vs page_001.tif). Access files
    without a master (e.g. a compiled PDF only supplied as an access copy) are
    kept, as is everything outside those two folders.

    :param list[str] bronze_binary_paths: CaskFS paths ('cask:/'-prefixed).
    :returns: The paths to use, in original order.
    :rtype: list[str]
    """
    masters = set()
    for path in bronze_binary_paths:
        parent, _, filename = path.rpartition("/")
        item_dir, _, folder = parent.rpartition("/")
        if folder == PRESERVATION_DIR:
            masters.add((item_dir, os.path.splitext(filename)[0]))

    kept = []
    for path in bronze_binary_paths:
        parent, _, filename = path.rpartition("/")
        item_dir, _, folder = parent.rpartition("/")
        if folder == ACCESS_DIR and (item_dir, os.path.splitext(filename)[0]) in masters:
            continue
        kept.append(path)
    return kept


def filter_binaries_by_format(bronze_binary_paths, media_types):
    """Filter an item's bronze binaries down to just the ones matching a media format.

    Access copies that have a preservation master are left out first (see
    drop_access_duplicates()), so a page supplied as both Access/page_001.jpg
    and Preservation/page_001.tif is processed and shown once, from the master.

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
    for path in drop_access_duplicates(bronze_binary_paths):
        ext = os.path.splitext(path)[1].lower()
        fmt = FORMAT_BY_EXTENSION.get(ext)
        if fmt is None:
            raise ValueError(f"unrecognized media extension {ext!r} in bronze binary: {path}")
        if fmt in wanted:
            matches.append(path)
    if not matches:
        raise ValueError(f"no {'/'.join(sorted(wanted))} binaries found among this item's bronze sources")
    return matches


def detect_formats(bronze_binary_paths):
    """Detect which media-type formats are present among an item's bronze binaries.

    Unlike filter_binaries_by_format(), never raises — a format simply absent
    from bronze_binary_paths is a normal, expected case here, and an
    unrecognized/unclaimed extension (e.g. ".txt") is silently ignored rather
    than treated as an error. Used by dagster/lib/pipeline_fanout.py's
    process_derivatives to decide which `digtk showcase <format> derivatives`
    commands apply to a given (possibly mixed-format) item.

    :param list[str] bronze_binary_paths: CaskFS paths ('cask:/'-prefixed).
    :returns: Formats present, a subset of {"image", "pdf", "video", "audio"}.
    :rtype: set[str]
    """
    formats = set()
    for path in bronze_binary_paths:
        ext = os.path.splitext(path)[1].lower()
        fmt = FORMAT_BY_EXTENSION.get(ext)
        if fmt in ("image", "pdf", "video", "audio"):
            formats.add(fmt)
    return formats


def derivative_dir(item, subdir, *, type="silver", type_opts=None):
    """Compute the CaskFS directory a publish() call to this subdir would use.

    A pure function of inputs already known before calling publish() — lets a
    caller (e.g. digtk.models.digital_website.image's generate(), building its
    manifest's "extra" content) build cask:// url references into a directory
    before that directory's write/manifest actually happens, without needing
    publish()'s return value first. publish() itself calls this internally.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str subdir: Same 'subdir' publish() would be called with.
    :param str type: 'silver' (default) or 'gold' — see digtk.lib.derivative.dest_path().
    :param dict type_opts: dest_path()'s opts for `type`. Defaults to
        {'group': item.group, 'time': item.time} when `type` is left at its
        default 'silver'; required (no default) for any other `type`.
    :returns: CaskFS process directory ('cask:/...').
    :rtype: str
    :raises ValueError: If `type_opts` is omitted for a non-silver `type`.
    """
    rel_path = f"{item.item_path}/{subdir}" if item.item_path else subdir
    if type_opts is None:
        if type != "silver":
            raise ValueError(f"type_opts is required when type={type!r}")
        type_opts = {"group": item.group, "time": item.time}
    return f"cask:{derivative.dest_path(type=type, collection=item.collection, rel_path=rel_path, opts=type_opts)}"


def publish(item, subdir, local_outputs, command, params, *, type="silver", type_opts=None, opts={}):
    """Write local output files to CaskFS (with lineage) and record a manifest.

    Every write goes through digtk.lib.derivative.write() — the canonical way to
    write a silver/gold pipeline output (see CLAUDE.md) — rather than hand-building
    the CaskFS path and calling fs.write() directly, so each derivative's lineage
    back to the specific bronze binary it was generated from is recorded. CaskFS
    stores link metadata as opaque text (see caskfs's docs/structural-metadata.md),
    so the command/params are JSON-encoded here for structure on our side, not
    because CaskFS interprets it.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str subdir: Process directory under the item's silver/gold location (e.g.
        "derivatives", "ocr", "derivatives/page-000", "stream", "captions").
    :param list[tuple[str, str, str]] local_outputs: (filename, local_path, source_bronze_path)
        triples to publish — source_bronze_path is which of item.bronze_binary_paths this
        specific output was derived from.
    :param list[str] command: Full argv that produced these outputs, for the manifest
        and for each output's derivative-link metadata.
    :param dict params: Parsed/typed params, for the manifest and link metadata.
    :param str type: 'silver' (default) or 'gold' — see derivative_dir().
    :param dict type_opts: See derivative_dir() — forwarded as every output's
        (and the manifest's) destination opts, merged with the link/manifest
        opts below.
    :param dict opts: 'manifest_opts' forwarded to utils.write_manifest (e.g. duration_s).
        'manifest' (bool, default True) — set False to publish the files without
        writing the directory's manifest.json, for a directory several per-file
        runs share (e.g. an image item's combined ocr/ — see image.generate()'s
        'files' opt), whose manifest a later finalize step writes once.
        'link_relation' (str, optional) — forwarded to derivative.write() as every
        output's CaskFS lineage relation. The caller decides this per subdir rather than
        this function inferring it from the subdir string, since subdir naming doesn't map
        1:1 to a relation across every media type this pipeline now serves (e.g.
        "derivatives/page-000" for a PDF page vs. plain "derivatives" for an image).
    :returns: {"dir": CaskFS process directory ('cask:/...'), "outputs": [filenames],
        "manifest": {...}|None (None when opts['manifest'] is False)}
    :rtype: dict
    :raises ValueError: If `type_opts` is omitted for a non-silver `type`.
    """
    rel_path = f"{item.item_path}/{subdir}" if item.item_path else subdir
    if type_opts is None:
        if type != "silver":
            raise ValueError(f"type_opts is required when type={type!r}")
        type_opts = {"group": item.group, "time": item.time}
    write_opts = {
        **type_opts, "replace": True,
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
            type=type, collection=item.collection, rel_path=f"{rel_path}/{filename}",
            data=data, source_files=[source_bronze_path], opts=write_opts,
        )
        outputs.append(filename)
        if source_bronze_path not in source_files:
            source_files.append(source_bronze_path)

    manifest = None
    if opts.get("manifest", True):
        manifest = utils.write_manifest(
            item.collection, rel_path, command, params, outputs,
            source_files=source_files,
            type=type,
            opts={**opts.get("manifest_opts", {}), **type_opts},
        )
    dest_dir = derivative_dir(item, subdir, type=type, type_opts=type_opts)
    return {"dir": dest_dir, "outputs": outputs, "manifest": manifest}


def pdf_ocr_prefix(bronze_binary_paths, pdf_path):
    """The directory prefix a PDF's per-page ocr/ output lives under within its
    item — the one place this rule lives, so the code that writes it
    (pdf.derivatives()) and any code that reads it back can't disagree.

    A PDF's OCR output is namespaced by its own filename stem ("<stem>/ocr/...")
    whenever anything else in the item could also write to the bare "ocr/"
    directory: more than one PDF, or any non-PDF binary alongside it (e.g.
    image.generate() publishes its own ocr/manifest.json there). A pure
    single-PDF item keeps the bare "ocr/".

    :param list[str] bronze_binary_paths: Every binary of the item.
    :param str pdf_path: The PDF in question (one of bronze_binary_paths).
    :returns: "<stem>/" or "".
    :rtype: str
    """
    pdf_count = sum(
        1 for p in bronze_binary_paths if FORMAT_BY_EXTENSION.get(os.path.splitext(p)[1].lower()) == "pdf"
    )
    if pdf_count > 1 or pdf_count < len(bronze_binary_paths):
        return f"{os.path.splitext(os.path.basename(pdf_path))[0]}/"
    return ""


def select_binaries(bronze_binary_paths, files):
    """Pick out the source binaries a per-file run should process.

    Supports one Dagster step per image/PDF (see dagster/lib/pipeline_fanout.py):
    each step names its file, while output naming and item-level layout
    decisions still come from the whole item.

    :param list[str] bronze_binary_paths: The candidate binaries ('cask:/...'), in item order.
    :param iterable files: Selectors — a full bronze path, or a bare filename.
    :returns: The selected binaries, in item order.
    :rtype: list[str]
    :raises ValueError: If a selector matches nothing, or a bare filename matches
        more than one binary (e.g. the same name under Access/ and Preservation/ —
        pass the full path instead).
    """
    selected = set()
    for selector in files:
        matches = [p for p in bronze_binary_paths if p == selector or p.rsplit("/", 1)[-1] == selector]
        if not matches:
            raise ValueError(f"no source binary matches {selector!r}")
        if len(matches) > 1:
            raise ValueError(f"{selector!r} is ambiguous ({len(matches)} binaries share that name) — pass the full path")
        selected.add(matches[0])
    return [p for p in bronze_binary_paths if p in selected]


def image_objects_from_manifest(manifest, opts={}):
    """Rebuild the small/medium/large associatedMedia ImageObjects for one image
    (or PDF page) from the manifest content its derivatives run published — the
    same entries image.generate()/pdf.derivatives() build in-process, so a
    finalize step can write associatedMedia without regenerating anything.

    :param dict manifest: A per-image images/manifest.json, or one entry of a
        PDF's aggregate manifest "pages" array — {label: {"size": {"width",
        "height"} (strings), "url": str}} for each THUMBNAIL_SIZE_LABELS label.
    :param dict opts: 'position' (int, optional) — a PDF page's 0-indexed position.
    :returns: One ImageObject per label, in label order.
    :rtype: list[dict]
    """
    objects = []
    for label in THUMBNAIL_SIZE_LABELS:
        entry = {
            "@type": "ImageObject",
            "contentUrl": manifest[label]["url"],
            "encodingFormat": "image/jpeg",
        }
        if "position" in opts:
            entry["position"] = opts["position"]
        entry["width"] = int(manifest[label]["size"]["width"])
        entry["height"] = int(manifest[label]["size"]["height"])
        objects.append(entry)
    return objects


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


def source_filename(content_url):
    """The bronze source filename an associatedMedia entry was derived from,
    read from its contentUrl's public-frontend-derivatives/ segment:
    images/<file>/images/... for an image, <file>/... for a PDF, video or
    audio source.

    :param str content_url: The entry's contentUrl.
    :returns: The source filename, or None for a URL outside that layout.
    :rtype: str|None
    """
    marker = f"/{PUBLIC_FRONTEND_DIR}/"
    if marker not in content_url:
        return None
    rest = content_url.split(marker, 1)[1]
    parts = rest.split("/")
    if parts[0] == "images" and len(parts) > 1:
        return parts[1]
    return parts[0] or None


def group_media_by_source(associated_media):
    """Group a flat associatedMedia list by source group and source file.

    Every entry carries the "argonath:sourceGroup" (image, pdf, video, audio)
    it was built for (see digtk.models.digital_website.item_record).

    ASSUMPTION: page order within an image list is the filenames' sort order —
    nothing records an explicit order. Correct for zero-padded sequential scan
    names (page_001.tif, page_002.tif).

    :param list associated_media: Flat associatedMedia list.
    :returns: {source group: {filename: [entries]}}, filenames sorted.
    :rtype: dict
    """
    groups = {}
    for entry in associated_media:
        group = entry.get("argonath:sourceGroup")
        filename = source_filename(entry.get("contentUrl", ""))
        if group and filename:
            groups.setdefault(group, {}).setdefault(filename, []).append(entry)
    return {group: {f: files[f] for f in sorted(files)} for group, files in groups.items()}


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


def build_ocr_products(orientation, tmp_dir, stem, multi, large_size, opts={}):
    """Build the real djvu OCR text-layer + stats.json for one source, out of
    the hOCR select_orientation() already produced deciding orientation — a
    legacy-DAMS-compatible OCR product (see digtk.lib.djvu's module
    docstring); it deliberately reuses that already-run Tesseract pass rather
    than running OCR a third time, which is why this only works when called
    right after select_orientation() in the same tmp_dir/process.

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
        page_size=large_size,
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
