"""Tests for tolerant date reading.

Every misspelling asserted here is one KNOWLEDGE.md records from the live DA index. They are
not invented cases — "Marhc 20, 2025" is what the site actually says.
"""

from datetime import date

import pytest

from presyowatch.sources.dates import read_date

# -- the filename shapes the sources really use ----------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # National DA, all four shapes observed on one page.
        ("Daily-Price-Index-July-24-2026", date(2026, 7, 24)),
        ("July-25-2026-DPI-AFC", date(2026, 7, 25)),
        ("Revised-Daily-Price-Index-May-29-2026", date(2026, 5, 29)),
        ("Daily-Price-Index-July-14-2026-1", date(2026, 7, 14)),
        # Caraga, two shapes in the same series.
        ("Cabadbaran-City-Public-Market_June-24-2026", date(2026, 6, 24)),
        ("April-2026-April-10", date(2026, 4, 10)),
        # Link-text forms.
        ("July 24, 2026", date(2026, 7, 24)),
        ("24 July 2026", date(2026, 7, 24)),
        ("Daily Price Index (July 24, 2026)", date(2026, 7, 24)),
    ],
)
def test_real_world_shapes_are_read(raw: str, expected: str) -> None:
    reading = read_date(raw)

    assert reading.ok, reading.reason
    assert reading.value == expected


def test_cms_dedup_suffix_is_not_mistaken_for_the_day() -> None:
    """`-1` is WordPress deduplicating a filename, not the first of the month.

    Two day-shaped numbers are present; the one touching the month wins.
    """
    reading = read_date("Daily-Price-Index-July-14-2026-1")

    assert reading.value == date(2026, 7, 14)


def test_month_appearing_twice_still_reads_once() -> None:
    """`April-2026-April-10.pdf` is a real Caraga filename."""
    assert read_date("April-2026-April-10").value == date(2026, 4, 10)


# -- typos, all observed live -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected", "corrected"),
    [
        ("JUne 6, 2026", date(2026, 6, 6), False),
        ("Marhc 20, 2025", date(2025, 3, 20), True),
        ("Janauary 19, 2026", date(2026, 1, 19), True),
        ("Setpember 3, 2025", date(2025, 9, 3), True),
        ("Decemebr 1, 2025", date(2025, 12, 1), True),
        ("Febuary 4, 2026", date(2026, 2, 4), True),
        ("Agusut 7, 2026", date(2026, 8, 7), True),
        # A truncation rather than a transposition, but the same kind of error.
        ("Januar 19, 2026", date(2026, 1, 19), True),
    ],
)
def test_misspelled_months_are_repaired(raw: str, expected: date, corrected: bool) -> None:
    """Case differences are not typos; genuine misspellings are flagged as corrections."""
    reading = read_date(raw)

    assert reading.ok, reading.reason
    assert reading.value == expected
    assert reading.month_was_corrected is corrected


def test_a_correction_records_what_it_corrected() -> None:
    """Typo rates are reportable rather than hidden inside a successful parse."""
    reading = read_date("Marhc 20, 2025")

    assert reading.corrected_from == "marhc"


@pytest.mark.parametrize("abbreviation", ["Jan", "Feb", "Sept", "Sep", "Dec", "Aug"])
def test_abbreviations_are_exact_matches_not_corrections(abbreviation: str) -> None:
    reading = read_date(f"{abbreviation} 5, 2026")

    assert reading.ok, reading.reason
    assert reading.month_was_corrected is False


# -- the dangerous direction: matching a month that is not there ------------------


@pytest.mark.parametrize(
    "word",
    [
        "Market",
        "Monitoring",
        "Manila",
        "Municipal",
        "Butuan",
        "Marine",
        "Average",
        # These are the near misses that set the threshold. Each scores 0.600-0.750
        # against some month, and each would be a silent misdating if admitted.
        "Mayor",
        "Major",
        "Maybe",
        "Junk",
    ],
)
def test_words_that_merely_resemble_months_are_not_months(word: str) -> None:
    """A false positive here files a price sheet under the wrong month, silently.

    `Market` appears in nearly every Caraga filename. If fuzzy matching read it as `March`,
    every Cabadbaran sheet would be dated three months wrong and no test elsewhere would
    notice. This is the case that justifies the similarity floor.

    `Mayor` and `Maybe` are the specific pair that forced three-letter month names out of
    fuzzy matching: against `may` they score 0.750, which is indistinguishable from a real
    typo. `Junk` against `june` scores exactly 0.750, which is why the cutoff is 0.78.
    """
    reading = read_date(f"Public {word} 24 2026")

    assert not reading.ok
    assert reading.reason == "no month name found"


def test_market_in_a_real_filename_does_not_shift_the_month() -> None:
    """The same guard, in situ: the month must come from `June`, not from `Market`."""
    reading = read_date("Cabadbaran-City-Public-Market_June-24-2026")

    assert reading.value == date(2026, 6, 24)


# -- refusals: things it must not guess -------------------------------------------


def test_a_date_range_is_refused() -> None:
    """ "Janauary 19-24" is a week of prices. Either end would be a fabrication."""
    reading = read_date("Janauary 19-24, 2026")

    assert not reading.ok
    assert "range" in reading.reason.lower() if reading.reason else False


def test_a_missing_year_is_not_assumed() -> None:
    """Defaulting to the current year would misdate the whole backfill archive."""
    reading = read_date("Daily Price Index July 24")

    assert not reading.ok
    assert reading.reason == "no four-digit year found"


def test_a_missing_day_is_not_assumed() -> None:
    reading = read_date("Weekly Average Prices July 2026")

    assert not reading.ok
    assert reading.reason == "no unambiguous day of month found"


def test_the_number_touching_the_month_wins() -> None:
    """Adjacency resolves what would otherwise be a coin flip between three numbers."""
    reading = read_date("12 report 19 July 2026 summary 20")

    assert reading.value == date(2026, 7, 19), "the token before the month should win"


def test_a_genuinely_ambiguous_day_is_refused() -> None:
    """With nothing adjacent to the month, there is no basis to choose — so do not.

    Picking the first or the largest would be a fabrication dressed up as a heuristic.
    """
    reading = read_date("12 report July 2026 summary 20")

    assert not reading.ok
    assert reading.reason == "no unambiguous day of month found"


def test_an_impossible_date_is_not_rounded() -> None:
    reading = read_date("February 30, 2026")

    assert not reading.ok
    assert reading.reason is not None
    assert "not a real calendar date" in reading.reason


def test_text_with_no_date_is_refused() -> None:
    reading = read_date("Price Monitoring")

    assert not reading.ok
    assert reading.reason == "no month name found"


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_empty_text_is_refused(raw: str) -> None:
    reading = read_date(raw)

    assert not reading.ok
    assert reading.reason == "empty text"


def test_an_out_of_range_year_is_not_a_year() -> None:
    """A four-digit number need not be a year; 1234 in a filename is not 1234 AD."""
    reading = read_date("July 24 1234")

    assert not reading.ok
    assert reading.reason == "no four-digit year found"


@pytest.mark.parametrize("day", ["0", "32", "99"])
def test_impossible_day_numbers_are_not_days(day: str) -> None:
    reading = read_date(f"July {day} 2026")

    assert not reading.ok


# -- formatting tolerance --------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "July 24 2026",
        "July-24-2026",
        "July_24_2026",
        "July.24.2026",
        "July/24/2026",
        "July 24, 2026",
        "(July 24, 2026)",
        "  July   24   2026  ",
        "JULY 24 2026",
        "july 24 2026",
    ],
)
def test_separators_and_case_do_not_matter(raw: str) -> None:
    assert read_date(raw).value == date(2026, 7, 24)


@pytest.mark.parametrize("raw", ["July 24th, 2026", "July 1st, 2026", "July 2nd, 2026"])
def test_ordinal_suffixes_are_tolerated(raw: str) -> None:
    reading = read_date(raw)

    assert reading.ok, reading.reason
    assert reading.value is not None
    assert reading.value.month == 7


def test_leap_day_is_valid_in_a_leap_year() -> None:
    assert read_date("February 29, 2024").value == date(2024, 2, 29)


def test_leap_day_is_refused_in_a_common_year() -> None:
    reading = read_date("February 29, 2025")

    assert not reading.ok
