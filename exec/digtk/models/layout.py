"""
Vision layout segmentation for document pages: which parts of the page are
headings, running text, or tables (with header + rows). Used by
digtk.models.accessibility.tag_pdf (--tables) to emit real Table/TR/TD
structure elements via pdf_tag.tag_scan_pdf's opts['layouts'] — the model
decides *what is a table row*; cell boundaries come from word geometry
(pdf_tag's own alignment logic, not this module).

Ported from the digitization toolkit's layout.py — same prompt, same one
vision-call-per-page shape as alt_utils.generate_alt, reusing its
parse_model_json rather than duplicating fence-safe JSON parsing.
"""
from digtk.lib import ai
from digtk.models.alt_utils import parse_model_json

_LAYOUT_PROMPT = """Analyze this scanned document page and return its layout as JSON.

Read top-to-bottom. Split the page into blocks of three kinds:
- {"type": "heading", "text": "..."} — a title or section heading line
- {"type": "text", "text": "..."} — a paragraph or freestanding line of text
- {"type": "table", "header": ["col", ...], "rows": [["cell", ...], ...]} — a
  data table; header is the column-label row (empty list if the table has no
  header row printed), rows are the data rows in order, one array entry per cell.

Transcribe cell/heading text as printed. Every visible line of the page must be
covered by exactly one block, in page order. Ignore faint rotated or mirrored
edge text from scanning artifacts.

Return ONLY JSON: {"blocks": [...]}"""


def page_layout(image, opts={}):
    """Vision layout segmentation for one page image.

    Endpoint failures (chat_completion raising after its own retries) are not
    caught here and propagate to the caller — silently tagging without tables
    during an outage would look like success. Only unusable *output* (missing/
    malformed JSON) is treated as "no layout for this page" and returns None.

    :param image: Page image — a file path (local or 'cask:/'-prefixed) or an
        ai.Image (see digtk.lib.ai.chat_completion).
    :param dict opts: 'model' passed through to the chat call.
    :returns: {"blocks": [...]} — see pdf_tag.tag_scan_pdf's opts['layouts']
        shape — or None if the model output is unusable (caller falls back to
        paragraph-only tagging for this page).
    :rtype: dict|None
    """
    chat_opts = {"image": image}
    if opts.get("model"):
        chat_opts["model"] = opts["model"]
    try:
        obj = parse_model_json(ai.chat_completion(_LAYOUT_PROMPT, chat_opts))
    except ValueError:
        return None
    blocks = obj.get("blocks")
    if not isinstance(blocks, list):
        return None
    return {"blocks": blocks}
