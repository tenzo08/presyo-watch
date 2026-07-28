"""The backfill runner: one pass over a source's index, reconciling a window of dates.

CLAUDE.md rule 5: *the ingester backfills*. Each run reconciles every missing date in a
lookback window rather than fetching "today", because a scheduled run is not a promise.
GitHub Actions gives cron no timing SLA, drops runs under load, only fires from the default
branch, and disables the schedule entirely after 60 quiet days (KNOWLEDGE.md). A run that
assumed it fired once per day would leave a permanent hole every time any of that happened.

**What a run does, and what it refuses to let stop it.** The index is re-read every time —
it is the one mutable thing here and is deliberately not cached. Everything it points at is
fetched at most once ever, parsed from the cache, and upserted on the natural key. Along the
way, each of these is a *skipped file*, not a failed run:

- a dead href (Caraga's index carries some),
- bytes that will not parse (one committed fixture is a real seven-column sheet),
- a province nobody has mapped,
- a header date in the future,
- a commodity name with no alias.

Every one of them is written to ``quarantine`` with the reason and enough of the raw payload
to reprocess it later. Dropping a row silently is the one failure this project treats as
unacceptable, because it is the one nobody would ever notice.

**A robots disallow is different and does stop the run.** It is not bad data, it is a source
telling us not to fetch, and the correct response is to stop rather than to record 500
quarantined files and carry on knocking.

**Transactions are per file, not per run.** PLANNING.md asks that one source's failure not
poison another's writes; a transaction per source would also mean that a run failing on file
490 of 500 discards the other 489, and that the database holds a single write lock for the
length of a network-bound run. A file is the natural unit: it either lands whole or not at
all, and a rerun picks up exactly where the last one stopped.

**Ordering matters and is the runner's responsibility.** ``upsert_observations`` compares
figures, not filenames, so whichever file is ingested last wins. Entries are therefore
processed oldest first and, within a date, in the order the index lists them, so a
republished correction is applied after the sheet it corrects.
"""

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from presyowatch.cache import CacheConflictError, CachingFetcher, RawCache
from presyowatch.commodities import CommodityResolver
from presyowatch.commodities import to_quarantine_row as alias_quarantine_row
from presyowatch.db.engine import session_scope
from presyowatch.db.models import (
    Commodity,
    IngestionRun,
    QuarantinedRow,
    RunStatus,
    Source,
)
from presyowatch.db.seed import resolve_market
from presyowatch.db.upsert import PendingObservation, upsert_observations
from presyowatch.log import bind_run_id, get_logger, new_run_id
from presyowatch.net.client import HttpClient
from presyowatch.net.errors import HttpRequestError, RobotsDisallowedError
from presyowatch.places import RegionResolver, SeedRegion
from presyowatch.sources.bantay_presyo import ParsedSheet, SheetParseError, parse_sheet
from presyowatch.sources.bantay_presyo import to_quarantine_row as parse_quarantine_row
from presyowatch.sources.index import (
    IndexEntry,
    IndexSpec,
    fetch_index,
    scrape_index,
)
from presyowatch.sources.index import to_quarantine_row as index_quarantine_row

logger = get_logger(__name__)

DEFAULT_LOOKBACK: Final = timedelta(days=14)
"""How far back a run reconciles.

Fourteen days is comfortably longer than any plausible run of missed schedules, and short
enough that a daily run stays cheap: nearly every file in the window is already cached, so
the cost of the overlap is a dictionary lookup, not a download.
"""


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    """What one run did. Mirrors the ``ingestion_runs`` row it wrote."""

    run_id: str
    status: RunStatus
    files_seen: int = 0
    files_fetched: int = 0
    files_failed: int = 0
    rows_upserted: int = 0
    rows_quarantined: int = 0
    date_mismatches: int = 0
    """Files whose index date disagreed with the date printed on the sheet.

    Counted rather than acted on. The header wins — that is settled — but a disagreement
    means a mislabelled file at the source and belongs on the data quality page.
    """

    error: str | None = None


@dataclass(frozen=True, slots=True)
class _RunContext:
    """Everything that is fixed for the whole run and needed at every stage.

    Bundled because these six values are constants of the run, threaded alike through
    fetching, parsing and storing; passing them one by one made several signatures
    longer than the functions under them.
    """

    source_id: int
    run_id: str
    today: date
    resolver: CommodityResolver
    regions: RegionResolver
    commodity_ids: Mapping[str, int]


@dataclass
class _Tally:
    """Running totals for one backfill, accumulated across per-file transactions."""

    files_seen: int = 0
    files_fetched: int = 0
    files_failed: int = 0
    rows_upserted: int = 0
    rows_quarantined: int = 0
    date_mismatches: int = 0
    quarantine: list[QuarantinedRow] = field(default_factory=list)

    def hold(self, rows: Iterable[QuarantinedRow]) -> None:
        """Stage quarantine records for the current file's transaction."""
        held = list(rows)
        self.quarantine.extend(held)
        self.rows_quarantined += len(held)

    def flush_into(self, session: Session) -> None:
        session.add_all(self.quarantine)
        self.quarantine.clear()


def run_backfill(
    *,
    session_factory: sessionmaker[Session],
    client: HttpClient,
    cache: RawCache,
    spec: IndexSpec,
    today: date,
    lookback: timedelta = DEFAULT_LOOKBACK,
    resolver: CommodityResolver | None = None,
    regions: RegionResolver | None = None,
    run_id: str | None = None,
) -> BackfillOutcome:
    """Reconcile one source's lookback window.

    Args:
        session_factory: Sessions for the per-file transactions.
        client: Used for the index page only. Source files go through ``cache``.
        cache: The permanent content-addressed store.
        spec: Which index page to read and which of its links are source files.
        today: The run's notion of the current date. Passed rather than read from the clock
            so that a run is reproducible and testable.
        lookback: How far back to reconcile.
        resolver: Commodity resolver. Defaults to the committed seed.
        regions: Province resolver. Defaults to the committed seed.
        run_id: Correlation id. Generated if omitted.

    Returns:
        What the run did. The same figures are written to ``ingestion_runs``, including on
        failure — a run that broke must be visible, not absent.
    """
    identifier = run_id or new_run_id()
    bind_run_id(identifier)
    resolver = resolver or CommodityResolver.from_seed()
    regions = regions or RegionResolver.from_seed()

    with session_scope(session_factory) as session:
        source_id = _source_id(session, spec.slug)
        commodity_ids = _commodity_ids(session)
        run_row_id = _open_run(session, run_id=identifier, source_id=source_id)

    context = _RunContext(
        source_id=source_id,
        run_id=identifier,
        today=today,
        resolver=resolver,
        regions=regions,
        commodity_ids=commodity_ids,
    )
    tally = _Tally()
    status: RunStatus = "succeeded"
    error: str | None = None

    try:
        entries = _discover(
            session_factory=session_factory,
            client=client,
            spec=spec,
            context=context,
            not_before=today - lookback,
            tally=tally,
        )
        fetcher = CachingFetcher(client=client, cache=cache)
        for entry in entries:
            tally.files_seen += 1
            _process_entry(
                entry,
                session_factory=session_factory,
                fetcher=fetcher,
                context=context,
                tally=tally,
            )
        if tally.files_failed:
            status = "partial"
    except RobotsDisallowedError as exc:
        # Not bad data. A source saying "do not fetch" is answered by stopping, not by
        # recording hundreds of failures and continuing to knock.
        status, error = "failed", f"robots.txt forbids this source: {exc}"
        logger.exception("backfill_robots_disallowed", source=spec.slug, detail=str(exc))
    except HttpRequestError as exc:
        # Raised here only by the index fetch; a per-file failure is caught per file.
        status, error = "failed", f"index page unavailable: {exc}"
        logger.exception("backfill_index_unavailable", source=spec.slug, detail=str(exc))

    with session_scope(session_factory) as session:
        tally.flush_into(session)
        _close_run(session, run_row_id, status=status, tally=tally, error=error)

    outcome = BackfillOutcome(
        run_id=identifier,
        status=status,
        files_seen=tally.files_seen,
        files_fetched=tally.files_fetched,
        files_failed=tally.files_failed,
        rows_upserted=tally.rows_upserted,
        rows_quarantined=tally.rows_quarantined,
        date_mismatches=tally.date_mismatches,
        error=error,
    )
    logger.info("backfill_finished", source=spec.slug, **asdict(outcome))
    return outcome


def _source_id(session: Session, slug: str) -> int:
    """Return the id of the source ``slug``.

    Raises:
        LookupError: If the source has not been seeded. Writing observations against a
            source that does not exist is not something to paper over.
    """
    found = session.scalar(select(Source.id).where(Source.slug == slug))
    if found is None:
        msg = f"source {slug!r} is not in the database; run the reference data seed first"
        raise LookupError(msg)
    return found


def _commodity_ids(session: Session) -> dict[str, int]:
    """Return every seeded commodity's id, keyed by canonical slug.

    Read once per run rather than per row. The mapping cannot change during a run — the seed
    load happens before ingestion, never during it — so one query replaces about 1,800.

    Raises:
        LookupError: If nothing is seeded. Every row would quarantine, which would look like
            a source problem rather than an empty database.
    """
    rows = session.execute(select(Commodity.canonical_slug, Commodity.id)).tuples().all()
    ids: dict[str, int] = dict(rows)
    if not ids:
        msg = "no commodities in the database; run the reference data seed first"
        raise LookupError(msg)
    return ids


def _open_run(session: Session, *, run_id: str, source_id: int) -> int:
    """Record a run as started, and return the row's id."""
    row = IngestionRun(run_id=run_id, source_id=source_id, status="running")
    session.add(row)
    session.flush()
    logger.info("backfill_started", run_row_id=row.id)
    return row.id


def _close_run(
    session: Session,
    run_row_id: int,
    *,
    status: RunStatus,
    tally: _Tally,
    error: str | None,
) -> None:
    """Finalise the run row. Written even on failure — an invisible run is the worst kind."""
    row = session.get(IngestionRun, run_row_id)
    if row is None:  # pragma: no cover - the row was written by this same process
        return
    row.status = status
    row.finished_at = datetime.now(UTC)
    row.files_seen = tally.files_seen
    row.files_fetched = tally.files_fetched
    row.rows_upserted = tally.rows_upserted
    row.rows_quarantined = tally.rows_quarantined
    row.error = error


def _discover(
    *,
    session_factory: sessionmaker[Session],
    client: HttpClient,
    spec: IndexSpec,
    context: _RunContext,
    not_before: date,
    tally: _Tally,
) -> list[IndexEntry]:
    """Read the index and return the entries to process, oldest first.

    No ``not_after`` bound is applied. Caraga's index really does carry a file dated 2029
    whose sheet says 2026, and rejecting it on the filename would discard a valid sheet
    (KNOWLEDGE.md). The index date decides *what to fetch*; the date printed on the sheet is
    the one stored, and a future date is caught after parsing, where it can be judged on the
    authoritative value.
    """
    html = fetch_index(client, spec)
    scrape = scrape_index(html, page_url=spec.index_url, spec=spec, not_before=not_before)

    with session_scope(session_factory) as session:
        tally.hold(
            index_quarantine_row(rejected, source_id=context.source_id, run_id=context.run_id)
            for rejected in scrape.rejected
        )
        tally.flush_into(session)

    # Stable within a date: `sorted` keeps the index's own order for equal keys, so a
    # correction listed after the sheet it corrects is applied after it.
    return sorted(scrape.entries, key=lambda entry: entry.observed_on)


def _process_entry(
    entry: IndexEntry,
    *,
    session_factory: sessionmaker[Session],
    fetcher: CachingFetcher,
    context: _RunContext,
    tally: _Tally,
) -> None:
    """Fetch, parse and store one file, or quarantine it and carry on.

    The fetch happens outside any transaction. Caraga answers in about eight seconds
    (KNOWLEDGE.md) and holding a database transaction open across that would keep a write
    lock for the length of a network round trip, on a Postgres that charges for connections.
    """
    fetched = _fetch(entry, fetcher=fetcher, context=context, tally=tally)
    if fetched is None:
        return
    body, sha256, from_cache = fetched
    if not from_cache:
        tally.files_fetched += 1

    try:
        sheet = parse_sheet(body)
    except SheetParseError as exc:
        tally.files_failed += 1
        tally.hold(
            [
                QuarantinedRow(
                    source_id=context.source_id,
                    run_id=context.run_id,
                    stage="parse",
                    reason=exc.reason,
                    source_file_sha256=sha256,
                    source_url=entry.url,
                    payload={
                        "filename": entry.filename,
                        "index_date": entry.observed_on.isoformat(),
                    },
                )
            ]
        )
        with session_scope(session_factory) as session:
            tally.flush_into(session)
        logger.info("file_unparseable", url=entry.url, reason=exc.reason)
        return

    if sheet.header.observed_on != entry.observed_on:
        tally.date_mismatches += 1
        logger.info(
            "index_date_disagrees_with_sheet",
            url=entry.url,
            index_date=entry.observed_on.isoformat(),
            sheet_date=sheet.header.observed_on.isoformat(),
        )

    with session_scope(session_factory) as session:
        _store(sheet, session=session, entry=entry, sha256=sha256, context=context, tally=tally)
        tally.flush_into(session)


def _fetch(
    entry: IndexEntry,
    *,
    fetcher: CachingFetcher,
    context: _RunContext,
    tally: _Tally,
) -> tuple[bytes, str, bool] | None:
    """Return ``(body, sha256, from_cache)``, or ``None`` if the file could not be had.

    Raises:
        RobotsDisallowedError: Propagated, because it is about the source rather than the
            file and must end the run.
    """
    try:
        result = fetcher.fetch_once(entry.url)
    except HttpRequestError as exc:
        # Caraga's index lists hrefs that 404. One dead link is a skipped file.
        tally.files_failed += 1
        tally.hold([_file_quarantine(entry, context, f"could not fetch: {exc}")])
        logger.info("file_unavailable", url=entry.url, detail=str(exc))
        return None
    except CacheConflictError as exc:
        # A URL we have already stored is now serving different bytes. Keeping the old bytes
        # silently would hide a source rewriting history; replacing them would destroy the
        # evidence. Quarantine the fact and move on.
        tally.files_failed += 1
        tally.hold([_file_quarantine(entry, context, f"cache conflict: {exc}")])
        logger.warning("file_cache_conflict", url=entry.url, detail=str(exc))
        return None
    return result.read_bytes(), result.entry.sha256, result.from_cache


def _file_quarantine(entry: IndexEntry, context: _RunContext, reason: str) -> QuarantinedRow:
    return QuarantinedRow(
        source_id=context.source_id,
        run_id=context.run_id,
        stage="index",
        reason=reason,
        source_url=entry.url,
        payload={
            "href": entry.url,
            "filename": entry.filename,
            "link_text": entry.link_text,
            "index_date": entry.observed_on.isoformat(),
        },
    )


def _store(
    sheet: ParsedSheet,
    *,
    session: Session,
    entry: IndexEntry,
    sha256: str,
    context: _RunContext,
    tally: _Tally,
) -> None:
    """Turn one parsed sheet into observations, quarantining whatever cannot be placed."""
    tally.hold(
        parse_quarantine_row(
            rejected,
            source_id=context.source_id,
            run_id=context.run_id,
            source_file_sha256=sha256,
        )
        for rejected in sheet.rejected
    )

    placement = _place(sheet, today=context.today, regions=context.regions)
    if isinstance(placement, str):
        tally.files_failed += 1
        tally.hold(
            [
                QuarantinedRow(
                    source_id=context.source_id,
                    run_id=context.run_id,
                    stage="validate",
                    reason=placement,
                    source_file_sha256=sha256,
                    source_url=entry.url,
                    payload={
                        "province": sheet.header.province,
                        "municipality": sheet.header.municipality,
                        "market": sheet.header.market,
                        "observed_on": sheet.header.observed_on.isoformat(),
                    },
                )
            ]
        )
        logger.info("sheet_unusable", url=entry.url, reason=placement)
        return

    market = resolve_market(
        session,
        region_psgc_code=placement.psgc_code,
        municipality=sheet.header.municipality,
        market=sheet.header.market,
    )

    pending: list[PendingObservation] = []
    for row in sheet.rows:
        resolution = context.resolver.resolve(row)
        if resolution.commodity is None:
            tally.hold(
                [
                    alias_quarantine_row(
                        row,
                        resolution,
                        source_id=context.source_id,
                        run_id=context.run_id,
                        source_file_sha256=sha256,
                    )
                ]
            )
            continue
        pending.append(
            PendingObservation(
                source_id=context.source_id,
                market_id=market.id,
                commodity_id=context.commodity_ids[resolution.commodity.canonical_slug],
                observed_on=sheet.header.observed_on,
                low=row.low,
                high=row.high,
                prevailing=row.prevailing,
                average=row.average,
                unavailable=row.unavailable,
                source_file_sha256=sha256,
            )
        )

    outcome = upsert_observations(session, pending)
    tally.rows_upserted += outcome.written
    tally.hold(
        QuarantinedRow(
            source_id=context.source_id,
            run_id=context.run_id,
            stage="validate",
            reason=conflict.reason,
            source_file_sha256=sha256,
            source_url=entry.url,
            payload={"natural_key": [str(part) for part in conflict.key]},
        )
        for conflict in outcome.conflicts
    )


def _place(sheet: ParsedSheet, *, today: date, regions: RegionResolver) -> SeedRegion | str:
    """Return the region a sheet belongs to, or a string saying why it cannot be stored.

    A future observation date is judged here rather than at index stage, on the sheet's own
    date rather than its filename's. That ordering is the whole lesson of the 2029 file: its
    filename lies and its header is right, so the check has to come after parsing or it
    throws away a valid sheet.
    """
    region = regions.resolve(sheet.header.province)
    if region is None:
        return f"province {sheet.header.province!r} is not a seeded region"
    if sheet.header.observed_on > today:
        return (
            f"sheet is dated {sheet.header.observed_on.isoformat()}, "
            f"after the run date {today.isoformat()}"
        )
    return region
