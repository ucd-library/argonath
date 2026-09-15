"""
Spread-splitting — detect the binding gutter in a scanned two-page spread,
let a human verify/correct the detection, then cut confirmed spreads into
separate left/right page images.

Per-item, like every other derivative in this codebase: reads bronze image
binaries via digtk.lib.items.resolve() and publishes every output
(gutters.json, review strips, contact sheets, cut pages) as silver
derivatives of that same item — not a promotion into new bronze items. A cut
page is a display-oriented derivative of the original (uncut) bronze scan,
the same way tag_pdf's tagged PDF is a derivative of its source PDF rather
than a replacement for it.

Ported from the digitization toolkit's split.py (originally a pre-ingest,
whole-directory tool), adapted in three ways:
- Operates per-item (ItemLocation) against bronze/silver instead of a raw
  src_dir/out_dir pair — this item's own silver location replaces local
  directories throughout.
- Cropping and the review-strip/contact-sheet visualization use
  digtk.lib.image_magick (this codebase's existing raster pipeline) instead
  of Pillow, which isn't a dependency here.
- gutters.json is scoped to one item's own image binaries, not a whole
  collection's — typically a handful of entries, sometimes just one.

Two-phase, deliberately human-in-the-loop workflow:
  1. detect(ark) — for every bronze image binary of this item not already in
     gutters.json, runs the vision gutter-detection call, publishes a review
     strip (gutter line, or a border for non-spreads), and rebuilds contact
     sheets tiling every review strip seen so far for this item. Never
     re-detects or overwrites an existing gutters.json entry — protects a
     human's manual corrections the same way this codebase already protects
     other manifests from being silently clobbered by a second writer.
  2. Someone pulls this item's split-spreads/review-sheets/ + gutters.json
     via existing CaskFS access, reviews the contact sheet(s), hand-edits
     gutters.json, and pushes the corrected file back. No code runs this step.
  3. apply(ark) — reads the (now human-verified) gutters.json back and cuts
     each confirmed spread into left/right pages.

Two caveats inherited from the reference implementation, worth knowing before
trusting a detection: the vision model's gutter_x is normalized to 0-1000,
not pixels — everything here stores and edits normalized units, converting
to real pixels only at review-strip/cut time (dividing by image width would
silently corrupt every entry if this were forgotten) — and a proposal of
exactly 500 is also the model's fallback answer for "I don't know", so it's
worth extra scrutiny during review, not just the median case. A review strip
is also never regenerated after gutters.json is edited — it always reflects
the original detection, not a later human correction.
"""
import json
import os
import tempfile

from digtk.lib import ai, derivative, fs, image_magick, items
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS
from digtk.models.alt_utils import parse_model_json
from digtk.models.digital_website import pipeline

_GUTTER_PROMPT = (
    "This is a scan of an open photo album or book: a two-page spread. Find "
    "the GUTTER — the vertical binding seam where the left page meets the "
    "right page. It is usually near the middle but not always exactly "
    "centered. Return ONLY JSON: {\"gutter_x\": <x-coordinate of the seam in "
    "0-1000 normalized image coordinates>, \"is_spread\": <true if this "
    "really shows two pages, false if a single page/cover/loose item>, "
    "\"confidence\": <\"high\"|\"medium\"|\"low\">}"
)

_GUTTER_LINE_COLOR = "#ff2828"
_NON_SPREAD_BORDER_COLOR = "#2878ff"
_SHEET_BATCH_SIZE = 8
_SHEET_COLS = 2
_TILE_WIDTH = 620
_TILE_HEIGHT = 320
_TILE_BORDER = 10
_SHEET_BACKGROUND = "#181818"

# Distinct from image.py/pdf.py's own "derivativeOf"/"ocrPrepOf" relations,
# and from ocr.py's HOCR_LINK_RELATION/TRANSCRIPT_LINK_RELATION — see
# digtk.models.ocr's identical convention for why each derivative kind gets
# its own relation rather than a generic one.
GUTTERS_LINK_RELATION = f"{ARGONATH_NS}spreadGuttersOf"
REVIEW_LINK_RELATION = f"{ARGONATH_NS}spreadReviewOf"
SPLIT_PAGE_LINK_RELATION = f"{ARGONATH_NS}spreadPageOf"


def _silver_rel_path(item, *parts):
    """Silver rel_path (relative to the collection/time root) under this
    item's own location — see digtk.models.ocr/accessibility's identical helper.

    :param ItemLocation item: Resolved item location.
    :param str parts: Path segments to join after the item's own item_path.
    :rtype: str
    """
    subdir = "/".join(parts)
    return f"{item.item_path}/{subdir}" if item.item_path else subdir


def _stem(cask_path):
    """
    :param str cask_path: A cask:/ file path.
    :returns: Filename stem (no directory, no extension).
    :rtype: str
    """
    filename = cask_path.rsplit("/", 1)[-1]
    return filename.rsplit(".", 1)[0]


def _detect_gutter(jpeg_bytes, opts={}):
    """One vision call: locate the gutter in a rendered spread image.

    Endpoint failures (chat_completion raising after its own retries) and
    unusable output (no parseable JSON) both propagate — unlike
    layout.page_layout()'s "missing table layout is fine, tag without it",
    a bad gutter detection here is a hard blocker worth stopping the whole
    detect() run over, matching the reference implementation's own
    no-try/except-per-file behavior.

    :param bytes jpeg_bytes: The detection-render JPEG bytes.
    :param dict opts: 'model' passed through to the chat call.
    :returns: {"gutter_x": int (0-1000 normalized), "is_spread": bool,
        "confidence": "high"|"medium"|"low"}.
    :rtype: dict
    :raises ValueError: If the model's response has no parseable JSON.
    """
    chat_opts = {"image": ai.Image(data=jpeg_bytes, mimetype="image/jpeg")}
    if opts.get("model"):
        chat_opts["model"] = opts["model"]
    obj = parse_model_json(ai.chat_completion(_GUTTER_PROMPT, chat_opts))
    return {
        "gutter_x": int(obj.get("gutter_x") or 0),
        "is_spread": bool(obj.get("is_spread")),
        "confidence": str(obj.get("confidence") or "low"),
    }


def _draw_review_strip(src_path, dest_path, info, dims):
    """Draw a gutter line (spread) or border (non-spread) onto a copy of the
    detection render, for a human reviewer to eyeball.

    :param str src_path: Local path to the detection-render JPEG.
    :param str dest_path: Local output path for the annotated review strip.
    :param dict info: This file's _detect_gutter() result.
    :param dict dims: {"width": int, "height": int} of the image at src_path
        (image_magick.probe_dimensions()) — the gutter_x -> pixel conversion
        must use the *render's* width, since that's the image being drawn on.
    """
    width, height = dims["width"], dims["height"]
    if info["is_spread"]:
        x = round(info["gutter_x"] * width / 1000)
        x0, x1 = max(x - 3, 0), min(x + 3, width)
        argv = [
            "convert", src_path, "-fill", _GUTTER_LINE_COLOR,
            "-draw", f"rectangle {x0},0 {x1},{height}",
            dest_path,
        ]
    else:
        inset = 4
        argv = [
            "convert", src_path, "-fill", "none", "-stroke", _NON_SPREAD_BORDER_COLOR,
            "-strokewidth", "8",
            "-draw", f"rectangle {inset},{inset} {width - inset},{height - inset}",
            dest_path,
        ]
    image_magick.run(argv)


def _montage(local_paths, dest_path):
    """Tile review strips into one contact-sheet image.

    Built from plain resize/pad/append operations rather than ImageMagick's
    own `montage` command. `montage` always tries to render a per-tile
    caption (the filename) — even passing `-label ''` still hits the same
    font lookup, since it computes label geometry before checking whether
    the text is empty — and that rendering needs a system font resolvable
    via Freetype/fontconfig, which isn't guaranteed across dev machines and
    containers alike (a host with none configured, `identify -list font`
    empty, makes the whole montage call fail outright). `-resize`/`-extent`/
    `-border`/`+append`/`-append` never touch text rendering at all, so this
    path is safe regardless of font availability. Each review strip is
    already individually named by stem in CaskFS, so a caption was only ever
    a convenience, not load-bearing for review.

    :param list[str] local_paths: Local review-strip JPEGs to tile, in order.
    :param str dest_path: Local output path for the sheet.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        padded = []
        for i, local_path in enumerate(local_paths):
            padded_path = os.path.join(tmp_dir, f"pad-{i}.jpg")
            image_magick.run([
                "convert", local_path,
                "-resize", f"{_TILE_WIDTH}x{_TILE_HEIGHT}",
                "-background", _SHEET_BACKGROUND, "-gravity", "center",
                "-extent", f"{_TILE_WIDTH}x{_TILE_HEIGHT}",
                "-bordercolor", _SHEET_BACKGROUND, "-border", str(_TILE_BORDER),
                padded_path,
            ])
            padded.append(padded_path)

        rows = []
        for i in range(0, len(padded), _SHEET_COLS):
            row_path = os.path.join(tmp_dir, f"row-{i // _SHEET_COLS}.jpg")
            image_magick.run(["convert", *padded[i:i + _SHEET_COLS], "+append", row_path])
            rows.append(row_path)

        if len(rows) == 1:
            image_magick.run(["convert", rows[0], dest_path])
        else:
            image_magick.run(["convert", *rows, "-background", _SHEET_BACKGROUND, "-append", dest_path])


def detect(ark, opts={}):
    """Detect the gutter in every not-yet-detected image binary of this item.

    Requires the ARK to already resolve to a bronze item — reads its image
    binaries directly rather than a staged workspace copy. Resumable: an
    interrupted run loses at most the one file it was mid-detecting on the
    next run, and re-running never touches an entry already present in
    gutters.json — including one a human has since edited.

    :param str ark: Item ARK identifier.
    :param dict opts: 'max_px' (int, default CONFIG["split"]["max_px"] —
        detection render width), 'model' (forwarded to the vision call).
    :returns: {"status": "success", "ark": str, "detected": int (newly
        processed this run), "total": int (entries in gutters.json now)}.
    :rtype: dict
    :raises LookupError: If the ARK doesn't resolve to a bronze item.
    :raises ValueError: If the item has no image binaries.
    """
    item = items.resolve(ark)
    image_paths, _ = pipeline.partition_by_format(item.bronze_binary_paths)
    if not image_paths:
        raise ValueError(f"no image binaries found among this item's bronze sources: {item.bronze_binary_paths}")

    max_px = opts.get("max_px", CONFIG["split"]["max_px"])
    write_opts = {"group": item.group, "time": item.time, "replace": True}

    gutters_path = f"{pipeline.derivative_dir(item, 'split-spreads')}/gutters.json"
    try:
        results = json.loads(fs.read(gutters_path))
    except FileNotFoundError:
        results = {}

    detected = 0
    new_review_bytes = {}

    for bronze_path in image_paths:
        stem = _stem(bronze_path)
        if stem in results:
            continue

        with tempfile.TemporaryDirectory() as tmp_dir:
            local_raw = os.path.join(tmp_dir, bronze_path.rsplit("/", 1)[-1])
            with open(local_raw, "wb") as f:
                f.write(fs.read(bronze_path))

            local_jpg = os.path.join(tmp_dir, f"{stem}.jpg")
            image_magick.thumbnail(local_raw, local_jpg, max_px)
            with open(local_jpg, "rb") as f:
                jpeg_bytes = f.read()

            info = _detect_gutter(jpeg_bytes, opts)
            results[stem] = info
            detected += 1

            dims = image_magick.probe_dimensions(local_jpg)
            review_local = os.path.join(tmp_dir, f"{stem}-review.jpg")
            _draw_review_strip(local_jpg, review_local, info, dims)
            with open(review_local, "rb") as f:
                review_bytes = f.read()

        new_review_bytes[stem] = review_bytes
        derivative.write(
            type="silver", collection=item.collection,
            rel_path=_silver_rel_path(item, "split-spreads", "review", f"{stem}.jpg"),
            data=review_bytes, source_files=[bronze_path],
            opts={**write_opts, "link_relation": REVIEW_LINK_RELATION},
        )

    derivative.write(
        type="silver", collection=item.collection,
        rel_path=_silver_rel_path(item, "split-spreads", "gutters.json"),
        data=json.dumps(results, indent=1).encode(), source_files=image_paths,
        opts={**write_opts, "link_relation": GUTTERS_LINK_RELATION},
    )
    _build_contact_sheets(item, results, new_review_bytes, write_opts)

    return {"status": "success", "ark": ark, "detected": detected, "total": len(results)}


def _build_contact_sheets(item, results, new_review_bytes, write_opts):
    """Rebuild every contact sheet from the full set of review strips.

    Cheap to rebuild in full each time (small JPEGs, and few per item), so
    this always covers every stem in `results`, not just ones detected this
    run. Strips built this run are already in memory (`new_review_bytes`);
    strips from an earlier invocation of detect() are read back from CaskFS
    instead of re-fetching what was just written.

    :param ItemLocation item: Resolved item location.
    :param dict results: The full (just-written) gutters.json content.
    :param dict new_review_bytes: {stem: bytes} for strips built this run.
    :param dict write_opts: {"group": item.group, "time": item.time,
        "replace": True} — forwarded to every derivative.write() call.
    """
    stems = sorted(results)
    if not stems:
        return

    review_dir = pipeline.derivative_dir(item, "split-spreads/review")
    with tempfile.TemporaryDirectory() as tmp_dir:
        entries = []
        for stem in stems:
            review_path = f"{review_dir}/{stem}.jpg"
            data = new_review_bytes.get(stem) or fs.read(review_path)
            local_path = os.path.join(tmp_dir, f"{stem}.jpg")
            with open(local_path, "wb") as f:
                f.write(data)
            entries.append((review_path, local_path))

        for i in range(0, len(entries), _SHEET_BATCH_SIZE):
            batch = entries[i:i + _SHEET_BATCH_SIZE]
            sheet_num = i // _SHEET_BATCH_SIZE + 1
            sheet_local = os.path.join(tmp_dir, f"sheet-{sheet_num:02d}.jpg")
            _montage([local for _, local in batch], sheet_local)
            with open(sheet_local, "rb") as f:
                sheet_bytes = f.read()
            derivative.write(
                type="silver", collection=item.collection,
                rel_path=_silver_rel_path(item, "split-spreads", "review-sheets", f"sheet-{sheet_num:02d}.jpg"),
                data=sheet_bytes, source_files=[cask_path for cask_path, _ in batch],
                opts={**write_opts, "link_relation": REVIEW_LINK_RELATION},
            )


def apply(ark, opts={}):
    """Cut every confirmed spread per this item's (human-reviewed) gutters.json.

    Requires detect() to have already run (and, ideally, for a human to have
    reviewed/corrected its output) — reads gutters.json back rather than
    re-detecting anything.

    :param str ark: Item ARK identifier.
    :param dict opts: Unused; present for signature symmetry with detect().
    :returns: {"status": "success", "ark": str, "split": int (spreads cut
        into L/R pages), "singles": int (non-spreads published unchanged),
        "skipped": list[str] (bronze filenames present on the item but absent
        from gutters.json — rerun detect for these)}.
    :rtype: dict
    :raises LookupError: If the ARK doesn't resolve to a bronze item.
    :raises FileNotFoundError: If this item has no gutters.json at all
        (detect() hasn't been run yet).
    """
    item = items.resolve(ark)
    gutters_path = f"{pipeline.derivative_dir(item, 'split-spreads')}/gutters.json"
    results = json.loads(fs.read(gutters_path))

    write_opts = {"group": item.group, "time": item.time, "replace": True}
    image_paths, _ = pipeline.partition_by_format(item.bronze_binary_paths)
    split_count = singles_count = 0
    skipped = []

    for bronze_path in image_paths:
        filename = bronze_path.rsplit("/", 1)[-1]
        stem = _stem(bronze_path)
        info = results.get(stem)
        if info is None:
            skipped.append(filename)
            continue

        bronze_bytes = fs.read(bronze_path)

        if not info["is_spread"]:
            derivative.write(
                type="silver", collection=item.collection,
                rel_path=_silver_rel_path(item, "split-spreads", "pages", filename),
                data=bronze_bytes, source_files=[bronze_path],
                opts={**write_opts, "link_relation": SPLIT_PAGE_LINK_RELATION},
            )
            singles_count += 1
            continue

        with tempfile.TemporaryDirectory() as tmp_dir:
            local_raw = os.path.join(tmp_dir, filename)
            with open(local_raw, "wb") as f:
                f.write(bronze_bytes)

            dims = image_magick.probe_dimensions(local_raw)
            width, height = dims["width"], dims["height"]
            x = round(info["gutter_x"] * width / 1000)
            ext = os.path.splitext(filename)[1]

            for side_name, box in ((f"{stem}_L{ext}", (0, 0, x, height)),
                                    (f"{stem}_R{ext}", (x, 0, width - x, height))):
                local_side = os.path.join(tmp_dir, side_name)
                image_magick.crop(local_raw, local_side, box)
                with open(local_side, "rb") as f:
                    side_bytes = f.read()
                derivative.write(
                    type="silver", collection=item.collection,
                    rel_path=_silver_rel_path(item, "split-spreads", "pages", side_name),
                    data=side_bytes, source_files=[bronze_path, gutters_path],
                    opts={**write_opts, "link_relation": SPLIT_PAGE_LINK_RELATION},
                )
        split_count += 1

    return {
        "status": "success", "ark": ark,
        "split": split_count, "singles": singles_count, "skipped": skipped,
    }
