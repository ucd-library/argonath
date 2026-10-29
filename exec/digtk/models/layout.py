"""
Vision layout segmentation for document pages: which parts of the page are
headings (with a level), running text, lists, or tables (with header + rows),
and how many text columns the page has. Used by
digtk.models.accessibility.tag_pdf (--tables) to emit real H1-H6, L/LI and
Table/TR/TD structure via pdf_tag.tag_scan_pdf's opts['layouts'] — the model
decides *what* each block is; word/cell boundaries come from word geometry
(pdf_tag's own alignment logic, not this module).

Ported from the digitization toolkit's layout.py — same prompt, same one
vision-call-per-page shape as alt_utils.generate_alt, reusing its
parse_model_json rather than duplicating fence-safe JSON parsing.
"""
from digtk.lib import ai, utils
from digtk.models.alt_utils import RAW_LOG_CHARS, parse_model_json

# Bump when the output shape changes.
LAYOUT_VERSION = 2

_RETRY_NOTE = ('\n\nYour previous reply was not usable. Return ONLY the JSON object '
               '{"columns": ..., "blocks": [...]}, with any double quotes inside strings escaped as \\".')

_LAYOUT_PROMPT = """Analyze this scanned document page and return its layout as JSON.

Read top-to-bottom. Split the page into blocks of four kinds:
- {"type": "heading", "level": 1, "text": "..."} — a title or section heading
  line. level 1 = the document/part/chapter title, 2 = a main section heading,
  3 = a subsection heading, 4 = anything lower. Judge by size, weight and
  numbering (e.g. "3" is level 2, "3.1" level 3, "3.1.2" level 4). Running
  heads, page numbers and figure/table captions are NOT headings.
- {"type": "text", "text": "..."} — a paragraph or freestanding line of text
- {"type": "list", "ordered": true, "items": ["...", "..."]} — a numbered,
  lettered or bulleted list; one entry per item, label included as printed
  ("1. First point"); ordered is false for bullets/dashes.
- {"type": "table", "header": ["col", ...], "rows": [["cell", ...], ...]} — a
  data table; header is the column-label row (empty list if the table has no
  header row printed), rows are the data rows in order, one array entry per cell.

Add "lang": "<BCP-47 code>" (e.g. "es", "fr", "de", "la") to a block ONLY when
its text is in a different language from the rest of the page. Omit it otherwise.

Transcribe cell/heading text as printed. Every visible line of the page must be
covered by exactly one block, in reading order (a left column top to bottom,
then the next column). Ignore faint rotated or mirrored edge text from
scanning artifacts.

Also report "columns": how many side-by-side columns the body text is set in
(1 for an ordinary page; 2 or 3 for newspaper / journal layouts; a narrow
margin of notes is not a column).

Return ONLY JSON: {"columns": 1, "blocks": [...]}"""


def page_layout(image, opts={}):
    """Vision layout segmentation for one page image.

    Endpoint failures (chat_completion raising after its own retries) are not
    caught here and propagate to the caller — silently tagging without tables
    during an outage would look like success. Unusable *output* (missing/
    malformed JSON) gets one retry; if that fails too, it's logged and treated
    as "no layout for this page" (None).

    :param image: Page image — a file path (local or 'cask:/'-prefixed) or an
        ai.Image (see digtk.lib.ai.chat_completion).
    :param dict opts: 'model' and 'max_tokens' passed through to the chat call.
    :returns: {"version": LAYOUT_VERSION, "blocks": [...], "columns": int
        (1-4, when the model gave one)} — see pdf_tag.tag_scan_pdf's
        opts['layouts'] — or None if the model output is unusable (caller
        falls back to paragraph-only tagging for this page).
    :rtype: dict|None
    """
    chat_opts = {"image": image}
    for key in ("model", "max_tokens"):
        if opts.get(key):
            chat_opts[key] = opts[key]
    raw = ai.chat_completion(_LAYOUT_PROMPT, chat_opts)
    try:
        return _parse_layout(raw)
    except ValueError as first_err:
        raw_retry = ai.chat_completion(_LAYOUT_PROMPT + _RETRY_NOTE, chat_opts)
        try:
            return _parse_layout(raw_retry)
        except ValueError as retry_err:
            utils.warn(f"unusable layout reply, retry failed too. "
                       f"First ({first_err}): {raw[:RAW_LOG_CHARS]!r} "
                       f"Retry ({retry_err}): {raw_retry[:RAW_LOG_CHARS]!r}")
            return None


def _parse_layout(raw):
    """Turn a layout reply into page_layout()'s result.

    :param str raw: Raw model output.
    :rtype: dict
    :raises ValueError: If there's no JSON object or "blocks" isn't a list.
    """
    obj = parse_model_json(raw)
    blocks = obj.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError(f'"blocks" is {type(blocks).__name__}, not a list')
    out = {"version": LAYOUT_VERSION, "blocks": blocks}
    try:
        cols = int(obj.get("columns"))
    except (TypeError, ValueError):
        cols = 0
    if cols >= 1:
        out["columns"] = min(cols, 4)
    return out


def is_current(layout):
    """Check whether a cached layout came from the current prompt.

    :param dict|None layout: A cached page_layout() result.
    :rtype: bool
    """
    return bool(layout) and layout.get("version") == LAYOUT_VERSION
