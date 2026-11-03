"""
digtk.lib.edtf_dates — EDTF date parsing for search year ranges and display.

Ported from dams' services/fin/models/date-utils.js (dc-circa-dates branch,
ucd-library/dams#152). Turns a date value (plain year, ISO date, or an EDTF
Level 0/1 expression like "1962~", "196X" or "2000/2010") into the
yearPublished/yearPublishedStart/yearPublishedEnd/datePrecision/dateDisplay
fields the digital library website searches and displays.
"""
import re

import edtf

DEFAULT_UNCERTAINTY_YEARS = 5
DEFAULT_APPROXIMATE_PREFIX = "circa"
DEFAULT_UNCERTAIN_SUFFIX = "uncertain"

WIDENABLE_PRECISIONS = {"uncertain", "approximate", "uncertain-approximate"}

MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

# python-edtf parses Level 2 too; dams only accepts Level 0/1
LEVEL_0_1_TYPES = {
    "Date", "DateAndTime", "Interval", "Level1Interval",
    "UncertainOrApproximate", "Unspecified", "LongYear", "Season",
}

_COMPONENTS_RE = re.compile(r"^(-?[\dX]{4})(?:-([\dX]{2}))?(?:-([\dX]{2}))?")


def parse(raw):
    """Parses a raw date value into an unwidened year range and the parts
    needed to render it. Falls back to a bare 4-digit year when the value
    isn't valid EDTF Level 0/1 (e.g. legacy free-text dates).

    :param str|list|None raw: A date value; only the first of a list is used.
    :returns: {"start_year", "end_year", "precision", "type", "year", "month",
        "day"} (month/day are None when absent or unspecified), or None if no
        year could be found.
    :rtype: dict|None
    """
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None

    # open-ended intervals ("1885/", "/1893", "1885/..") get bogus bounds
    if "/" in value and any(part in ("", "..") for part in value.split("/")):
        return _legacy_fallback(value)

    try:
        parsed = edtf.parse_edtf(value)
        if type(parsed).__name__ not in LEVEL_0_1_TYPES:
            return _legacy_fallback(value)
        start_year = parsed.lower_strict().tm_year
        end_year = parsed.upper_strict().tm_year
    except Exception:
        # python-edtf raises assorted errors on bad input
        return _legacy_fallback(value)

    if start_year > end_year:
        return _legacy_fallback(value)

    is_interval = isinstance(parsed, edtf.Interval)
    result = {
        "start_year": start_year,
        "end_year": end_year,
        "precision": _precision_for(parsed, value, start_year, end_year),
        "type": "Interval" if is_interval else "Date",
        "year": start_year,
        "month": None,
        "day": None,
    }
    if not is_interval and type(parsed).__name__ != "Season":
        result.update(_components(value))
    return result


def compute_fields(parsed, opts={}):
    """Builds the indexed date fields from parse()'s result. Circa/uncertain
    dates are widened by the uncertainty window only when it was set
    explicitly; the display string always shows the cataloged date.

    :param dict parsed: parse()'s return.
    :param dict opts: 'uncertainty_years' (int, default 5), 'uncertainty_years_explicit'
        (bool — whether it came from an item/collection override), 'approximate_prefix'
        (str, default "circa"), 'uncertain_suffix' (str, default "uncertain").
    :returns: {"yearPublished", "yearPublishedStart", "yearPublishedEnd",
        "datePrecision", "dateDisplay"}; yearPublished is None unless the range
        is a single year.
    :rtype: dict
    """
    start_year = parsed["start_year"]
    end_year = parsed["end_year"]
    precision = parsed["precision"]
    uncertainty_years = opts.get("uncertainty_years")
    if not isinstance(uncertainty_years, (int, float)) or isinstance(uncertainty_years, bool):
        uncertainty_years = DEFAULT_UNCERTAINTY_YEARS

    date_display = _format_display(parsed, uncertainty_years, opts)

    if precision in WIDENABLE_PRECISIONS and opts.get("uncertainty_years_explicit"):
        start_year -= uncertainty_years
        end_year += uncertainty_years

    return {
        "yearPublished": start_year if start_year == end_year else None,
        "yearPublishedStart": start_year,
        "yearPublishedEnd": end_year,
        "datePrecision": precision,
        "dateDisplay": date_display,
    }


def _components(value):
    """
    :param str value: An EDTF date (not an interval).
    :returns: {"month", "day"} as ints, None where absent or unspecified.
    :rtype: dict
    """
    match = _COMPONENTS_RE.match(value)
    month, day = (match.group(2), match.group(3)) if match else (None, None)
    month = int(month) if month and "X" not in month else None
    day = int(day) if month and day and "X" not in day else None
    return {"month": month, "day": day}


def _precision_for(parsed, value, start_year, end_year):
    """
    :param edtf.EDTFObject parsed: Parsed EDTF value.
    :param str value: The raw EDTF string.
    :param int start_year: Unwidened start year.
    :param int end_year: Unwidened end year.
    :returns: The datePrecision keyword.
    :rtype: str
    """
    if isinstance(parsed, edtf.Interval):
        return "range"
    if parsed.is_uncertain_and_approximate:
        return "uncertain-approximate"
    if parsed.is_approximate:
        return "approximate"
    if parsed.is_uncertain:
        return "uncertain"

    year_masked = "X" in value.lstrip("-")[:4]
    if year_masked and start_year != end_year:
        span = end_year - start_year
        if span == 9:
            return "unspecified-decade"
        if span == 99:
            return "unspecified-century"
        return "unspecified-range"
    return "exact"


def _format_display(parsed, uncertainty_years, opts={}):
    """
    :param dict parsed: parse()'s return.
    :param int uncertainty_years: Resolved widening window.
    :param dict opts: Same as compute_fields()'s opts.
    :returns: Human-readable date, e.g. "circa May 1962".
    :rtype: str
    """
    start_year = parsed["start_year"]
    end_year = parsed["end_year"]
    precision = parsed["precision"]

    if parsed["type"] == "Interval" or precision == "unspecified-range":
        return f"{start_year}–{end_year}"
    if precision == "unspecified-decade":
        return f"{start_year}s"
    if precision == "unspecified-century":
        return f"{_ordinal(start_year // 100 + 1)} century"

    if parsed["day"]:
        base = f"{MONTH_NAMES[parsed['month'] - 1]} {parsed['day']}, {parsed['year']}"
    elif parsed["month"]:
        base = f"{MONTH_NAMES[parsed['month'] - 1]} {parsed['year']}"
    else:
        base = f"{parsed['year']}"

    # a real window replaces the generic "uncertain" word
    years_suffix = f"±{uncertainty_years} years" if opts.get("uncertainty_years_explicit") else None
    uncertain_word = years_suffix or opts.get("uncertain_suffix") or DEFAULT_UNCERTAIN_SUFFIX
    prefix = opts.get("approximate_prefix") or DEFAULT_APPROXIMATE_PREFIX

    if precision == "uncertain":
        return f"{base} ({uncertain_word})"
    if precision == "approximate":
        return f"{prefix} {base} ({years_suffix})" if years_suffix else f"{prefix} {base}"
    if precision == "uncertain-approximate":
        return f"{prefix} {base} ({uncertain_word})"
    return base


def _ordinal(n):
    """
    :param int n: A positive integer.
    :returns: n with its English ordinal suffix, e.g. "20th".
    :rtype: str
    """
    suffixes = ["th", "st", "nd", "rd"]
    v = n % 100
    if 10 <= v <= 20:
        return f"{n}th"
    return f"{n}{suffixes[n % 10] if n % 10 < 4 else 'th'}"


def _legacy_fallback(value):
    """
    :param str value: A non-EDTF date string.
    :returns: A single exact year parsed from the first 4-digit run, or None.
    :rtype: dict|None
    """
    match = re.search(r"-?\d{4}", value)
    if not match:
        return None
    year = int(match.group(0))
    return {
        "start_year": year, "end_year": year, "precision": "exact",
        "type": "Date", "year": year, "month": None, "day": None,
    }
