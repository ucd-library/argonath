"""
ADA/WCAG alt-text generation — collection-tuned prompt, Qwen vision call
(via digtk.lib.ai), fence-safe JSON parse, and validation.

Ported from the digitization toolkit's alt pipeline, where it generated the
reviewed alt text for the Martin Yan, Bradbury, Riley, SCA, Ichinose, and
Zelma Long collections. Alt style rules: one sentence, bounded length,
never an "image of…" opener (screen-reader context already announces it's
an image), people named only when the collection wants it, short legible
text quoted when significant.

Flow: build_alt_prompt() -> generate_alt() -> (alt, description,
long_description, flags).
Non-empty flags mean a human should look: TOO_LONG survivors, banned
openers, unparseable model output. Callers queue flagged results for
review rather than dropping them — a reviewer fixing one line beats a
silently missing description.
"""
import json
import re

from digtk.lib import ai, utils

DEFAULT_MAX_ALT_CHARS = 150
LENGTH_GRACE = 20  # chars over max before TOO_LONG flags; models overshoot slightly
RAW_LOG_CHARS = 500  # raw reply chars logged on failure

# Openers that add nothing for screen-reader users (context announces it's an image).
_BANNED_OPENERS = re.compile(
    r"^\s*(an?\s+)?(image|photo(graph)?|picture|scan|view)\s+(of|show(s|ing))\b",
    re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def build_alt_prompt(metadata, opts={}):
    """Build the vision prompt for one image.

    :param dict metadata: Item context, all optional: 'title', 'description',
        'date', 'creator', 'page' ((n, total) tuple for multi-page items).
    :param dict opts:
        'collection_context' (str) — what this collection is, tuned per collection;
        'name_people' (bool, default True) — name people the metadata identifies;
        'transcribe_text' (bool, default True) — quote short significant text;
        'tone' (str, default 'neutral, factual');
        'language' (str, default 'en-US');
        'max_alt_chars' (int, default 150);
        'extra_instructions' (str) — collection-specific style guidance;
        'want_description' (bool, default False) — also ask for a 2-4 sentence
            catalog description;
        'want_long' (bool, default False) — long description for maps, charts,
            posters and other information-dense images.
    :returns: Prompt text.
    :rtype: str
    """
    max_chars = opts.get("max_alt_chars", DEFAULT_MAX_ALT_CHARS)
    page = metadata.get("page")
    page_line = f"\n  This is page {page[0]} of {page[1]} in the item." if page else ""
    people = ("Name people identified in the metadata when they are clearly the subject."
              if opts.get("name_people", True) else
              "Do not name individuals; describe them generically.")
    transcribe = ("If short text or signage is significant and legible, quote it exactly."
                  if opts.get("transcribe_text", True) else
                  "Do not transcribe visible text.")
    desc_ask = (
        '\nAlso write "description": 2-4 sentences suitable as a catalog description, '
        "complementing the title without repeating it."
        if opts.get("want_description") else '\nSet "description" to null.')
    long_ask = (
        '\nIf the image is information-dense — a map, poster, broadside, chart, diagram, '
        'table, architectural or technical drawing, sheet music, or a page that is mostly '
        'text — also write "long_description": what a sighted reader gets from it that the '
        'short alt cannot carry: the layout, every legible piece of text (transcribe a '
        'poster\'s wording in reading order), labels, legend entries, place names, data '
        'values and the relationships between them. Use as many sentences as that takes. '
        'For an ordinary photograph or artwork set "long_description" to null.'
        if opts.get("want_long") else '\nSet "long_description" to null.')
    return f"""You are writing ADA/WCAG-compliant alternative text for an image in a digital library collection.

Collection context: {opts.get("collection_context") or "(none provided)"}

Item metadata (context only — describe what is VISIBLE, do not copy metadata verbatim):
  Title: {metadata.get("title") or "(none)"}
  Existing description: {metadata.get("description") or "(none)"}
  Date: {metadata.get("date") or "(unknown)"}  Creator: {metadata.get("creator") or "(unknown)"}{page_line}

Write "alt": one sentence, at most {max_chars} characters, stating the image's essential visual content. Never begin with "image of", "photo of", "picture of" or similar. {people} {transcribe}
Tone: {opts.get("tone", "neutral, factual")}. Follow {opts.get("language", "en-US")} conventions. {opts.get("extra_instructions", "")}{desc_ask}{long_ask}

Return ONLY a JSON object: {{"alt": "...", "description": "..." or null, "long_description": "..." or null}}"""


def parse_model_json(text):
    """Extract the JSON object from model output.

    The model may wrap JSON in ``` fences or surround it with prose — pull
    the outermost object either way.

    :param str text: Raw model output.
    :returns: Parsed object.
    :rtype: dict
    :raises ValueError: If no JSON object can be found or parsed.
    """
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in model output: {text[:120]!r}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in model output: {e}") from e


def _optional_text(value):
    """Return stripped text, or None when empty.

    :param value: Model output field.
    :returns: Stripped string or None.
    :rtype: str|None
    """
    return value.strip() if isinstance(value, str) and value.strip() else None


def validate_alt(alt, opts={}):
    """Validate alt text against the style rules.

    :param str alt: Candidate alt text.
    :param dict opts: 'max_alt_chars' (int, default 150).
    :returns: Flags — empty list means clean. Possible: EMPTY, TOO_LONG
        (over max + a small grace margin), BANNED_OPENER.
    :rtype: list[str]
    """
    if not alt or not alt.strip():
        return ["EMPTY"]
    flags = []
    if len(alt) > opts.get("max_alt_chars", DEFAULT_MAX_ALT_CHARS) + LENGTH_GRACE:
        flags.append("TOO_LONG")
    if _BANNED_OPENERS.match(alt):
        flags.append("BANNED_OPENER")
    return flags


def generate_alt(image, metadata, opts={}):
    """Generate alt text (and optionally a description) for one image.

    One vision call, plus one retry if the reply isn't valid JSON, and one
    shorten retry if the alt runs long. Failed retries are flagged for review,
    and unparseable replies are logged.

    :param image: Image to describe — a file path (local or 'cask:/') or an
        ai.Image (see digtk.lib.ai.chat_completion).
    :param dict metadata: Item context for the prompt (see build_alt_prompt).
    :param dict opts: build_alt_prompt() opts, plus 'model', 'max_tokens'
        and 'temperature' passed through to the chat call.
    :returns: (alt, description_or_None, long_description_or_None, flags).
        On unparseable output after the retry: ("", None, None,
        ["ERROR:unparseable-model-output"]).
    :rtype: tuple
    """
    prompt = build_alt_prompt(metadata, opts)
    chat_opts = {"image": image}
    for key in ("model", "max_tokens", "temperature"):
        if opts.get(key) is not None:
            chat_opts[key] = opts[key]
    raw = ai.chat_completion(prompt, chat_opts)
    try:
        obj = parse_model_json(raw)
    except ValueError as first_err:
        retry = prompt + ("\n\nYour previous reply was not valid JSON. Return ONLY the JSON "
                          'object, with any double quotes inside strings escaped as \\".')
        raw_retry = ai.chat_completion(retry, chat_opts)
        try:
            obj = parse_model_json(raw_retry)
        except ValueError as retry_err:
            utils.warn(f"unparseable alt-text reply, retry failed too. "
                       f"First ({first_err}): {raw[:RAW_LOG_CHARS]!r} "
                       f"Retry ({retry_err}): {raw_retry[:RAW_LOG_CHARS]!r}")
            return "", None, None, ["ERROR:unparseable-model-output"]
    alt = (obj.get("alt") or "").strip()
    desc = _optional_text(obj.get("description"))
    long_desc = _optional_text(obj.get("long_description"))

    flags = validate_alt(alt, opts)
    if "TOO_LONG" in flags:
        max_chars = opts.get("max_alt_chars", DEFAULT_MAX_ALT_CHARS)
        retry = prompt + (f"\n\nYour previous alt was too long: {alt!r}\n"
                          f"Rewrite it as one sentence under {max_chars} characters. "
                          'Return ONLY {"alt": "...", "description": null, "long_description": null}')
        try:
            shorter = (parse_model_json(ai.chat_completion(retry, chat_opts))
                       .get("alt") or "").strip()
            if shorter:
                alt = shorter
                flags = validate_alt(alt, opts)
        except (ValueError, RuntimeError):
            pass  # keep the long one, flagged for the reviewer
    return alt, desc, long_desc, flags
