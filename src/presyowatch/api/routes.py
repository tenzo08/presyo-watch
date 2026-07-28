"""The read endpoints.

All of them are filters over one of four things: what is published (`/meta/sources`), what
exists (`/commodities`, `/markets`, `/regions`), what was observed (`/observations`), and
how the ingester is doing (`/meta/runs`). Nothing here writes.

**Every list is paginated, with a hard ceiling.** ``limit`` is capped rather than trusted:
an unbounded query against a growing time series is the easiest way to make a free-tier
database time out, and a client asking for a million rows is asking by mistake.

**Ordering is total, never partial.** Every list orders by something unique as its last key.
A query ordered only by ``observed_on`` returns rows in whatever order the plan produced,
so page 2 can repeat a row from page 1 and skip another entirely — a paging bug that only
appears on real data.
"""

from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, aliased

from presyowatch.analytics.anomalies import (
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW,
    AnomalyConfig,
    Point,
    flag_series,
)
from presyowatch.api.deps import get_session
from presyowatch.api.schemas import (
    CommodityOut,
    FlaggedOut,
    MarketOut,
    MoverOut,
    ObservationOut,
    Page,
    Quality,
    QuarantineCount,
    RegionOut,
    RunOut,
    SourceOut,
    SourceQuality,
)
from presyowatch.db.models import (
    Commodity,
    IngestionRun,
    Market,
    PriceObservation,
    QuarantinedRow,
    Region,
    Source,
)
from presyowatch.sources.bantay_presyo import AGRICULTURAL_INPUT_GROUPS

router = APIRouter()

MAX_LIMIT = 500
DEFAULT_LIMIT = 100

HTTP_404_NOT_FOUND = 404
HTTP_422_UNPROCESSABLE = 422
"""Written out rather than taken from ``starlette.status``.

That module is mid-rename — ``HTTP_422_UNPROCESSABLE_ENTITY`` now emits a deprecation
warning in favour of ``HTTP_422_UNPROCESSABLE_CONTENT`` — and these two numbers are not
going to change.
"""

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Rows per page.")]
Offset = Annotated[int, Query(ge=0, description="Rows to skip.")]

# Imported from the parser rather than restated, so "what counts as a farm input" has one
# definition. A second copy here would drift the first time a source added a group.
_INPUT_GROUPS = tuple(sorted(AGRICULTURAL_INPUT_GROUPS))


def _count(session: Session, statement: Select[Any]) -> int:
    """Return how many rows the same filters match, ignoring paging.

    Counted over the statement's own subquery rather than by rebuilding the filters, so the
    total can never drift from the page it describes.
    """
    return session.scalar(select(func.count()).select_from(statement.subquery())) or 0


@router.get("/meta/sources", response_model=list[SourceOut], tags=["meta"])
def list_sources(session: Annotated[Session, Depends(get_session)]) -> list[Source]:
    """Every publisher, with its licence and attribution text.

    Attribution is a condition of use rather than a courtesy (CLAUDE.md rule 8), so it is
    served alongside the data instead of living only in a README the dashboard might not
    read.
    """
    return list(session.scalars(select(Source).order_by(Source.slug)))


@router.get("/meta/runs", response_model=Page[RunOut], tags=["meta"])
def list_runs(
    session: Annotated[Session, Depends(get_session)],
    source: Annotated[str | None, Query(description="Source slug.")] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[RunOut]:
    """Recent ingestion runs, newest first.

    Public on purpose. PLANNING.md treats observability as a feature: a run that failed, or
    a day with no run at all, should be visible to anyone rather than buried in a log.
    """
    statement = (
        select(IngestionRun, Source.slug)
        .join(Source, Source.id == IngestionRun.source_id)
        .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc())
    )
    if source is not None:
        statement = statement.where(Source.slug == source)

    total = _count(session, statement)
    rows = session.execute(statement.limit(limit).offset(offset)).all()
    items = [
        RunOut(
            run_id=run.run_id,
            source_slug=slug,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            files_seen=run.files_seen,
            files_fetched=run.files_fetched,
            rows_upserted=run.rows_upserted,
            rows_quarantined=run.rows_quarantined,
            error=run.error,
        )
        for run, slug in rows
    ]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/regions", response_model=list[RegionOut], tags=["reference"])
def list_regions(session: Annotated[Session, Depends(get_session)]) -> list[Region]:
    """Every seeded region, keyed by PSGC code."""
    return list(session.scalars(select(Region).order_by(Region.psgc_code)))


@router.get("/markets", response_model=Page[MarketOut], tags=["reference"])
def list_markets(
    session: Annotated[Session, Depends(get_session)],
    region: Annotated[str | None, Query(description="PSGC code.")] = None,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[MarketOut]:
    """Monitored markets, the finest granularity the source publishes."""
    statement = select(Market).order_by(Market.municipality, Market.name, Market.id)
    if region is not None:
        statement = statement.where(Market.region_psgc_code == region)

    total = _count(session, statement)
    markets = session.scalars(statement.limit(limit).offset(offset)).all()
    return Page(
        items=[MarketOut.model_validate(market) for market in markets],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/commodities", response_model=Page[CommodityOut], tags=["reference"])
def list_commodities(
    session: Annotated[Session, Depends(get_session)],
    q: Annotated[str | None, Query(description="Case-insensitive substring of the name.")] = None,
    group: Annotated[str | None, Query(description="Exact commodity group.")] = None,
    include_agricultural_inputs: Annotated[
        bool, Query(description="Include feeds, fertiliser and pesticides.")
    ] = True,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[CommodityOut]:
    """The canonical commodity list — what the dashboard's search box searches."""
    statement = select(Commodity).order_by(Commodity.group, Commodity.name, Commodity.id)
    if q:
        statement = statement.where(Commodity.name.ilike(f"%{q}%"))
    if group:
        statement = statement.where(Commodity.group == group)
    if not include_agricultural_inputs:
        statement = statement.where(Commodity.group.notin_(_INPUT_GROUPS))

    total = _count(session, statement)
    found = session.scalars(statement.limit(limit).offset(offset)).all()
    return Page(
        items=[_commodity_out(commodity) for commodity in found],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/commodities/{canonical_slug}", response_model=CommodityOut, tags=["reference"])
def get_commodity(
    canonical_slug: str, session: Annotated[Session, Depends(get_session)]
) -> CommodityOut:
    """One commodity by its stable slug.

    Raises:
        HTTPException: 404 if no commodity has that slug.
    """
    found = session.scalar(select(Commodity).where(Commodity.canonical_slug == canonical_slug))
    if found is None:
        raise HTTPException(HTTP_404_NOT_FOUND, f"no commodity {canonical_slug!r}")
    return _commodity_out(found)


def _commodity_out(commodity: Commodity) -> CommodityOut:
    """Derive the presentation shape, including the farm-input flag.

    Derived from the group at read time rather than stored on every observation: the group
    already determines it, and a duplicated column is a second source of truth that will
    eventually disagree with the first.
    """
    return CommodityOut(
        canonical_slug=commodity.canonical_slug,
        group=commodity.group,
        name=commodity.name,
        specification=commodity.specification,
        unit=commodity.unit,
        is_agricultural_input=commodity.group.upper() in AGRICULTURAL_INPUT_GROUPS,
    )


@router.get("/observations", response_model=Page[ObservationOut], tags=["prices"])
def list_observations(
    session: Annotated[Session, Depends(get_session)],
    commodity: Annotated[str | None, Query(description="Canonical slug.")] = None,
    market: Annotated[int | None, Query(description="Market id.")] = None,
    region: Annotated[str | None, Query(description="PSGC code.")] = None,
    source: Annotated[str | None, Query(description="Source slug.")] = None,
    date_from: Annotated[date | None, Query(description="Earliest observation date.")] = None,
    date_to: Annotated[date | None, Query(description="Latest observation date.")] = None,
    include_unavailable: Annotated[
        bool, Query(description="Include rows the source listed with no figures.")
    ] = False,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[ObservationOut]:
    """The time series. Oldest first, so a chart can plot the page as it arrives.

    ``include_unavailable`` defaults to *false* because a client asking for a price series
    usually wants prices; the rows are real data and stay available, but a caller has to ask
    for them rather than having to remember to filter them out.

    Raises:
        HTTPException: 422 if ``date_from`` is later than ``date_to``, which returns nothing
            and is always a mistake rather than an empty result worth reporting.
    """
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            HTTP_422_UNPROCESSABLE,
            f"date_from {date_from.isoformat()} is after date_to {date_to.isoformat()}",
        )

    statement = (
        select(PriceObservation, Commodity.canonical_slug, Market, Source.slug)
        .join(Commodity, Commodity.id == PriceObservation.commodity_id)
        .join(Market, Market.id == PriceObservation.market_id)
        .join(Source, Source.id == PriceObservation.source_id)
        .order_by(PriceObservation.observed_on, PriceObservation.id)
    )
    if commodity is not None:
        statement = statement.where(Commodity.canonical_slug == commodity)
    if market is not None:
        statement = statement.where(PriceObservation.market_id == market)
    if region is not None:
        statement = statement.where(Market.region_psgc_code == region)
    if source is not None:
        statement = statement.where(Source.slug == source)
    if date_from is not None:
        statement = statement.where(PriceObservation.observed_on >= date_from)
    if date_to is not None:
        statement = statement.where(PriceObservation.observed_on <= date_to)
    if not include_unavailable:
        statement = statement.where(PriceObservation.unavailable.is_(False))

    total = _count(session, statement)
    rows = session.execute(statement.limit(limit).offset(offset)).all()
    items = [
        ObservationOut(
            observed_on=observation.observed_on,
            commodity_slug=slug,
            market_id=market_row.id,
            market=market_row.name,
            municipality=market_row.municipality,
            region_psgc_code=market_row.region_psgc_code,
            source_slug=source_slug,
            low=observation.low,
            high=observation.high,
            prevailing=observation.prevailing,
            average=observation.average,
            unavailable=observation.unavailable,
            revision_no=observation.revision_no,
            ingested_at=observation.ingested_at,
        )
        for observation, slug, market_row, source_slug in rows
    ]
    return Page(items=items, total=total, limit=limit, offset=offset)


MAX_WINDOW_DAYS = 365
DEFAULT_WINDOW_DAYS = 7
MIN_OBSERVATIONS_FOR_A_MOVE = 2


@router.get("/movers", response_model=Page[MoverOut], tags=["prices"])
def list_movers(
    session: Annotated[Session, Depends(get_session)],
    window_days: Annotated[
        int, Query(ge=1, le=MAX_WINDOW_DAYS, description="Length of the comparison window.")
    ] = DEFAULT_WINDOW_DAYS,
    as_of: Annotated[
        date | None, Query(description="End of the window. Defaults to the latest data.")
    ] = None,
    region: Annotated[str | None, Query(description="PSGC code.")] = None,
    include_agricultural_inputs: Annotated[
        bool, Query(description="Include feeds, fertiliser and pesticides.")
    ] = False,
    limit: Limit = 20,
    offset: Offset = 0,
) -> Page[MoverOut]:
    """Commodities whose price moved most, largest absolute percentage first.

    ``as_of`` defaults to the latest date in the data rather than to today. A run can be
    late, or a source can skip a day, and anchoring on the wall clock would quietly return an
    empty table on any morning the ingester had not finished yet — which reads as "nothing
    moved" rather than "nothing is loaded".

    Farm inputs are excluded by default here, unlike on ``/commodities``. A "biggest movers"
    table is read as a story about food, and fertiliser priced by the sack swamps it.
    """
    anchor = as_of or session.scalar(select(func.max(PriceObservation.observed_on)))
    if anchor is None:
        return Page(items=[], total=0, limit=limit, offset=offset)
    start = anchor - timedelta(days=window_days)

    window = (
        select(
            PriceObservation.commodity_id.label("commodity_id"),
            PriceObservation.market_id.label("market_id"),
            PriceObservation.observed_on.label("observed_on"),
            PriceObservation.average.label("average"),
            func.row_number()
            .over(
                partition_by=(PriceObservation.commodity_id, PriceObservation.market_id),
                order_by=PriceObservation.observed_on.asc(),
            )
            .label("from_start"),
            func.row_number()
            .over(
                partition_by=(PriceObservation.commodity_id, PriceObservation.market_id),
                order_by=PriceObservation.observed_on.desc(),
            )
            .label("from_end"),
            func.count()
            .over(partition_by=(PriceObservation.commodity_id, PriceObservation.market_id))
            .label("observations"),
        )
        # A null average carries no comparison, and a zero would make the percentage
        # infinite. Blanks are stored as NULL rather than 0 precisely so this stays simple.
        .where(PriceObservation.average.is_not(None))
        .where(PriceObservation.average > 0)
        .where(PriceObservation.observed_on > start)
        .where(PriceObservation.observed_on <= anchor)
        .subquery()
    )

    earliest = aliased(window, name="earliest")
    latest = aliased(window, name="latest")
    change = latest.c.average - earliest.c.average
    percent = change * 100 / earliest.c.average

    statement = (
        select(
            Commodity,
            Market,
            earliest.c.observed_on,
            latest.c.observed_on,
            earliest.c.average,
            latest.c.average,
            change,
            percent,
            latest.c.observations,
        )
        .select_from(earliest)
        .join(
            latest,
            (latest.c.commodity_id == earliest.c.commodity_id)
            & (latest.c.market_id == earliest.c.market_id),
        )
        .join(Commodity, Commodity.id == earliest.c.commodity_id)
        .join(Market, Market.id == earliest.c.market_id)
        .where(earliest.c.from_start == 1)
        .where(latest.c.from_end == 1)
        # One figure in the window is not a movement, it is a single price.
        .where(latest.c.observations >= MIN_OBSERVATIONS_FOR_A_MOVE)
        # Commodity and market last, so paging is stable when two rows moved identically.
        .order_by(func.abs(percent).desc(), Commodity.id, Market.id)
    )
    if region is not None:
        statement = statement.where(Market.region_psgc_code == region)
    if not include_agricultural_inputs:
        statement = statement.where(Commodity.group.notin_(_INPUT_GROUPS))

    total = _count(session, statement)
    rows = session.execute(statement.limit(limit).offset(offset)).all()
    items = [
        MoverOut(
            commodity_slug=commodity.canonical_slug,
            commodity=commodity.name,
            group=commodity.group,
            unit=commodity.unit,
            market_id=market_row.id,
            market=market_row.name,
            municipality=market_row.municipality,
            region_psgc_code=market_row.region_psgc_code,
            first_observed_on=first_on,
            last_observed_on=last_on,
            first_average=first_average,
            last_average=last_average,
            change=amount,
            percent_change=float(share),
            observations=count,
        )
        for (
            commodity,
            market_row,
            first_on,
            last_on,
            first_average,
            last_average,
            amount,
            share,
            count,
        ) in rows
    ]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/anomalies", response_model=Page[FlaggedOut], tags=["prices"])
def list_anomalies(
    session: Annotated[Session, Depends(get_session)],
    commodity: Annotated[str, Query(description="Canonical slug. Required.")],
    market: Annotated[int | None, Query(description="Market id.")] = None,
    date_from: Annotated[date | None, Query(description="Earliest observation date.")] = None,
    date_to: Annotated[date | None, Query(description="Latest observation date.")] = None,
    window: Annotated[
        int, Query(ge=3, le=61, description="Neighbouring days compared.")
    ] = DEFAULT_WINDOW,
    threshold: Annotated[
        float, Query(gt=0, le=50, description="Modified z-score cut-off.")
    ] = DEFAULT_THRESHOLD,
    only_flagged: Annotated[bool, Query(description="Return only the flagged rows.")] = True,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[FlaggedOut]:
    """Annotate one commodity's prices, per market.

    A commodity is required rather than optional. Flagging is only meaningful within a single
    series, and running it across everything at once would compare the price of rice with the
    price of fertiliser and report the difference as remarkable.

    Each market is judged separately for the same reason — a Butuan price is not an anomalous
    Tandag price — so the series are split before anything is computed.

    ``window`` and ``threshold`` are query parameters because PLANNING.md requires them to be
    configuration rather than magic numbers, and because a reader who distrusts a flag should
    be able to move the goalposts and watch what happens.
    """
    statement = (
        select(PriceObservation, Market)
        .join(Market, Market.id == PriceObservation.market_id)
        .join(Commodity, Commodity.id == PriceObservation.commodity_id)
        .where(Commodity.canonical_slug == commodity)
        .order_by(PriceObservation.market_id, PriceObservation.observed_on)
    )
    if market is not None:
        statement = statement.where(PriceObservation.market_id == market)
    if date_from is not None:
        statement = statement.where(PriceObservation.observed_on >= date_from)
    if date_to is not None:
        statement = statement.where(PriceObservation.observed_on <= date_to)

    config = AnomalyConfig(window=window, threshold=threshold)
    by_market: dict[int, list[tuple[PriceObservation, Market]]] = {}
    for observation, market_row in session.execute(statement).tuples().all():
        by_market.setdefault(observation.market_id, []).append((observation, market_row))

    flagged: list[FlaggedOut] = []
    for rows in by_market.values():
        points = [
            Point(
                observed_on=observation.observed_on,
                average=observation.average,
                low=observation.low,
                high=observation.high,
            )
            for observation, _ in rows
        ]
        for (observation, market_row), flag in zip(rows, flag_series(points, config), strict=True):
            if only_flagged and not (flag.is_anomaly or flag.is_impossible):
                continue
            flagged.append(
                FlaggedOut(
                    observed_on=flag.observed_on,
                    commodity_slug=commodity,
                    market_id=market_row.id,
                    market=market_row.name,
                    average=observation.average,
                    low=observation.low,
                    high=observation.high,
                    # An infinite score is arithmetically true and useless to a reader, so it
                    # travels as null with the explanation carried in `reason` instead.
                    score=_finite(flag.score),
                    is_anomaly=flag.is_anomaly,
                    is_impossible=flag.is_impossible,
                    reason=flag.reason,
                )
            )

    flagged.sort(key=lambda item: (item.observed_on, item.market_id))
    return Page(
        items=flagged[offset : offset + limit],
        total=len(flagged),
        limit=limit,
        offset=offset,
    )


def _finite(score: float | None) -> float | None:
    """Return ``score`` unless it is infinite, which JSON cannot represent anyway."""
    if score is None or score in (float("inf"), float("-inf")):
        return None
    return score


@router.get("/meta/quality", response_model=Quality, tags=["meta"])
def data_quality(session: Annotated[Session, Depends(get_session)]) -> Quality:
    """What loaded, and what did not.

    Public and deliberately unflattering. A page that only reports successes is not evidence
    of anything, so the quarantine counts and the never-observed commodities sit beside the
    row counts rather than behind them.
    """
    run_statement = (
        select(
            Source,
            func.count(IngestionRun.id),
            func.count(IngestionRun.id).filter(IngestionRun.status == "succeeded"),
            func.count(IngestionRun.id).filter(IngestionRun.status == "failed"),
            func.count(IngestionRun.id).filter(IngestionRun.status == "partial"),
            func.max(IngestionRun.started_at),
            func.max(IngestionRun.started_at).filter(IngestionRun.status == "succeeded"),
            func.sum(IngestionRun.rows_upserted),
            func.sum(IngestionRun.rows_quarantined),
        )
        # Outer, so a seeded source that has never run appears with zeroes rather than
        # vanishing. "No run has ever happened" is the most important thing this page can
        # say, and an inner join would hide exactly that.
        .outerjoin(IngestionRun, IngestionRun.source_id == Source.id)
        .group_by(Source.id)
        .order_by(Source.slug)
    )
    sources = [
        SourceQuality(
            slug=source.slug,
            name=source.name,
            runs=runs,
            succeeded=succeeded,
            failed=failed,
            partial=partial,
            last_run_at=last_run,
            last_success_at=last_success,
            rows_upserted=upserted or 0,
            rows_quarantined=quarantined or 0,
        )
        for (
            source,
            runs,
            succeeded,
            failed,
            partial,
            last_run,
            last_success,
            upserted,
            quarantined,
        ) in session.execute(run_statement).tuples()
    ]

    quarantine_statement = (
        select(
            QuarantinedRow.stage,
            func.count(QuarantinedRow.id),
            func.min(QuarantinedRow.reason),
        )
        .group_by(QuarantinedRow.stage)
        .order_by(func.count(QuarantinedRow.id).desc())
    )
    quarantine = [
        QuarantineCount(stage=stage, rows=rows, example_reason=reason)
        for stage, rows, reason in session.execute(quarantine_statement).tuples()
    ]

    totals = session.execute(
        select(
            func.count(PriceObservation.id),
            func.count(PriceObservation.id).filter(PriceObservation.unavailable.is_(True)),
            func.count(PriceObservation.id).filter(
                PriceObservation.average.is_not(None)
                & (
                    (PriceObservation.average < PriceObservation.low)
                    | (PriceObservation.average > PriceObservation.high)
                )
            ),
            func.min(PriceObservation.observed_on),
            func.max(PriceObservation.observed_on),
        )
    ).one()

    return Quality(
        sources=sources,
        quarantine=quarantine,
        observations=totals[0] or 0,
        unavailable=totals[1] or 0,
        impossible=totals[2] or 0,
        commodities_seeded=session.scalar(select(func.count()).select_from(Commodity)) or 0,
        commodities_observed=session.scalar(
            select(func.count(func.distinct(PriceObservation.commodity_id)))
        )
        or 0,
        earliest_observed_on=totals[3],
        latest_observed_on=totals[4],
    )
