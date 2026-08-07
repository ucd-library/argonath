"""
PDF/UA-1 accessibility structure tagging for digtk-built searchable PDFs
(digtk.lib.pdf_utils.build_searchable_pdf output).

The page scan becomes an /Artifact (standard for OCR'd documents: the text
layer carries the semantics) or, when alt text is supplied, a /Figure with
that description; every invisible OCR word gets a marked-content ID.
Baseline structure is per-line /P elements in reading order. When a vision
layout is supplied (opts['layouts'] — a per-page list of {'blocks': [...]}
dicts, each block {'type': 'table'|'heading'|'text', ...}):

- data tables become /Table -> /THead|/TBody -> /TR -> /TH|/TD (TH
  Scope=Column; with 'row_header': true on the block, each body row's
  leading label cell becomes a /TH Scope=Row so readers announce it as
  context for the row),
- a heading immediately preceding its table becomes the table's /Caption
  (screen readers announce it on entering the table),
- other headings become /H1 (first) then /H2,
- with opts['strict'], lines the layout doesn't account for (rotated
  scanner junk, mirror text) are demoted to untagged artifacts instead of /P.

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
_LINE_TOL = 3.0   # points: words within this y-distance share a line
_MATCH_MIN = 0.5  # token-overlap floor for row/heading<->line matching
_CELL_MIN = 0.6   # per-row mean cell-alignment floor (below -> row stays a /P)
DEFAULT_LOOKAHEAD = 40  # max lines past the cursor a layout block may match


@dataclass
class _Word:
    stream_index: int
    x: float
    y: float
    text: str
    mcid: int = -1
    artifact: bool = False


@dataclass
class _Line:
    words: List[_Word]

    @property
    def y(self):
        return self.words[0].y

    @property
    def text(self):
        return " ".join(w.text for w in self.words)


@dataclass
class _Heading:
    line: _Line


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


def _classify_streams(streams):
    """(image_stream_indexes, words) for one page's content-stream list."""
    imgs, words = [], []
    for i, data in enumerate(streams):
        if b" Do" in data and b"BT" not in data:
            imgs.append(i)
        elif b"BT" in data:
            m, t = _TM.search(data), _TJ.search(data)
            if not (m and t):
                continue
            text = bytes.fromhex(t.group(1).decode("ascii")).decode("cp1252", "replace")
            words.append(_Word(i, float(m.group(1)), float(m.group(2)), text))
    return imgs, words


def _lines(words):
    out = []
    for w in sorted(words, key=lambda w: -w.y):
        if out and abs(out[-1][0].y - w.y) <= _LINE_TOL:
            out[-1].append(w)
        else:
            out.append([w])
    for line in out:
        line.sort(key=lambda w: w.x)
    return [_Line(ws) for ws in out]


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
    Returns (cells, mean_quality) or (None, 0.0)."""
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


def _match_layout(lines, blocks, strict=False, lookahead=DEFAULT_LOOKAHEAD):
    """Merge layout blocks with PDF lines -> ordered elements: _Line (-> /P),
    _Heading, _CaptionMark, _ArtifactLine, or _TableGroup. Sequential fuzzy
    matching; unmatched table rows and all unaligned content stay paragraphs
    (or artifacts when strict — the layout was told to ignore scanner junk).

    lookahead caps how far past the cursor a block may match, so one bad block
    can't drag the rest of the page out of order. Raise it when a table sits
    below more lines of prose than that (the cursor only advances on a match)."""
    specs = []  # (block_index, kind, payload) in block/page order
    for bi, b in enumerate(blocks):
        t = b.get("type")
        if t == "table" and isinstance(b.get("rows"), list):
            header = b.get("header") or []
            if isinstance(header, list) and header:
                specs.append((bi, "row", (True, [str(c) for c in header])))
            for r in b["rows"]:
                if isinstance(r, list) and r:
                    specs.append((bi, "row", (False, [str(c) for c in r])))
        elif t in ("heading", "text") and b.get("text"):
            specs.append((bi, t, str(b["text"])))

    assigned = {}  # line index -> tuple(kind, ...)
    li = 0
    for bi, kind, payload in specs:
        if kind == "row":
            is_header, cells = payload
            row_text = " ".join(cells)
            best = next((j for j in range(li, min(li + lookahead, len(lines)))
                         if _similarity(row_text, lines[j].text) >= _MATCH_MIN), None)
            if best is None:
                continue
            split, quality = _align_cells(lines[best].words, cells)
            li = best + 1
            if split is None or quality < _CELL_MIN:
                assigned[best] = ("demoted",)  # real content: stays /P even in strict
                continue
            assigned[best] = ("row", bi, is_header, split)
        elif kind == "heading":
            best = next((j for j in range(li, min(li + lookahead, len(lines)))
                         if _similarity(payload, lines[j].text) >= _MATCH_MIN), None)
            if best is None:
                continue
            assigned[best] = ("heading", bi)
            li = best + 1
        else:  # text block: may span several consecutive lines
            block_tokens = _tokens(payload)
            j = li
            while j < min(li + lookahead, len(lines)):
                lt = _tokens(lines[j].text)
                if lt and block_tokens and len(lt & block_tokens) / len(lt) >= 0.5:
                    while j < len(lines):
                        lt = _tokens(lines[j].text)
                        if lt and len(lt & block_tokens) / len(lt) >= 0.5:
                            assigned[j] = ("text", bi)
                            j += 1
                        else:
                            break
                    li = j
                    break
                j += 1

    # everything the layout read anywhere on the page, squashed to bare
    # alphanumerics — the survival test for otherwise-unmatched lines in
    # strict mode ("Oslo 20 02" is inside a heading even when OCR split the year)
    def _squash(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())
    blob_parts = []
    for b in blocks:
        blob_parts.append(str(b.get("text") or ""))
        blob_parts.extend(str(c) for c in (b.get("header") or []))
        for r in (b.get("rows") or []):
            if isinstance(r, list):
                blob_parts.extend(str(c) for c in r)
    page_blob = _squash(" ".join(blob_parts))

    elements, current = [], None  # current = open _TableGroup
    for idx, line in enumerate(lines):
        a = assigned.get(idx)
        if a and a[0] == "row":
            _, bi, is_header, split = a
            if current is None or current.block_index != bi:
                current = _TableGroup(
                    block_index=bi,
                    row_header=bool(blocks[bi].get("row_header")))
                elements.append(current)
            current.rows.append(_Row(line, split, header=is_header))
        elif a and a[0] == "heading":
            bi = a[1]
            if bi + 1 < len(blocks) and blocks[bi + 1].get("type") == "table":
                elements.append(_CaptionMark(line, bi + 1))
            else:
                elements.append(_Heading(line))
        elif a:  # "text" or "demoted": real content, plain paragraph
            elements.append(line)
        else:
            # unmatched line: junk only if the layout read NOTHING like it —
            # whole line or any single word (OCR splits/joins break the other)
            sq = _squash(line.text)
            keep = bool(sq) and (sq in page_blob or any(
                len(w_sq) >= 3 and w_sq in page_blob
                for w_sq in (_squash(w.text) for w in line.words)))
            if strict and not keep:
                elements.append(_ArtifactLine(line))
            else:
                elements.append(line)

    # PDF/UA 7.2: table rows must span the same number of columns — demote
    # rows that disagree with the table's dominant width to /P (placed after
    # the table), then demote any "table" left with fewer than two rows
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

    # attach captions to their (surviving) tables; orphans become headings
    final = []
    for i, e in enumerate(out):
        if isinstance(e, _CaptionMark):
            target = next((t for t in out[i + 1:] if isinstance(t, _TableGroup)
                           and t.block_index == e.table_block), None)
            if target is not None and target.caption is None:
                target.caption = e.line
            else:
                final.append(_Heading(e.line))
            continue
        final.append(e)
    return final


def _table_attrs(pdf, scope):
    return pdf.make_indirect(Dictionary(O=Name.Table, Scope=Name("/" + scope)))


def _kids(mcids):
    return mcids[0] if len(mcids) == 1 else pikepdf.Array(mcids)


def tag_scan_pdf(src_path, dest_path, title, opts={}):
    """Add a PDF/UA-1 structure tree to a pdf_utils-built searchable PDF.

    :param str src_path: Source searchable PDF (build_searchable_pdf output).
    :param str dest_path: Tagged output PDF path.
    :param str title: Document title (dc:title, /Title, DisplayDocTitle).
    :param dict opts:
        'lang' (str, default 'en-US') — document language;
        'alt' (str or list[str]) — scan description: one string for every
            page, or a per-page list; pages whose entry is empty keep the
            scan as an /Artifact (right for a blank verso, which no
            description can honestly describe);
        'layouts' (list) — per-page {'blocks': [...]} vision layouts, or
            None entries for pages without one;
        'strict' (bool) — demote lines the layout doesn't account for to
            untagged artifacts (scanner-junk suppression);
        'lookahead' (int, default 40) — how far a layout block may match past
            the matcher's cursor; raise for pages whose table sits below
            more than that many lines of prose.
    :returns: Stats dict: pages, words, lines, tables, headings, captions, artifacts.
    :rtype: dict
    """
    lang = opts.get("lang", "en-US")
    alt = opts.get("alt")
    layouts = opts.get("layouts")
    strict = bool(opts.get("strict", False))
    lookahead = opts.get("lookahead", DEFAULT_LOOKAHEAD)

    pdf = pikepdf.open(src_path)
    root = pdf.Root

    doc_elem = pdf.make_indirect(Dictionary(
        Type=Name.StructElem, S=Name.Document, K=pikepdf.Array()))
    struct_root = pdf.make_indirect(Dictionary(
        Type=Name.StructTreeRoot, K=doc_elem, ParentTreeNextKey=len(pdf.pages)))
    doc_elem.P = struct_root
    parent_tree_nums = pikepdf.Array()

    stats = {"pages": len(pdf.pages), "words": 0, "lines": 0, "tables": 0,
             "headings": 0, "captions": 0, "artifacts": 0}
    seen_h1 = False
    for page_no, page in enumerate(pdf.pages):
        contents = page.obj.get("/Contents")
        if contents is None:
            continue
        stream_objs = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
        streams = [s.read_bytes() for s in stream_objs]
        imgs, words = _classify_streams(streams)

        lines = _lines(words)
        layout = layouts[page_no] if layouts and page_no < len(layouts) else None
        elements = (_match_layout(lines, layout["blocks"], strict=strict,
                                  lookahead=lookahead)
                    if layout and layout.get("blocks") else lines)

        # reading order = page geometry, always: stable-sort top-level elements
        # by their topmost word so no matcher quirk can reorder content
        def _top_y(e):
            if isinstance(e, _TableGroup):
                ln = ([e.caption] if e.caption else []) + [r.line for r in e.rows]
                return max(max(w.y for w in l.words) for l in ln)
            line = e.line if isinstance(e, (_Heading, _ArtifactLine)) else e
            return max(w.y for w in line.words)
        elements = sorted(elements, key=_top_y, reverse=True)

        # absorb stray lines that sit INSIDE a table's vertical span (demoted
        # rows, orphaned header fragments) as full-width rows at their true
        # position — otherwise they read after the whole table (reviewer-
        # reported: "tables reading out of order in weird ways")
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

        # page-local MCIDs: figure (if any) first, then non-artifact words
        mcid = 0
        fig_elem = None
        img_open, img_close = b"/Artifact BMC\n", b"EMC\n"
        if isinstance(alt, (list, tuple)):
            page_alt = alt[page_no] if page_no < len(alt) else None
        else:
            page_alt = alt
        if page_alt and imgs:  # empty/None alt -> scan stays an artifact
            fig_elem = pdf.make_indirect(Dictionary(
                Type=Name.StructElem, S=Name.Figure, P=doc_elem,
                Pg=page.obj, K=mcid, Alt=pikepdf.String(page_alt)))
            img_open = b"/Figure <</MCID %d>> BDC\n" % mcid
            mcid += 1
        for w in words:
            if not w.artifact:
                w.mcid = mcid
                mcid += 1

        parts = []
        for i, data in enumerate(streams):
            if i in imgs:
                parts += [img_open, data, b"\n", img_close]
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

        def make_elem(s, parent, line_words, **extra):
            el = Dictionary(Type=Name.StructElem, S=s, P=parent, Pg=page.obj,
                            **extra)
            if line_words:
                el.K = _kids([w.mcid for w in line_words])
            el = pdf.make_indirect(el)
            for w in line_words:
                word_parent[w.mcid] = el
            return el

        word_parent = {}
        if fig_elem is not None:
            doc_elem.K.append(fig_elem)
        for e in elements:
            if isinstance(e, _ArtifactLine):
                continue
            if isinstance(e, _Line):
                doc_elem.K.append(make_elem(Name.P, doc_elem, e.words))
                stats["lines"] += 1
            elif isinstance(e, _Heading):
                s = Name.H1 if not seen_h1 else Name.H2
                seen_h1 = True
                doc_elem.K.append(make_elem(s, doc_elem, e.line.words))
                stats["headings"] += 1
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
                            # leading label cell of a body row: a row header, so
                            # readers announce it as context for the whole row
                            stub = row_header and ci == 0 and not (row.header or row.raw)
                            s = Name.TH if (row.header or stub) else Name.TD
                            cell = Dictionary(Type=Name.StructElem, S=s,
                                              P=tr, Pg=page.obj)
                            if cell_words:
                                cell.K = _kids([w.mcid for w in cell_words])
                            if row.header:
                                cell.A = _table_attrs(pdf, "Column")
                            elif stub:
                                cell.A = _table_attrs(pdf, "Row")
                            elif row.raw and width > 1:  # spans every column
                                cell.A = pdf.make_indirect(Dictionary(
                                    O=Name.Table, ColSpan=width))
                            cell = pdf.make_indirect(cell)
                            tr.K.append(cell)
                            for w in cell_words:
                                word_parent[w.mcid] = cell
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

        page_parents = pikepdf.Array()
        for m in range(mcid):
            if fig_elem is not None and m == 0:
                page_parents.append(fig_elem)
            else:
                page_parents.append(word_parent[m])
        page.obj.StructParents = page_no
        page.obj.Tabs = Name.S
        parent_tree_nums.append(page_no)
        parent_tree_nums.append(pdf.make_indirect(page_parents))
        stats["words"] += len(words)

    struct_root.ParentTree = pdf.make_indirect(Dictionary(Nums=parent_tree_nums))
    root.StructTreeRoot = struct_root
    root.MarkInfo = Dictionary(Marked=True)
    root.Lang = pikepdf.String(lang)
    root.ViewerPreferences = Dictionary(DisplayDocTitle=True)

    with pdf.open_metadata() as meta:
        meta["dc:title"] = title
        # PDF/UA identification (ISO 14289-1 clause 5) — veraPDF-verified claim
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
