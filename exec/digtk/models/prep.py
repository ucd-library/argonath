"""
Input preparation shared by the root-level tools (digtk.models.ocr,
digtk.models.accessibility, digtk.models.split) and the showcase pipeline
(digtk.models.digital_website.pipeline re-exports the helpers it also uses).

The root-level tools prepare their own inputs in-process rather than reading
back anything `digtk showcase image|pdf derivatives` published: every source
binary is staged into a local temp dir, PDFs are rasterized per page, and each
tool builds whichever working image it needs (a Tesseract-tuned OCR-prep image
via a real orientation comparison, or a plain resized JPEG for a vision model)
next to it. Nothing in here publishes to CaskFS — the working images are
intermediate, and each tool's own outputs record lineage straight back to the
source binary they came from.

Everything here takes an already-resolved digtk.lib.items.ItemLocation (bronze
or workspace — see digtk.models.workspace) and is otherwise media-type-agnostic
beyond the image/PDF split: a rasterized PDF page is just a local image file by
the time ocr_image()/vision_image() see it.
"""
import os
import re

from digtk.lib import fs, image_magick, ocr_utils, pdf_render, utils
from digtk.lib.config import CONFIG

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
    # Recognized-but-unclaimed: no showcase pipeline processes these, but an
    # item legitimately having one shouldn't fail filter_binaries_by_format()'s
    # unrecognized-extension safety net the way a truly unhandled binary type
    # should -- e.g. Preservica's "Access" representation bundle includes a
    # plain-text OCR transcript (its own, not digtk.models.ocr's) alongside
    # page images/a combined PDF for the same item.
    ".txt": "text",
}


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


def natural_key(name):
    """Sort key that orders embedded numbers by value (page_2 before page_10).

    :param str name: Filename.
    :rtype: list
    """
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


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
        that only want their own format's binaries (see digtk.models.digital_website.pipeline.filter_binaries_by_format())
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


def _page_key(page):
    """Filename-safe key unique to one staged page within a tmp dir.

    :param dict page: A stage_pages() entry.
    :returns: e.g. 'photo' for an image, 'document-page-003' for a PDF page.
    :rtype: str
    """
    return f"{page['stem']}-{page['page_tag']}" if page["page_tag"] else page["stem"]


def stage_pages(item, tmp_dir, opts={}):
    """Stage an item's image and PDF binaries locally, one entry per page.

    Each image binary is one page; each PDF binary is rasterized page by page
    via digtk.lib.pdf_render.rasterize_page(). Binaries of any other format
    (video/audio/text, or unrecognized) are skipped — see partition_by_format().

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param str tmp_dir: Local directory to stage and rasterize into.
    :param dict opts: 'formats' (set[str], default {"image", "pdf"}) — only stage
        binaries of these formats; 'files' (set[str], optional) — only stage
        binaries with these filenames (one Dagster step per file — see
        digtk.models.workspace.run()); 'raster_format' (str, default
        CONFIG["pdf"]["raster_format"]); 'rasterize' (bool, default True —
        False lists PDF pages with local_path None); anything else is
        forwarded to pdf_render.page_count()/rasterize_page().
    :returns: Pages in source order (all of one binary's pages before the next
        binary's). Each is {"source_path": "cask:/..." (the binary this page came
        from), "filename": str, "stem": str, "kind": "image"|"pdf",
        "page_no": int|None (1-indexed, PDFs only), "count": int|None (the
        PDF's page count), "page_tag": str|None (page_tag() of this page, PDFs
        only), "local_path": str (the staged image or rasterized page; None
        with 'rasterize' False), "pdf_path": str (PDFs only — the staged PDF)}.
    :rtype: list[dict]
    """
    formats = opts.get("formats", {"image", "pdf"})
    files = opts.get("files")
    image_paths, pdf_paths = partition_by_format(item.bronze_binary_paths)
    wanted = [
        path for path in item.bronze_binary_paths
        if ((path in image_paths and "image" in formats) or (path in pdf_paths and "pdf" in formats))
        and (not files or os.path.basename(path) in files)
    ]
    raster_format = opts.get("raster_format", CONFIG["pdf"]["raster_format"])

    pages = []
    for src in stage_sources(item, tmp_dir, opts={"bronze_binary_paths": wanted}):
        filename = os.path.basename(src["bronze_path"])
        base = {"source_path": src["bronze_path"], "filename": filename, "stem": src["stem"]}
        if src["bronze_path"] in image_paths:
            pages.append({
                **base, "kind": "image", "page_no": None, "count": None,
                "page_tag": None, "local_path": src["local_path"],
            })
            continue

        count = pdf_render.page_count(src["local_path"], opts)
        for page_index in range(count):
            page = {
                **base, "kind": "pdf", "page_no": page_index + 1, "count": count,
                "page_tag": page_tag(page_index, count), "local_path": None,
                "pdf_path": src["local_path"],
            }
            if opts.get("rasterize", True):
                rasterize(page, tmp_dir, {**opts, "raster_format": raster_format})
            pages.append(page)
    return pages


def rasterize(page, tmp_dir, opts={}):
    """Rasterize a PDF page entry that was staged without rasterizing.

    No-op when the page already has a local image.

    :param dict page: A stage_pages() PDF entry; its 'local_path' is set.
    :param str tmp_dir: Local directory to write into.
    :param dict opts: 'raster_format', plus pdf_render.rasterize_page() opts.
    :returns: The page's local image path.
    :rtype: str
    """
    if page.get("local_path"):
        return page["local_path"]
    raster_format = opts.get("raster_format", CONFIG["pdf"]["raster_format"])
    utils.log(f"{page['filename']} page {page['page_no']}/{page['count']}: rasterizing...")
    raster_path = os.path.join(tmp_dir, f"{page['stem']}-{page['page_tag']}-raster.{raster_format}")
    page["local_path"] = pdf_render.rasterize_page(
        page["pdf_path"], page["page_no"] - 1, raster_path, opts)["dest_path"]
    return page["local_path"]


def ocr_image(page, tmp_dir, opts={}):
    """Build the Tesseract-tuned OCR-prep image for one staged page.

    Decides orientation with select_orientation() (two OCR-prep candidates,
    one Tesseract pass each), then renders the winning OCR-prep image — the
    same two steps `digtk showcase image|pdf derivatives` take, just kept
    local instead of published.

    :param dict page: A stage_pages() entry.
    :param str tmp_dir: Local directory to write the candidates and result into.
    :param dict opts: Forwarded to select_orientation() and
        image_magick.prep_ocr() (size, dpi, fuzz, quality, threshold, enhance,
        plus Tesseract's lang/psm/oem for the orientation comparison). Callers
        must not pass a non-Tesseract 'lang' (e.g. tag_pdf's BCP-47 document
        language) through here.
    :returns: {"local_path": str, "deskew": bool, "angle": float}
    :rtype: dict
    """
    orientation = select_orientation(page["local_path"], tmp_dir, opts)
    base_name = "prep-deskew.jpg" if orientation["deskew"] else "prep.jpg"
    dest = os.path.join(tmp_dir, f"{_page_key(page)}-{base_name}")
    image_magick.prep_ocr(page["local_path"], dest, opts={**opts, "deskew": orientation["deskew"]})
    return {"local_path": dest, "deskew": orientation["deskew"], "angle": orientation["angle"]}


def geometry_opts(opts={}):
    """Resolve the OCR-prep settings that decide word-box geometry.

    Two renders with equal geometry_opts() and the same deskew line up 1:1.

    :param dict opts: image_magick.prep_ocr() opts.
    :returns: {'size', 'dpi', 'enhance', 'threshold'} with defaults filled in.
    :rtype: dict
    """
    return {
        "size": opts.get("size", CONFIG["image"]["ocr_prep_size"]),
        "dpi": opts.get("dpi", CONFIG["image"]["dpi"]),
        "enhance": opts.get("enhance") or None,
        "threshold": opts.get("threshold", CONFIG["image"]["deskew_threshold"]),
    }


def display_image(page, tmp_dir, opts={}):
    """Render the page as ocr_image() does, minus binarization.

    Same resize, enhance and deskew, so OCR word boxes line up 1:1.
    Used as the visible page in searchable PDFs.

    :param dict page: A stage_pages() entry.
    :param str tmp_dir: Local directory to write the result into.
    :param dict opts: image_magick.prep_ocr() opts; pass ocr_image()'s 'deskew'.
    :returns: Local path to the rendered page.
    :rtype: str
    """
    dest = os.path.join(tmp_dir, f"{_page_key(page)}-display.jpg")
    image_magick.prep_ocr(page["local_path"], dest, opts={**opts, "binarize": False})
    return dest


def vision_image(page, tmp_dir, opts={}):
    """Build a plain resized JPEG of one staged page for a vision model.

    Not the OCR-prep image — that's binarized/contrast-tuned for Tesseract,
    not for a vision model or a human reader. Defaults to the showcase "large"
    width (the largest of CONFIG["image"]["thumbnail_widths"]) so vision/
    alt-text quality matches what the tools got from showcase's large.jpg
    before they prepared their own inputs.

    :param dict page: A stage_pages() entry.
    :param str tmp_dir: Local directory to write the result into.
    :param dict opts: 'vision_width' (int, default the largest
        CONFIG["image"]["thumbnail_widths"] entry), 'quality' (int, default
        CONFIG["image"]["thumbnail_quality"]).
    :returns: Local path to the resized JPEG.
    :rtype: str
    """
    width = opts.get("vision_width") or parse_widths(CONFIG["image"]["thumbnail_widths"])[-1]
    quality = opts.get("quality", CONFIG["image"]["thumbnail_quality"])
    dest = os.path.join(tmp_dir, f"{_page_key(page)}-vision.jpg")
    image_magick.thumbnail(page["local_path"], dest, width, opts={"quality": quality})
    return dest
