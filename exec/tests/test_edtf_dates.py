import pytest

from digtk.lib import edtf_dates


def fields(value, **opts):
    return edtf_dates.compute_fields(edtf_dates.parse(value), opts)


EXPLICIT = {"uncertainty_years": 5, "uncertainty_years_explicit": True}


# --- dams#152 notation table ---------------------------------------------------

@pytest.mark.parametrize("value, display, start, end, precision", [
    ("2020", "2020", 2020, 2020, "exact"),
    ("2020-05", "May 2020", 2020, 2020, "exact"),
    ("2020-05-11", "May 11, 2020", 2020, 2020, "exact"),
    ("2000/2010", "2000–2010", 2000, 2010, "range"),
    ("1962?", "1962 (uncertain)", 1962, 1962, "uncertain"),
    ("1962~", "circa 1962", 1962, 1962, "approximate"),
    ("1962-05~", "circa May 1962", 1962, 1962, "approximate"),
    ("1962%", "circa 1962 (uncertain)", 1962, 1962, "uncertain-approximate"),
    ("196X", "1960s", 1960, 1969, "unspecified-decade"),
    ("19XX", "20th century", 1900, 1999, "unspecified-century"),
    ("1962-XX", "1962", 1962, 1962, "exact"),
])
def test_compute_fields_matches_the_notation_table(value, display, start, end, precision):
    result = fields(value)
    assert result["dateDisplay"] == display
    assert result["yearPublishedStart"] == start
    assert result["yearPublishedEnd"] == end
    assert result["datePrecision"] == precision


def test_year_published_only_for_a_single_year():
    assert fields("2020-05")["yearPublished"] == 2020
    assert fields("2000/2010")["yearPublished"] is None
    assert fields("196X")["yearPublished"] is None


# --- uncertainty window --------------------------------------------------------

def test_default_window_does_not_widen_or_show():
    result = fields("1962~", uncertainty_years=5)
    assert (result["yearPublishedStart"], result["yearPublishedEnd"]) == (1962, 1962)
    assert result["yearPublished"] == 1962
    assert result["dateDisplay"] == "circa 1962"


@pytest.mark.parametrize("value, display", [
    ("1962~", "circa 1962 (±5 years)"),
    ("1962?", "1962 (±5 years)"),
    ("1962%", "circa 1962 (±5 years)"),
])
def test_explicit_window_widens_and_shows(value, display):
    result = fields(value, **EXPLICIT)
    assert (result["yearPublishedStart"], result["yearPublishedEnd"]) == (1957, 1967)
    assert result["yearPublished"] is None
    assert result["dateDisplay"] == display


def test_explicit_window_leaves_exact_dates_alone():
    result = fields("1962", **EXPLICIT)
    assert (result["yearPublishedStart"], result["yearPublishedEnd"]) == (1962, 1962)
    assert result["dateDisplay"] == "1962"


def test_custom_labels():
    assert fields("1962~", approximate_prefix="about")["dateDisplay"] == "about 1962"
    assert fields("1962?", uncertain_suffix="unverified")["dateDisplay"] == "1962 (unverified)"


# --- parse() fallbacks -----------------------------------------------------------

def test_parse_uses_the_first_list_value():
    assert edtf_dates.parse(["1962~", "1970"])["precision"] == "approximate"


def test_parse_falls_back_to_a_year_in_free_text():
    parsed = edtf_dates.parse("circa 1920s")
    assert (parsed["start_year"], parsed["end_year"], parsed["precision"]) == (1920, 1920, "exact")


@pytest.mark.parametrize("value", ["1885/", "/1893", "1885/.."])
def test_parse_treats_open_intervals_as_legacy(value):
    parsed = edtf_dates.parse(value)
    assert parsed["start_year"] == parsed["end_year"]
    assert parsed["precision"] == "exact"


def test_parse_rejects_level_2_expressions():
    parsed = edtf_dates.parse("{1960,1961}")
    assert (parsed["start_year"], parsed["end_year"]) == (1960, 1960)


@pytest.mark.parametrize("value", [None, "", "   ", [], "undated", 1962])
def test_parse_returns_none_without_a_year(value):
    assert edtf_dates.parse(value) is None


def test_ordinal_suffixes():
    assert [edtf_dates._ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 20, 21, 22)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "20th", "21st", "22nd",
    ]
