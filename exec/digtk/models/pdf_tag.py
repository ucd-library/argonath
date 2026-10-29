"""
PDF/UA-1 accessibility structure tagging for digtk-built searchable PDFs
(digtk.models.pdf_utils.build_searchable_pdf output).

The page scan becomes an /Artifact (standard for OCR'd documents: the text
layer carries the semantics) or, when alt text is supplied, a /Figure with
that description; every invisible OCR word gets a marked-content ID.
Baseline structure is per-line /P elements in reading order. When a vision
layout is supplied (opts['layouts'] — a per-page list of
digtk.models.layout.page_layout() results):

- data tables become /Table -> /THead|/TBody -> /TR -> /TH|/TD (TH
  Scope=Column; with 'row_header': true on the block, each body row's
  leading label cell becomes a /TH Scope=Row),
- a heading immediately preceding its table becomes the table's /Caption,
- other headings become /H1-/H6 at the layout's level, clamped so levels
  never skip (PDF/UA-1 7.4.2), and each one becomes a bookmark,
- list blocks become /L -> /LI -> /Lbl + /LBody,
- a block whose layout 'lang' differs from the document gets its own /Lang,
- with opts['paragraphs'], each text block is one /P instead of one per line,
- with opts['strict'], lines the layout doesn't account for (rotated
  scanner junk, mirror text) are demoted to untagged artifacts instead of /P.

Reading order always comes from page geometry: words group into lines,
margin notes split off their baseline, and multi-column pages (the layout's
'columns' count, or local two-column bands) read column by column.

Any block that doesn't align confidently falls back to paragraphs — wrong
semantics are worse than plain ones. Alignment strategy: the layout decides
WHICH lines are table rows/headings (fuzzy text match, both read
top-to-bottom); WORD->CELL splits come from aligning consecutive words to
the layout's cell texts (DP; empty cells allowed).

Relies on the content-stream shape pdf_utils produces (one stream per
inserted element: `q ... /img Do Q`, then one `q BT ... Tm ... [<hex>]TJ ET Q`
per word). Anything unrecognized passes through untouched. Verify claims
with `verapdf --flavour ua1`.
"""
import re
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

import pikepdf
from pikepdf import Dictionary, Name

_TM = re.compile(rb"1 0 0 1 ([\d.+-]+) ([\d.+-]+) Tm")
_TJ = re.compile(rb"\[<([0-9a-fA-F]+)>\]TJ")
_TF_NAME = re.compile(rb"/(\S+) ([\d.]+) Tf")
_CMAP_CHAR = re.compile(rb"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>")
_CMAP_RANGE = re.compile(
    rb"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*(<([0-9a-fA-F]+)>|\[(.*?)\])", re.S)
_LINE_TOL = 3.0   # points: min y-gap that splits lines
_LINE_TOL_SCALE = 0.3  # ...or this share of font size
_MATCH_MIN = 0.5  # token-overlap floor for row/heading<->line matching
_CELL_MIN = 0.6   # per-row mean cell-alignment floor (below -> row stays a /P)
DEFAULT_LOOKAHEAD = 40  # max lines past the cursor a layout block may match
_BACKTRACK = 3    # lines a block may match behind cursor
_HEAD_SKIP = 2    # body lines a wrapped heading may straddle
_HEADING_MAX_WORDS = 20  # longer "headings" are tagged as text
_MARGIN_SLACK = 10.0  # points: margin run clearance from body
_GUTTER_MIN_W = 8.0   # points: narrowest column gutter
_GUTTER_STRADDLE = 0.05  # share of lines crossing a gutter
_GUTTER_BOTH = 0.25   # share of lines with words both sides
_LOCAL_GAP_MIN = 14.0   # points: narrowest local-band gap
_LOCAL_GAP_RATIO = 3.0  # ...times the line's usual word gap
_LOCAL_BAND_MIN = 3     # lines sharing a gap make a band
_FIG_INSET = 0.01  # page share a figure box shrinks by
_FIG_LINE_SHARE = 0.6  # run share inside to join

_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_ORDERED_LABEL_RE = re.compile(
    r"^\(?(\d{1,3}(?:[.\-–]\d{1,3})?|[a-zA-Z]|[ivxlcIVXLC]{1,6})[.)\]:]?$")
_BULLET_RE = re.compile(r"^[•·▪■●○◦‣*\-–—]$")


@dataclass
class _Word:
    stream_index: int
    x: float
    y: float
    text: str
    mcid: int = -1
    artifact: bool = False
    size: float = 0.0  # font size in points; 0 = unknown
    stm: object = None  # MCID's Form XObject; None = page
    x1: float = 0.0  # right edge when known; 0 = unknown


@dataclass
class _Line:
    words: List[_Word]
    lang: str = ""  # set when it differs from the document
    rank: tuple = ()  # reading-order key (band, column, -y)

    @property
    def y(self):
        return self.words[0].y

    @property
    def text(self):
        out = []
        for i, w in enumerate(self.words):
            prev = self.words[i - 1] if i else None
            # kerned word split over two operators
            glue = (prev is not None and prev.x1 > 0 and not prev.text.endswith(" ")
                    and w.x - prev.x1 < 0.12 * max(prev.size, w.size, 1.0))
            out.append(("" if glue or not out else " ") + w.text)
        return "".join(out)


@dataclass
class _Para:
    """Consecutive lines of one layout text block, emitted as a single /P."""
    lines: List[_Line]
    lang: str = ""
    block_index: int = -1

    @property
    def words(self):
        return [w for ln in self.lines for w in ln.words]


@dataclass
class _Heading:
    """A heading, possibly wrapped over several lines."""
    lines: List[_Line]
    level: int = 0  # layout level 1-6; 0 = unknown
    lang: str = ""
    title: str = ""  # bookmark text; OCR lines as fallback
    block_index: int = -1

    @property
    def words(self):
        return [w for ln in self.lines for w in ln.words]

    @property
    def text(self):
        return self.title or " ".join(ln.text for ln in self.lines)


@dataclass
class _ListItem:
    index: int
    lines: List[_Line] = field(default_factory=list)
    lang: str = ""

    @property
    def words(self):
        return [w for ln in self.lines for w in ln.words]


@dataclass
class _ListGroup:
    """One layout list block, emitted as /L with one /LI per item."""
    block_index: int
    ordered: bool
    items: List[_ListItem] = field(default_factory=list)


@dataclass
class _ArtifactLine:
    line: _Line


@dataclass
class _Row:
    line: _Line
    cells: List[List[_Word]]
    header: bool = False
    raw: bool = False  # unaligned in-table line -> one TD spanning all columns


@dataclass
class _TableGroup:
    block_index: int
    rows: List[_Row] = field(default_factory=list)
    caption: Optional[_Line] = None
    row_header: bool = False  # first cell of each body row is a /TH Scope=Row


@dataclass
class _CaptionMark:
    line: _Line
    table_block: int


@dataclass
class _Figure:
    """An in-page figure region: its alt, caption, and the words inside it."""
    bbox: List[float]  # PDF points, y up
    alt: str
    caption: str = ""
    kind: str = ""
    words: List[_Word] = field(default_factory=list)
    caption_line: Optional[_Line] = None
    mcid: int = -1
    content: List[tuple] = field(default_factory=list)  # (stm, mcid) of its graphics


def _utf16_hex(hexs):
    """Decode a ToUnicode hex string.

    :param bytes hexs: UTF-16BE hex digits.
    :returns: Decoded text.
    :rtype: str
    """
    return bytes.fromhex(hexs.decode("ascii")).decode("utf-16-be", "replace")


def _parse_tounicode(data):
    """Parse a ToUnicode CMap's bfchar and bfrange sections.

    :param bytes data: CMap stream bytes.
    :returns: Character code to text.
    :rtype: dict[int, str]
    """
    out = {}
    for m in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in _CMAP_CHAR.findall(m.group(1)):
            out[int(src, 16)] = _utf16_hex(dst)
    for m in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, _all, dst, arr in _CMAP_RANGE.findall(m.group(1)):
            lo_i, hi_i = int(lo, 16), int(hi, 16)
            if arr:
                for k, d in enumerate(re.findall(rb"<([0-9a-fA-F]+)>", arr)):
                    out[lo_i + k] = _utf16_hex(d)
            elif dst:
                base = _utf16_hex(dst)
                for k in range(min(hi_i - lo_i, 65535) + 1):
                    out[lo_i + k] = base[:-1] + chr(ord(base[-1]) + k) if base else ""
    return out


def _page_cmaps(page):
    """Map each Type0 font on a page to its ToUnicode table.

    The embedded text-layer font writes 2-byte glyph codes, not cp1252.

    :param pikepdf.Page page: Page to inspect.
    :returns: Font resource name to code map.
    :rtype: dict[str, dict[int, str]]
    """
    maps = {}
    fonts = (page.obj.get("/Resources") or {}).get("/Font")
    if fonts is None:
        return maps
    for name, font in fonts.items():
        if str(font.get("/Subtype")) == "/Type0" and font.get("/ToUnicode") is not None:
            try:
                maps[str(name)] = _parse_tounicode(font.ToUnicode.read_bytes())
            except Exception:  # noqa: BLE001 — fall back to cp1252
                continue
    return maps


def _classify_streams(streams, cmaps=None):
    """Split a page's content streams into images and words.

    :param list[bytes] streams: The page's content streams.
    :param dict cmaps: Optional _page_cmaps result for CID fonts.
    :returns: (image_stream_indexes, words)
    :rtype: tuple[list[int], list[_Word]]
    """
    imgs, words = [], []
    for i, data in enumerate(streams):
        if b" Do" in data and b"BT" not in data:
            imgs.append(i)
        elif b"BT" in data:
            m, t = _TM.search(data), _TJ.search(data)
            if not (m and t):
                continue
            raw = bytes.fromhex(t.group(1).decode("ascii"))
            f = _TF_NAME.search(data)
            cmap = cmaps.get("/" + f.group(1).decode("ascii")) if (cmaps and f) else None
            if cmap is not None:  # 2-byte CIDs via ToUnicode
                text = "".join(cmap.get(int.from_bytes(raw[k:k + 2], "big"), "")
                               for k in range(0, len(raw) - 1, 2))
            else:
                text = raw.decode("cp1252", "replace")
            words.append(_Word(i, float(m.group(1)), float(m.group(2)), text,
                               size=float(f.group(2)) if f else 0.0))
    return imgs, words


def _lines(words):
    """Group words into lines by single linkage down the page.

    A word joins the open line while its y-gap to the line's lowest word is
    small, so descender jitter chains together but a real line gap breaks.

    :param list[_Word] words: One page's words.
    :returns: Lines, with margin runs split off (see _split_margins).
    :rtype: list[_Line]
    """
    out = []
    for w in sorted(words, key=lambda w: -w.y):
        prev = out[-1][-1] if out else None
        if prev is not None and prev.y - w.y <= max(
                _LINE_TOL, _LINE_TOL_SCALE * max(prev.size, w.size)):
            out[-1].append(w)
        else:
            out.append([w])
    for line in out:
        line.sort(key=lambda w: w.x)
    return _split_margins([_Line(ws) for ws in out])


def _runs(line_words):
    """Split one baseline into runs at large horizontal gaps.

    A gap is over 2.5x the line's median word pitch and over 25pt.

    :param list[_Word] line_words: Words of one line, left to right.
    :returns: Runs of words.
    :rtype: list[list[_Word]]
    """
    if len(line_words) < 3:
        return [line_words]
    pitches = sorted(b.x - a.x for a, b in zip(line_words, line_words[1:]))
    median = pitches[len(pitches) // 2]
    runs, cur = [], [line_words[0]]
    for a, b in zip(line_words, line_words[1:]):
        if b.x - a.x > max(25.0, 2.5 * median):
            runs.append(cur)
            cur = []
        cur.append(b)
    runs.append(cur)
    return runs


def _word_end(w):
    """A word's right edge: exact when known, else about 0.5 em per character.

    :param _Word w: The word.
    :returns: Right edge x in points.
    :rtype: float
    """
    return w.x1 if w.x1 > w.x else w.x + 0.5 * (w.size or 8.0) * len(w.text)


def _split_margins(lines):
    """Split margin notes and margin headings off the body lines they share a baseline with.

    The body column comes from the long lines' median start and end x.
    Margin runs become their own lines, placed before that baseline's body text.

    :param list[_Line] lines: Lines in page order.
    :returns: Lines with margin runs split off.
    :rtype: list[_Line]
    """
    long = [ln for ln in lines if len(ln.words) >= 5]
    if len(long) < 5:
        return lines
    starts = sorted(ln.words[0].x for ln in long)
    ends = sorted(_word_end(ln.words[-1]) for ln in long)
    body_x0 = starts[len(starts) // 2]
    body_x1 = ends[len(ends) // 2]
    out = []
    for ln in lines:
        runs = _runs(ln.words)
        if len(runs) < 2:
            out.append(ln)
            continue
        margin = [r for r in runs if _word_end(r[-1]) < body_x0 - _MARGIN_SLACK
                  or r[0].x > body_x1 + _MARGIN_SLACK]
        body = [w for r in runs if r not in margin for w in r]
        if not margin or not body:
            out.append(ln)
            continue
        out.extend(_Line(r) for r in margin)
        out.append(_Line(body))
    return out


def _find_gutters(lines, k, both_share):
    """Find up to k vertical gutters between text columns.

    A gutter is a gap almost no word crosses, with words on both sides in a
    fair share of lines (which rules out ragged margins and table gaps).

    :param list[_Line] lines: Lines in page order.
    :param int k: Most gutters to return.
    :param float both_share: Share of lines needing words on both sides.
    :returns: [(x_center, width)] sorted by x.
    :rtype: list[tuple]
    """
    if k <= 0 or len(lines) < 6:
        return []
    spans = [[(w.x, _word_end(w)) for w in ln.words] for ln in lines]
    lo = min(x0 for sp in spans for x0, _ in sp)
    hi = max(x1 for sp in spans for _, x1 in sp)
    if hi - lo < 4 * _GUTTER_MIN_W:
        return []
    n = len(lines)
    ok = []  # per 1-pt step: gutter candidate?
    xs = [lo + i for i in range(int(hi - lo) + 1)]
    for x in xs:
        straddle = sum(1 for sp in spans if any(x0 < x < x1 for x0, x1 in sp))
        both = sum(1 for sp in spans
                   if any(x1 <= x for _, x1 in sp) and any(x0 >= x for x0, _ in sp))
        ok.append(straddle <= _GUTTER_STRADDLE * n and both >= both_share * n)
    runs, start = [], None
    for i, flag in enumerate(ok + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            if i - start >= _GUTTER_MIN_W:
                runs.append((i - start, xs[start] + (i - start) / 2.0))
            start = None
    runs.sort(reverse=True)
    return sorted((x, float(wd)) for wd, x in runs[:k])


def _line_gaps(ln):
    """Find a line's wide internal gaps, with at least two words on each side.

    :param _Line ln: The line.
    :returns: [(x0, x1)], widest first.
    :rtype: list[tuple]
    """
    ws = sorted(ln.words, key=lambda w: w.x)
    if len(ws) < 4:
        return []
    gaps = [(b.x - _word_end(a), _word_end(a), b.x, i) for i, (a, b) in enumerate(zip(ws, ws[1:]))]
    usual = sorted(g[0] for g in gaps)[len(gaps) // 2]
    out = [(x0, x1) for g, x0, x1, i in gaps
           if g >= max(_LOCAL_GAP_MIN, _LOCAL_GAP_RATIO * max(usual, 0.5))
           and i >= 1 and i <= len(ws) - 3]
    return sorted(out, key=lambda t: -(t[1] - t[0]))


def _local_bands(lines):
    """Read local two-column bands column by column.

    A band is consecutive lines sharing one wide gap (a boxed sidebar set in
    two columns); a line crossing the gap ends it.

    :param list[_Line] lines: Lines sorted top to bottom.
    :returns: (lines in reading order, locate(x, y) -> rank, 1 if any band
        was found else 0)
    :rtype: tuple
    """
    bands = []
    cur = None

    def _close():
        nonlocal cur
        if cur is not None:
            if cur["n_gap"] < _LOCAL_BAND_MIN:
                bands.extend({"lines": [l], "gap": None} for l in cur["lines"])
            else:
                bands.append(cur)
            cur = None

    for ln in lines:
        gaps = _line_gaps(ln)
        if cur is not None:
            gx0, gx1 = cur["gap"]
            shared = next(((max(gx0, a), min(gx1, b)) for a, b in gaps
                           if min(gx1, b) - max(gx0, a) >= _GUTTER_MIN_W), None)
            if shared is not None:
                cur["gap"] = shared
                cur["lines"].append(ln)
                cur["n_gap"] += 1
                continue
            one_side = all(_word_end(w) <= gx0 + 2 for w in ln.words) or \
                all(w.x >= gx1 - 2 for w in ln.words)
            if one_side:
                cur["lines"].append(ln)
                continue
            _close()
        if gaps:
            cur = {"lines": [ln], "gap": gaps[0], "n_gap": 1}
        else:
            bands.append({"lines": [ln], "gap": None})
    _close()

    if not any(b["gap"] for b in bands):
        return lines, (lambda x, y: (0, 0, -y)), 0
    out = []
    index = []  # (band, y_top, y_bot, gap)
    for b, band in enumerate(bands):
        ys = [l.y for l in band["lines"]]
        index.append((b, max(ys), min(ys), band["gap"]))
        if band["gap"] is None:
            for l in band["lines"]:
                l.rank = (b, 0, -l.y)
                out.append(l)
            continue
        gx = (band["gap"][0] + band["gap"][1]) / 2
        for l in band["lines"]:
            groups = {}
            for w in l.words:
                groups.setdefault(0 if w.x < gx else 1, []).append(w)
            for c, ws in sorted(groups.items()):
                piece = l if len(groups) == 1 else _Line(ws, lang=l.lang)
                piece.rank = (b, c, -l.y)
                out.append(piece)
    out.sort(key=lambda l: l.rank)

    def locate(x, y):
        for b, y_top, y_bot, gap in index:
            if y_bot - 2 <= y <= y_top + 2 or y > y_top:
                col = 0 if gap is None else (0 if x < (gap[0] + gap[1]) / 2 else 1)
                return (b, col, -y)
        return (len(index) - 1, 0, -y)
    return out, locate, 1


def _spans_gutter(ln, gutters):
    """Check whether a line runs across a column gutter.

    True for a word crossing a gutter, or an ordinary word space where it is.

    :param _Line ln: The line.
    :param list[tuple] gutters: _find_gutters() result.
    :rtype: bool
    """
    ws = sorted(ln.words, key=lambda w: w.x)
    gaps = sorted(b.x - _word_end(a) for a, b in zip(ws, ws[1:]))
    usual = gaps[len(gaps) // 2] if gaps else 0.0
    size = max((w.size for w in ws), default=8.0) or 8.0
    for g, _wd in gutters:
        if any(w.x < g < _word_end(w) for w in ws):
            return True
        left = [w for w in ws if _word_end(w) <= g]
        right = [w for w in ws if w.x >= g]
        if left and right and \
                right[0].x - _word_end(left[-1]) <= max(3.0 * usual, 1.5 * size):
            return True
    return False


def _columnize(lines, n_cols):
    """Put a page's lines in reading order: bands top to bottom, columns left to right.

    A full-width line (a heading across the columns) is its own band.
    Without page-wide gutters, falls back to _local_bands().

    :param list[_Line] lines: Lines from _lines().
    :param int|None n_cols: The layout's column count; None = unknown.
    :returns: (lines in reading order, locate(x, y) -> rank for figures,
        number of gutters found)
    :rtype: tuple
    """
    lines = sorted(lines, key=lambda ln: -ln.y)
    for ln in lines:
        ln.rank = (0, 0, -ln.y)
    found = []
    if n_cols is not None and n_cols >= 2:
        found = _find_gutters(lines, n_cols - 1, _GUTTER_BOTH)
    if not found:
        return _local_bands(lines)
    gutters = [x for x, _ in found]

    def col_of(x):
        return sum(1 for g in gutters if x >= g)

    out = []
    bands = []  # (band, y_top, y_bot, full_width)
    band = 0
    in_cols = False
    for ln in lines:
        if _spans_gutter(ln, found):
            if in_cols:
                band += 1
                in_cols = False
            ln.rank = (band, 0, -ln.y)
            out.append(ln)
            bands.append((band, ln.y, ln.y, True))
            band += 1
            continue
        in_cols = True
        groups = {}
        for w in ln.words:
            groups.setdefault(col_of(w.x), []).append(w)
        for c, ws in sorted(groups.items()):
            piece = ln if len(groups) == 1 else _Line(ws, lang=ln.lang)
            piece.rank = (band, c, -ln.y)
            out.append(piece)
        if bands and bands[-1][0] == band and not bands[-1][3]:
            bands[-1] = (band, bands[-1][1], ln.y, False)
        else:
            bands.append((band, ln.y, ln.y, False))
    out.sort(key=lambda ln: ln.rank)

    def locate(x, y):
        for b, y_top, y_bot, _full in bands:
            if y_bot - 2 <= y <= y_top + 2 or y > y_top:
                return (b, col_of(x), -y)
        return (bands[-1][0] if bands else 0, col_of(x), -y)
    return out, locate, len(gutters)


def _tokens(s):
    return set(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split())


def _similarity(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _align_cells(ws, specs):
    """Assign the line's words (consecutive, in order) to the layout's cells,
    maximizing per-cell text similarity — DP over (cell, words-consumed).
    Cells may be empty (scores 1 when the layout also read them empty).

    :param list[_Word] ws: The line's words, in order.
    :param list[str] specs: The layout's cell texts for this row.
    :returns: (cells, mean_quality) — cells is a list of word lists, one per
        spec — or (None, 0.0) if no alignment is possible.
    :rtype: tuple
    """
    m, k = len(ws), len(specs)
    if k == 0 or m == 0:
        return None, 0.0
    NEG = float("-inf")
    dp = [[NEG] * (m + 1) for _ in range(k + 1)]
    back = [[None] * (m + 1) for _ in range(k + 1)]
    dp[0][0] = 0.0
    for j in range(1, k + 1):
        spec = specs[j - 1]
        empty_score = 1.0 if not _tokens(spec) else 0.0
        for i in range(m + 1):
            if dp[j - 1][i] > NEG and dp[j - 1][i] + empty_score > dp[j][i]:
                dp[j][i], back[j][i] = dp[j - 1][i] + empty_score, i
            for t in range(i):
                if dp[j - 1][t] == NEG:
                    continue
                s = dp[j - 1][t] + _similarity(
                    " ".join(w.text for w in ws[t:i]), spec)
                if s > dp[j][i]:
                    dp[j][i], back[j][i] = s, t
    if dp[k][m] == NEG:
        return None, 0.0
    cells, i = [], m
    for j in range(k, 0, -1):
        t = back[j][i]
        cells.append(ws[t:i])
        i = t
    cells.reverse()
    return cells, dp[k][m] / k


def _level_of(block):
    """Return a heading block's level, clamped to 0-6.

    :param dict block: Layout block.
    :rtype: int
    """
    try:
        return max(0, min(6, int(block.get("level") or 0)))
    except (TypeError, ValueError):
        return 0


def _lang_of(block):
    """Return a block's BCP-47 'lang', or '' when absent or malformed.

    :param dict block: Layout block.
    :rtype: str
    """
    lang = block.get("lang")
    return lang.strip() if isinstance(lang, str) and _LANG_RE.match(lang.strip()) else ""


def _is_ordered(block):
    """Decide whether a list block is ordered.

    The layout's flag, or labels like "a." / "2-1." / "iv)" on most items.

    :param dict block: Layout list block.
    :rtype: bool
    """
    if block.get("ordered"):
        return True
    items = [str(i).split(None, 1)[0] for i in (block.get("items") or []) if str(i).strip()]
    labelled = sum(1 for t in items if _ORDERED_LABEL_RE.match(t))
    return bool(items) and labelled >= max(2, len(items) - 1) or (len(items) == 1 and labelled == 1)


def _list_label(item_words, ordered):
    """Split a list item's leading label ('1.', 'a)', or a bullet) from its body.

    :param list[_Word] item_words: The item's words.
    :param bool ordered: Whether the list is ordered.
    :returns: (label_words, body_words)
    :rtype: tuple
    """
    pat = _ORDERED_LABEL_RE if ordered else _BULLET_RE
    if item_words and pat.match(item_words[0].text):
        return item_words[:1], item_words[1:]
    return [], item_words


def _list_numbering(group):
    """Pick the /ListNumbering attribute from the first item's label.

    :param _ListGroup group: The list.
    :rtype: pikepdf.Name
    """
    first = next((it.words[0].text for it in group.items if it.words), "")
    if not group.ordered:
        return Name.Disc if _BULLET_RE.match(first) else Name("/None")
    if not _ORDERED_LABEL_RE.match(first):
        return Name.Decimal
    core = first.strip("().]:")
    if re.fullmatch(r"\d+([.\-–]\d+)?", core):
        return Name.Decimal
    if re.fullmatch(r"[ivxlc]+", core) and len(core) > 1 or core == "i":
        return Name.LowerRoman
    if re.fullmatch(r"[IVXLC]+", core) and len(core) > 1 or core == "I":
        return Name.UpperRoman
    if core.isalpha() and len(core) == 1:
        return Name.LowerAlpha if core.islower() else Name.UpperAlpha
    return Name.Disc


def _layout_specs(blocks):
    """Flatten layout blocks into matchable specs, in block order.

    :param list[dict] blocks: Layout blocks.
    :returns: [(block_index, kind, payload)]; kind is row/heading/text/item.
    :rtype: list[tuple]
    """
    specs = []
    for bi, b in enumerate(blocks):
        t = b.get("type")
        if t == "table" and isinstance(b.get("rows"), list):
            header = b.get("header") or []
            if isinstance(header, list) and header:
                specs.append((bi, "row", (True, [str(c) for c in header])))
            for r in b["rows"]:
                if isinstance(r, list) and r:
                    specs.append((bi, "row", (False, [str(c) for c in r])))
        elif t == "heading" and b.get("text"):
            if len(str(b["text"]).split()) > _HEADING_MAX_WORDS:  # mislabelled paragraph
                specs.append((bi, "text", (str(b["text"]), _lang_of(b))))
            else:
                specs.append((bi, t, (str(b["text"]), _level_of(b), _lang_of(b))))
        elif t == "text" and b.get("text"):
            specs.append((bi, t, (str(b["text"]), _lang_of(b))))
        elif t == "list" and isinstance(b.get("items"), list):
            for ii, item in enumerate(b["items"]):
                if item:
                    specs.append((bi, "item", (ii, str(item), _lang_of(b))))
    return specs


def _page_blob(blocks):
    """Squash everything the layout read into one alphanumeric string.

    :param list[dict] blocks: Layout blocks.
    :rtype: str
    """
    parts = []
    for b in blocks:
        parts.append(str(b.get("text") or ""))
        parts.extend(str(c) for c in (b.get("items") or []))
        parts.extend(str(c) for c in (b.get("header") or []))
        for r in (b.get("rows") or []):
            if isinstance(r, list):
                parts.extend(str(c) for c in r)
    return _squash(" ".join(parts))


def _squash(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _assign_lines(lines, specs, opts={}):
    """Match each layout spec to the line(s) it covers, by fuzzy text match.

    Matching moves a cursor down the page, so one bad block can't drag the
    rest out of order; 'behind' also lets multi-column pages match earlier lines.

    :param list[_Line] lines: Lines in reading order.
    :param list[tuple] specs: _layout_specs() result.
    :param dict opts: 'lookahead' (int), 'behind' (bool).
    :returns: {line index: (kind, ...)}
    :rtype: dict
    """
    lookahead = opts.get("lookahead", DEFAULT_LOOKAHEAD)
    behind = opts.get("behind", False)
    assigned = {}
    li = 0

    def _window(li, allow_behind=False):
        fwd = range(max(0, li - _BACKTRACK), min(li + lookahead, len(lines)))
        yield from (j for j in fwd if j not in assigned)
        if behind and allow_behind:
            yield from (j for j in range(0, fwd.start) if j not in assigned)

    def _strong(block_tokens, line):
        lt = _tokens(line.text)
        return bool(lt) and len(lt & block_tokens) / len(lt) >= 0.6 and len(lt & block_tokens) >= 3

    for bi, kind, payload in specs:
        if kind == "row":
            is_header, cells = payload
            row_text = " ".join(cells)
            best = next((j for j in _window(li)
                         if _similarity(row_text, lines[j].text) >= _MATCH_MIN), None)
            if best is None:
                continue
            split, quality = _align_cells(lines[best].words, cells)
            li = best + 1
            if split is None or quality < _CELL_MIN:
                assigned[best] = ("demoted",)  # stays /P even in strict
                continue
            assigned[best] = ("row", bi, is_header, split)
        elif kind == "heading":
            text, level, lang = payload
            h_tokens = _tokens(text)

            def _in_heading(line):
                lt = _tokens(line.text)
                return len(lt & h_tokens) / len(lt) if lt else 0.0

            first_tok = next(iter(_tokens(text.split(None, 1)[0])), None) if text.strip() else None

            def _starts_heading(line):
                lt = _tokens(line.text)
                if not lt or _in_heading(line) < 0.6:
                    return False
                if _similarity(text, line.text) >= _MATCH_MIN:
                    return True
                return (first_tok in lt) and (len(lt & h_tokens) >= 2 or len(h_tokens) == 1)

            best = next((j for j in _window(li, allow_behind=True)
                         if _starts_heading(lines[j])
                         and (j >= max(0, li - _BACKTRACK)
                              or _similarity(text, lines[j].text) >= 0.6)), None)
            if best is None:
                continue
            assigned[best] = ("heading", bi, level, lang, text)
            covered = _tokens(lines[best].text) & h_tokens
            j, skipped = best + 1, 0  # follow a wrapped heading
            while j < len(lines) and len(covered) < len(h_tokens) and j not in assigned:
                lt = _tokens(lines[j].text)
                if _in_heading(lines[j]) >= 0.6 and (lt & h_tokens) - covered:
                    assigned[j] = ("heading", bi, level, lang, text)
                    covered |= lt
                    skipped = 0
                elif skipped < _HEAD_SKIP:  # may straddle body lines
                    skipped += 1
                else:
                    break
                j += 1
            li = best + 1
        else:  # text block or list item, possibly multi-line
            if kind == "item":
                ii, text, lang = payload
                tag = ("item", bi, ii, lang)
            else:
                text, lang = payload
                tag = ("text", bi, lang)
            block_tokens = _tokens(text)
            for j in _window(li, allow_behind=True):
                lt = _tokens(lines[j].text)
                if j < max(0, li - _BACKTRACK) and not _strong(block_tokens, lines[j]):
                    continue
                if lt and block_tokens and len(lt & block_tokens) / len(lt) >= 0.5:
                    while j < len(lines) and j not in assigned:
                        lt = _tokens(lines[j].text)
                        if lt and len(lt & block_tokens) / len(lt) >= 0.5:
                            assigned[j] = tag
                            j += 1
                        else:
                            break
                    li = j
                    break
    return assigned


def _match_layout(lines, blocks, opts={}):
    """Merge layout blocks with the page's lines into ordered elements.

    Elements are _Line (/P), _Para, _Heading, _ListGroup, _ArtifactLine or
    _TableGroup; anything that doesn't align confidently stays a paragraph.

    :param list[_Line] lines: Lines in reading order (_columnize()).
    :param list[dict] blocks: The page's layout blocks.
    :param dict opts: 'strict' (bool) — unmatched lines the layout never
        read become artifacts; 'lookahead' (int); 'paragraphs' (bool) — one
        _Para per text block; 'behind' (bool) — see _assign_lines().
    :returns: Elements in line order.
    :rtype: list
    """
    strict = opts.get("strict", False)
    paragraphs = opts.get("paragraphs", False)
    assigned = _assign_lines(lines, _layout_specs(blocks), opts)
    page_blob = _page_blob(blocks)

    # one element per block, in line order
    elements, current = [], None  # current = open _TableGroup
    paras, lists, heads = {}, {}, {}  # block index -> element
    for idx, line in enumerate(lines):
        a = assigned.get(idx)
        if a and a[0] == "item":
            _, bi, ii, lang = a
            lst = lists.get(bi)
            if lst is None:
                lst = lists[bi] = _ListGroup(bi, ordered=_is_ordered(blocks[bi]))
                elements.append(lst)
            if not lst.items or lst.items[-1].index != ii:
                lst.items.append(_ListItem(ii, lang=lang))
            lst.items[-1].lines.append(line)
            continue
        if paragraphs and a and a[0] == "text":
            para = paras.get(a[1])
            if para is None:
                para = paras[a[1]] = _Para([], lang=a[2], block_index=a[1])
                elements.append(para)
            para.lines.append(line)
            continue
        if a and a[0] == "text":
            line.lang = a[2]
        if a and a[0] == "row":
            _, bi, is_header, split = a
            if current is None or current.block_index != bi:
                current = _TableGroup(
                    block_index=bi,
                    row_header=bool(blocks[bi].get("row_header")))
                elements.append(current)
            current.rows.append(_Row(line, split, header=is_header))
        elif a and a[0] == "heading":
            _, bi, level, lang, text = a
            if bi + 1 < len(blocks) and blocks[bi + 1].get("type") == "table":
                elements.append(_CaptionMark(line, bi + 1))
            elif bi in heads:
                heads[bi].lines.append(line)
            else:
                heads[bi] = _Heading([line], level, lang, text, block_index=bi)
                elements.append(heads[bi])
        elif a:  # "text" or "demoted": real content, plain paragraph
            elements.append(line)
        else:
            # junk unless the layout read it
            sq = _squash(line.text)
            keep = bool(sq) and (sq in page_blob or any(
                len(w_sq) >= 3 and w_sq in page_blob
                for w_sq in (_squash(w.text) for w in line.words)))
            if strict and not keep:
                elements.append(_ArtifactLine(line))
            else:
                elements.append(line)

    # PDF/UA 7.2: equal row widths
    out = []
    for e in elements:
        if not isinstance(e, _TableGroup):
            out.append(e)
            continue
        widths = {}
        for r in e.rows:
            widths[len(r.cells)] = widths.get(len(r.cells), 0) + 1
        n = max(widths, key=lambda k: widths[k])
        kept = [r for r in e.rows if len(r.cells) == n]
        demoted = [r.line for r in e.rows if len(r.cells) != n]
        if len(kept) < 2:
            out.extend(r.line for r in e.rows)
            continue
        e.rows = kept
        out.append(e)
        out.extend(demoted)

    # attach captions; orphans become headings
    final = []
    for i, e in enumerate(out):
        if isinstance(e, _CaptionMark):
            target = next((t for t in out[i + 1:] if isinstance(t, _TableGroup)
                           and t.block_index == e.table_block), None)
            if target is not None and target.caption is None:
                target.caption = e.line
            else:
                final.append(_Heading([e.line]))
            continue
        final.append(e)
    return final


def _page_figures(box, specs, words):
    """Turn figure specs (page fractions, origin top-left) into _Figure regions.

    A run of words joins a figure (axis labels, legend) only when most of it
    sits inside the box shrunk by _FIG_INSET; text beside a figure stays in the flow.

    :param list[float] box: The page box the fractions refer to, in points.
    :param list[dict] specs: [{'bbox', 'alt', 'caption'?, 'kind'?}] or None.
    :param list[_Word] words: The page's words.
    :rtype: list[_Figure]
    """
    if not specs:
        return []
    px0, py0, px1, py1 = box
    W, H = px1 - px0, py1 - py0
    figs = []
    for f in specs:
        fx0, fy0, fx1, fy1 = f["bbox"]
        bb = [px0 + fx0 * W, py0 + (1 - fy1) * H, px0 + fx1 * W, py0 + (1 - fy0) * H]
        figs.append(_Figure(bb, str(f.get("alt") or ""), str(f.get("caption") or ""), str(f.get("kind") or "")))
    dx, dy = _FIG_INSET * W, _FIG_INSET * H
    for line in _lines(words):
        for run in _runs(line.words):
            for fg in figs:
                x0, y0, x1, y1 = fg.bbox
                inside = [w for w in run if x0 + dx <= w.x <= x1 - dx and y0 + dy <= w.y <= y1 - dy]
                if len(inside) >= _FIG_LINE_SHARE * len(run):
                    fg.words.extend(run)
                    break
    return figs


def _element_lines(e):
    """The lines an element is made of, for caption claiming.

    :rtype: list[_Line]
    """
    if isinstance(e, _Line):
        return [e]
    if isinstance(e, (_Heading, _Para)):
        return list(e.lines)
    if isinstance(e, _ListGroup):
        return [ln for it in e.items for ln in it.lines]
    return []


def _claim_captions(figs, elements, page_h):
    """Give each figure the lines that read as its caption, as its /Caption.

    Candidates are lines near the box whose words mostly come from the caption
    text, extended over following lines while they keep matching (captions wrap).

    :param list[_Figure] figs: The page's figures.
    :param list elements: Elements; claimed lines are removed.
    :param float page_h: Page height in points.
    :returns: The elements, with emptied ones left for the caller to drop.
    :rtype: list
    """
    for fg in figs:
        cap_tokens = _tokens(fg.caption)
        if len(cap_tokens) < 2:
            continue

        def _fits(line):
            lt = _tokens(line.text)
            return bool(lt) and len(lt & cap_tokens) / len(lt) >= 0.6

        cands = [(i, ln) for i, e in enumerate(elements) for ln in _element_lines(e)
                 if min(abs(ln.y - fg.bbox[3]), abs(ln.y - fg.bbox[1])) <= 0.2 * page_h]
        cands.sort(key=lambda t: -t[1].y)
        start = next((k for k, (i, ln) in enumerate(cands)
                      if _fits(ln) and ("figure" in _tokens(ln.text)
                                        or len(_tokens(ln.text) & cap_tokens) >= 3)), None)
        if start is None:
            continue
        claimed, covered = [], set()
        for i, ln in cands[start:]:
            if not _fits(ln) or len(covered) >= len(cap_tokens):
                break
            claimed.append((i, ln))
            covered |= _tokens(ln.text)
        words = [w for _, ln in claimed for w in ln.words]
        for i, ln in sorted(claimed, key=lambda t: -t[0]):
            e = elements[i]
            if isinstance(e, (_Para, _Heading)):
                # remove lines singly, not the element
                if ln in e.lines:
                    e.lines.remove(ln)
            elif isinstance(e, _ListGroup):
                for it in e.items:
                    if ln in it.lines:
                        it.lines.remove(ln)
                e.items = [it for it in e.items if it.lines]
            elif i < len(elements):
                elements.pop(i)
        fg.caption_line = _Line(words)
    return elements


def _headings_first(elements):
    """Move each heading to just before the element of the block it introduces.

    A margin heading's baseline often sits below that block's first line.

    :param list elements: Elements in geometric order.
    :rtype: list
    """
    def _bi(e):
        return getattr(e, "block_index", -1)

    out = list(elements)
    for h in [e for e in elements if isinstance(e, _Heading) and e.block_index >= 0]:
        nxt = next((e for e in out if _bi(e) == h.block_index + 1), None)
        if nxt is None:
            continue
        hi, ni = out.index(h), out.index(nxt)
        if ni < hi:
            out.pop(hi)
            out.insert(ni, h)
    return out


def _element_rank(e, locate):
    """Return an element's reading-order key from its first line.

    :param e: Any _match_layout() element, or a _Figure.
    :param locate: _columnize()'s locate(x, y), for figures.
    :rtype: tuple
    """
    if isinstance(e, _Figure):
        return locate((e.bbox[0] + e.bbox[2]) / 2, e.bbox[3])
    if isinstance(e, _TableGroup):
        lines = ([e.caption] if e.caption else []) + [r.line for r in e.rows]
    elif isinstance(e, (_Para, _Heading)):
        lines = e.lines
    elif isinstance(e, _ListGroup):
        lines = [ln for it in e.items for ln in it.lines]
    elif isinstance(e, _ArtifactLine):
        lines = [e.line]
    else:
        lines = [e]
    return min(ln.rank for ln in lines)


def _build_elements(words, layout, opts={}):
    """Turn one page's words, layout and figures into elements in reading order.

    Reading order is always page geometry, so no matcher quirk can reorder
    content; stray lines inside a table's span become full-width rows.

    :param list[_Word] words: The page's words.
    :param dict|None layout: The page's layout (blocks, columns).
    :param dict opts: _match_layout() opts, plus 'figures' (specs for
        _page_figures()) and 'page_box' (points; needed with figures).
    :returns: (elements in reading order, the page's _Figure list)
    :rtype: tuple
    """
    page_figs = _page_figures(opts.get("page_box"), opts.get("figures"), words)
    fig_words = {id(w) for fg in page_figs for w in fg.words}
    lines, locate, n_gutters = _columnize(
        _lines([w for w in words if id(w) not in fig_words]), layout.get("columns") if layout else None)
    if layout and layout.get("blocks"):
        elements = _match_layout(lines, layout["blocks"], {**opts, "behind": n_gutters > 0})
    else:
        elements = lines
    if page_figs:
        box = opts["page_box"]
        elements = _claim_captions(page_figs, list(elements), box[3] - box[1])
        elements = [e for e in elements if not (isinstance(e, (_Para, _Heading)) and not e.lines)]
        elements = elements + page_figs
    elements = _headings_first(sorted(elements, key=lambda e: _element_rank(e, locate)))

    for g in [e for e in elements if isinstance(e, _TableGroup)]:
        ys = [w.y for r in g.rows for w in r.line.words]
        y_hi, y_lo = max(ys), min(ys)
        for i, e in enumerate(elements):
            if isinstance(e, _Line) and y_lo < e.y < y_hi:
                g.rows.append(_Row(e, cells=[e.words], raw=True))
                elements[i] = None
        g.rows.sort(key=lambda r: -max(w.y for w in r.line.words))
    elements = [e for e in elements if e is not None]

    for e in elements:
        if isinstance(e, _ArtifactLine):
            for w in e.line.words:
                w.artifact = True
    return elements, page_figs


def _part_lang(block_lang, doc_lang):
    """Return a /Lang entry only when a block's language differs from the document's.

    :param str block_lang: The block's BCP-47 language, or ''.
    :param str doc_lang: The document's primary language subtag.
    :rtype: dict
    """
    if block_lang and block_lang.split("-")[0].lower() != doc_lang:
        return {"Lang": pikepdf.String(block_lang)}
    return {}


def _emit_elements(ctx, page, page_no, elements, first=None):
    """Append one page's elements to the structure tree.

    :param dict ctx: Shared state: pdf, doc_elem, stats, doc_lang,
        prev_level (last heading level), bookmarks.
    :param pikepdf.Page page: The page.
    :param int page_no: 0-based page index.
    :param list elements: _build_elements() result.
    :param first: Whole-page /Figure element to list first, if any.
    :returns: {_mc_key(stm, mcid): StructElem} for the parent tree.
    :rtype: dict
    """
    pdf, doc_elem, stats = ctx["pdf"], ctx["doc_elem"], ctx["stats"]
    word_parent = {}

    def make_elem(s, parent, line_words, **extra):
        el = Dictionary(Type=Name.StructElem, S=s, P=parent, Pg=page.obj, **extra)
        if line_words:
            el.K = _kids(line_words, page.obj)
        el = pdf.make_indirect(el)
        for w in line_words:
            word_parent[_mc_key(w.stm, w.mcid)] = el
        return el

    def lang_of(block_lang):
        return _part_lang(block_lang, ctx["doc_lang"])

    if first is not None:
        doc_elem.K.append(first)
    for e in elements:
        if isinstance(e, _ArtifactLine):
            continue
        if isinstance(e, _Line):
            doc_elem.K.append(make_elem(Name.P, doc_elem, e.words, **lang_of(e.lang)))
            stats["lines"] += 1
        elif isinstance(e, _Para):
            doc_elem.K.append(make_elem(Name.P, doc_elem, e.words, **lang_of(e.lang)))
            stats["lines"] += len(e.lines)
            stats["paragraphs"] += 1
        elif isinstance(e, _ListGroup):
            if not e.items:
                continue
            lst = pdf.make_indirect(Dictionary(
                Type=Name.StructElem, S=Name.L, P=doc_elem, Pg=page.obj,
                K=pikepdf.Array(),
                A=pdf.make_indirect(Dictionary(O=Name.List, ListNumbering=_list_numbering(e)))))
            for it in e.items:
                if not it.words:
                    continue
                li_el = pdf.make_indirect(Dictionary(
                    Type=Name.StructElem, S=Name.LI, P=lst, Pg=page.obj,
                    K=pikepdf.Array(), **lang_of(it.lang)))
                label, body = _list_label(it.words, e.ordered)
                if label:
                    li_el.K.append(make_elem(Name.Lbl, li_el, label))
                if body:
                    li_el.K.append(make_elem(Name.LBody, li_el, body))
                lst.K.append(li_el)
                stats["lines"] += len(it.lines)
            doc_elem.K.append(lst)
            stats["lists"] += 1
        elif isinstance(e, _Figure):
            if not e.content:
                continue
            x0, y0, x1, y1 = e.bbox
            fig = pdf.make_indirect(Dictionary(
                Type=Name.StructElem, S=Name.Figure, P=doc_elem, Pg=page.obj,
                Alt=pikepdf.String(e.alt), K=pikepdf.Array(),
                A=pdf.make_indirect(Dictionary(
                    O=Name.Layout, Placement=Name.Block,
                    BBox=pikepdf.Array([round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)])))))
            if e.caption_line is not None:
                fig.K.append(make_elem(Name.Caption, fig, e.caption_line.words))
                stats["captions"] += 1
            for stm, m in e.content:
                fig.K.append(_kid_ref(stm, m, page.obj))
                word_parent[_mc_key(stm, m)] = fig
            for w in e.words:  # labels inside, read in context
                if not w.artifact and _mc_key(w.stm, w.mcid) not in word_parent:
                    fig.K.append(_kid_ref(w.stm, w.mcid, page.obj))
                    word_parent[_mc_key(w.stm, w.mcid)] = fig
            doc_elem.K.append(fig)
            stats["figures"] += 1
        elif isinstance(e, _Heading):
            # clamp so levels never skip
            prev_level = ctx["prev_level"]
            want = e.level if e.level > 0 else (1 if prev_level == 0 else 2)
            level = max(1, min(want, prev_level + 1, 6))
            ctx["prev_level"] = level
            doc_elem.K.append(make_elem(Name("/H%d" % level), doc_elem, e.words, **lang_of(e.lang)))
            stats["headings"] += 1
            stats["lines"] += len(e.lines)
            ctx["bookmarks"].append((level, e.text, page_no, max(w.y for w in e.words)))
        else:  # _TableGroup
            table = pdf.make_indirect(Dictionary(
                Type=Name.StructElem, S=Name.Table, P=doc_elem,
                Pg=page.obj, K=pikepdf.Array()))
            if e.caption is not None:
                table.K.append(make_elem(Name.Caption, table, e.caption.words))
                stats["captions"] += 1
            head_rows = [r for r in e.rows if r.header]
            body_rows = [r for r in e.rows if not r.header]
            width = max((len(r.cells) for r in e.rows if not r.raw), default=1)

            def emit_rows(rows, parent, row_header=False):
                for row in rows:
                    tr = pdf.make_indirect(Dictionary(
                        Type=Name.StructElem, S=Name.TR, P=parent,
                        Pg=page.obj, K=pikepdf.Array()))
                    for ci, cell_words in enumerate(row.cells):
                        # body row's leading label cell
                        stub = row_header and ci == 0 and not (row.header or row.raw)
                        s = Name.TH if (row.header or stub) else Name.TD
                        cell = Dictionary(Type=Name.StructElem, S=s, P=tr, Pg=page.obj)
                        if cell_words:
                            cell.K = _kids(cell_words, page.obj)
                        if row.header:
                            cell.A = _table_attrs(pdf, "Column")
                        elif stub:
                            cell.A = _table_attrs(pdf, "Row")
                        elif row.raw and width > 1:  # spans every column
                            cell.A = pdf.make_indirect(Dictionary(O=Name.Table, ColSpan=width))
                        cell = pdf.make_indirect(cell)
                        tr.K.append(cell)
                        for w in cell_words:
                            word_parent[_mc_key(w.stm, w.mcid)] = cell
                    parent.K.append(tr)

            if head_rows and body_rows:
                thead = pdf.make_indirect(Dictionary(
                    Type=Name.StructElem, S=Name.THead, P=table,
                    Pg=page.obj, K=pikepdf.Array()))
                tbody = pdf.make_indirect(Dictionary(
                    Type=Name.StructElem, S=Name.TBody, P=table,
                    Pg=page.obj, K=pikepdf.Array()))
                emit_rows(head_rows, thead)
                emit_rows(body_rows, tbody, e.row_header)
                table.K.append(thead)
                table.K.append(tbody)
            else:
                emit_rows(e.rows, table, e.row_header)
            doc_elem.K.append(table)
            stats["tables"] += 1
    return word_parent


def _write_outline(pdf, bookmarks):
    """Write the document outline from the headings, nested by level.

    Each destination is the heading's baseline plus a little headroom.

    :param pikepdf.Pdf pdf: The document.
    :param list[tuple] bookmarks: (level, title, page_no, top_y) in order.
    :returns: Number of bookmarks written.
    :rtype: int
    """
    n = 0
    with pdf.open_outline() as outline:
        stack = []  # (level, OutlineItem)
        for level, title, page_no, top_y in bookmarks:
            item = pikepdf.OutlineItem(title[:200], page_no, "XYZ",
                                       left=0, top=top_y + 14, zoom=0)
            while stack and stack[-1][0] >= level:
                stack.pop()
            (stack[-1][1].children if stack else outline.root).append(item)
            stack.append((level, item))
            n += 1
    return n


def page_box(page_obj):
    """The page's crop box (else media box), normalized, in points.

    :rtype: list[float]
    """
    box = page_obj.get("/CropBox") or page_obj.get("/MediaBox") or [0, 0, 612, 792]
    x0, y0, x1, y1 = (float(v) for v in box)
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def _table_attrs(pdf, scope):
    return pdf.make_indirect(Dictionary(O=Name.Table, Scope=Name("/" + scope)))


def _kid_ref(stm, mcid, page_obj):
    """K entry for a marked-content sequence: a bare MCID, or an MCR in a form.

    :param stm: Form XObject holding it, or None for page content.
    :param int mcid: Its MCID.
    :param page_obj: The page dictionary.
    """
    if stm is None:
        return mcid
    return Dictionary(Type=Name.MCR, Pg=page_obj, Stm=stm, MCID=mcid)


def _mc_key(stm, mcid):
    """Hashable key for a marked-content sequence (pikepdf objects aren't).

    :rtype: tuple
    """
    return (None if stm is None else tuple(stm.objgen), mcid)


def _kids(words, page_obj):
    """K value for an element's words, one entry per marked-content sequence.

    :param list[_Word] words: The element's words.
    :param page_obj: The page dictionary.
    """
    refs, seen = [], set()
    for w in words:
        key = _mc_key(w.stm, w.mcid)
        if key not in seen:
            seen.add(key)
            refs.append(_kid_ref(w.stm, w.mcid, page_obj))
    return refs[0] if len(refs) == 1 else pikepdf.Array(refs)


def tag_scan_pdf(src_path, dest_path, title, opts={}):
    """Add a PDF/UA-1 structure tree to a pdf_utils-built searchable PDF.

    :param str src_path: Source searchable PDF (build_searchable_pdf output).
    :param str dest_path: Tagged output PDF path.
    :param str title: Document title (dc:title, /Title, DisplayDocTitle).
    :param dict opts:
        'lang' (str, default 'en-US') — document language;
        'alt' (str or list[str]) — scan description, one for every page or
            per page; pages without one keep the scan as an /Artifact;
        'layouts' (list) — per-page layout.page_layout() results, or None;
        'strict' (bool) — demote lines the layout doesn't account for to
            untagged artifacts;
        'lookahead' (int, default 40) — how far a layout block may match
            past the matcher's cursor;
        'paragraphs' (bool) — one /P per layout text block instead of per line;
        'figures' (list) — per-page [{'bbox' (page fractions, origin top-left),
            'alt', 'caption'?, 'kind'?}], see figures.for_tagger(); a page with
            figures ignores 'alt' (the rest of the scan is background).
    :returns: Stats: pages, words, lines, paragraphs, tables, headings,
        captions, artifacts, lists, bookmarks, figures.
    :rtype: dict
    """
    lang = opts.get("lang", "en-US")
    alt = opts.get("alt")
    layouts = opts.get("layouts")
    figures = opts.get("figures")
    match_opts = {
        "strict": bool(opts.get("strict", False)),
        "lookahead": opts.get("lookahead", DEFAULT_LOOKAHEAD),
        "paragraphs": bool(opts.get("paragraphs", False)),
    }

    pdf = pikepdf.open(src_path)
    root = pdf.Root

    doc_elem = pdf.make_indirect(Dictionary(
        Type=Name.StructElem, S=Name.Document, K=pikepdf.Array()))
    struct_root = pdf.make_indirect(Dictionary(
        Type=Name.StructTreeRoot, K=doc_elem, ParentTreeNextKey=len(pdf.pages)))
    doc_elem.P = struct_root
    parent_tree_nums = pikepdf.Array()

    stats = {"pages": len(pdf.pages), "words": 0, "lines": 0, "paragraphs": 0,
             "tables": 0, "headings": 0, "captions": 0, "artifacts": 0,
             "lists": 0, "bookmarks": 0, "figures": 0}
    ctx = {"pdf": pdf, "doc_elem": doc_elem, "stats": stats, "prev_level": 0,
           "bookmarks": [], "doc_lang": lang.split("-")[0].lower()}
    for page_no, page in enumerate(pdf.pages):
        contents = page.obj.get("/Contents")
        if contents is None:
            continue
        stream_objs = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
        streams = [s.read_bytes() for s in stream_objs]
        imgs, words = _classify_streams(streams, _page_cmaps(page))
        layout = layouts[page_no] if layouts and page_no < len(layouts) else None
        specs = figures[page_no] if figures and page_no < len(figures) else None
        elements, page_figs = _build_elements(words, layout, {
            **match_opts, "figures": specs, "page_box": page_box(page.obj)})

        # page-local MCIDs: figure first, then words
        mcid = 0
        fig_elem = None
        img_open, img_close = b"/Artifact BMC\n", b"EMC\n"
        if isinstance(alt, (list, tuple)):
            page_alt = alt[page_no] if page_no < len(alt) else None
        else:
            page_alt = alt
        if page_alt and imgs and not page_figs:
            fig_elem = pdf.make_indirect(Dictionary(
                Type=Name.StructElem, S=Name.Figure, P=doc_elem,
                Pg=page.obj, K=mcid, Alt=pikepdf.String(page_alt)))
            img_open = b"/Figure <</MCID %d>> BDC\n" % mcid
            mcid += 1
        fig_redraws = b""
        if page_figs and imgs:
            scan = streams[imgs[0]]
            for fg in page_figs:
                fg.mcid = mcid
                fg.content = [(None, mcid)]
                mcid += 1
                x0, y0, x1, y1 = fg.bbox
                # same pixels, clipped, as Figure content
                fig_redraws += (b"/Figure <</MCID %d>> BDC\nq %.2f %.2f %.2f %.2f re W n\n"
                                % (fg.mcid, x0, y0, x1 - x0, y1 - y0) + scan + b"\nQ\nEMC\n")
        for w in words:
            if not w.artifact:
                w.mcid = mcid
                mcid += 1

        parts = []
        for i, data in enumerate(streams):
            if i in imgs:
                parts += [img_open, data, b"\n", img_close]
                if i == imgs[0]:
                    parts.append(fig_redraws)
                continue
            w = next((w for w in words if w.stream_index == i), None)
            if w is None:
                parts += [data, b"\n"]
            elif w.artifact:
                parts += [b"/Artifact BMC\n", data, b"\nEMC\n"]
                stats["artifacts"] += 1
            else:
                parts += [b"/P <</MCID %d>> BDC\n" % w.mcid, data, b"\nEMC\n"]
        page.obj.Contents = pdf.make_stream(b"".join(parts))

        word_parent = _emit_elements(ctx, page, page_no, elements, first=fig_elem)
        page_parents = pikepdf.Array()
        for m in range(mcid):
            page_parents.append(fig_elem if fig_elem is not None and m == 0 else word_parent[(None, m)])
        page.obj.StructParents = page_no
        page.obj.Tabs = Name.S
        parent_tree_nums.append(page_no)
        parent_tree_nums.append(pdf.make_indirect(page_parents))
        stats["words"] += len(words)

    struct_root.ParentTree = pdf.make_indirect(Dictionary(Nums=parent_tree_nums))
    if ctx["bookmarks"]:
        stats["bookmarks"] = _write_outline(pdf, ctx["bookmarks"])
    root.StructTreeRoot = struct_root
    root.MarkInfo = Dictionary(Marked=True)
    root.Lang = pikepdf.String(lang)
    root.ViewerPreferences = Dictionary(DisplayDocTitle=True)

    with pdf.open_metadata() as meta:
        meta["dc:title"] = title
        # PDF/UA identification (ISO 14289-1 clause 5)
        meta.register_xml_namespace("http://www.aiim.org/pdfua/ns/id/", "pdfuaid")
        meta["pdfuaid:part"] = "1"
    if pdf.docinfo is None:
        pdf.docinfo = Dictionary()
    pdf.docinfo["/Title"] = pikepdf.String(title)

    pdf.save(dest_path)
    pdf.close()
    return stats


def validate_ua(pdf_path, opts={}):
    """Validate a tagged PDF against PDF/UA-1 with veraPDF.

    veraPDF exits 0 for compliant and 1 for non-compliant — both produce a
    report; any other exit means the tool itself failed.

    :param str pdf_path: PDF to validate.
    :param dict opts: 'verapdf' (binary path/name, default 'verapdf' on PATH),
        'flavour' (veraPDF flavour, default 'ua1').
    :returns: Dict with 'compliant' (bool), 'passed_rules' (int), 'failed_rules' (int).
    :rtype: dict
    :raises RuntimeError: If veraPDF is not installed or fails to produce a report.
    """
    binary = opts.get("verapdf", "verapdf")
    flavour = opts.get("flavour", "ua1")
    try:
        proc = subprocess.run(
            [binary, "--flavour", flavour, pdf_path], capture_output=True
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"veraPDF not found ({binary!r}) — install it or pass opts['verapdf']"
        ) from e
    report = proc.stdout.decode("utf-8", errors="replace")
    m = re.search(r'isCompliant="(true|false)"', report)
    if proc.returncode not in (0, 1) or m is None:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"veraPDF failed ({proc.returncode}) on {pdf_path}: {stderr[:500]}"
        )
    passed = re.search(r'passedRules="(\d+)"', report)
    failed = re.search(r'failedRules="(\d+)"', report)
    return {
        "compliant": m.group(1) == "true",
        "passed_rules": int(passed.group(1)) if passed else 0,
        "failed_rules": int(failed.group(1)) if failed else 0,
    }
