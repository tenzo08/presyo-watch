"""Shared fixtures.

The robots.txt files under ``fixtures/robots/`` are byte-exact captures taken from the
live hosts on 2026-07-28. They are not hand-written, and they are excluded from the
whitespace-fixing pre-commit hooks and from git's line-ending normalisation so they stay
that way — ``www.da.gov.ph`` really does serve CRLF.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import pytest

from presyowatch.sources.bantay_presyo import ParsedSheet, parse_sheet

FIXTURES = Path(__file__).parent / "fixtures"
ROBOTS_FIXTURES = FIXTURES / "robots"

# Shaped like the real thing, and valid per Settings' rule-7 check.
TEST_USER_AGENT = "PresyoWatch/0.1 (+https://example.invalid; contact: tester@example.ph)"


@pytest.fixture
def robots_text() -> Callable[[str], str]:
    """Return a reader for a captured robots.txt, keyed by host."""

    def read(host: str) -> str:
        path = ROBOTS_FIXTURES / f"{host}.robots.txt"
        if not path.is_file():
            pytest.fail(f"missing robots fixture for {host}: {path}")
        # Decoded rather than read as text so the CRLF bytes survive as far as the
        # parser, which is what happens with a real response body.
        return path.read_bytes().decode("utf-8")

    return read


@dataclass
class FakeClock:
    """A monotonic clock that only advances when something sleeps.

    Lets the rate limiter and backoff be asserted exactly — both the fact that a wait
    happened and how long it was — with no real delay and no flakiness.
    """

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


PDF_FIXTURES = FIXTURES / "pdf"

SHEET_NAMES = (
    "ADN-July-25-2026.pdf",
    "ADS-April-23-2026.pdf",
    "Cabadbaran-City-Public-Market_June-24-2026.pdf",
    "July-2026.xlsx-July-24.pdf",
    "Libertad-Public-Market-July-22-2026.pdf",
    "Libertad-Public-Market_Jan-07-2025.pdf",
    "Luha-Public-Market.xlsx-July-28-2026.pdf",
    "Mayor-Salvador-Calo-July-19-2029.pdf",
    "Mayor-Salvador-Calo-July-23-2026.xlsx.pdf",
    "San-Jose-Public-Market_July-28-2026.pdf",
    "SanFracisco-April-23-2026.pdf",
    "Surigao-City-Public-Market.xlsx-July-24-2026.pdf",
)
"""The committed monitoring sheets the parser can read.

Seven markets across five provinces, fetched from ``caraga.da.gov.ph`` and committed
byte-exact. Chosen for what is wrong with them as much as for coverage:

``Cabadbaran-City-Public-Market_June-24-2026.pdf``
    A collapsed table row: one cell holding three stacked values.
``Mayor-Salvador-Calo-July-19-2029.pdf``
    Filename year is a typo; the sheet's header says 2026.
``Libertad-Public-Market_Jan-07-2025.pdf``
    Same disagreement the other way round, and an older template with 20 groups rather
    than 21 and a different commodity vocabulary.
``Mayor-Salvador-Calo-July-23-2026.xlsx.pdf``
    Five pages, doubled extension, and a header block repeated on every page that
    extracts one line per row instead of as a single cell.
``Libertad-Public-Market-July-22-2026.pdf``
    Header labels truncated by the source's own column width: ``Municipality/Ci:``.
``July-2026.xlsx-July-24.pdf``
    Two pages, and a filename naming neither its market nor its full date.
``ADS-April-23-2026.pdf`` / ``SanFracisco-April-23-2026.pdf``
    The same sheet published under two URLs, byte for byte. Note the source's typo.
"""

UNREADABLE_SHEET_NAME = "Libertad-Public-Market.xlsx-June-3-2026.pdf"
"""A real sheet the parser rejects, committed so that the failure is exercised.

Its table has seven columns rather than eight — no ``Specifications`` — so the column
header does not match and the whole file is quarantined. Supporting that layout would
change what identifies a commodity, which is a decision for TASK.md, not a silent widening
of the parser.
"""


@cache
def load_sheet(name: str) -> ParsedSheet:
    """Parse a fixture sheet, once per session.

    Shared by every test module rather than cached per module: parsing three ruled pages
    takes a second or so, and the pre-commit hook runs the whole suite on every commit.
    Safe to share because ``ParsedSheet`` is frozen.
    """
    return parse_sheet((PDF_FIXTURES / name).read_bytes())
