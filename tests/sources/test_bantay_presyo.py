"""Tests for the Bantay Presyo PDF parser.

Runs against twelve real sheets fetched from ``caraga.da.gov.ph`` and committed byte-exact —
seven markets across five provinces — plus one the parser deliberately cannot read.
``tests/conftest.py`` documents what is wrong with each. Synthetic PDFs would exercise none
of it.

Widening this corpus from four sheets to twelve found two real bugs, both on the five-page
``Mayor-Salvador-Calo-July-23-2026.xlsx.pdf``, and both caught by the cross-sheet agreement
check at the bottom of this file rather than by anything anyone thought to assert.

The pure inner functions are also tested directly. ``parse_header`` takes text and
``read_body`` takes extracted cells, so the branch behaviour — forward filling a group,
rejecting an unreadable cell — can be asserted without hand-building a PDF, which nothing in
the dependency set can do.
"""

from datetime import date
from decimal import Decimal
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
    _field_for,
    _parse_header,
    _read_body,
    normalise_unit,
    parse_sheet,
)
from tests.conftest import SHEET_NAMES, UNREADABLE_SHEET_NAME, load_sheet

FIXTURES = Path(__file__).parent.parent / "fixtures" / "pdf"

CABADBARAN = "Cabadbaran-City-Public-Market_June-24-2026.pdf"
LUHA = "Luha-Public-Market.xlsx-July-28-2026.pdf"
MAYOR_CALO = "Mayor-Salvador-Calo-July-19-2029.pdf"
SAN_JOSE = "San-Jose-Public-Market_July-28-2026.pdf"
SURIGAO = "Surigao-City-Public-Market.xlsx-July-24-2026.pdf"
LIBERTAD_TRUNCATED = "Libertad-Public-Market-July-22-2026.pdf"
LIBERTAD_OLD = "Libertad-Public-Market_Jan-07-2025.pdf"
REPEATED_HEADER = "Mayor-Salvador-Calo-July-23-2026.xlsx.pdf"
TWO_PAGE = "July-2026.xlsx-July-24.pdf"
REPUBLISHED = ("ADS-April-23-2026.pdf", "SanFracisco-April-23-2026.pdf")
ALL_SHEETS = SHEET_NAMES

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


def load(name: str) -> ParsedSheet:
    """Parse a fixture sheet. Cached in ``conftest`` so every module shares one parse."""
    return load_sheet(name)


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
def test_every_sheet_carries_the_full_set_of_groups(name: str) -> None:
    """21 groups, or 20 on the oldest sheet — which genuinely has no LEGUMES section.

    Verified against its raw text: the word does not appear. A drop below that would mean
    the forward fill broke and a block was absorbed into its neighbour.
    """
    expected = 20 if name == LIBERTAD_OLD else 21

    assert len({row.group for row in load(name).rows}) == expected


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
    assert sheet.page_count >= 2


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
    page_numbers, rows = _extract_rows(_StubPdf([_StubPage([]), _StubPage([])]))

    assert rows == []
    assert page_numbers == []


def test_a_short_row_is_ignored() -> None:
    """The metadata block above the table extracts as a row with too few cells."""
    rows, rejected = _read_body(
        [
            (),  # stray ruling lines occasionally yield a row with no cells at all
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


# -- group headings across page breaks -------------------------------------------
#
# A block whose heading cell is split by a page break extracts as `""` rather than `None`,
# and the heading lands on one side or the other. Forward filling every empty cell put the
# same commodities in different groups on different sheets, which is how this was caught.

RICE_IN_BOTH_GROUPS = frozenset(
    {
        "Basmati",
        "Glutinous",
        "Japonica/Jasponica",
        "Other Special Rice",
        "Premium",
        "Regular Milled",
        "Well Milled",
    }
)
"""Names that genuinely appear under both IMPORTED and LOCAL COMMERCIAL RICE.

These are the reason a commodity's identity needs its group, not just its name.
"""


@pytest.mark.parametrize("name", ALL_SHEETS)
@pytest.mark.parametrize(
    ("commodity", "expected_group"),
    [
        # Heading drawn on the page *after* its first rows.
        ("Avocado", "FRUITS"),
        ("Banana (Saba)", "FRUITS"),
        # Heading drawn on the page *before* its last rows.
        ("Porkchop", "LIVESTOCK MEAT PRODUCTS"),
        ("Pork ribs", "LIVESTOCK MEAT PRODUCTS"),
        ("Bottle Gourd (Upo)", "HIGHLAND VEGETABLES"),
        ("Ampalaya", "LOWLAND VEGETABLES"),
    ],
)
def test_commodities_split_by_a_page_break_get_the_right_group(
    name: str, commodity: str, expected_group: str
) -> None:
    """Avocado is not a spice and pork is not poultry, on any sheet."""
    matching = [row for row in load(name).rows if row.commodity == commodity]

    for row in matching:
        assert row.group == expected_group


def test_no_commodity_lands_in_different_groups_on_different_sheets() -> None:
    """Cross-sheet agreement is the evidence that the page-break rule is right.

    Four markets publish the same 21 groups in the same order, so a name appearing under
    two different groups across sheets means the parser, not the source, is inconsistent.
    The rice names are the genuine exception and are listed explicitly.
    """
    groups_by_commodity: dict[str, set[str]] = {}
    for name in ALL_SHEETS:
        for row in load(name).rows:
            groups_by_commodity.setdefault(row.commodity, set()).add(row.group)

    inconsistent = {
        commodity: sorted(groups)
        for commodity, groups in groups_by_commodity.items()
        if len(groups) > 1 and commodity not in RICE_IN_BOTH_GROUPS
    }

    assert inconsistent == {}


def test_the_rice_exception_is_real_and_not_an_artefact() -> None:
    """`Premium` is a different commodity depending on which rice group it is in."""
    rows = [row for row in load(CABADBARAN).rows if row.commodity == "Premium"]

    assert {row.group for row in rows} == {
        "IMPORTED COMMERCIAL RICE",
        "LOCAL COMMERCIAL RICE",
    }


def test_a_heading_absorbed_into_a_repeated_header_block_is_not_used_as_a_group() -> None:
    """One sheet repeats its header block on every page and swallows a heading into it.

    The cell reads `...Date of Monitoring : July 19, 2026\nLIVESTOCK MEAT\nPRODUCTS`.
    Treating that as a heading would set the group to the entire header block.
    """
    for row in load(MAYOR_CALO).rows:
        assert "Bantay Presyo" not in row.group
        assert "Date of Monitoring" not in row.group


def test_an_empty_heading_at_the_top_of_a_page_continues_the_previous_block() -> None:
    """The block carries on from the previous page; the next heading is unrelated."""
    rows, _ = _read_body(
        [
            HEADER_ROW,
            data_row("Pork Belly", group="LIVESTOCK MEAT PRODUCTS", average="300.00"),
            # page break, header repeated, then the block's tail with an empty heading cell
            HEADER_ROW,
            data_row("Porkchop", group="", average="310.00"),
            data_row("Whole Chicken", group="POULTRY MEAT PRODUCTS", average="200.00"),
        ],
        [0, 0, 1, 1, 1],
    )

    by_commodity = {row.commodity: row.group for row in rows}
    assert by_commodity["Porkchop"] == "LIVESTOCK MEAT PRODUCTS"
    assert by_commodity["Whole Chicken"] == "POULTRY MEAT PRODUCTS"


def test_an_empty_heading_mid_page_belongs_to_the_block_that_follows() -> None:
    """The block starts here but its heading is drawn overleaf."""
    rows, _ = _read_body(
        [
            HEADER_ROW,
            data_row("Sweet Pepper", group="SPICES", average="100.00"),
            data_row("Avocado", group="", average="120.00"),
            # page break; the heading renders here, at the top of the next page
            data_row("Calamansi", group="FRUITS", average="60.00"),
        ],
        [0, 0, 0, 1],
    )

    by_commodity = {row.commodity: row.group for row in rows}
    assert by_commodity["Avocado"] == "FRUITS"
    assert by_commodity["Sweet Pepper"] == "SPICES"


# -- the sheet's own metadata, mistaken for data ----------------------------------
#
# Most sheets extract their header block as one tall cell that never reaches the body. The
# five-page `Mayor-Salvador-Calo-July-23-2026.xlsx.pdf` repeats the block on every page and
# extracts it one line per row, split across the first two columns as
# `Province | : Agusan Del Norte`. That looks exactly like a commodity row carrying a group
# heading, and treating it as one broke two unrelated things on that sheet.


def test_a_repeated_header_block_never_becomes_a_commodity_group() -> None:
    """Seven highland vegetables were filed under a group called `Province`."""
    groups = {row.group for row in load(REPEATED_HEADER).rows}

    assert "Province" not in groups
    assert not any(_field_for(group) for group in groups)


def test_a_repeated_header_block_is_not_a_rejected_row_either() -> None:
    """It is not a commodity row that went wrong; it is not a commodity row.

    Quarantining it would inflate the failure count on the data quality page with rows the
    source never intended as data.
    """
    sheet = load(REPEATED_HEADER)

    assert sheet.rejected == ()
    assert not any(row.commodity.startswith(":") for row in sheet.rows)


def test_a_block_split_by_a_page_break_survives_a_repeated_header() -> None:
    """`Cooking Oil (Coconut)` opens page 4 below a repeated header block.

    It is the first *data* row of that page, so it continues OTHER BASIC COMMODITIES. The
    header block's own blank rows have an empty group cell too, and letting one of those
    reach forward for the next heading moved this row into LIVESTOCK & POULTRY FEEDS.
    """
    rows = [row for row in load(REPEATED_HEADER).rows if row.commodity == "Cooking Oil (Coconut)"]

    assert len(rows) == 2
    assert {row.group for row in rows} == {"OTHER BASIC COMMODITIES"}


def test_a_blank_row_in_a_header_block_does_not_reach_for_the_next_heading() -> None:
    """The rule in miniature: only a commodity row can be the split half of a heading."""
    rows, _ = _read_body(
        [
            HEADER_ROW,
            data_row("Sugar (Brown)", group="OTHER BASIC COMMODITIES", average="80.00"),
            # page break, header block repeated: blank rows whose group cell is "" too
            ("", "", "", "", "", "", "", ""),
            ("Province", ": Agusan Del Norte", "", "", "", "", "", ""),
            HEADER_ROW,
            data_row("Cacao Beans", group="", average="200.00"),
            data_row("Hog Booster", group="LIVESTOCK & POULTRY FEEDS", average="1500.00"),
        ],
        [0, 0, 1, 1, 1, 1, 1],
    )

    by_commodity = {row.commodity: row.group for row in rows}
    assert by_commodity["Cacao Beans"] == "OTHER BASIC COMMODITIES"
    assert by_commodity["Hog Booster"] == "LIVESTOCK & POULTRY FEEDS"


# -- header labels the source truncated ------------------------------------------


def test_labels_clipped_by_the_source_are_still_read() -> None:
    """`Libertad-Public-Market-July-22-2026.pdf` reads `Municipality/Ci: Butuan City`.

    The spreadsheet column was too narrow and the export clipped the label, not the value.
    Quarantining a sheet whose data is complete over a printing artefact would be losing
    real data to fussiness.
    """
    header = load(LIBERTAD_TRUNCATED).header

    assert header.province == "Agusan Del Norte"
    assert header.municipality == "Butuan City"
    assert header.market == "Libertad Public Market"
    assert header.observed_on.isoformat() == "2026-07-22"


def test_a_truncated_label_is_matched_by_prefix() -> None:
    assert _field_for("Municipality/Ci") == "municipality"
    assert _field_for("Date of Monitor") == "observed_on"
    assert _field_for("Province") == "province"


def test_a_stub_too_short_to_identify_a_label_is_refused() -> None:
    """Better an unreadable sheet than a field read off three characters."""
    assert _field_for("Mun") is None
    assert _field_for("") is None


def test_something_that_is_not_a_header_label_is_not_one() -> None:
    assert _field_for("Commodity Group") is None
    assert _field_for("Note") is None


def test_the_first_occurrence_of_a_repeated_label_wins() -> None:
    """Sheets that repeat the block on every page must be read from the first page."""
    header = _parse_header(
        "Province : Agusan del Norte\n"
        "Municipality/City : Cabadbaran City\n"
        "Market Monitored : Cabadbaran City Public Market\n"
        "Date of Monitoring : June 24, 2026\n"
        "Province : Somewhere Else\n"
        "Date of Monitoring : June 25, 2026\n"
    )

    assert header.province == "Agusan del Norte"
    assert header.observed_on.isoformat() == "2026-06-24"


# -- a layout the parser refuses --------------------------------------------------


def test_a_seven_column_sheet_is_quarantined_whole() -> None:
    """One real sheet omits the Specifications column entirely.

    Reading it anyway would shift every price one column left. Supporting the layout is a
    real decision — it changes what identifies a commodity — so the file is quarantined and
    the decision is left to a human rather than taken by the parser.
    """
    body = (FIXTURES / UNREADABLE_SHEET_NAME).read_bytes()

    with pytest.raises(SheetParseError, match="no column header row"):
        parse_sheet(body)


# -- the same sheet published twice ----------------------------------------------


def test_a_sheet_republished_under_two_urls_is_byte_identical() -> None:
    """Caraga published one San Francisco sheet under two names, one of them misspelled.

    This is why the raw cache is content-addressed: two URLs, one blob. It is also why the
    upsert compares figures rather than filenames — neither of these is a correction.
    """
    first, second = ((FIXTURES / name).read_bytes() for name in REPUBLISHED)

    assert first == second


def test_a_republished_sheet_parses_to_the_same_observations() -> None:
    left, right = (load(name) for name in REPUBLISHED)

    assert left.header == right.header
    assert left.rows == right.rows


# -- the filename disagrees with the sheet, in both directions --------------------


def test_an_older_sheet_also_disagrees_with_its_filename() -> None:
    """`Libertad-Public-Market_Jan-07-2025.pdf` says `Date of Monitoring : January 7, 2026`.

    The 2029 sheet is not a one-off. A January file carrying the previous year in its name
    is the ordinary new-year slip, and it is the second piece of evidence that the index
    date is provisional and the header is authoritative.
    """
    assert load(LIBERTAD_OLD).header.observed_on.isoformat() == "2026-01-07"
    assert "Jan-07-2025" in LIBERTAD_OLD


def test_no_sheet_is_read_as_a_date_in_the_future() -> None:
    """Every header date lands in the past, including the file whose filename says 2029."""
    for name in ALL_SHEETS:
        assert load(name).header.observed_on <= date(2026, 7, 28)
