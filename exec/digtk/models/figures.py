"""
In-page figures for illustrated documents: find every chart, photo, diagram,
map, drawing or logo on a page, describe each one for screen-reader users,
and keep the result reviewable.

Per page: detect() finds the figures (one Samwise vision call), describe()
writes alt text for each informative one (one call per figure; charts,
diagrams and maps also get a long description, since WCAG 1.1.1 wants the
figure's information, not just its existence). page_figures() caches the
result as accessibility/<file>[/<page>]/figures.json with a crop per figure.

Coordinates: the model grounds in 0-1000 normalized page space; boxes are
converted to fractions of the page (0-1, origin top-left) straight away, so
nothing downstream depends on image size.

Review: each source file gets one accessibility/<file>/figure-review.csv
(one row per figure). A reviewer can fix alt text, mark a figure decorative,
or put reject/skip in its flag column, then upload the CSV back; its rows win
over the cache on the next run.

Ported from the digitization toolkit's figures.py.
"""
import csv
import io
import json
import re

import pymupdf

from digtk.lib import ai, derivative, fs, items, utils
from digtk.lib.schema import ARGONATH_NS
from digtk.models import alt_utils

KINDS = ("photo", "chart", "diagram", "map", "drawing", "logo", "ornament")
COMPLEX_KINDS = frozenset({"chart", "diagram", "map"})
_PAD = 0.008      # grow boxes by this page share
_MIN_SIDE = 0.03  # drop boxes thinner than this
_CROP_MAX_PX = 1024

FIGURES_LINK_RELATION = f"{ARGONATH_NS}figuresOf"
FIGURE_CROP_LINK_RELATION = f"{ARGONATH_NS}figureCropOf"
REVIEW_LINK_RELATION = f"{ARGONATH_NS}figureReviewOf"

REVIEW_FIELDS = ["page", "n", "kind", "decorative", "bbox", "caption",
                 "alt", "long_description", "flag", "notes", "crop"]

_DETECT_PROMPT = """You are preparing this scanned page for accessibility tagging. Find every FIGURE on the page: photographs, charts, graphs, diagrams, maps, drawings, illustrations, logos and decorative ornaments. Do NOT include data tables, running text, headings, equations or page numbers.

For each figure return:
- "bbox": [x0, y0, x1, y1] — the figure's bounding box INCLUDING its axis labels and legend but EXCLUDING its caption, in 0-1000 normalized page coordinates (0,0 = top-left)
- "kind": one of "photo", "chart", "diagram", "map", "drawing", "logo", "ornament"
- "caption": the printed caption or title text next to it, if any (verbatim, else "")
- "decorative": true only if the figure conveys no information (an ornament, a rule, or a logo whose wording already appears as page text)

Return ONLY JSON: {"figures": [...]}. If the page has no figures: {"figures": []}"""


def _describe_prompt(kind, caption, opts):
    """Build the alt-text prompt for one figure crop.

    :param str kind: Figure kind.
    :param str caption: Its printed caption, or ''.
    :param dict opts: alt_utils.build_alt_prompt() opts, plus 'title' and
        'page' ((n, total)).
    :rtype: str
    """
    page = opts.get("page")
    page_line = f" This is on page {page[0]} of {page[1]}." if page else ""
    long_ask = (
        '\nAlso write "long_description": 2-5 sentences giving a sighted reader\'s '
        "understanding of the figure — what is plotted or depicted, axes/units, the "
        "trend or relationship, and any labeled values — so a reader who cannot see it "
        "gets the same information. Do not repeat the caption."
        if kind in COMPLEX_KINDS else '\nSet "long_description" to null.')
    transcribe = ("If short text in the figure is significant and legible, quote it exactly."
                  if opts.get("transcribe_text", True) else "Do not transcribe visible text.")
    max_chars = opts.get("max_alt_chars", alt_utils.DEFAULT_MAX_ALT_CHARS)
    return f"""You are writing ADA/WCAG-compliant alternative text for a {kind} that appears inside a document page.

Collection context: {opts.get("collection_context") or "(none provided)"}
Document: {opts.get("title") or "(untitled)"}.{page_line}
Printed caption (context only — do not repeat it): {caption or "(none)"}

Write "alt": one sentence, at most {max_chars} characters, stating the figure's essential content. Never begin with "image of", "chart of", "picture of" or similar. {transcribe}
Tone: {opts.get("tone", "neutral, factual")}. Follow {opts.get("language", "en-US")} conventions. {opts.get("extra_instructions", "")}{long_ask}

Return ONLY a JSON object: {{"alt": "...", "long_description": "..." or null}}"""


def _clamp_box(b, pad=_PAD):
    """Turn a 0-1000 model box into padded page fractions, or None if too thin.

    :rtype: list[float]|None
    """
    try:
        x0, y0, x1, y1 = (float(v) / 1000.0 for v in b)
    except (TypeError, ValueError):
        return None
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    x0, y0 = max(0.0, x0 - pad), max(0.0, y0 - pad)
    x1, y1 = min(1.0, x1 + pad), min(1.0, y1 + pad)
    if x1 - x0 < _MIN_SIDE or y1 - y0 < _MIN_SIDE:
        return None
    return [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]


def _parse_figure_list(raw):
    """The model's figure list: wrapped in {"figures": [...]}, a bare array, or one object.

    :param str raw: Model output.
    :returns: The list, or None when nothing parses.
    :rtype: list|None
    """
    for text in (raw, alt_utils._FENCE.sub(lambda m: m.group(1), raw)):
        a, b = text.find("["), text.rfind("]")
        o, c = text.find("{"), text.rfind("}")
        candidates = []
        if o >= 0 and c > o:
            candidates.append(text[o:c + 1])
        if a >= 0 and b > a and (o < 0 or a < o):
            candidates.insert(0, text[a:b + 1])
        for cand in candidates:
            try:
                data = json.loads(cand)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                figs = data.get("figures")
                if isinstance(figs, list):
                    return figs
                return [data] if ("bbox" in data or "bbox_2d" in data) else []
            if isinstance(data, list):
                return data
    return None


def detect(image, opts={}):
    """Find the figures on one page image.

    Endpoint failures propagate: silently finding no figures during an outage
    would look like a clean page.

    :param image: Page image path or ai.Image.
    :param dict opts: 'model' passed to the chat call.
    :returns: [{'bbox' (fractions), 'kind', 'caption', 'decorative'}], or
        None when the output can't be parsed (don't cache that).
    :rtype: list[dict]|None
    """
    chat_opts = {"image": image, "max_tokens": 1500, "temperature": 0.0}
    if opts.get("model"):
        chat_opts["model"] = opts["model"]
    figs = _parse_figure_list(ai.chat_completion(_DETECT_PROMPT, chat_opts))
    if figs is None:
        return None
    out = []
    for f in figs:
        if not isinstance(f, dict):
            continue
        box = _clamp_box(f.get("bbox") or f.get("bbox_2d"))
        if box is None:
            continue
        kind = str(f.get("kind") or "drawing").lower()
        if kind not in KINDS:
            kind = "drawing"
        out.append({"bbox": box, "kind": kind, "caption": str(f.get("caption") or "").strip(),
                    "decorative": bool(f.get("decorative")) or kind == "ornament"})
    return out


def crop(image_path, bbox, opts={}):
    """JPEG of a page region, longest edge at most 'max_px'.

    :param str image_path: Local page image.
    :param list[float] bbox: Region in page fractions.
    :param dict opts: 'max_px' (int, default 1024).
    :rtype: ai.Image
    """
    full_px = pymupdf.Pixmap(image_path).width
    with pymupdf.open(image_path) as doc:
        page = doc[0]
        r = page.rect
        clip = pymupdf.Rect(r.x0 + bbox[0] * r.width, r.y0 + bbox[1] * r.height,
                            r.x0 + bbox[2] * r.width, r.y0 + bbox[3] * r.height)
        # page points -> source pixels (image DPI)
        native = full_px / r.width
        longest_px = max(clip.width, clip.height, 1.0) * native
        zoom = native * min(1.0, opts.get("max_px", _CROP_MAX_PX) / longest_px)
        pix = page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(zoom, zoom))
        return ai.Image(data=pix.tobytes("jpeg"), mimetype="image/jpeg")


def describe(crop_image, kind, caption, opts={}):
    """Write alt text (and, for charts/diagrams/maps, a long description) for one figure.

    Model-output problems become flags, never exceptions.

    :param ai.Image crop_image: The figure crop.
    :param str kind: Figure kind.
    :param str caption: Its printed caption, or ''.
    :param dict opts: _describe_prompt() opts, plus 'model'.
    :returns: (alt, long_description_or_None, flags)
    :rtype: tuple
    """
    prompt = _describe_prompt(kind, caption, opts)
    chat_opts = {"image": crop_image, "max_tokens": 700, "temperature": 0.0}
    if opts.get("model"):
        chat_opts["model"] = opts["model"]
    try:
        obj = alt_utils.parse_model_json(ai.chat_completion(prompt, chat_opts))
    except ValueError:
        return "", None, ["ERROR:unparseable-model-output"]
    alt = (obj.get("alt") or "").strip()
    long_desc = alt_utils._optional_text(obj.get("long_description"))
    flags = alt_utils.validate_alt(alt, opts)
    if "TOO_LONG" in flags:
        max_chars = opts.get("max_alt_chars", alt_utils.DEFAULT_MAX_ALT_CHARS)
        retry = prompt + (f"\n\nYour previous alt was too long: {alt!r}\nRewrite it as one "
                          f"sentence under {max_chars} characters. "
                          'Return ONLY {"alt": "...", "long_description": null}')
        try:
            shorter = (alt_utils.parse_model_json(ai.chat_completion(retry, {**chat_opts, "max_tokens": 200}))
                       .get("alt") or "").strip()
            if shorter:
                alt, flags = shorter, alt_utils.validate_alt(shorter, opts)
        except (ValueError, RuntimeError):
            pass  # keep the long one, flagged
    if kind in COMPLEX_KINDS and not long_desc:
        flags.append("NO_LONG_DESCRIPTION")
    return alt, long_desc, flags


def for_tagger(figs):
    """The figures pdf_tag should tag: informative ones with something to say.

    :param list[dict] figs: page_figures() records.
    :returns: [{'bbox', 'alt' (alt plus long description), 'caption', 'kind'}]
    :rtype: list[dict]
    """
    out = []
    for f in figs:
        if f.get("decorative") or _rejected(f.get("flag")):
            continue
        alt = (f.get("alt") or "").strip()
        if not alt:
            continue  # nothing honest to say
        long_desc = (f.get("long_description") or "").strip()
        out.append({"bbox": f["bbox"], "alt": f"{alt} {long_desc}".strip() if long_desc else alt,
                    "caption": f.get("caption") or "", "kind": f.get("kind", "")})
    return out


def _rejected(flag):
    """True when the reviewer put reject or skip anywhere in the flag cell.

    :rtype: bool
    """
    tokens = re.split(r"[;,\s]+", (flag or "").lower())
    return any(t in ("reject", "skip") for t in tokens)


def review_rows_text(rows, header=True):
    """CSV text for figure records, one row each.

    :param list[dict] rows: page_figures() records plus 'page'.
    :param bool header: Include the header row.
    :rtype: str
    """
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=REVIEW_FIELDS)
    if header:
        w.writeheader()
    for r in rows:
        flags = r.get("flags")
        w.writerow({
            "page": r["page"], "n": r.get("n", 1), "kind": r.get("kind", ""),
            "decorative": "y" if r.get("decorative") else "",
            "bbox": " ".join(f"{v:.4f}" for v in r["bbox"]), "caption": r.get("caption", ""),
            "alt": r.get("alt", ""), "long_description": r.get("long_description") or "",
            "flag": ";".join(flags) if isinstance(flags, list) else (r.get("flag") or ""),
            "notes": r.get("notes", ""), "crop": r.get("crop", ""),
        })
    return buf.getvalue()


def parse_review(text):
    """Read a figure-review CSV into per-page figure records with the reviewer's edits.

    decorative accepts y/yes/true/1; reject/skip in flag drops the figure.

    :param str text: CSV text.
    :returns: {page: [records]}
    :rtype: dict
    """
    out = {}
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        try:
            page = int(row.get("page") or 0)
            n = int(float(row.get("n") or 1))
            bbox = [float(v) for v in re.split(r"[\s,]+", (row.get("bbox") or "").strip())]
        except ValueError:
            continue
        if len(bbox) != 4:
            continue
        out.setdefault(page, []).append({
            "n": n, "bbox": bbox, "kind": row.get("kind", ""),
            "caption": row.get("caption", ""),
            "decorative": (row.get("decorative") or "").strip().lower() in ("y", "yes", "true", "1"),
            "alt": (row.get("alt") or "").strip(),
            "long_description": (row.get("long_description") or "").strip() or None,
            "flag": (row.get("flag") or "").strip(), "notes": row.get("notes", ""),
            "crop": row.get("crop", ""),
        })
    return out


def _page_number(page):
    return page["page_no"] or 1


def page_dir(page):
    """Output path parts for a page's figures, under the item.

    :param dict page: A prep.stage_pages() entry.
    :rtype: tuple
    """
    if page["kind"] == "pdf":
        return ("accessibility", page["filename"], str(page["page_no"]))
    return ("accessibility", page["filename"])


def review_path(item, filename):
    """CaskFS path of a source file's figure-review.csv.

    :rtype: str
    """
    return items.dest_dir(item, f"accessibility/{filename}") + "/figure-review.csv"


def read_review(item, filename):
    """A source file's reviewed figures, or {} when there's no review CSV yet.

    :rtype: dict
    """
    try:
        return parse_review(fs.read(review_path(item, filename)).decode("utf-8"))
    except FileNotFoundError:
        return {}


def append_review(item, filename, source_path, rows):
    """Append newly detected figures to a source file's review CSV.

    :param ItemLocation item: Resolved item location.
    :param str filename: The source file.
    :param str source_path: Its CaskFS path, for lineage.
    :param list[dict] rows: Records plus 'page'.
    :returns: The CSV path, or None when there was nothing to add.
    :rtype: str|None
    """
    if not rows:
        return None
    path = review_path(item, filename)
    try:
        existing = fs.read(path).decode("utf-8")
    except FileNotFoundError:
        existing = ""
    text = existing + review_rows_text(rows, header=not existing)
    return derivative.write(
        type=item.dest_type, collection=item.collection,
        rel_path=items.dest_rel(item, "accessibility", filename, "figure-review.csv"),
        data=text.encode("utf-8"), source_files=[source_path],
        opts={**items.dest_opts(item), "replace": True, "link_relation": REVIEW_LINK_RELATION},
    )["path"]


def page_figures(item, page, image, opts={}):
    """Detected and described figures for one page: reviewed, cached, or new.

    Reviewer rows win, then a cached figures.json; otherwise one detect call
    plus one describe call per informative figure, published with crops.

    :param ItemLocation item: Resolved item location.
    :param dict page: A prep.stage_pages() entry.
    :param str image: Local page image to detect on and crop from.
    :param dict opts: 'review' ({page: records} from read_review()),
        'page_alt' (str — reuse for the largest figure instead of generating),
        plus describe() opts.
    :returns: (records, fresh) — fresh is True when just detected.
    :rtype: tuple[list[dict], bool]
    """
    page_no = _page_number(page)
    review = opts.get("review") or {}
    if page_no in review:
        return review[page_no], False
    parts = page_dir(page)
    cache_path = items.dest_dir(item, "/".join(parts)) + "/figures.json"
    try:
        return json.loads(fs.read(cache_path)), False
    except FileNotFoundError:
        pass

    found = detect(image, opts)
    if found is None:
        utils.warn(f"{page['filename']} page {page_no}: figure detection output unusable; "
                   "left without figures (not cached)")
        return [], False
    informative = [i for i, f in enumerate(found) if not f["decorative"]]
    curated = None
    if opts.get("page_alt") and informative:
        curated = max(informative, key=lambda i: (found[i]["bbox"][2] - found[i]["bbox"][0])
                      * (found[i]["bbox"][3] - found[i]["bbox"][1]))
    write_opts = {**items.dest_opts(item), "replace": True}
    figs = []
    for n, f in enumerate(found, 1):
        rec = dict(f, n=n, alt="", long_description=None, flags=[], crop="")
        cropped = crop(image, f["bbox"])
        crop_name = f"figure-{n}.jpg"
        derivative.write(
            type=item.dest_type, collection=item.collection,
            rel_path=items.dest_rel(item, *parts, crop_name), data=cropped.data,
            source_files=[page["source_path"]],
            opts={**write_opts, "link_relation": FIGURE_CROP_LINK_RELATION},
        )
        rec["crop"] = "/".join((*parts[1:], crop_name))
        if not f["decorative"]:
            if curated == n - 1:
                rec["alt"] = opts["page_alt"]
            else:
                total = page["count"] or 1
                alt, long_desc, flags = describe(cropped, f["kind"], f["caption"],
                                                 {**opts, "page": (page_no, total)})
                rec.update(alt=alt, long_description=long_desc, flags=flags)
        figs.append(rec)
    derivative.write(
        type=item.dest_type, collection=item.collection,
        rel_path=items.dest_rel(item, *parts, "figures.json"),
        data=json.dumps(figs, ensure_ascii=False, indent=1).encode("utf-8"),
        source_files=[page["source_path"]],
        opts={**write_opts, "link_relation": FIGURES_LINK_RELATION},
    )
    return figs, True
