"""
In-place PDF/UA-1 tagging of born-digital PDFs.

pdf_tag.tag_scan_pdf rebuilds pages (scan image + invisible text), which
throws away a born-digital page's fonts, vector art and non-Latin text. This
module keeps every page's content exactly as produced and adds the structure
on top: each graphics object in the content stream (a text span, a painted
path, an image) becomes a marked-content sequence, and pdf_tag's layout /
reading-order machinery builds the tree from them.

How the content is read:
- pikepdf parses the content stream into operators; a small interpreter
  tracks the CTM, the text matrix and the path being built, so every
  text-showing operator has a start point and every painted path / image has
  a bounding box in page space.
- PyMuPDF's get_texttrace() gives the page's text as spans in execution
  order, with per-glyph Unicode and boxes. Spans are walked in step with the
  text-showing operators, matched by start point.
- Words come from the span's glyphs, so the text layer is the real text.

What becomes what:
- Text operators -> /P marked content (the structure element decides the real
  role: P, H1, LI, TD ...); text the layout flags as junk -> /Artifact.
- Images and paths inside a figure region -> that /Figure's content; a
  scan covering the whole page -> the page /Figure (with alt) or an
  artifact; every other drawing -> /Artifact.
- A Form XObject that contains text is inlined into the page content, since
  a form can have only one structure parent (PDF/UA-1 7.20).
- Existing MCID / Artifact marks and structure tree are dropped and rebuilt;
  an existing outline (bookmarks) is kept.

Ported from the digitization toolkit's inplace.py.
Known limits: Type 3 / no-Unicode text has no words (tagged as artifact);
non-embedded fonts fail PDF/UA until embed_fonts() rewrites the file.
"""
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

import pikepdf
import pymupdf
from pikepdf import Dictionary, Name, Operator

from digtk.models import pdf_tag

_TEXT_SHOW = {"Tj", "TJ", "'", '"'}
_PATH_CONSTRUCT = {"m", "l", "c", "v", "y", "h", "re"}
_PATH_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
# changing these starts a MuPDF span
_STATE_OPS = {"Tf", "Tr", "Tz", "Ts", "g", "rg", "k", "cs", "sc", "scn",
              "G", "RG", "K", "CS", "SC", "SCN"}
_FLUSH_OPS = {"gs", "BDC", "BMC", "EMC", "d0", "d1"}
_POS_TOL = 0.75     # points: span origin matches operator start
_WORD_GAP = 0.25    # em: glyph gap that splits words
_WHOLE_PAGE = 0.85  # image page share meaning "the scan"
_MAX_FORM_DEPTH = 4
_GS_TIMEOUT_S = 1800

_IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# operator -> (operand index, resource category)
_RES_OPS = {
    "Tf": (0, "/Font"), "Do": (0, "/XObject"), "gs": (0, "/ExtGState"),
    "cs": (0, "/ColorSpace"), "CS": (0, "/ColorSpace"), "sh": (0, "/Shading"),
    "scn": (-1, "/Pattern"), "SCN": (-1, "/Pattern"), "BDC": (1, "/Properties"),
}


def _mul(a, b):
    """Multiply PDF matrices: apply a, then b.

    :rtype: tuple
    """
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def _apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _invert(m):
    """Invert a non-singular PDF matrix.

    :rtype: tuple
    """
    det = m[0] * m[3] - m[1] * m[2]
    a, b, c, d = m[3] / det, -m[1] / det, -m[2] / det, m[0] / det
    return (a, b, c, d, -(m[4] * a + m[5] * c), -(m[4] * b + m[5] * d))


def _floats(operands):
    out = []
    for v in operands:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


@dataclass
class _Unit:
    """One marked-content sequence to be: a run of operators in one stream."""
    kind: str        # text/path/image/form/shading
    stm: object      # None = page content
    start: int       # op index range [start, end)
    end: int
    bbox: Optional[List[float]] = None  # page space, graphics only
    words: List[pdf_tag._Word] = field(default_factory=list)
    mcid: int = -1
    role: str = ""   # "P" | "Figure" | "" (artifact)
    ctm: Optional[tuple] = None  # images: CTM at the Do
    redraw_of: Optional["_Unit"] = None  # clipped copy of a page image
    owner: object = None  # its _Figure, or "page"


@dataclass
class _Stream:
    obj: object  # None for the page itself
    ops: list
    units: List[_Unit] = field(default_factory=list)
    resources: object = None  # its /Resources, to resolve named property lists


@dataclass
class _Span:
    ox: float  # first glyph origin, page space
    oy: float
    words: List[pdf_tag._Word]
    taken: List[bool]
    rotated: bool = False  # not horizontal: one word, taken whole


class _Walker:
    """Interpret one page's content into units, aligning text operators with PyMuPDF's spans."""

    def __init__(self, spans):
        self.spans = spans
        self.si = 0
        self.streams = []
        self.unmatched = 0  # spans consumed without a position match
        self._span_cur = None

    def _text_unit(self, st, i, x, y, pos_known, state_changed, cur):
        """Start a new text unit at operator i, or extend the open one.

        :rtype: _Unit
        """
        sp = self.spans[self.si] if self.si < len(self.spans) else None
        near = sp is not None and abs(sp.ox - x) <= _POS_TOL and abs(sp.oy - y) <= _POS_TOL
        starts_span = cur is None or (pos_known and near) or (not pos_known and state_changed)
        if starts_span:
            unit = _Unit("text", st.obj, i, i + 1)
            if sp is not None:
                if not near and pos_known:
                    self.unmatched += 1
                unit.words = self._take(sp, None if not pos_known else (x, y))
                self.si += 1
                self._span_cur = sp
            st.units.append(unit)
            return unit
        # a repositioned operator inside one span
        sp = self._span_cur
        if pos_known and sp is not None:
            words = self._take(sp, (x, y))
            if words:
                unit = _Unit("text", st.obj, i, i + 1, words=words)
                st.units.append(unit)
                return unit
        cur.end = i + 1
        return cur

    def _take(self, sp, at):
        """Hand out a span's unclaimed words; with `at`, only those on that baseline from x on.

        :rtype: list[pdf_tag._Word]
        """
        out = []
        for k, w in enumerate(sp.words):
            if sp.taken[k]:
                continue
            if at is not None and not sp.rotated and \
                    not (abs(w.y - at[1]) <= 2.0 and w.x >= at[0] - _POS_TOL):
                continue
            sp.taken[k] = True
            out.append(w)
        return out

    def walk(self, stm_obj, ops, resources):
        """Interpret a content stream's operators into units.

        :param stm_obj: The stream's form, or None for the page.
        :param list ops: Parsed (operands, operator) pairs.
        :param resources: The stream's /Resources.
        :rtype: _Stream
        """
        st = _Stream(stm_obj, ops, resources=resources)
        self.streams.append(st)
        ctm = _IDENT
        stack = []
        tm = tlm = _IDENT
        leading = rise = 0.0
        pos_known = state_changed = False
        tstate = {}  # op -> operands, to spot real changes
        cur = None
        path_start = None
        pts = []
        for i, (operands, op) in enumerate(ops):
            o = str(op)
            if o == "q":
                stack.append(ctm)
            elif o == "Q":
                ctm = stack.pop() if stack else ctm
            elif o == "cm" and len(operands) == 6:
                ctm = _mul(tuple(_floats(operands)), ctm)
            elif o == "BT":
                tm = tlm = _IDENT
                pos_known, state_changed, cur = True, False, None
            elif o == "ET":
                cur = None
            elif o == "Tm" and len(operands) == 6:
                tm = tlm = tuple(_floats(operands))
                pos_known = True
            elif o in ("Td", "TD") and len(operands) == 2:
                tx, ty = _floats(operands)
                tlm = _mul((1, 0, 0, 1, tx, ty), tlm)
                tm = tlm
                pos_known = True
                if o == "TD":
                    leading = -ty
            elif o == "TL" and operands:
                leading = _floats(operands)[0]
            elif o == "T*":
                tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
                tm = tlm
                pos_known = True
            elif o in _STATE_OPS:
                if o == "Ts" and operands:
                    rise = _floats(operands)[0]
                key = tuple(str(v) for v in operands)
                if tstate.get(o) != key:  # unchanged re-issues don't count
                    tstate[o] = key
                    state_changed = True
            elif o in _FLUSH_OPS:
                state_changed = True
            elif o in _TEXT_SHOW:
                if o in ("'", '"'):
                    tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
                    tm = tlm
                    pos_known = True
                x, y = _apply(_mul(tm, ctm), 0.0, rise)
                cur = self._text_unit(st, i, x, y, pos_known, state_changed, cur)
                pos_known = state_changed = False
            elif o in _PATH_CONSTRUCT:
                if path_start is None:
                    path_start = i
                v = _floats(operands)
                if o == "re" and len(v) == 4:
                    for px, py in ((v[0], v[1]), (v[0] + v[2], v[1] + v[3])):
                        pts.append(_apply(ctm, px, py))
                elif o != "h" and len(v) >= 2:
                    for k in range(0, len(v) - 1, 2):
                        pts.append(_apply(ctm, v[k], v[k + 1]))
            elif o in _PATH_PAINT or o == "n":
                if o != "n" and path_start is not None and pts:
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                    st.units.append(_Unit("path", stm_obj, path_start, i + 1,
                                          bbox=[min(xs), min(ys), max(xs), max(ys)]))
                path_start, pts = None, []
            elif o == "Do" and operands:
                xobjs = resources.get("/XObject") if resources is not None else None
                xo = xobjs.get(str(operands[0])) if xobjs is not None else None
                sub = str(xo.get("/Subtype")) if xo is not None else ""
                if sub == "/Image":
                    st.units.append(_Unit("image", stm_obj, i, i + 1, bbox=_unit_box(ctm), ctm=ctm))
                elif sub == "/Form":
                    # text forms were inlined already
                    if "/StructParents" in xo:
                        del xo["/StructParents"]
                    st.units.append(_Unit("form", stm_obj, i, i + 1, bbox=_unit_box(ctm)))
            elif o == "sh":
                st.units.append(_Unit("shading", stm_obj, i, i + 1))
            elif o == "INLINE IMAGE":
                st.units.append(_Unit("image", stm_obj, i, i + 1, bbox=_unit_box(ctm)))
        return st


def _unit_box(ctm):
    """Page-space box of the unit square under a CTM (an image's footprint).

    :rtype: list[float]
    """
    cs = [_apply(ctm, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
    return [min(c[0] for c in cs), min(c[1] for c in cs),
            max(c[0] for c in cs), max(c[1] for c in cs)]


def _spans(fpage, box):
    """PyMuPDF's text spans, in execution order, as page-space words.

    PyMuPDF is y-down from the crop box's top-left; page space is the PDF's.

    :param pymupdf.Page fpage: The page.
    :param list[float] box: pdf_tag.page_box() of the same page.
    :rtype: list[_Span]
    """
    cx0, cy1 = box[0], box[3]
    out = []
    for sp in fpage.get_texttrace():
        chars = sp.get("chars") or []
        if not chars:
            continue
        size = float(sp.get("size") or 0.0)
        d = sp.get("dir") or (1.0, 0.0)
        rotated = not (abs(d[0] - 1.0) < 0.05 and abs(d[1]) < 0.05)
        if rotated:  # vertical sidebar: one word
            text = "".join(chr(c[0]) if isinstance(c[0], int) else str(c[0]) for c in chars).strip()
            ox, oy = chars[0][2]
            w = pdf_tag._Word(-1, cx0 + ox, cy1 - oy, text, size=size)
            out.append(_Span(cx0 + ox, cy1 - oy, [w] if text else [], [False] if text else [], rotated=True))
            continue
        words, cur = [], []

        def flush():
            if not cur:
                return
            text = "".join(c[0] for c in cur)
            x1 = max(c[2][2] for c in cur)
            words.append(pdf_tag._Word(-1, cx0 + cur[0][1][0], cy1 - cur[0][1][1], text,
                                       size=size, x1=cx0 + x1))
            cur.clear()

        for uni, _glyph, origin, bbox in chars:
            ch = chr(uni) if isinstance(uni, int) else str(uni)
            if ch.isspace():
                flush()
                continue
            if cur:
                last = cur[-1]
                gap = origin[0] - last[2][2]
                if gap > _WORD_GAP * max(size, 1.0) or abs(origin[1] - last[1][1]) > 0.5:
                    flush()
            cur.append((ch, origin, bbox))
        flush()
        ox, oy = chars[0][2]
        out.append(_Span(cx0 + ox, cy1 - oy, words, [False] * len(words)))
    return out


def _form_has_text(xo, depth=0):
    """Check whether a Form XObject (or one it draws) shows text.

    :rtype: bool
    """
    if depth > _MAX_FORM_DEPTH:
        return False
    try:
        ops = list(pikepdf.parse_content_stream(xo))
    except Exception:  # noqa: BLE001 — unparseable: treat as graphics
        return False
    xobjs = (xo.get("/Resources") or Dictionary()).get("/XObject")
    for operands, op in ops:
        o = str(op)
        if o in _TEXT_SHOW:
            return True
        if o == "Do" and operands and xobjs is not None:
            inner = xobjs.get(str(operands[0]))
            if inner is not None and str(inner.get("/Subtype")) == "/Form" \
                    and _form_has_text(inner, depth + 1):
                return True
    return False


def _merge_resources(page, fres):
    """Add a form's resources to the page's, renaming any that collide.

    :returns: {(category, old name): new name} for renamed entries.
    :rtype: dict
    """
    rename = {}
    if fres is None:
        return rename
    for cat, entries in fres.items():
        if not isinstance(entries, Dictionary):
            continue
        for name, obj in entries.items():
            pres = page.obj.get("/Resources")
            have = (pres.get(cat) or Dictionary()).get(name) if pres is not None else None
            if have is not None:
                same = (have.objgen == obj.objgen and obj.objgen != (0, 0)) or have == obj
                if same:
                    continue
                rename[(cat, name)] = str(page.add_resource(obj, Name(cat), prefix="f"))
            else:
                page.add_resource(obj, Name(cat), Name(name))
    return rename


def _renamed(ops, rename):
    """Rewrite resource names in operators per _merge_resources()'s renames.

    :rtype: list
    """
    if not rename:
        return ops
    out = []
    for operands, op in ops:
        spec = _RES_OPS.get(str(op))
        if spec and operands:
            idx, cat = spec
            try:
                target = operands[idx]
            except IndexError:
                target = None
            if isinstance(target, Name) and (cat, str(target)) in rename:
                operands = list(operands)
                operands[idx] = Name(rename[(cat, str(target))])
        out.append((operands, op))
    return out


def _inline_forms(page, ops, resources, depth=0):
    """Replace each text-bearing form's Do with the form's own operators.

    Wrapped in q / its Matrix / its BBox clip / Q, so drawing is unchanged.

    :rtype: list
    """
    if depth > _MAX_FORM_DEPTH:
        return ops
    xobjs = resources.get("/XObject") if resources is not None else None
    if xobjs is None:
        return ops
    out = []
    for operands, op in ops:
        if str(op) == "Do" and operands:
            xo = xobjs.get(str(operands[0]))
            if xo is not None and str(xo.get("/Subtype")) == "/Form" and _form_has_text(xo):
                try:
                    fops = list(pikepdf.parse_content_stream(xo))
                except Exception:  # noqa: BLE001 — keep the Do
                    fops = None
                if fops:
                    fops = _renamed(fops, _merge_resources(page, xo.get("/Resources")))
                    fops = _inline_forms(page, fops, page.obj.get("/Resources"), depth + 1)
                    out.append(([], Operator("q")))
                    if "/Matrix" in xo and len(xo.Matrix) == 6:
                        out.append((list(xo.Matrix), Operator("cm")))
                    if "/BBox" in xo and len(xo.BBox) == 4:
                        x0, y0, x1, y1 = _floats(xo.BBox)
                        out.append(([min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)], Operator("re")))
                        out.append(([], Operator("W")))
                        out.append(([], Operator("n")))
                    out.extend(fops)
                    out.append(([], Operator("Q")))
                    continue
        out.append((operands, op))
    return out


def _rewrite(st):
    """The stream's operators with new marked content, old MCID/Artifact marks removed.

    :param _Stream st: A walked stream.
    :rtype: bytes
    """
    opens = {u.start: u for u in st.units if u.redraw_of is None}
    closes = {u.end - 1: u for u in st.units if u.redraw_of is None}
    redraws = {}
    for u in st.units:
        if u.redraw_of is not None:
            redraws.setdefault(u.redraw_of.end - 1, []).append(u)
    out = []
    drop_stack = []
    for i, (operands, op) in enumerate(st.ops):
        o = str(op)
        if o in ("BDC", "BMC"):
            tag = str(operands[0]) if operands else ""
            props = operands[1] if o == "BDC" and len(operands) > 1 else None
            if isinstance(props, Name) and st.resources is not None:
                # e.g. Ghostscript moves <</MCID n>> into /Properties as /R11
                props = (st.resources.get("/Properties") or Dictionary()).get(str(props))
            drop = tag == "/Artifact" or (isinstance(props, Dictionary) and "/MCID" in props)
            drop_stack.append(drop)
            if drop:
                continue
        elif o == "EMC":
            if drop_stack and drop_stack.pop():
                continue
        u = opens.get(i)
        if u is not None:
            if u.role in ("P", "Figure"):
                out.append(([Name("/" + u.role), Dictionary(MCID=u.mcid)], Operator("BDC")))
            else:
                out.append(([Name.Artifact], Operator("BMC")))
        out.append((operands, op))
        if i in closes:
            out.append(([], Operator("EMC")))
        for rd in redraws.get(i, []):  # same pixels, clipped
            # Emitted right after the scan's Do, so the scan's CTM is still in
            # effect: undo it to clip in page space, then redo it for the Do.
            # The scan covers most of the page, so its CTM is invertible. Decimal
            # because pikepdf writes floats to ~6 places, too coarse for 1/3686.
            x0, y0, x1, y1 = rd.bbox
            out += [
                ([Name.Figure, Dictionary(MCID=rd.mcid)], Operator("BDC")),
                ([], Operator("q")),
                ([Decimal(f"{v:.12g}") for v in _invert(rd.redraw_of.ctm)], Operator("cm")),
                ([round(x0, 2), round(y0, 2), round(x1 - x0, 2), round(y1 - y0, 2)], Operator("re")),
                ([], Operator("W")), ([], Operator("n")),
                ([round(v, 4) for v in rd.redraw_of.ctm], Operator("cm")),
                (list(st.ops[rd.redraw_of.start][0]), Operator("Do")),
                ([], Operator("Q")), ([], Operator("EMC")),
            ]
    return pikepdf.unparse_content_stream(out)


def _rotate_specs(specs, rotate):
    """Map figure boxes from the rendered (rotated) page onto the unrotated page.

    :param list[dict] specs: Figure specs in page fractions, or None.
    :param int rotate: The page's /Rotate.
    :rtype: list[dict]|None
    """
    if not specs or rotate % 360 == 0:
        return specs
    r = rotate % 360

    def unrot(fx, fy):
        if r == 90:
            return fy, 1 - fx
        if r == 180:
            return 1 - fx, 1 - fy
        return 1 - fy, fx  # 270

    out = []
    for f in specs:
        x0, y0, x1, y1 = f["bbox"]
        cs = [unrot(x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
        out.append(dict(f, bbox=[min(c[0] for c in cs), min(c[1] for c in cs),
                                 max(c[0] for c in cs), max(c[1] for c in cs)]))
    return out


def _center_in(bbox, box):
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def _tag_annots(pdf, page, doc_elem, parent_tree_nums, next_key, stats):
    """Put link and widget annotations in the structure tree (PDF/UA-1 7.18).

    Each gets a /Link or /Form element, described by its contents or URI.

    :returns: The next free parent-tree key.
    :rtype: int
    """
    annots = page.obj.get("/Annots")
    if annots is None:
        return next_key
    for annot in annots:
        try:
            sub = str(annot.get("/Subtype"))
        except Exception:  # noqa: BLE001 — skip broken annot
            continue
        if sub == "/Link":
            s = Name.Link
        elif sub == "/Widget":
            s = Name.Form
        else:
            continue
        desc = str(annot.get("/Contents") or "")
        if not desc:
            action = annot.get("/A")
            if action is not None and action.get("/URI") is not None:
                desc = str(action.URI)
        extra = {"Alt": pikepdf.String(desc)} if desc else {}
        el = pdf.make_indirect(Dictionary(
            Type=Name.StructElem, S=s, P=doc_elem, Pg=page.obj,
            K=pdf.make_indirect(Dictionary(Type=Name.OBJR, Obj=annot, Pg=page.obj)), **extra))
        if desc and "/Contents" not in annot:
            annot.Contents = pikepdf.String(desc)
        annot.StructParent = next_key
        parent_tree_nums.append(next_key)
        parent_tree_nums.append(el)
        next_key += 1
        doc_elem.K.append(el)
        stats["annotations"] += 1
    return next_key


def _has_outline(root):
    ol = root.get("/Outlines")
    return ol is not None and ol.get("/First") is not None


def _assign_roles(units, page_area, opts):
    """Decide each unit's role: text, a figure's content, the page scan, or an artifact.

    :param list[_Unit] units: The page's units.
    :param float page_area: Page area in square points.
    :param dict opts: 'page_alt' (str|None), 'figures' (list[pdf_tag._Figure]).
    :returns: Number of images left as artifacts.
    :rtype: int
    """
    untagged = 0
    page_alt = opts.get("page_alt")
    figs = opts.get("figures") or []
    has_page_figure = False
    for u in units:
        if u.kind == "text":
            u.role = "P" if any(not w.artifact for w in u.words) else ""
            continue
        if u.kind == "form":
            continue  # graphics-only form: artifact
        area = (u.bbox[2] - u.bbox[0]) * (u.bbox[3] - u.bbox[1]) if u.bbox else 0.0
        whole = u.kind == "image" and area >= _WHOLE_PAGE * page_area
        owner = None if whole else next(
            (fg for fg in figs if u.bbox and _center_in(u.bbox, fg.bbox)), None)
        if owner is not None:
            u.role, u.owner = "Figure", owner
        elif whole and not figs and page_alt and not has_page_figure:
            u.role, u.owner = "Figure", "page"
            has_page_figure = True
        elif u.kind == "image" and not whole:
            untagged += 1
    return untagged


def _add_redraws(walker, units, figs, page_area):
    """Give figures drawn only by the page scan a clipped redraw of it as content.

    For a scan wrapped in a PDF, the figure has no graphics of its own.

    :param _Walker walker: The page's walker; its page stream gains units.
    :param list[_Unit] units: The page's units.
    :param list[pdf_tag._Figure] figs: The page's figures.
    :param float page_area: Page area in square points.
    """
    scan = next((u for u in units if u.kind == "image" and u.ctm and u.stm is None and u.bbox
                 and (u.bbox[2] - u.bbox[0]) * (u.bbox[3] - u.bbox[1]) >= _WHOLE_PAGE * page_area), None)
    if scan is None or not figs:
        return
    st0 = walker.streams[0]
    at = st0.units.index(scan) + 1
    for fg in figs:
        if any(u.owner is fg for u in units):
            continue
        rd = _Unit("redraw", None, scan.start, scan.end, bbox=list(fg.bbox),
                   role="Figure", redraw_of=scan, owner=fg)
        st0.units.insert(at, rd)
        at += 1


def tag_pdf_inplace(src_path, dest_path, title, opts={}):
    """Tag a born-digital PDF in place, keeping every page's content as produced.

    Takes pdf_tag.tag_scan_pdf()'s opts; alt applies to a whole-page scan image.

    :param str src_path: Source PDF.
    :param str dest_path: Tagged output path.
    :param str title: Document title.
    :param dict opts: 'lang', 'alt', 'layouts', 'strict', 'lookahead',
        'paragraphs', 'figures' — see pdf_tag.tag_scan_pdf(). Figure boxes
        are fractions of the rendered page; /Rotate is undone here.
    :returns: tag_scan_pdf()'s stats plus 'untagged_images' (images left as
        artifacts), 'unmatched_spans' (text operators that didn't line up
        with PyMuPDF's spans; many means misplaced words) and 'annotations'.
    :rtype: dict
    """
    lang = opts.get("lang", "en-US")
    alt = opts.get("alt")
    layouts = opts.get("layouts")
    figures = opts.get("figures")
    match_opts = {
        "strict": bool(opts.get("strict", False)),
        "lookahead": opts.get("lookahead", pdf_tag.DEFAULT_LOOKAHEAD),
        "paragraphs": bool(opts.get("paragraphs", False)),
    }

    pdf = pikepdf.open(src_path)
    fdoc = pymupdf.open(src_path)
    root = pdf.Root
    doc_elem = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, K=pikepdf.Array()))
    struct_root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot, K=doc_elem))
    doc_elem.P = struct_root
    parent_tree_nums = pikepdf.Array()
    next_key = 0

    stats = {"pages": len(pdf.pages), "words": 0, "lines": 0, "paragraphs": 0,
             "tables": 0, "headings": 0, "captions": 0, "artifacts": 0, "lists": 0,
             "bookmarks": 0, "figures": 0, "untagged_images": 0, "unmatched_spans": 0, "annotations": 0}
    ctx = {"pdf": pdf, "doc_elem": doc_elem, "stats": stats, "prev_level": 0,
           "bookmarks": [], "doc_lang": lang.split("-")[0].lower()}

    for page_no, page in enumerate(pdf.pages):
        try:
            ops = list(pikepdf.parse_content_stream(page))
        except Exception:  # noqa: BLE001 — unparseable page stays untagged
            ops = []
        box = pdf_tag.page_box(page.obj)
        if "/StructParents" in page.obj:  # stale key from the old tree
            del page.obj["/StructParents"]
        ops = _inline_forms(page, ops, page.obj.get("/Resources"))
        walker = _Walker(_spans(fdoc[page_no], box))
        walker.walk(None, ops, page.obj.get("/Resources"))
        stats["unmatched_spans"] += walker.unmatched

        units = [u for st in walker.streams for u in st.units]
        words = [w for u in units if u.kind == "text" for w in u.words]
        for u in units:
            for w in u.words:
                w.stm = u.stm
        layout = layouts[page_no] if layouts and page_no < len(layouts) else None
        specs = _rotate_specs(figures[page_no] if figures and page_no < len(figures) else None,
                              int(page.obj.get("/Rotate", 0) or 0))
        elements, page_figs = pdf_tag._build_elements(words, layout, {
            **match_opts, "figures": specs, "page_box": box})

        page_alt = (alt[page_no] if page_no < len(alt) else None) if isinstance(alt, (list, tuple)) else alt
        page_area = max(1.0, (box[2] - box[0]) * (box[3] - box[1]))
        stats["untagged_images"] += _assign_roles(units, page_area, {"page_alt": page_alt, "figures": page_figs})
        _add_redraws(walker, units, page_figs, page_area)
        units = [u for st in walker.streams for u in st.units]

        # MCIDs per stream, in operator order
        for st in walker.streams:
            n = 0
            for u in st.units:
                if u.role:
                    u.mcid = n
                    n += 1
                    for w in u.words:
                        w.mcid = u.mcid
                elif u.kind == "text":
                    stats["artifacts"] += 1

        fig_elem = None
        page_units = [u for u in units if u.owner == "page"]
        for u in units:
            if u.role == "Figure" and u.owner != "page":
                u.owner.content.append((u.stm, u.mcid))
        if page_units:
            fig_elem = pdf.make_indirect(Dictionary(
                Type=Name.StructElem, S=Name.Figure, P=doc_elem, Pg=page.obj,
                K=pikepdf.Array([pdf_tag._kid_ref(u.stm, u.mcid, page.obj) for u in page_units]),
                Alt=pikepdf.String(page_alt)))
        word_parent = pdf_tag._emit_elements(ctx, page, page_no, elements, first=fig_elem)
        for u in page_units:
            word_parent[pdf_tag._mc_key(u.stm, u.mcid)] = fig_elem

        for st in walker.streams:
            tagged = [u for u in st.units if u.role]
            data = _rewrite(st)
            if st.obj is None:
                page.obj.Contents = pdf.make_stream(data)
            else:
                st.obj.write(data)
            if not tagged:
                continue
            rows = pikepdf.Array()
            for m in range(len(tagged)):
                rows.append(word_parent.get(pdf_tag._mc_key(st.obj, m), doc_elem))
            holder = page.obj if st.obj is None else st.obj
            holder.StructParents = next_key
            parent_tree_nums.append(next_key)
            parent_tree_nums.append(pdf.make_indirect(rows))
            next_key += 1
        next_key = _tag_annots(pdf, page, doc_elem, parent_tree_nums, next_key, stats)
        page.obj.Tabs = Name.S
        stats["words"] += len(words)

    struct_root.ParentTree = pdf.make_indirect(Dictionary(Nums=parent_tree_nums))
    struct_root.ParentTreeNextKey = next_key
    if ctx["bookmarks"] and not _has_outline(root):
        stats["bookmarks"] = pdf_tag._write_outline(pdf, ctx["bookmarks"])
    root.StructTreeRoot = struct_root
    root.MarkInfo = Dictionary(Marked=True)
    root.Lang = pikepdf.String(lang)
    root.ViewerPreferences = Dictionary(DisplayDocTitle=True)
    with pdf.open_metadata() as meta:
        meta["dc:title"] = title
        meta.register_xml_namespace("http://www.aiim.org/pdfua/ns/id/", "pdfuaid")
        meta["pdfuaid:part"] = "1"
    if pdf.docinfo is None:
        pdf.docinfo = Dictionary()
    pdf.docinfo["/Title"] = pikepdf.String(title)
    pdf.save(dest_path)
    pdf.close()
    fdoc.close()
    return stats


def text_pages(path):
    """Which pages of a PDF have extractable text (born-digital, not a scan).

    :param str path: Local PDF.
    :returns: One bool per page.
    :rtype: list[bool]
    """
    with pymupdf.open(path) as doc:
        return [bool(page.get_text("words")) for page in doc]


def page_texts(path):
    """Each page's extractable text, in reading order.

    :param str path: Local PDF.
    :rtype: list[str]
    """
    with pymupdf.open(path) as doc:
        return [page.get_text().strip() for page in doc]


def page_widths(path):
    """Each page's width in points, as rendered (crop box, rotation applied).

    :param str path: Local PDF.
    :rtype: list[float]
    """
    with pymupdf.open(path) as doc:
        return [page.rect.width for page in doc]


def unembedded_fonts(path):
    """Names of the fonts a PDF uses without embedding them.

    PDF/UA-1 7.21.4.1 requires every font embedded; Type 3 fonts carry their glyphs.

    :param str path: Local PDF.
    :rtype: list[str]
    """
    names, seen = [], set()

    def _check(font):
        try:
            if font.objgen in seen:
                return
            seen.add(font.objgen)
            sub = str(font.get("/Subtype"))
            if sub == "/Type3":
                return
            if sub == "/Type0":
                for d in font.get("/DescendantFonts") or []:
                    _check(d)
                return
            fd = font.get("/FontDescriptor")
            if fd is None or not any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                names.append(str(font.get("/BaseFont") or "?").lstrip("/"))
        except Exception:  # noqa: BLE001 — skip broken font
            return

    def _resources(res, depth=0):
        if res is None or depth > _MAX_FORM_DEPTH:
            return
        for f in (res.get("/Font") or Dictionary()).values():
            _check(f)
        for x in (res.get("/XObject") or Dictionary()).values():
            try:
                if str(x.get("/Subtype")) == "/Form":
                    _resources(x.get("/Resources"), depth + 1)
            except Exception:  # noqa: BLE001
                continue

    with pikepdf.open(path) as pdf:
        for page in pdf.pages:
            _resources(page.obj.get("/Resources"))
    return sorted(set(names))


def embed_fonts(src_path, dest_path, opts={}):
    """Rewrite a PDF through Ghostscript so every font is embedded.

    URW clones stand in for the base-14 fonts; the result renders the same.

    :param str src_path: Local PDF.
    :param str dest_path: Rewritten PDF path.
    :param dict opts: 'gs' (binary, default 'gs' on PATH), 'timeout' (s).
    :returns: True when every font is now embedded; False if Ghostscript
        is missing or failed.
    :rtype: bool
    """
    gs = shutil.which(opts.get("gs", "gs"))
    if gs is None:
        return False
    cmd = [gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite",
           "-dCompatibilityLevel=1.7", "-dEmbedAllFonts=true", "-dSubsetFonts=true",
           "-dPreserveAnnots=true", "-dAutoRotatePages=/None",
           "-dPassThroughJPEGImages=true", f"-sOutputFile={dest_path}",
           # default NeverEmbed is the base 14
           "-c", "<</NeverEmbed []>> setdistillerparams", "-f", src_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=opts.get("timeout", _GS_TIMEOUT_S))
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0 or not os.path.isfile(dest_path):
        return False
    return not unembedded_fonts(dest_path)
