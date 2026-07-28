"""Response models for the public API.

Separate from the SQLAlchemy models on purpose. An ORM class is a description of a table;
these are a description of a promise. Serving the tables directly would make every column
rename a breaking change for the dashboard, and would leak fields nobody outside this
process should depend on — ``source_file_sha256`` points into a private cache, and the
primary keys are not stable identities the way ``canonical_slug`` and ``psgc_code`` are.

**Prices are serialised as strings, not JSON numbers.** They are ``Decimal`` all the way
from the PDF, and JSON has only IEEE-754 doubles: ``52.80`` becomes ``52.79999999999999716``
in any consumer that parses numbers as floats. A string survives the round trip exactly and
the client decides what to do with it. This is the one place where the obvious choice is
wrong in a way that only shows up as a chart with 15 decimal places on it.

**Missing prices stay ``null``.** PLANNING.md: gaps stay gaps in storage, and nothing is
interpolated on the way out either. A row also carries ``unavailable``, which distinguishes
"the source listed this commodity and published no figures" from "no row exists".
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

Money = Annotated[
    Decimal | None,
    PlainSerializer(
        lambda value: None if value is None else f"{value:.2f}", return_type=str | None
    ),
]
"""A peso amount, rendered as a fixed two-decimal string. See the module docstring."""


class Page[Item](BaseModel):
    """One page of results.

    ``total`` is the number of rows matching the filters, not the number returned, so a
    client can show "1 to 50 of 8,214" without walking the whole set. It costs a second
    ``COUNT`` per request, which is the right trade at this size and is the first thing to
    reconsider if the table ever gets large.
    """

    items: list[Item]
    total: int
    limit: int
    offset: int


class SourceOut(BaseModel):
    """A publisher, with the attribution that is a condition of using its data (rule 8)."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "slug": "da-caraga",
                    "name": "DA Regional Field Office XIII (Caraga)",
                    "base_url": "https://caraga.da.gov.ph",
                    "licence": None,
                    "attribution_text": (
                        "Bantay Presyo price monitoring published by the Department of "
                        "Agriculture Regional Field Office XIII (Caraga). Reproduced under "
                        "RA 8293 s. 176."
                    ),
                }
            ]
        },
    )

    slug: str
    name: str
    base_url: str
    licence: str | None
    attribution_text: str


class RegionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    psgc_code: str
    name: str
    level: str


class MarketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    municipality: str
    region_psgc_code: str


class CommodityOut(BaseModel):
    """A canonical commodity.

    ``group`` is part of its identity, not decoration: seven rice varieties appear under both
    ``IMPORTED`` and ``LOCAL COMMERCIAL RICE`` and are different products.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "canonical_slug": "imported-commercial-rice-premium-5-broken",
                    "group": "IMPORTED COMMERCIAL RICE",
                    "name": "Premium",
                    "specification": "5% Broken",
                    "unit": "kg",
                    "is_agricultural_input": False,
                }
            ]
        },
    )

    canonical_slug: str
    group: str
    name: str
    specification: str | None
    unit: str
    is_agricultural_input: bool = Field(
        description=(
            "Feeds, fertiliser and pesticides. Published by the source and kept, but flagged "
            "so a food-price chart can exclude them rather than averaging fungicide into the "
            "cost of rice."
        )
    )


class ObservationOut(BaseModel):
    """One commodity's prices at one market on one day."""

    # A real row, copied from a live run rather than invented, so the docs show the shapes a
    # client will actually meet — including prices as strings and a 2026 date on a file whose
    # name says 2029.
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "observed_on": "2026-07-19",
                    "commodity_slug": "imported-commercial-rice-glutinous-white",
                    "market_id": 1,
                    "market": "Mayor Salvador Calo Public Market",
                    "municipality": "Butuan City",
                    "region_psgc_code": "160200000",
                    "source_slug": "da-caraga",
                    "low": "54.00",
                    "high": "60.00",
                    "prevailing": "55.00",
                    "average": "56.33",
                    "unavailable": False,
                    "revision_no": 0,
                    "ingested_at": "2026-07-28T13:24:16.890465Z",
                }
            ]
        },
    )

    observed_on: date
    commodity_slug: str
    market_id: int
    market: str
    municipality: str
    region_psgc_code: str
    source_slug: str

    low: Money
    high: Money
    prevailing: Money
    average: Money

    unavailable: bool = Field(
        description="The source listed this commodity but published no figures for it."
    )
    revision_no: int = Field(
        description=(
            "How many times a correction has superseded these figures. 0 as first published."
        )
    )
    ingested_at: datetime


class RunOut(BaseModel):
    """One ingestion run. The raw material of the data quality page."""

    model_config = ConfigDict(from_attributes=True)

    run_id: str
    source_slug: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    files_seen: int
    files_fetched: int
    rows_upserted: int
    rows_quarantined: int
    error: str | None


class Health(BaseModel):
    """Liveness plus the one dependency that matters.

    ``database`` is checked with an actual query rather than reported optimistically: on
    Neon's free tier the compute suspends when idle, so "the process is up" and "the database
    will answer" are genuinely different questions.
    """

    status: str
    database: str
    version: str


class MoverOut(BaseModel):
    """How far one commodity's price moved at one market over a window.

    **Per market, not averaged across markets.** Averaging the averages of Butuan and Tandag
    would produce a number that is nobody's price and that moves when coverage changes rather
    than when prices do — a market dropping out of monitoring would look like a price swing.
    Each row here is a real comparison between two figures the source actually published.

    ``first`` and ``last`` are the earliest and latest observations *inside the window*, not
    the window's endpoints: the source does not publish every day, and pretending a gap is a
    price would be interpolation by another name. The dates are returned so a reader can see
    what was actually compared.
    """

    commodity_slug: str
    commodity: str
    group: str
    unit: str
    market_id: int
    market: str
    municipality: str
    region_psgc_code: str

    first_observed_on: date
    last_observed_on: date
    first_average: Money
    last_average: Money
    change: Money
    percent_change: float
    observations: int = Field(
        description="How many days inside the window carried a figure. Two is the minimum."
    )


class FlaggedOut(BaseModel):
    """One observation, annotated. Nothing is removed — see `presyowatch.analytics`."""

    observed_on: date
    commodity_slug: str
    market_id: int
    market: str
    average: Money
    low: Money
    high: Money
    score: float | None = Field(
        description=(
            "Modified z-score against the surrounding median. Null when there were too few "
            "neighbouring days to judge — three prices are not a distribution."
        )
    )
    is_anomaly: bool = Field(
        description="Unusual against its neighbours. Unusual is not the same as wrong."
    )
    is_impossible: bool = Field(
        description=(
            "Arithmetically self-contradictory — an average outside its own low-to-high "
            "range. Certain rather than probable, and needs no threshold."
        )
    )
    reason: str | None


class SourceQuality(BaseModel):
    """How one source's ingestion is actually going."""

    slug: str
    name: str
    runs: int
    succeeded: int
    failed: int
    partial: int
    last_run_at: datetime | None
    last_success_at: datetime | None
    rows_upserted: int
    rows_quarantined: int

    @property
    def success_rate(self) -> float | None:
        return self.succeeded / self.runs if self.runs else None


class QuarantineCount(BaseModel):
    """How many rows are held at one stage, and the commonest reason."""

    stage: str
    rows: int
    example_reason: str | None


class Quality(BaseModel):
    """The public data quality summary.

    PLANNING.md calls observability a feature and this the single highest-signal thing a
    reviewer sees. It is deliberately unflattering: it reports what did not load as
    prominently as what did, because a dashboard that only shows successes is not evidence
    of anything.
    """

    sources: list[SourceQuality]
    quarantine: list[QuarantineCount]
    observations: int
    unavailable: int = Field(
        description="Rows the source listed with no figures at all. Expected, not a fault."
    )
    impossible: int = Field(
        description="Stored rows whose average falls outside their own low-to-high range."
    )
    commodities_seeded: int
    commodities_observed: int = Field(
        description=(
            "How many seeded commodities have ever been observed. The gap is vocabulary that "
            "exists in the seed but has never been monitored."
        )
    )
    earliest_observed_on: date | None
    latest_observed_on: date | None
