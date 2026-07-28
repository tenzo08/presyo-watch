"""Tolerant extraction of a monitoring date from human-written text.

The sources' link text and filenames are typed by hand and are full of real errors.
Observed in the live DA index (KNOWLEDGE.md): ``"JUne 6, 2026"``, ``"Marhc 20, 2025"``,
``"Janauary 19-24"``. Filenames vary just as much: ``Daily-Price-Index-July-24-2026.pdf``,
``July-25-2026-DPI-AFC.pdf``, ``April-2026-April-10.pdf``.

So this is deliberately forgiving about *spelling* and strict about *meaning*. It will
repair a mistyped month name, but it will not guess a missing year, will not pick a date out
of a range, and will not decide which of two plausible numbers is the day. Anything it
cannot read unambiguously is reported as a failure with a reason, so the caller can
quarantine it with the evidence attached rather than inventing a value.

**Why fuzzy matching is bounded, and where the threshold comes from.** A loose match is worse
than no match: silently reading ``Market`` as ``March`` would file every Cabadbaran price
sheet three months wrong, and no other test would notice. So fuzzy matching runs only after
exact matching fails, and the bounds were measured rather than guessed
(``difflib.SequenceMatcher`` ratios, 2026-07-28):

===================  =======  ==================  =======
Real typo            Ratio    Not a month         Ratio
===================  =======  ==================  =======
``janauary``           0.933  ``mayor``             0.600
``febuary``            0.933  ``major``             0.600
``januar``             0.923  ``market``            0.545
``setpember``          0.889  ``manila``            0.545
``decemebr``           0.875  ``marine``            0.545
``agusut``             0.833  ``butuan``            0.429
``marhc``              0.800  ``average``           0.400
===================  =======  ==================  =======

Genuine typos cluster at 0.80 and above, unrelated words at 0.60 and below, so the cutoff
sits in the gap at 0.78.

That clean separation only exists because **three-letter month names are excluded from fuzzy
matching**. Allowing ``may`` as a target scored ``mayor`` and ``maybe`` at 0.750, which is
indistinguishable from a real typo — the short name is the entire problem. ``May`` is also
the month least likely to be misspelled, so nothing is really lost. For the same reason the
cutoff is 0.78 rather than 0.75: ``junk``/``june`` scores exactly 0.750.
"""

import difflib
import re
from dataclasses import dataclass
from datetime import date

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_MONTH_ABBREVIATIONS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_MONTH_SIMILARITY_CUTOFF = 0.78
"""Below this, a token is not treated as a misspelled month. Measured; see the docstring."""

_MAX_MONTH_LENGTH_DELTA = 2
"""A token far from any month in length is a different word, not a typo."""

_MIN_FUZZY_LENGTH = 4
"""Short tokens are too easy to match by accident to be worth repairing."""

_MIN_FUZZY_MONTH_LENGTH = 4
"""``may`` is too short to fuzzy-match safely: ``mayor`` and ``maybe`` both score 0.750."""

_EARLIEST_YEAR = 1990
_LATEST_YEAR = 2100
_MAX_DAY = 31
_YEAR_DIGITS = 4
_RANGE_TOKEN_COUNT = 2
"""A month followed by exactly two day-shaped numbers reads as a span, not a date."""

# Separators that appear between date parts in link text and filenames alike.
_SEPARATORS = re.compile(r"[-_/.,()\[\]:;|+]+")
_ORDINAL = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)$")
_DIGITS = re.compile(r"^\d+$")


@dataclass(frozen=True, slots=True)
class DateReading:
    """The result of trying to read a date out of a string.

    Exactly one of ``value`` and ``reason`` is set. A failure is a normal outcome, not an
    exception: the caller quarantines it and carries on with the rest of the index.
    """

    value: date | None = None
    reason: str | None = None
    month_was_corrected: bool = False
    """True when the month name had to be repaired, e.g. ``Marhc`` to ``March``."""

    corrected_from: str | None = None
    """The misspelled token, kept so typo rates can be reported rather than hidden."""

    @property
    def ok(self) -> bool:
        return self.value is not None


def _tokenise(raw: str) -> list[str]:
    """Split ``raw`` into lower-case tokens, treating punctuation as whitespace."""
    return _SEPARATORS.sub(" ", raw).lower().split()


def _month_from_token(token: str) -> tuple[int | None, bool]:
    """Return ``(month_number, was_corrected)`` for ``token``.

    Exact matches, including common abbreviations, are tried first. Only then is a
    bounded fuzzy match attempted.
    """
    if token in _MONTHS:
        return _MONTHS[token], False
    if token in _MONTH_ABBREVIATIONS:
        return _MONTH_ABBREVIATIONS[token], False

    if len(token) < _MIN_FUZZY_LENGTH or not token.isalpha():
        return None, False

    plausible = [
        name
        for name in _MONTHS
        if len(name) >= _MIN_FUZZY_MONTH_LENGTH
        and abs(len(name) - len(token)) <= _MAX_MONTH_LENGTH_DELTA
    ]
    close = difflib.get_close_matches(token, plausible, n=1, cutoff=_MONTH_SIMILARITY_CUTOFF)
    if not close:
        return None, False
    return _MONTHS[close[0]], True


def _as_day(token: str) -> int | None:
    """Return ``token`` as a day of the month, tolerating an ordinal suffix."""
    ordinal = _ORDINAL.match(token)
    text = ordinal.group(1) if ordinal else token
    if not _DIGITS.match(text):
        return None
    value = int(text)
    return value if 1 <= value <= _MAX_DAY else None


def _as_year(token: str) -> int | None:
    if not _DIGITS.match(token) or len(token) != _YEAR_DIGITS:
        return None
    value = int(token)
    return value if _EARLIEST_YEAR <= value <= _LATEST_YEAR else None


def _looks_like_a_range(tokens: list[str], month_index: int) -> bool:
    """Return whether the tokens after the month read as a span of days.

    ``"Janauary 19-24"`` normalises to ``january 19 24``: two day-shaped numbers in a row
    with no year between them. That is a week's worth of prices, not one day's, and
    choosing either end would be a guess.
    """
    after = tokens[month_index + 1 : month_index + 3]
    return len(after) == _RANGE_TOKEN_COUNT and all(_as_day(token) is not None for token in after)


def read_date(raw: str) -> DateReading:
    """Read a single calendar date out of ``raw``.

    Args:
        raw: Link text or a filename stem. Pass a filename *without* its directories —
            a path like ``/PriceMonitoring/FY2025/june/...`` carries a fiscal year and a
            directory month that both contradict the filename (KNOWLEDGE.md).

    Returns:
        A :class:`DateReading`, successful or with the reason it is not.
    """
    if not raw.strip():
        return DateReading(reason="empty text")

    tokens = _tokenise(raw)

    month: int | None = None
    month_index = -1
    corrected = False
    corrected_from: str | None = None
    for index, token in enumerate(tokens):
        candidate, was_corrected = _month_from_token(token)
        if candidate is not None:
            month, month_index = candidate, index
            corrected = was_corrected
            corrected_from = token if was_corrected else None
            break

    if month is None:
        return DateReading(reason="no month name found")

    if _looks_like_a_range(tokens, month_index):
        return DateReading(reason="looks like a date range, which is not a single observation date")

    years = [(index, _as_year(t)) for index, t in enumerate(tokens)]
    year_matches = [(index, value) for index, value in years if value is not None]
    if not year_matches:
        return DateReading(reason="no four-digit year found")
    year_index, year = year_matches[0]

    day = _pick_day(tokens, month_index=month_index, year_index=year_index)
    if day is None:
        return DateReading(reason="no unambiguous day of month found")

    try:
        value = date(year, month, day)
    except ValueError as exc:
        # e.g. "February 30". The source wrote something impossible; do not round it.
        return DateReading(reason=f"not a real calendar date: {exc}")

    return DateReading(
        value=value,
        month_was_corrected=corrected,
        corrected_from=corrected_from,
    )


def _pick_day(tokens: list[str], *, month_index: int, year_index: int) -> int | None:
    """Return the day of the month, or ``None`` if it cannot be chosen confidently.

    Adjacency decides it. ``Daily-Price-Index-July-14-2026-1.pdf`` contains two day-shaped
    numbers, ``14`` and the CMS's ``-1`` deduplication suffix; the one touching the month is
    the real day. Where nothing is adjacent and more than one candidate remains, the caller
    is told rather than being handed a coin flip.
    """
    candidates = {
        index: day
        for index, token in enumerate(tokens)
        if index != year_index and (day := _as_day(token)) is not None
    }
    if not candidates:
        return None

    for neighbour in (month_index + 1, month_index - 1):
        if neighbour in candidates:
            return candidates[neighbour]

    if len(candidates) == 1:
        return next(iter(candidates.values()))
    return None
