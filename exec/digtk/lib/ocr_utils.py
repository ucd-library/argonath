"""
OCR processing utilities — Tesseract and ML-based OCR wrappers.
Tesseract is the baseline; ML OCR (surya or docTR) is the fallback
when Tesseract confidence falls below threshold.

run_tesseract/analyze_words/compare_orientations are ARK/CaskFS-agnostic
(local file paths only) and deliberately shared: digtk.models.digital_website.image
calls them during derivatives generation to pick the correct scan orientation,
and the future `digtk ocr tesseract` command (phase 0.5) calls run_tesseract
independently, later, to produce an item's final searchable hOCR/text output.
Neither caller depends on the other having run.
"""
import difflib
import os
import re
import subprocess

from digtk.lib import utils
from digtk.lib.config import CONFIG

CONFIDENCE_THRESHOLD = CONFIG["ocr"]["confidence_threshold"]

# hOCR word spans carry title='bbox ...; x_wconf NN' — x_wconf appears nowhere else.
_X_WCONF_RE = re.compile(r"x_wconf\s+(\d+)")


class TesseractError(RuntimeError):
    """Raised when a Tesseract subprocess invocation fails, times out, or exits
    0 without writing its output files.

    :param list[str] argv: The command that was run.
    :param int|None returncode: Process exit code, or None if it timed out.
    :param str stderr: Captured stderr output.
    :param bool timed_out: True if the failure was a timeout rather than a non-zero exit.
    """

    def __init__(self, argv, returncode, stderr, timed_out=False):
        reason = "timed out" if timed_out else f"exited with code {returncode}"
        super().__init__(f"Tesseract command {reason}: {' '.join(argv)}\n{stderr}")
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out


def run_tesseract(image_path, output_base, opts={}):
    """Run Tesseract OCR on an image, producing .hocr and .txt outputs.

    `tesseract <image_path> <output_base> --dpi {dpi} -l {lang} --psm {psm}
    --oem {oem} hocr txt`. Output is captured as bytes and decoded with
    errors="replace" — Tesseract can emit non-UTF-8 bytes on stderr, and a
    strict text-mode decode would turn that into an uncaught UnicodeDecodeError.

    :param str image_path: Path to the prepared (deskewed, high-DPI) image.
    :param str output_base: Base output path (Tesseract appends .hocr / .txt).
    :param dict opts: 'lang' (str, default CONFIG["ocr"]["lang"]), 'psm' (int,
        default CONFIG["ocr"]["psm"]), 'oem' (int, default CONFIG["ocr"]["oem"]),
        'dpi' (int, default CONFIG["image"]["dpi"]), 'timeout' (float, default
        CONFIG["image"]["timeout_s"]), 'retries' (int, default 1 — extra tries
        after a timeout), 'threads' (int, default CONFIG["ocr"]["threads"] —
        OMP_THREAD_LIMIT unless the environment already sets it), plus anything
        analyze_words() accepts (forwarded when computing 'confidence').
    :returns: {"hocr_path": str, "txt_path": str, "confidence": float}
    :rtype: dict
    :raises TesseractError: If `tesseract` fails, times out, or exits 0
        without writing its output files.
    """
    lang = opts.get("lang", CONFIG["ocr"]["lang"])
    psm = opts.get("psm", CONFIG["ocr"]["psm"])
    oem = opts.get("oem", CONFIG["ocr"]["oem"])
    dpi = opts.get("dpi", CONFIG["image"]["dpi"])
    timeout = opts.get("timeout", CONFIG["image"]["timeout_s"])

    os.makedirs(os.path.dirname(os.path.abspath(output_base)), exist_ok=True)

    argv = [
        "tesseract", image_path, output_base,
        "--dpi", str(dpi), "-l", lang, "--psm", str(psm), "--oem", str(oem),
        "hocr", "txt",
    ]
    threads = opts.get("threads", CONFIG["ocr"]["threads"])
    env = {**os.environ}
    env.setdefault("OMP_THREAD_LIMIT", str(threads))
    retries = opts.get("retries", 1)
    for attempt in range(retries + 1):
        try:
            result = subprocess.run(
                argv, shell=False, check=True, timeout=timeout, capture_output=True, env=env,
            )
            break
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else e.stderr
            raise TesseractError(argv, e.returncode, stderr) from e
        except subprocess.TimeoutExpired as e:
            if attempt >= retries:
                raise TesseractError(argv, None, "", timed_out=True) from e
            utils.warn(f"tesseract timed out after {timeout}s on {image_path}, retrying")

    hocr_path = f"{output_base}.hocr"
    txt_path = f"{output_base}.txt"
    for path in (hocr_path, txt_path):
        if not os.path.exists(path):
            raise TesseractError(argv, result.returncode, f"tesseract exited 0 but did not write {path}")

    return {"hocr_path": hocr_path, "txt_path": txt_path, "confidence": extract_confidence(hocr_path)}


def _word_confidences(hocr_path):
    """Extract every word's x_wconf confidence value from an hOCR file.

    Parses the `x_wconf NN` token directly out of each word span's `title`
    attribute via regex rather than a full XML/DOM parse — hOCR's structure is
    simple enough that this is robust and avoids an extra XML dependency (see
    the phase 0.4 migration notes on skipping the reference implementation's
    djvu-XML intermediate format). Decoded with errors="replace" since
    Tesseract can emit non-UTF-8 bytes.

    :param str hocr_path: Path to the hOCR output file.
    :returns: Word confidence values (0.0-100.0), in document order.
    :rtype: list[float]
    """
    with open(hocr_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return [float(m) for m in _X_WCONF_RE.findall(text)]


def extract_confidence(hocr_path):
    """Parse a Tesseract hOCR file and return the mean word confidence score.

    A page with no recognized words scores 0.0 — downstream that reads as
    "below threshold", which is the right signal for both genuinely unreadable
    pages and blank ones.

    :param str hocr_path: Path to the hOCR output file.
    :returns: Mean confidence score (0.0-100.0), or 0.0 if the file has no words.
    :rtype: float
    """
    confidences = _word_confidences(hocr_path)
    return sum(confidences) / len(confidences) if confidences else 0.0


def analyze_words(hocr_path, opts={}):
    """Summarize word confidence stats for an hOCR file, dropping low-confidence noise.

    :param str hocr_path: Path to the hOCR output file.
    :param dict opts: 'confidence_threshold' (float, default
        CONFIG["ocr"]["orientation_word_confidence_threshold"]) — words below
        this are excluded from the stats entirely, matching the reference
        implementation's fuzz/OCR noise filtering.
    :returns: {"count": int, "avg": float, "median": float}
    :rtype: dict
    """
    threshold = opts.get("confidence_threshold", CONFIG["ocr"]["orientation_word_confidence_threshold"])
    confidences = sorted(c for c in _word_confidences(hocr_path) if c >= threshold)
    if not confidences:
        return {"count": 0, "avg": 0.0, "median": 0.0}
    count = len(confidences)
    return {
        "count": count,
        "avg": sum(confidences) / count,
        "median": confidences[count // 2],
    }


def compare_orientations(candidates, opts={}):
    """Run Tesseract against both a deskewed and non-deskewed candidate image
    and decide which one is actually right-side-up/legible.

    The deskewed candidate wins only if it has both a higher word count and a
    higher-or-equal median confidence than the original, there's enough
    combined OCR evidence (word_count_threshold) to trust the comparison at
    all rather than flipping orientation based on a couple of noisy matches
    on a mostly-blank image, *and* (when a measured skew angle is supplied)
    the page is actually skewed enough to be worth correcting
    (angle_threshold) — matching the reference implementation's three-signal
    selection (word count, confidence, deskew angle), which this used to be
    missing the third of.

    :param dict candidates: Exactly the keys "original" and "deskewed", each a
        local path to an OCR-prep candidate image.
    :param dict opts: Forwarded to run_tesseract()/analyze_words() (lang, psm,
        oem, dpi, timeout, confidence_threshold), plus 'word_count_threshold'
        (int, default CONFIG["ocr"]["orientation_word_count_threshold"]),
        'angle' (float|None, default None) — the skew angle in degrees
        measured off the "original" candidate (see
        digtk.lib.image_magick.detect_skew_angle()); when omitted, the angle
        gate is skipped entirely (treated as "unknown, don't block on it"),
        preserving old behavior for callers that don't measure it. 'angle_threshold'
        (float, default CONFIG["ocr"]["orientation_angle_threshold"]) — only
        consulted when 'angle' is provided.
    :returns: {"selected": "original"|"deskewed", "stats": {"original": {...}, "deskewed": {...}}}
        — each stats entry also carries "hocr_path" (the Tesseract hOCR file
        that produced it), so callers building a real OCR text layer (e.g.
        digtk.lib.djvu) can reuse it without re-running Tesseract.
    :rtype: dict
    :raises ValueError: If candidates doesn't have exactly "original" and "deskewed" keys.
    :raises TesseractError: If either Tesseract invocation fails or times out.
    """
    if set(candidates) != {"original", "deskewed"}:
        raise ValueError('candidates must have exactly "original" and "deskewed" keys')

    stats = {}
    for label, path in candidates.items():
        tesseract_result = run_tesseract(path, os.path.splitext(path)[0], opts)
        stats[label] = analyze_words(tesseract_result["hocr_path"], opts)
        stats[label]["hocr_path"] = tesseract_result["hocr_path"]

    word_count_threshold = opts.get("word_count_threshold", CONFIG["ocr"]["orientation_word_count_threshold"])
    angle_threshold = opts.get("angle_threshold", CONFIG["ocr"]["orientation_angle_threshold"])
    angle = opts.get("angle")

    original, deskewed = stats["original"], stats["deskewed"]
    enough_evidence = (original["count"] + deskewed["count"]) >= word_count_threshold
    improved = deskewed["count"] > original["count"] and deskewed["median"] >= original["median"]
    skewed_enough = angle is None or abs(angle) >= angle_threshold

    return {
        "selected": "deskewed" if (enough_evidence and improved and skewed_enough) else "original",
        "stats": stats,
    }


def run_ml_ocr(image_path, output_base, opts={}):
    """Run ML-based OCR (surya or docTR) on an image.

    Used automatically when Tesseract confidence is below CONFIDENCE_THRESHOLD.

    :param str image_path: Path to the source image.
    :param str output_base: Base output path for hOCR and text outputs.
    :param dict opts: Additional model options.
    :returns: Dict with 'hocr_path', 'txt_path'.
    :rtype: dict
    """
    raise NotImplementedError("phase 0.5")


# --- correction layer ---------------------------------------------------------

_EDGE_PUNCT = re.compile(r"^(\W*)(.*?)(\W*)$", re.DOTALL)


def _norm(s):
    return re.sub(r"[^\w]", "", s.lower())


def _sim(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _merge(old, new):
    """Return new's core inside old's edge punctuation, or None if the change isn't trusted.

    Rejected: punctuation-only or case-only changes, any change to the
    digits, dropped letters, and text outside cp1252.

    :param str old: The OCR word.
    :param str new: The vision model's reading.
    :rtype: str|None
    """
    lead, old_core, trail = _EDGE_PUNCT.match(old).groups()
    new_core = _EDGE_PUNCT.match(new).group(2)
    if not old_core or not new_core or old_core == new_core:
        return None
    # typography only, unless it splits words
    if re.sub(r"[^\w]", "", old_core) == re.sub(r"[^\w]", "", new_core) \
            and " " not in new_core:
        return None
    if re.sub(r"\D", "", old_core) != re.sub(r"\D", "", new_core):
        return None
    # mostly-numeric tokens may only be split
    digits = sum(ch.isdigit() for ch in old_core)
    if digits >= 0.5 * len(_norm(old_core)) and new_core.replace(" ", "") != old_core:
        return None
    # case fixes only for "succesS"-style OCR errors
    if old_core.lower() == new_core.lower() and not re.search(r"[a-z][A-Z]", old_core):
        return None
    if len(_norm(new_core)) < 0.8 * len(_norm(old_core)):  # never drop letters
        return None
    try:
        new_core.encode("cp1252")
    except UnicodeEncodeError:
        return None
    return lead + new_core + trail


def correct_words(words, model_text, opts={}):
    """Rewrite OCR words' text in place from a vision model's transcript of the same page.

    Only substitutions that still look like the same word are applied, so a
    transcript that skipped a paragraph can't map prose onto the wrong boxes.
    Pages that barely align, or have no usable transcript, are left untouched.

    :param list[dict] words: [{'text', 'bbox', ...}] in reading order; changed in place.
    :param str model_text: The vision model's verbatim transcript.
    :param dict opts: 'min_sim' (float, default 0.75) — per-word similarity
        floor; 'min_page_ratio' (float, default 0.3) — whole-page alignment floor.
    :returns: Change log, [{'i', 'old', 'new'}].
    :rtype: list[dict]
    """
    min_sim = opts.get("min_sim", 0.75)
    min_page_ratio = opts.get("min_page_ratio", 0.3)
    if not words:
        return []
    q = (model_text or "").split()
    if not q or model_text.strip() == "[BLANK]":
        return []
    # align word-bearing tokens only
    hi = [i for i, w in enumerate(words) if _norm(w["text"])]
    q = [t for t in q if _norm(t)]
    h = [words[i]["text"] for i in hi]
    if not h or not q:
        return []
    sm = difflib.SequenceMatcher(None, [_norm(x) for x in h], [_norm(x) for x in q],
                                 autojunk=False)
    if sm.ratio() < min_page_ratio:
        return []
    changes = []

    def apply(k, new):
        i = hi[k]
        old = words[i]["text"]
        # second half of a hyphenated break
        if i and words[i - 1]["text"].endswith("-") and new.lower().endswith(old.lower()) \
                and len(new) > len(old):
            return
        merged = _merge(old, new)
        if merged is not None:
            changes.append({"i": i, "old": old, "new": merged})
            words[i]["text"] = merged

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        n, m = i2 - i1, j2 - j1
        if tag == "equal":
            for k in range(n):  # case-only differences
                apply(i1 + k, q[j1 + k])
        elif tag == "replace":
            # skip 1-2 letter tokens
            if n == m:
                for k in range(n):
                    if len(_norm(h[i1 + k])) > 2 and _sim(h[i1 + k], q[j1 + k]) >= min_sim:
                        apply(i1 + k, q[j1 + k])
            elif n == 1 and m <= 3:  # an OCR join, e.g. "further.Increasing"
                new = " ".join(q[j1:j2])
                if len(_norm(h[i1])) > 2 and _sim(h[i1], new) >= min_sim:
                    apply(i1, new)
    return changes
