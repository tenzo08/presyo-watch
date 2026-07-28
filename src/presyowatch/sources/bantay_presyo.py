"""Parser for DA "Bantay Presyo" retail price monitoring PDFs.

The PDFs are text-based, so no OCR (KNOWLEDGE.md). They are also *ruled* tables, which
turns out to matter more than anything else here.

**Why the ruling lines are the answer to the group-label problem.** Read as text, a group
label appears *after* the rows it governs::

    Basmati kg
    Glutinous White kg 54.00 65.00 57.00 59.00
    ...
    IMPORTED
    COMMERCIAL RICE Regular Milled 20-40% Bran Streak kg

The label is one tall cell spanning its whole block, and text extraction emits it wherever
its baseline happens to fall — usually in the middle or at the end. Assigning groups by
reading order would attach every block to the wrong commodities. KNOWLEDGE.md therefore
requires positional extraction, and the strongest positional signal available is the table's
own ruling lines: ``find_tables()`` reconstructs the grid, puts the label in the **first**
row of its span, and gives ``None`` for the continuation rows underneath it. Group
assignment is then a forward fill, and the geometry has been done by something better at it
than a y-coordinate heuristic would be.

The distinction between ``None`` and ``""`` in an extracted cell is load-bearing:

``None``
    There is no cell here — it is covered by a vertical span above. Means "same group".
``""``
    A real, empty cell. In a price column that means the commodity was not monitored.

**The PDF's own date wins over the filename.** ``Mayor-Salvador-Calo-July-19-2029.pdf``
contains ``Date of Monitoring : July 19, 2026`` (verified 2026-07-28). The filename year is
a typo and the header is right, so the header is what is trusted. The index scraper's date
is for *discovery* — deciding what to fetch — and this one is the *observation* date.

**Blank is not zero.** The sheets say so themselves, in a footer row inside the table:
"Note: Blank items means commodities are not available during monitoring". A row with no
figures at all is recorded with ``unavailable=True`` and every price ``None``.
"""

import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

import pdfplumber
from pydantic import BaseModel, ConfigDict, model_validator

from presyowatch.log import get_logger
from presyowatch.sources.dates import read_date

logger = get_logger(__name__)

EXPECTED_COLUMNS: Final = (
    "commodity group",
    "commodities",
    "specifications",
    "unit",
    "low",
    "high",
    "prevailing",
    "average",
)
"""The header row, lower-cased. Verified rather than assumed, so a sheet with a different
column order is quarantined instead of being read with the columns silently transposed."""

_GROUP, _COMMODITY, _SPECIFICATION, _UNIT = 0, 1, 2, 3
_PRICE_COLUMNS: Final = (4, 5, 6, 7)

AGRICULTURAL_INPUT_GROUPS: Final = frozenset(
    {
        "LIVESTOCK & POULTRY FEEDS",
        "FERTILIZER",
        "INSECTICIDE",
        "HERBICIDE",
        "FUNGICIDE",
        "MOLLUSCIDE",
        "RODENTICIDE",
    }
)
"""Groups that are farm inputs, not food.

KNOWLEDGE.md requires this to be an explicit decision rather than an accident. They are
parsed and kept — silently dropping published data is not acceptable — but flagged, so a
"food prices" chart can exclude them instead of quietly averaging fungicide into the cost
of rice.
"""

_UNIT_ALIASES: Final = {
    "kg": "kg",
    "kilo": "kg",
    "kilogram": "kg",
    "g": "g",
    "gram": "g",
    "grams": "g",
    "l": "L",
    "liter": "L",
    "litre": "L",
    "liters": "L",
    "ml": "mL",
    "pc": "pc",
    "pcs": "pc",
    "piece": "pc",
    "pieces": "pc",
    "box": "box",
    "bag": "bag",
    "sack": "sack",
    "tray": "tray",
    "bundle": "bundle",
}
"""Casing and pluralisation vary between sheets (``L``, ``Liter``, ``kg``, ``Kg``).
Unrecognised units are kept verbatim rather than dropped — an unknown unit is still data,
and mapping it is the commodity resolver's job, not the PDF reader's."""

_HEADER_FIELDS: Final = {
    "province": re.compile(r"^\s*Province\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE),
    "municipality": re.compile(r"^\s*Municipality/City\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE),
    "market": re.compile(r"^\s*Market Monitored\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE),
    "observed_on": re.compile(r"^\s*Date of Monitoring\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE),
}

_FOOTER_NOTE = re.compile(r"^\s*note\s*:", re.IGNORECASE)
_THOUSANDS = re.compile(r",")
_AMOUNT = re.compile(r"^\d+(?:\.\d+)?$")


class SheetParseError(Exception):
    """The document as a whole could not be read, so no row from it can be trusted.

    Distinct from a rejected row: this quarantines the *file*.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class SheetHeader(BaseModel):
    """Where and when the monitoring happened, taken from the sheet's own header."""

    model_config = ConfigDict(frozen=True)

    province: str
    municipality: str
    market: str
    observed_on: date


class PriceRow(BaseModel):
    """One commodity's prices on one sheet.

    The validators deliberately mirror the ``price_observations`` check constraints. A row
    that the database would refuse is caught here and quarantined with a reason, rather
    than failing an insert halfway through a run.
    """

    model_config = ConfigDict(frozen=True)

    group: str
    commodity: str
    specification: str | None
    unit: str
    low: Decimal | None
    high: Decimal | None
    prevailing: Decimal | None
    average: Decimal | None
    unavailable: bool
    is_agricultural_input: bool

    @model_validator(mode="after")
    def _check_range(self) -> "PriceRow":
        if self.low is not None and self.high is not None and self.low > self.high:
            msg = f"low {self.low} is above high {self.high}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_unavailable(self) -> "PriceRow":
        prices = (self.low, self.high, self.prevailing, self.average)
        if self.unavailable and any(price is not None for price in prices):
            msg = "row is marked unavailable but carries prices"
            raise ValueError(msg)
        return self


@dataclass(frozen=True, slots=True)
class RejectedRow:
    """A table row that could not be turned into a :class:`PriceRow`."""

    cells: tuple[str | None, ...]
    reason: str
    group: str | None


@dataclass(frozen=True, slots=True)
class ParsedSheet:
    """Everything one PDF yielded."""

    header: SheetHeader
    rows: tuple[PriceRow, ...]
    rejected: tuple[RejectedRow, ...]
    page_count: int

    @property
    def unavailable_count(self) -> int:
        """Commodities listed but not monitored. A data quality figure, not an error."""
        return sum(1 for row in self.rows if row.unavailable)


def _collapse(text: str | None) -> str:
    """Collapse all whitespace, including the newlines inside a wrapped cell."""
    return " ".join((text or "").split())


def normalise_unit(raw: str) -> str:
    """Return a canonical unit for ``raw``, or ``raw`` stripped if unrecognised."""
    collapsed = _collapse(raw)
    return _UNIT_ALIASES.get(collapsed.lower(), collapsed)


def _read_amount(cell: str | None) -> tuple[Decimal | None, str | None]:
    """Return ``(value, error)`` for one price cell.

    Blank is a value — ``(None, None)`` — because "not monitored" is information. A cell
    holding several stacked numbers is not: sheets exist where the grid collapsed and one
    cell contains ``1,865.00\\n1,805.00\\n40.00``, and there is no way to tell which figure
    belongs to this commodity, so the row is rejected rather than guessed at.
    """
    if cell is None or not cell.strip():
        return None, None

    lines = [line.strip() for line in cell.splitlines() if line.strip()]
    if len(lines) > 1:
        joined = " / ".join(lines)
        return None, f"several values in one cell ({joined})"

    text = _THOUSANDS.sub("", lines[0])
    if not _AMOUNT.match(text):
        return None, f"not a number ({lines[0]!r})"
    # No try/except: `_AMOUNT` admits only plain digit strings, for which Decimal cannot
    # fail. Guarding it anyway would be unreachable code pretending to be caution.
    return Decimal(text), None


def _parse_header(text: str) -> SheetHeader:
    """Read the province, municipality, market, and date from a page's text.

    Raises:
        SheetParseError: If any field is missing or the date cannot be read.
    """
    found: dict[str, str] = {}
    for field, pattern in _HEADER_FIELDS.items():
        match = pattern.search(text)
        if match is None:
            msg = f"sheet header has no {field!r} line"
            raise SheetParseError(msg)
        found[field] = _collapse(match.group("value"))

    reading = read_date(found["observed_on"])
    if reading.value is None:
        msg = f"unreadable Date of Monitoring {found['observed_on']!r}: {reading.reason}"
        raise SheetParseError(msg)

    return SheetHeader(
        province=found["province"],
        municipality=found["municipality"],
        market=found["market"],
        observed_on=reading.value,
    )


def _is_column_header(cells: tuple[str | None, ...]) -> bool:
    """Return whether ``cells`` is the table's column header row."""
    labels = tuple(_collapse(cell).lower() for cell in cells[: len(EXPECTED_COLUMNS)])
    return labels == EXPECTED_COLUMNS


def _extract_rows(pdf: Any) -> list[tuple[str | None, ...]]:  # noqa: ANN401
    """Return every table row across every page, in order.

    ``pdf`` is annotated ``Any`` because ``pdfplumber`` ships no type information; naming a
    type it does not export would be a fiction mypy could not check.

    The table continues across pages without repeating its header, so the pages are simply
    concatenated and a group span may legitimately continue from one page onto the next.
    """
    rows: list[tuple[str | None, ...]] = []
    for page in pdf.pages:
        tables = page.find_tables()
        if not tables:
            continue
        # The sheets carry exactly one table per page; the widest is the right one if a
        # stray line ever produces more.
        widest = max(tables, key=lambda table: len(table.rows))
        rows.extend(tuple(row) for row in widest.extract())
    return rows


def parse_sheet(pdf_bytes: bytes) -> ParsedSheet:
    """Parse a Bantay Presyo monitoring sheet.

    Takes bytes rather than a path so it can be driven straight from the content-addressed
    cache or from a committed fixture, with no filesystem assumptions and no network.

    Args:
        pdf_bytes: The PDF exactly as the source served it.

    Returns:
        The header, the rows understood, and the rows that were not.

    Raises:
        SheetParseError: If the document cannot be read at all — not a PDF, no table, no
            recognisable column header, or an unusable header block. The caller should
            quarantine the whole file.
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page_count = len(pdf.pages)
            first_page_text = pdf.pages[0].extract_text() or "" if page_count else ""
            table_rows = _extract_rows(pdf)
    except Exception as exc:
        # Deliberately broad: pdfplumber and its pdfminer backend raise an assorted and
        # undocumented set of errors on malformed input — and every one of them means the
        # same thing to us. Re-raised as our own type with the cause attached, never
        # swallowed.
        msg = f"could not open PDF: {type(exc).__name__}: {exc}"
        raise SheetParseError(msg) from exc

    if page_count == 0:  # pragma: no cover - needs a valid PDF declaring zero pages
        msg = "PDF has no pages"
        raise SheetParseError(msg)
    if not table_rows:
        msg = "no table found; the sheet may be a scan or a different layout"
        raise SheetParseError(msg)

    header = _parse_header(first_page_text)
    rows, rejected = _read_body(table_rows)

    logger.info(
        "sheet_parsed",
        market=header.market,
        observed_on=header.observed_on.isoformat(),
        pages=page_count,
        rows=len(rows),
        rejected=len(rejected),
        unavailable=sum(1 for row in rows if row.unavailable),
    )
    return ParsedSheet(
        header=header,
        rows=tuple(rows),
        rejected=tuple(rejected),
        page_count=page_count,
    )


def _read_body(
    table_rows: list[tuple[str | None, ...]],
) -> tuple[list[PriceRow], list[RejectedRow]]:
    """Turn raw table rows into price rows, carrying the group forward across spans."""
    rows: list[PriceRow] = []
    rejected: list[RejectedRow] = []
    group: str | None = None
    seen_column_header = False

    for cells in table_rows:
        if len(cells) < len(EXPECTED_COLUMNS):
            # The metadata block above the table extracts as a short row.
            continue
        if _is_column_header(cells):
            seen_column_header = True
            continue

        label = _collapse(cells[_GROUP])
        commodity = _collapse(cells[_COMMODITY])

        if _FOOTER_NOTE.match(label):
            # The "blank means not available" note is itself a row of the table.
            continue
        if label and not commodity:
            # The header block: one tall cell with no commodity beside it.
            continue
        if label:
            group = label
        if not commodity:
            continue

        resolved = _build_row(cells, group=group)
        if isinstance(resolved, PriceRow):
            rows.append(resolved)
        else:
            rejected.append(resolved)

    if not seen_column_header:
        msg = f"no column header row matching {EXPECTED_COLUMNS}"
        raise SheetParseError(msg)
    return rows, rejected


def _build_row(cells: tuple[str | None, ...], *, group: str | None) -> PriceRow | RejectedRow:
    """Build one :class:`PriceRow`, or explain why the row cannot be trusted."""
    if group is None:
        return RejectedRow(
            cells=cells,
            reason="row appears before any commodity group heading",
            group=None,
        )

    amounts: list[Decimal | None] = []
    for column in _PRICE_COLUMNS:
        value, error = _read_amount(cells[column])
        if error is not None:
            return RejectedRow(cells=cells, reason=error, group=group)
        amounts.append(value)

    unit = normalise_unit(cells[_UNIT] or "")
    if not unit:
        return RejectedRow(cells=cells, reason="row has no unit", group=group)

    specification = _collapse(cells[_SPECIFICATION]) or None
    if specification is not None and specification.casefold() == unit.casefold():
        # `Porkchop | kg | kg` — the unit is repeated into the specification column on
        # rows where no real specification was recorded (KNOWLEDGE.md).
        specification = None

    low, high, prevailing, average = amounts
    try:
        return PriceRow(
            group=group,
            commodity=_collapse(cells[_COMMODITY]),
            specification=specification,
            unit=unit,
            low=low,
            high=high,
            prevailing=prevailing,
            average=average,
            unavailable=all(amount is None for amount in amounts),
            is_agricultural_input=group.upper() in AGRICULTURAL_INPUT_GROUPS,
        )
    except ValueError as exc:
        # Pydantic's validators mirror the database's check constraints, so anything the
        # database would reject is quarantined here with the reason instead.
        return RejectedRow(cells=cells, reason=f"failed validation: {exc}", group=group)
