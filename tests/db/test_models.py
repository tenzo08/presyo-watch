"""Structural assertions about the schema.

These need no database — they inspect the SQLAlchemy metadata directly, so they run in CI
and in the pre-commit hook. They pin the invariants that are cheap to break and expensive
to discover: money stored as floating point, the natural key losing a column, a naive
timestamp creeping into a time series.
"""

import pytest
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    Numeric,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from presyowatch.db.models import (
    Base,
    CommodityAlias,
    IngestionRun,
    ObservationRevision,
    PriceObservation,
    QuarantinedRow,
    Source,
)

PRICE_COLUMNS = ("low", "high", "prevailing", "average")
MONEY_TABLES = (PriceObservation, ObservationRevision)

ALL_TABLES = tuple(Base.metadata.sorted_tables)


def table_of(model: type[Base]) -> Table:
    """Return a model's ``Table``.

    ``__table__`` is typed as the looser ``FromClause``, which has no ``constraints``.
    """
    mapped = model.__table__
    assert isinstance(mapped, Table)
    return mapped


def test_every_expected_table_exists() -> None:
    assert {table.name for table in ALL_TABLES} == {
        "sources",
        "regions",
        "markets",
        "commodities",
        "commodity_aliases",
        "price_observations",
        "observation_revisions",
        "ingestion_runs",
        "quarantine",
    }


# -- money ------------------------------------------------------------------------


@pytest.mark.parametrize("model", MONEY_TABLES)
@pytest.mark.parametrize("column", PRICE_COLUMNS)
def test_prices_are_exact_decimals_not_floats(model: type[Base], column: str) -> None:
    """`52.80` is not representable in binary floating point.

    A float column would accumulate rounding error into every average and publish a wrong
    number. `Float` subclasses `Numeric` in SQLAlchemy, so it must be excluded explicitly
    rather than by an `isinstance(Numeric)` check alone.
    """
    kind = model.__table__.c[column].type

    assert isinstance(kind, Numeric)
    assert not isinstance(kind, Float)
    assert kind.scale == 2
    assert kind.precision == 12


@pytest.mark.parametrize("model", MONEY_TABLES)
@pytest.mark.parametrize("column", PRICE_COLUMNS)
def test_prices_are_nullable_so_gaps_stay_gaps(model: type[Base], column: str) -> None:
    """A blank cell means "not monitored", which is neither zero nor a parse failure."""
    assert model.__table__.c[column].nullable is True


def test_unavailable_flag_distinguishes_missing_from_broken() -> None:
    """NULL prices alone are ambiguous, so the row records *why* they are NULL."""
    column = PriceObservation.__table__.c["unavailable"]

    assert isinstance(column.type, Boolean)
    assert column.nullable is False


# -- the natural key (rule 4) ----------------------------------------------------


def test_natural_key_is_unique_on_exactly_four_columns() -> None:
    """Idempotent upserts depend on this constraint existing and being complete.

    Drop `source_id` and two sources reporting the same market collide; drop `observed_on`
    and yesterday's price overwrites today's.
    """
    constraint = next(
        c
        for c in table_of(PriceObservation).constraints
        if isinstance(c, UniqueConstraint) and c.name == "uq_price_observations_natural_key"
    )

    assert [column.name for column in constraint.columns] == [
        "source_id",
        "market_id",
        "commodity_id",
        "observed_on",
    ]


def test_observed_on_is_a_date_not_a_timestamp() -> None:
    """The sources publish a monitoring *day*; a time would be invented precision."""
    assert isinstance(PriceObservation.__table__.c["observed_on"].type, Date)


def test_revisions_are_unique_per_observation_and_number() -> None:
    names = {
        tuple(column.name for column in c.columns)
        for c in table_of(ObservationRevision).constraints
        if isinstance(c, UniqueConstraint)
    }

    assert ("observation_id", "revision_no") in names


def test_check_constraints_guard_the_invariants() -> None:
    """The database enforces these, not just the parser that happens to write them."""
    names = {c.name for c in table_of(PriceObservation).constraints}

    assert "ck_price_observations_low_not_above_high" in names
    assert "ck_price_observations_unavailable_implies_no_prices" in names


# -- time -------------------------------------------------------------------------


def _timestamp_columns() -> list[tuple[str, str, DateTime]]:
    found = []
    for table in ALL_TABLES:
        for column in table.c:
            if isinstance(column.type, DateTime):
                found.append((table.name, column.name, column.type))
    return found


def test_there_are_timestamp_columns_to_check() -> None:
    """Guards the test below from silently passing on an empty list."""
    assert len(_timestamp_columns()) >= 5


def test_every_timestamp_is_timezone_aware() -> None:
    """A naive timestamp in a time series is a bug waiting for a DST boundary.

    Philippine Standard Time has no DST, which is exactly why this would go unnoticed
    locally and then break on a server running in UTC.
    """
    naive = [
        f"{table}.{column}" for table, column, kind in _timestamp_columns() if not kind.timezone
    ]

    assert naive == []


# -- provenance and attribution --------------------------------------------------


def test_observations_record_which_file_they_came_from() -> None:
    """Provenance for every number, so a bad figure can be traced to its source file."""
    column = PriceObservation.__table__.c["source_file_sha256"]

    assert column.nullable is False
    assert isinstance(column.type, String)
    assert column.type.length == 64, "a hex SHA-256 is 64 characters"


def test_attribution_text_is_mandatory() -> None:
    """Attribution is a condition of use, not a nice-to-have (CLAUDE.md rule 8)."""
    assert Source.__table__.c["attribution_text"].nullable is False


def test_sources_record_the_robots_check() -> None:
    """Rule 2 requires a per-host check; the schema keeps the evidence and its date."""
    columns = Source.__table__.c

    assert "robots_checked_at" in columns
    assert isinstance(columns["robots_checked_at"].type, DateTime)
    assert columns["robots_checked_at"].type.timezone is True


# -- quarantine ------------------------------------------------------------------


def test_quarantine_keeps_the_raw_payload_and_a_reason() -> None:
    """Rows that fail validation are kept, never dropped, and always explained."""
    columns = QuarantinedRow.__table__.c

    assert isinstance(columns["payload"].type, JSONB)
    assert columns["payload"].nullable is False
    assert columns["reason"].nullable is False


def test_quarantine_can_record_a_bad_href_with_no_file() -> None:
    """Index-stage failures have no file yet — only a URL that would not parse."""
    columns = QuarantinedRow.__table__.c

    assert columns["source_url"].nullable is True
    assert columns["source_file_sha256"].nullable is True


def test_quarantine_records_which_stage_rejected_the_row() -> None:
    kind = QuarantinedRow.__table__.c["stage"].type

    assert isinstance(kind, Enum)
    assert set(kind.enums) == {"index", "parse", "alias", "validate"}


# -- runs -------------------------------------------------------------------------


def test_run_status_includes_partial() -> None:
    """ "One source broke, the others worked" is neither success nor failure."""
    kind = IngestionRun.__table__.c["status"].type

    assert isinstance(kind, Enum)
    assert set(kind.enums) == {"running", "succeeded", "failed", "partial"}


def test_a_run_can_be_recorded_before_it_finishes() -> None:
    """A row is written at the start, so a crashed run leaves evidence."""
    assert IngestionRun.__table__.c["finished_at"].nullable is True
    assert IngestionRun.__table__.c["started_at"].nullable is False


def test_runs_correlate_with_log_lines() -> None:
    assert IngestionRun.__table__.c["run_id"].nullable is False


# -- conventions -----------------------------------------------------------------


def test_enums_are_not_native_postgres_types() -> None:
    """Adding a value to a native enum needs ALTER TYPE; a CHECK is altered normally."""
    native = [
        f"{table.name}.{column.name}"
        for table in ALL_TABLES
        for column in table.c
        if isinstance(column.type, Enum) and column.type.native_enum
    ]

    assert native == []


def test_every_table_has_a_primary_key() -> None:
    without = [table.name for table in ALL_TABLES if not table.primary_key.columns]

    assert without == []


def test_constraint_names_are_deterministic() -> None:
    """Server-invented names produce migrations that cannot drop what they created."""
    assert Base.metadata.naming_convention["uq"] == "uq_%(table_name)s_%(column_0_N_name)s"

    unnamed: list[str] = []
    for table in ALL_TABLES:
        unnamed.extend(
            f"{table.name}.{type(c).__name__}" for c in table.constraints if c.name is None
        )
    assert unnamed == []


def test_aliases_are_unique_per_raw_name() -> None:
    """One raw string maps to one commodity, or the mapping is not a mapping."""
    unique_columns = {
        tuple(column.name for column in c.columns)
        for c in table_of(CommodityAlias).constraints
        if isinstance(c, UniqueConstraint)
    }

    assert ("raw_name",) in unique_columns


def test_tables_sort_into_a_creatable_order() -> None:
    """`sorted_tables` respects foreign keys; a cycle here would break migrations."""
    order = [table.name for table in ALL_TABLES]
    assert isinstance(ALL_TABLES[0], Table)
    assert order.index("regions") < order.index("markets")
    assert order.index("commodities") < order.index("commodity_aliases")
    assert order.index("price_observations") < order.index("observation_revisions")
