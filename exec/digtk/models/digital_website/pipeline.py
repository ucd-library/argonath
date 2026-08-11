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

from digtk.lib import derivative, fs, image_magick, ocr_utils, utils
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


def parse_widths(widths):
    """Parse a thumbnail-widths spec into a sorted list of ints.

    :param str|list[int] widths: Comma-separated string (e.g. "256,512,1024"), or already a list.
    :returns: Widths sorted ascending.
    :rtype: list[int]
    """
    if isinstance(widths, str):
        widths = [int(w.strip()) for w in widths.split(",")]
    return sorted(widths)


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
    dest_dir = f"cask:{derivative.dest_path(type='silver', collection=item.collection, rel_path=rel_path, opts={'group': item.group, 'time': item.time})}"
    return {"dir": dest_dir, "outputs": outputs, "manifest": manifest}


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
    pdf derivatives` after `digtk image derivatives` against the same item doesn't
    wipe out the image run's associatedMedia entries.

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

    Generates both OCR-prep candidates (deskewed and non-deskewed), then hands
    them to digtk.lib.ocr_utils.compare_orientations() — the same shared,
    decoupled primitive the (separate, later) OCR phase will call on its own.
    Runs once per source image — a multi-source item decides orientation
    independently for each of its sources, since there's no reliable signal
    (yet) that sources sharing an item are a sequential document rather than
    independent captures that happen to share a folder.

    :param str local_src: Local path to the staged source image (or a rasterized PDF page).
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
