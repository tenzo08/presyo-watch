"""SQLAlchemy models for the schema in PLANNING.md.

Several choices here are deliberate and worth stating, because the obvious alternative is
wrong in a way that only shows up later.

**Prices are ``Numeric``, never floating point.** These are money. ``52.80`` is not
representable in binary floating point, and an average over a year of rounding errors is a
wrong number on a public chart. ``Numeric`` in Postgres maps to
:class:`decimal.Decimal` in Python and is exact.

**Blank means "not monitored", not zero.** The source PDFs state in their footer that a
blank cell means the commodity was not available during monitoring. Storing ``0`` would
claim the item was free; storing a bare ``NULL`` would be indistinguishable from a parse
failure. So each price column is nullable *and* the row carries ``unavailable``, set when
the source published the commodity with no figures at all.

**Enums render as ``VARCHAR`` with a ``CHECK``, not as native Postgres enums.** Adding a
value to a native enum requires ``ALTER TYPE`` and cannot be done inside a transaction on
older servers; a check constraint is altered like any other constraint. The Python side is
equally typed either way, so the flexibility is free.

**No foreign key on ``source_file_sha256``.** It points into the on-disk content-addressed
cache (:mod:`presyowatch.cache`), which is authoritative for raw bytes and is deliberately
not mirrored as a table. The column records provenance; integrity of the bytes is
``RawCache.verify()``'s job.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Explicit, predictable constraint names. Without a naming convention, Alembic emits
# migrations that cannot drop the constraints they created, because the server invented
# the names.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

RunStatus = Literal["running", "succeeded", "failed", "partial"]
"""Lifecycle of one ingestion run.

``partial`` exists because "one source broke, the rest worked" is a real and important
outcome that must not be recorded as either success or failure.
"""

QuarantineStage = Literal["index", "parse", "alias", "validate"]
"""Which step rejected a row, so the data quality page can say *where* things fail."""

RegionLevel = Literal["region", "province", "municipality", "city"]


class Base(DeclarativeBase):
    """Declarative base carrying the naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _utc_timestamp() -> Mapped[datetime]:
    """A timezone-aware timestamp defaulted by the database clock."""
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class Source(Base):
    """One upstream publisher of price data.

    ``robots_checked_at`` and ``robots_allowed`` record the rule 2 check per host, which
    :meth:`presyowatch.net.client.HttpClient.robots_decision` already returns.
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)

    robots_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    robots_allowed: Mapped[bool | None] = mapped_column()

    licence: Mapped[str | None] = mapped_column(String(255))
    attribution_text: Mapped[str] = mapped_column(Text, nullable=False)
    """Displayed in the UI. Not optional — attribution is a condition of use (rule 8)."""

    created_at: Mapped[datetime] = _utc_timestamp()

    observations: Mapped[list["PriceObservation"]] = relationship(back_populates="source")


class Region(Base):
    """A Philippine administrative area, keyed by its PSGC code."""

    __tablename__ = "regions"

    psgc_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    level: Mapped[RegionLevel] = mapped_column(
        Enum(
            "region",
            "province",
            "municipality",
            "city",
            name="region_level",
            native_enum=False,
            create_constraint=True,
        ),
        nullable=False,
    )

    markets: Mapped[list["Market"]] = relationship(back_populates="region")


class Market(Base):
    """A monitored market. The finest granularity the regional PDFs provide."""

    __tablename__ = "markets"
    __table_args__ = (
        # The same market name recurs across municipalities ("Public Market"), so
        # uniqueness needs all three parts.
        UniqueConstraint("region_psgc_code", "municipality", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region_psgc_code: Mapped[str] = mapped_column(
        String(10), ForeignKey("regions.psgc_code"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    municipality: Mapped[str] = mapped_column(String(255), nullable=False)

    region: Mapped[Region] = relationship(back_populates="markets")
    observations: Mapped[list["PriceObservation"]] = relationship(back_populates="market")


class Commodity(Base):
    """A canonical commodity.

    ``canonical_slug`` is the stable identifier; the many raw strings the sources use for
    the same thing are mapped onto it through :class:`CommodityAlias`.
    """

    __tablename__ = "commodities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    group: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    specification: Mapped[str | None] = mapped_column(String(255))
    unit: Mapped[str] = mapped_column(String(32), nullable=False)

    aliases: Mapped[list["CommodityAlias"]] = relationship(back_populates="commodity")
    observations: Mapped[list["PriceObservation"]] = relationship(back_populates="commodity")


class CommodityAlias(Base):
    """A raw source string mapped to a canonical commodity.

    Explicit by design. PLANNING.md is emphatic that unmapped names are quarantined and
    never guessed — fuzzy matching "Porkchop" onto the wrong cut silently corrupts a
    public price series, and nobody would notice.
    """

    __tablename__ = "commodity_aliases"
    __table_args__ = (UniqueConstraint("raw_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_name: Mapped[str] = mapped_column(String(255), nullable=False)
    """The source string, normalised for whitespace and case by the resolver."""

    commodity_id: Mapped[int] = mapped_column(Integer, ForeignKey("commodities.id"), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _utc_timestamp()

    commodity: Mapped[Commodity] = relationship(back_populates="aliases")


class PriceObservation(Base):
    """One commodity's prices at one market on one day.

    The natural key is ``(source_id, market_id, commodity_id, observed_on)``. Every write
    is an upsert on it (rule 4), so re-running the ingester over a date range converges
    rather than accumulating duplicates.
    """

    __tablename__ = "price_observations"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "market_id",
            "commodity_id",
            "observed_on",
            name="uq_price_observations_natural_key",
        ),
        CheckConstraint(
            "low IS NULL OR high IS NULL OR low <= high",
            name="low_not_above_high",
        ),
        CheckConstraint(
            "NOT unavailable OR "
            "(low IS NULL AND high IS NULL AND prevailing IS NULL AND average IS NULL)",
            name="unavailable_implies_no_prices",
        ),
        # The dashboard's main query is "this commodity over time, here".
        Index(
            "ix_price_observations_commodity_observed_on",
            "commodity_id",
            "observed_on",
        ),
        Index("ix_price_observations_observed_on", "observed_on"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    source_id: Mapped[int] = mapped_column(Integer, ForeignKey("sources.id"), nullable=False)
    market_id: Mapped[int] = mapped_column(Integer, ForeignKey("markets.id"), nullable=False)
    commodity_id: Mapped[int] = mapped_column(Integer, ForeignKey("commodities.id"), nullable=False)
    observed_on: Mapped[date] = mapped_column(Date, nullable=False)

    # Nullable because a blank cell means "not monitored that day". Gaps stay gaps;
    # nothing is interpolated at rest (PLANNING.md § Analytics decisions).
    low: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    high: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    prevailing: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    average: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    unavailable: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    """True when the source listed the commodity but published no figures for it."""

    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """Incremented when a ``Revised-`` file supersedes these values."""

    source_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = _utc_timestamp()

    source: Mapped[Source] = relationship(back_populates="observations")
    market: Mapped[Market] = relationship(back_populates="observations")
    commodity: Mapped[Commodity] = relationship(back_populates="observations")
    revisions: Mapped[list["ObservationRevision"]] = relationship(back_populates="observation")


class ObservationRevision(Base):
    """Append-only record of values a correction superseded.

    The DA republishes corrected figures as ``Revised-`` files. Rule 4 requires the
    observation row to be updated in place *and* the previous values kept, so that a chart
    can show what was published at the time as well as what is now believed true.
    """

    __tablename__ = "observation_revisions"
    __table_args__ = (
        UniqueConstraint("observation_id", "revision_no"),
        Index("ix_observation_revisions_observation_id", "observation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("price_observations.id"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    """The revision number these superseded values had, not the one replacing them."""

    low: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    high: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    prevailing: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    average: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    unavailable: Mapped[bool] = mapped_column(nullable=False, server_default="false")

    source_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    """The file these superseded values came from — not the file that replaced them."""

    superseded_at: Mapped[datetime] = _utc_timestamp()

    observation: Mapped[PriceObservation] = relationship(back_populates="revisions")


class IngestionRun(Base):
    """One execution of the ingester against one source.

    Written even when the run fails, so that a missed or broken run is visible on the data
    quality page rather than being silently invisible. GitHub Actions schedules drift and
    get skipped, so absence of a row is itself the signal.
    """

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        Index("ix_ingestion_runs_source_id_started_at", "source_id", "started_at"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="finished_after_started",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    """Correlates with the ``run_id`` on every structured log line for this run."""

    source_id: Mapped[int] = mapped_column(Integer, ForeignKey("sources.id"), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(
            "running",
            "succeeded",
            "failed",
            "partial",
            name="ingestion_status",
            native_enum=False,
            create_constraint=True,
        ),
        nullable=False,
    )

    started_at: Mapped[datetime] = _utc_timestamp()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    files_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    files_fetched: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """Distinct from ``files_seen``: most files in a lookback window are already cached."""

    rows_upserted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_quarantined: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)


class QuarantinedRow(Base):
    """A row that failed validation, kept with the reason it failed.

    Never dropped silently. The count per reason is surfaced publicly, which is what makes
    the failure rate an honest number rather than a hidden one.
    """

    __tablename__ = "quarantine"
    __table_args__ = (
        Index("ix_quarantine_stage_created_at", "stage", "created_at"),
        Index("ix_quarantine_source_file_sha256", "source_file_sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sources.id"))
    run_id: Mapped[str | None] = mapped_column(String(32))

    stage: Mapped[QuarantineStage] = mapped_column(
        Enum(
            "index",
            "parse",
            "alias",
            "validate",
            name="quarantine_stage",
            native_enum=False,
            create_constraint=True,
        ),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    source_file_sha256: Mapped[str | None] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)
    """Recorded for index-stage failures, where there is no file yet — only a bad href."""

    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    """The raw row exactly as encountered, so it can be reprocessed after a parser fix."""

    created_at: Mapped[datetime] = _utc_timestamp()
