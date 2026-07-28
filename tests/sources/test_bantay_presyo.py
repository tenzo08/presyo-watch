"""Tests for the Bantay Presyo PDF parser.

Runs against four real sheets fetched from ``caraga.da.gov.ph`` on 2026-07-28 and committed
byte-exact: four different markets across four provinces, one of them with a typo'd year in
its filename and one with a collapsed table row. Synthetic PDFs would exercise none of that.

The pure inner functions are also tested directly. ``parse_header`` takes text and
``read_body`` takes extracted cells, so the branch behaviour — forward filling a group,
rejecting an unreadable cell — can be asserted without hand-building a PDF, which nothing in
the dependency set can do.
"""

from decimal import Decimal
from functools import cache
from pathlib import Path

import pytest

from presyowatch.sources.bantay_presyo import (
    AGRICULTURAL_INPUT_GROUPS,
    EXPECTED_COLUMNS,
    ParsedSheet,
    PriceRow,
    RejectedRow,
    SheetParseError,
    _extract_rows,
    _parse_header,
    _read_body,
    normalise_unit,
    parse_sheet,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "pdf"

CABADBARAN = "Cabadbaran-City-Public-Market_June-24-2026.pdf"
LUHA = "Luha-Public-Market.xlsx-July-28-2026.pdf"
MAYOR_CALO = "Mayor-Salvador-Calo-July-19-2029.pdf"
SAN_JOSE = "San-Jose-Public-Market_July-28-2026.pdf"
ALL_SHEETS = (CABADBARAN, LUHA, MAYOR_CALO, SAN_JOSE)

HEADER_ROW: tuple[str | None, ...] = (
    "Commodity Group",
    "Commodities",
    "Specifications",
    "Unit",
    "LOW",
    "HIGH",
    "PREVAILING",
    "AVERAGE",
)


@cache
def load(name: str) -> ParsedSheet:
    """Parse a fixture, once per session.

    Parsing three pages of ruled table takes a few seconds, and these tests are run by the
    pre-commit hook on every commit. Cached because ``ParsedSheet`` is frozen, so no test
    can contaminate another through it.
    """
    return parse_sheet((FIXTURES / name).read_bytes())


@pytest.fixture(scope="module")
def cabadbaran() -> ParsedSheet:
    return load(CABADBARAN)


def data_row(
    commodity: str,
    *,
    group: str | None = None,
    specification: str = "",
    unit: str = "kg",
    low: str = "",
    high: str = "",
    prevailing: str = "",
    average: str = "",
) -> tuple[str | None, ...]:
    """Build one extracted table row. ``group=None`` is a merged-cell continuation."""
    return (group, commodity, specification, unit, low, high, prevailing, average)


# -- the header block -------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "market", "municipality", "province"),
    [
        (CABADBARAN, "Cabadbaran City Public Market", "Cabadbaran City", "Agusan del Norte"),
        (LUHA, "Luha Public Market", "Tandag City", "Surigao del Sur"),
        (MAYOR_CALO, "Mayor Salvador Calo Public Market", "Butuan City", "Agusan Del Norte"),
        (SAN_JOSE, "San Jose Public Market", "San Jose", "Province of Dinagat Islands"),
    ],
)
def test_header_identifies_where_monitoring_happened(
    name: str, market: str, municipality: str, province: str
) -> None:
    header = load(name).header

    assert header.market == market
    assert header.municipality == municipality
    assert header.province == province


def test_the_sheets_own_date_beats_the_filename() -> None:
    """`Mayor-Salvador-Calo-July-19-2029.pdf` says `Date of Monitoring : July 19, 2026`.

    The filename year is a typo; the sheet's header is right. This is the reason the parser
    treats the header as authoritative and the index scraper's date as provisional — good
    only for deciding what to fetch. Rejecting this file on its filename year would have
    discarded a perfectly valid 2026 sheet.
    """
    header = load(MAYOR_CALO).header

    assert header.observed_on.isoformat() == "2026-07-19"
    assert MAYOR_CALO.endswith("2029.pdf"), "the fixture's filename still claims 2029"


@pytest.mark.parametrize(
    ("name", "expected"),
    [(CABADBARAN, "2026-06-24"), (LUHA, "2026-07-28"), (SAN_JOSE, "2026-07-28")],
)
def test_dates_are_read_from_the_header(name: str, expected: str) -> None:
    assert load(name).header.observed_on.isoformat() == expected


def test_header_text_is_parsed_field_by_field() -> None:
    header = _parse_header(
        "Bantay Presyo Monitoring\n"
        "Province :  Agusan del Norte \n"
        "Municipality/City : Cabadbaran City\n"
        "Market Monitored : Cabadbaran City Public Market\n"
        "Date of Monitoring : June 24, 2026\n"
    )

    assert header.province == "Agusan del Norte"
    assert header.observed_on.isoformat() == "2026-06-24"


@pytest.mark.parametrize(
    "missing",
    ["Province", "Municipality/City", "Market Monitored", "Date of Monitoring"],
)
def test_a_missing_header_field_fails_the_whole_file(missing: str) -> None:
    """Without knowing which market or day this is, no row from it means anything."""
    lines = [
        "Province : Agusan del Norte",
        "Municipality/City : Cabadbaran City",
        "Market Monitored : Cabadbaran City Public Market",
        "Date of Monitoring : June 24, 2026",
    ]
    text = "\n".join(line for line in lines if not line.startswith(missing))

    with pytest.raises(SheetParseError, match="header has no"):
        _parse_header(text)


def test_an_unreadable_monitoring_date_fails_the_whole_file() -> None:
    with pytest.raises(SheetParseError, match="unreadable Date of Monitoring"):
        _parse_header(
            "Province : X\nMunicipality/City : Y\nMarket Monitored : Z\n"
            "Date of Monitoring : sometime last week\n"
        )


# -- group labels, the reason positional extraction is required -------------------


def test_the_group_label_reaches_the_rows_above_it(cabadbaran: ParsedSheet) -> None:
    """Read as text, `IMPORTED COMMERCIAL RICE` appears *after* Basmati and Glutinous.

    Its cell spans the whole block and the extractor emits the label wherever its baseline
    falls. Reading order would attach these rows to the previous group, or to nothing. The
    ruling lines put the label at the top of its span, so a forward fill is correct.
    """
    basmati = next(row for row in cabadbaran.rows if row.commodity == "Basmati")

    assert basmati.group == "IMPORTED COMMERCIAL RICE"


def test_multi_line_group_labels_are_collapsed(cabadbaran: ParsedSheet) -> None:
    """The label is `IMPORTED\\nCOMMERCIAL RICE` in the cell; one space, not a newline."""
    groups = {row.group for row in cabadbaran.rows}

    assert "IMPORTED COMMERCIAL RICE" in groups
    assert not any("\n" in group for group in groups)


def test_every_row_belongs_to_a_group(cabadbaran: ParsedSheet) -> None:
    assert all(row.group for row in cabadbaran.rows)


@pytest.mark.parametrize("name", ALL_SHEETS)
def test_all_twenty_one_groups_are_found(name: str) -> None:
    """Every sheet carries the same 21 groups; a drop would mean the fill broke."""
    assert len({row.group for row in load(name).rows}) == 21


def test_a_group_carries_forward_across_a_page_boundary() -> None:
    """Pages 2 and 3 have no header row, so a span may continue onto the next page."""
    rows, rejected = _read_body(
        [
            HEADER_ROW,
            data_row("Basmati", group="IMPORTED COMMERCIAL RICE", average="50.00"),
            # page break: the next page opens mid-span, group cell absent
            data_row("Glutinous", group=None, average="60.00"),
        ]
    )

    assert rejected == []
    assert [row.group for row in rows] == ["IMPORTED COMMERCIAL RICE"] * 2


def test_a_row_before_any_group_is_rejected_not_guessed() -> None:
    rows, rejected = _read_body([HEADER_ROW, data_row("Orphan", group=None, average="1.00")])

    assert rows == []
    assert "before any commodity group" in rejected[0].reason


# -- blanks: not zero, not a parse failure ---------------------------------------


@pytest.mark.parametrize("name", ALL_SHEETS)
def test_unmonitored_commodities_are_recorded_as_unavailable(name: str) -> None:
    """The sheets' own footer: "Blank items means commodities are not available"."""
    sheet = load(name)

    assert sheet.unavailable_count > 40, "these sheets always have many blanks"
    for row in sheet.rows:
        if row.unavailable:
            assert (row.low, row.high, row.prevailing, row.average) == (None, None, None, None)


def test_a_blank_price_is_never_zero(cabadbaran: ParsedSheet) -> None:
    """Zero would claim the commodity was free, which is a different and false statement."""
    assert not any(
        value == Decimal(0)
        for row in cabadbaran.rows
        for value in (row.low, row.high, row.prevailing, row.average)
        if value is not None
    )


def test_a_partly_blank_row_is_kept_and_not_marked_unavailable(
    cabadbaran: ParsedSheet,
) -> None:
    """Some figures published and some missing is a real state, distinct from "not monitored".

    In this sheet it is a side effect of the collapsed row below: `Layer 2` lost its LOW to
    the cell that swallowed three values.
    """
    layer_two = next(row for row in cabadbaran.rows if row.commodity == "Layer 2")

    assert layer_two.unavailable is False
    assert layer_two.low is None
    assert layer_two.high == Decimal("1805.00")


# -- numbers ---------------------------------------------------------------------


def test_prices_are_exact_decimals(cabadbaran: ParsedSheet) -> None:
    premium = next(
        row
        for row in cabadbaran.rows
        if row.commodity == "Premium" and row.group == "IMPORTED COMMERCIAL RICE"
    )

    assert premium.low == Decimal("48.00")
    assert premium.average == Decimal("52.00")
    assert isinstance(premium.average, Decimal)


def test_thousands_separators_are_handled(cabadbaran: ParsedSheet) -> None:
    """Feed prices run into the thousands and are written `1,791.00`."""
    booster = next(row for row in cabadbaran.rows if row.commodity == "Hog Booster")

    assert booster.average == Decimal("1791.00")


def test_a_cell_holding_several_values_is_rejected(cabadbaran: ParsedSheet) -> None:
    """One real row's grid collapsed and a single cell holds `1,865.00 1,805.00 40.00`.

    There is no way to know which figure belongs to this commodity, so the row is
    quarantined. Taking the first would be a coin flip presented as data.
    """
    collapsed = [row for row in cabadbaran.rejected if "several values" in row.reason]

    assert len(collapsed) == 1
    assert collapsed[0].group == "LIVESTOCK & POULTRY FEEDS"
    assert "1,865.00" in collapsed[0].reason


def test_a_non_numeric_price_is_rejected() -> None:
    rows, rejected = _read_body([HEADER_ROW, data_row("Mystery", group="FISH", low="n/a")])

    assert rows == []
    assert "not a number" in rejected[0].reason


def test_low_above_high_is_rejected() -> None:
    """Mirrors the database's check constraint, so the run fails at parse time not insert."""
    rows, rejected = _read_body(
        [HEADER_ROW, data_row("Backwards", group="FISH", low="99.00", high="10.00")]
    )

    assert rows == []
    assert "low 99.00 is above high 10.00" in rejected[0].reason


@pytest.mark.parametrize("name", ALL_SHEETS)
def test_low_never_exceeds_high_in_any_accepted_row(name: str) -> None:
    for row in load(name).rows:
        if row.low is not None and row.high is not None:
            assert row.low <= row.high


# -- units and specifications ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("kg", "kg"),
        ("Kg", "kg"),
        ("KG", "kg"),
        ("Liter", "L"),
        ("liter", "L"),
        ("L", "L"),
        ("ml", "mL"),
        ("gram", "g"),
        ("pcs", "pc"),
        ("piece", "pc"),
        (" box ", "box"),
        ("Sack", "sack"),
    ],
)
def test_units_are_normalised(raw: str, expected: str) -> None:
    assert normalise_unit(raw) == expected


def test_an_unknown_unit_is_kept_verbatim() -> None:
    """An unrecognised unit is still data; mapping it belongs to the commodity resolver."""
    assert normalise_unit("ganta") == "ganta"


@pytest.mark.parametrize("name", ALL_SHEETS)
def test_every_row_has_a_unit(name: str) -> None:
    assert all(row.unit for row in load(name).rows)


def test_units_in_the_fixtures_are_all_canonical(cabadbaran: ParsedSheet) -> None:
    assert {row.unit for row in cabadbaran.rows} <= {"kg", "g", "L", "mL", "pc", "box", "bag"}


def test_a_unit_repeated_into_the_specification_column_is_dropped(
    cabadbaran: ParsedSheet,
) -> None:
    """`Porkchop | kg | kg` — the sheets duplicate the unit where no spec was recorded.

    Storing "kg" as a specification would make it look like a meaningful distinction from
    the same commodity recorded without one.
    """
    porkchop = next(row for row in cabadbaran.rows if row.commodity == "Porkchop")

    assert porkchop.specification is None
    assert porkchop.unit == "kg"


def test_a_real_specification_is_kept(cabadbaran: ParsedSheet) -> None:
    premium = next(
        row
        for row in cabadbaran.rows
        if row.commodity == "Premium" and row.group == "IMPORTED COMMERCIAL RICE"
    )

    assert premium.specification == "5% Broken"


def test_a_row_without_a_unit_is_rejected() -> None:
    rows, rejected = _read_body([HEADER_ROW, data_row("Nameless", group="FISH", unit="")])

    assert rows == []
    assert rejected[0].reason == "row has no unit"


# -- food versus farm input ------------------------------------------------------


def test_farm_inputs_are_flagged_not_dropped(cabadbaran: ParsedSheet) -> None:
    """KNOWLEDGE.md demands an explicit decision. The decision is: keep, but label.

    Dropping published rows silently is forbidden; averaging fungicide into food prices is
    worse. So they are parsed and marked, and the caller chooses.
    """
    inputs = [row for row in cabadbaran.rows if row.is_agricultural_input]

    assert len(inputs) > 20
    assert {row.group for row in inputs} == set(AGRICULTURAL_INPUT_GROUPS)


def test_food_rows_are_not_flagged(cabadbaran: ParsedSheet) -> None:
    rice = [row for row in cabadbaran.rows if "RICE" in row.group]

    assert rice
    assert not any(row.is_agricultural_input for row in rice)


def test_fertiliser_is_not_food() -> None:
    assert "FERTILIZER" in AGRICULTURAL_INPUT_GROUPS
    assert "FISH" not in AGRICULTURAL_INPUT_GROUPS


# -- structure and whole-file failures -------------------------------------------


@pytest.mark.parametrize("name", ALL_SHEETS)
def test_a_sheet_yields_around_150_rows(name: str) -> None:
    sheet = load(name)

    assert 140 <= len(sheet.rows) <= 170
    assert sheet.page_count >= 3


def test_the_footer_note_is_not_a_data_row(cabadbaran: ParsedSheet) -> None:
    """The "blank means unavailable" note sits inside the table as its own row."""
    assert not any("Note:" in row.commodity for row in cabadbaran.rows)
    assert not any(row.group.startswith("Note") for row in cabadbaran.rows)


def test_the_metadata_block_is_not_a_data_row(cabadbaran: ParsedSheet) -> None:
    assert not any("Bantay Presyo" in row.group for row in cabadbaran.rows)


def test_the_column_header_is_not_a_data_row(cabadbaran: ParsedSheet) -> None:
    assert not any(row.commodity == "Commodities" for row in cabadbaran.rows)


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("empty", b""),
        ("plain text", b"not a pdf at all"),
        ("an HTML error page", b"<html><body><h1>404 Not Found</h1></body></html>"),
        ("a truncated PDF", b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\n"),
    ],
)
def test_something_that_is_not_a_readable_pdf_fails_the_file(name: str, payload: bytes) -> None:
    """A 404 page saved with a .pdf name is a real hazard; it must not parse to zero rows."""
    with pytest.raises(SheetParseError):
        parse_sheet(payload)


def test_a_table_without_the_expected_columns_fails_the_file() -> None:
    """Column positions are verified, never assumed — a reordered sheet is quarantined.

    Reading it anyway would silently swap, say, LOW and PREVAILING across a whole file.
    """
    with pytest.raises(SheetParseError, match="no column header row"):
        _read_body(
            [
                ("Group", "Item", "Spec", "Units", "MIN", "MAX", "USUAL", "MEAN"),
                data_row("Basmati", group="RICE", average="50.00"),
            ]
        )


def test_expected_columns_are_documented_in_order() -> None:
    assert EXPECTED_COLUMNS[:4] == ("commodity group", "commodities", "specifications", "unit")
    assert EXPECTED_COLUMNS[4:] == ("low", "high", "prevailing", "average")


def test_rejected_rows_keep_the_raw_cells() -> None:
    """Quarantine needs the original so it can be reprocessed after a parser fix."""
    _, rejected = _read_body([HEADER_ROW, data_row("Mystery", group="FISH", low="n/a")])

    assert isinstance(rejected[0], RejectedRow)
    assert rejected[0].cells[1] == "Mystery"


def test_price_rows_are_immutable(cabadbaran: ParsedSheet) -> None:
    """Parsed output should not be edited in place on its way to the database."""
    row = cabadbaran.rows[0]

    with pytest.raises(ValueError, match="frozen"):
        row.commodity = "something else"


def test_a_price_row_rejects_unavailable_with_prices() -> None:
    """The model mirrors the DB constraint that the two cannot both be true."""
    with pytest.raises(ValueError, match="marked unavailable but carries prices"):
        PriceRow(
            group="FISH",
            commodity="Bangus",
            specification=None,
            unit="kg",
            low=Decimal("10.00"),
            high=None,
            prevailing=None,
            average=None,
            unavailable=True,
            is_agricultural_input=False,
        )


# -- structural edge cases -------------------------------------------------------

# A hand-built PDF: valid enough for pdfminer to open, with a page and no ruling lines.
# Stands in for a scanned or re-laid-out sheet, which is the realistic way this happens.
TABLE_LESS_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R/Size 4>>\n"
    b"%%EOF\n"
)


def test_a_pdf_with_no_table_fails_the_file() -> None:
    """A readable PDF with no ruled table gives us no columns to trust.

    Returning zero rows would look like "this market reported nothing today", which is a
    false statement about the source rather than an error on our side.
    """
    with pytest.raises(SheetParseError, match="no table found"):
        parse_sheet(TABLE_LESS_PDF)


class _StubPage:
    """A page with a fixed set of tables, for exercising page-level branches."""

    def __init__(self, tables: list[object]) -> None:
        self._tables = tables

    def find_tables(self) -> list[object]:
        return self._tables


class _StubPdf:
    def __init__(self, pages: list[_StubPage]) -> None:
        self.pages = pages


def test_a_page_without_a_table_is_skipped_not_fatal() -> None:
    """A trailing blank page must not abort a sheet whose other pages are fine."""
    assert _extract_rows(_StubPdf([_StubPage([]), _StubPage([])])) == []


def test_a_short_row_is_ignored() -> None:
    """The metadata block above the table extracts as a row with too few cells."""
    rows, rejected = _read_body(
        [
            ("Bantay Presyo Monitoring\nProvince : X",),
            HEADER_ROW,
            data_row("Basmati", group="RICE", average="50.00"),
        ]
    )

    assert rejected == []
    assert [row.commodity for row in rows] == ["Basmati"]


def test_an_entirely_blank_row_is_ignored() -> None:
    """Ruling lines sometimes produce an empty row; it is not a commodity."""
    rows, rejected = _read_body(
        [
            HEADER_ROW,
            data_row("Basmati", group="RICE", average="50.00"),
            (None, "", "", "", "", "", "", ""),
        ]
    )

    assert rejected == []
    assert len(rows) == 1
