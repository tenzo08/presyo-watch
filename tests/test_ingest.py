"""End-to-end tests for the backfill runner.

Real index page, real PDFs, real database — only the socket is replaced. The transport
serves the committed fixtures for the URLs they were actually fetched from, taken from the
committed index rather than written by hand, and **404s everything else**. So a run over the
real listing meets the same dead links, mislabelled dates and unreadable layouts that
``caraga.da.gov.ph`` serves, without depending on a government server being up.

Where a test needs a specific set of files, it builds an index page from the real hrefs and
link text of exactly those entries. That is a narrower listing, not a friendlier one: every
href and label in it is verbatim from the source.
"""

import datetime as dt
from collections.abc import Callable
from functools import cache
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from presyowatch.__main__ import EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, build_parser
from presyowatch.cache import RawCache
from presyowatch.db.models import (
    IngestionRun,
    Market,
    PriceObservation,
    QuarantinedRow,
    Source,
)
from presyowatch.db.seed import seed_reference_data
from presyowatch.ingest import DEFAULT_LOOKBACK, BackfillOutcome, run_backfill
from presyowatch.net.client import HttpClient
from presyowatch.places import RegionResolver, SeedRegion
from presyowatch.sources.index import CARAGA, scrape_index
from tests.conftest import (
    FIXTURES,
    PDF_FIXTURES,
    TEST_USER_AGENT,
    UNREADABLE_SHEET_NAME,
    FakeClock,
)

INDEX_FIXTURE = FIXTURES / "index" / "caraga.da.gov.ph_price-monitoring.html"
ROBOTS_FIXTURE = FIXTURES / "robots" / "caraga.da.gov.ph.robots.txt"

# `lookback=0` from this date admits exactly the three entries dated on or after it: two
# sheets published that day, and the file whose *filename* says 2029 while its header says
# 2026-07-19. That last one is the point — see the index-date tests below.
RUN_DATE = dt.date(2026, 7, 28)
NO_LOOKBACK = dt.timedelta(0)

LUHA_JULY_28 = "Luha-Public-Market.xlsx-July-28-2026.pdf"
SAN_JOSE_JULY_28 = "San-Jose-Public-Market_July-28-2026.pdf"
MISLABELLED_2029 = "Mayor-Salvador-Calo-July-19-2029.pdf"
REPUBLISHED = ("ADS-April-23-2026.pdf", "SanFracisco-April-23-2026.pdf")
APRIL_23 = dt.date(2026, 4, 23)


@cache
def _index_entries() -> dict[str, tuple[str, str]]:
    """Return ``{filename: (url, link_text)}`` for every entry on the committed index."""
    html = INDEX_FIXTURE.read_text(encoding="utf-8", errors="replace")
    scrape = scrape_index(html, page_url=CARAGA.index_url, spec=CARAGA)
    return {entry.filename: (entry.url, entry.link_text) for entry in scrape.entries}


def index_page(*filenames: str) -> str:
    """Build an index page listing only ``filenames``, with their real hrefs and labels."""
    links = []
    for name in filenames:
        url, text = _index_entries()[name]
        links.append(f'<li><a href="{url}">{text or name}</a></li>')
    return "<html><body><ul>" + "".join(links) + "</ul></body></html>"


def caraga_transport(index_html: str, *, serve: set[str] | None = None) -> httpx.MockTransport:
    """A transport that answers as ``caraga.da.gov.ph`` does, from the committed fixtures.

    Args:
        index_html: What the index page returns.
        serve: Filenames the host will serve. Anything outside it 404s, which is what the
            real host does for several hrefs on its own index.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, content=ROBOTS_FIXTURE.read_bytes())
        if path.rstrip("/") == "/price-monitoring":
            return httpx.Response(200, text=index_html)

        name = path.rsplit("/", 1)[-1]
        pdf = PDF_FIXTURES / name
        if pdf.is_file() and (serve is None or name in serve):
            return httpx.Response(
                200, content=pdf.read_bytes(), headers={"Content-Type": "application/pdf"}
            )
        return httpx.Response(404, text="Not Found")

    return httpx.MockTransport(handle)


@pytest.fixture
def clientfactory(clock: FakeClock) -> Callable[..., HttpClient]:
    """Build an HttpClient over a fixture-backed transport, with no real waiting."""

    def build(index_html: str, *, serve: set[str] | None = None) -> HttpClient:
        return HttpClient(
            user_agent=TEST_USER_AGENT,
            transport=caraga_transport(index_html, serve=serve),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    return build


@pytest.fixture
def raw_cache(tmp_path: Path) -> RawCache:
    return RawCache(tmp_path / "raw")


@pytest.fixture
def reference(session_factory: sessionmaker[Session]) -> None:
    """Load the committed reference data, as a deploy would, before any run."""
    with session_factory() as session:
        seed_reference_data(session)
        session.commit()


@pytest.fixture
def real_index() -> str:
    return INDEX_FIXTURE.read_text(encoding="utf-8", errors="replace")


def run(
    session_factory: sessionmaker[Session],
    client: HttpClient,
    raw_cache: RawCache,
    *,
    today: dt.date = RUN_DATE,
    lookback: dt.timedelta = NO_LOOKBACK,
    regions: RegionResolver | None = None,
) -> BackfillOutcome:
    """Call the runner with this module's defaults."""
    return run_backfill(
        session_factory=session_factory,
        client=client,
        cache=raw_cache,
        spec=CARAGA,
        today=today,
        lookback=lookback,
        regions=regions,
        run_id="testrun",
    )


def observations(session: Session) -> list[PriceObservation]:
    session.expire_all()
    return list(session.scalars(select(PriceObservation).order_by(PriceObservation.id)))


def quarantine(session: Session, stage: str | None = None) -> list[QuarantinedRow]:
    session.expire_all()
    statement = select(QuarantinedRow).order_by(QuarantinedRow.id)
    if stage is not None:
        statement = statement.where(QuarantinedRow.stage == stage)
    return list(session.scalars(statement))


# -- a run over the real listing --------------------------------------------------


def test_a_run_stores_observations_from_the_real_index(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
    real_index: str,
) -> None:
    served = {LUHA_JULY_28, SAN_JOSE_JULY_28, MISLABELLED_2029}
    outcome = run(session_factory, clientfactory(real_index, serve=served), raw_cache)

    stored = observations(session)
    assert outcome.status == "succeeded"
    assert outcome.files_seen == 3
    assert outcome.files_fetched == 3
    assert len(stored) > 350, "three sheets of about 140 resolvable rows each"
    assert {row.observed_on for row in stored} == {
        dt.date(2026, 7, 28),
        dt.date(2026, 7, 19),
    }


def test_a_run_writes_its_ingestion_row(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
    real_index: str,
) -> None:
    """A run that left no row would be invisible, which is the failure mode to avoid."""
    served = {LUHA_JULY_28, SAN_JOSE_JULY_28, MISLABELLED_2029}
    outcome = run(session_factory, clientfactory(real_index, serve=served), raw_cache)

    session.expire_all()
    row = session.scalars(select(IngestionRun)).one()
    assert row.run_id == "testrun"
    assert row.status == "succeeded"
    assert row.finished_at is not None
    assert row.files_seen == outcome.files_seen
    assert row.rows_upserted > 0
    assert row.error is None


def test_the_markets_a_run_meets_are_created_from_the_sheets(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
    real_index: str,
) -> None:
    served = {LUHA_JULY_28, SAN_JOSE_JULY_28, MISLABELLED_2029}
    run(session_factory, clientfactory(real_index, serve=served), raw_cache)

    session.expire_all()
    markets = {(row.municipality, row.name) for row in session.scalars(select(Market))}
    assert markets == {
        ("Tandag City", "Luha Public Market"),
        ("San Jose", "San Jose Public Market"),
        ("Butuan City", "Mayor Salvador Calo Public Market"),
    }


def test_running_twice_writes_nothing_the_second_time(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
    real_index: str,
) -> None:
    """Rule 5's other half: a backfill overlaps itself every day and must converge.

    The second run also fetches nothing — every file is already in the content-addressed
    cache — which is rule 3 holding at the level of a whole run.
    """
    served = {LUHA_JULY_28, SAN_JOSE_JULY_28}
    first = run(session_factory, clientfactory(real_index, serve=served), raw_cache)
    before = [
        (row.id, row.low, row.high, row.revision_no, row.ingested_at)
        for row in observations(session)
    ]

    second = run(session_factory, clientfactory(real_index, serve=served), raw_cache)

    after = [
        (row.id, row.low, row.high, row.revision_no, row.ingested_at)
        for row in observations(session)
    ]
    assert after == before
    assert second.files_fetched == 0
    assert second.rows_upserted == 0
    assert first.rows_upserted > 0


# -- the index date is provisional; the sheet's own date is not -------------------


def test_a_sheet_is_stored_under_its_own_date_not_its_filename(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
) -> None:
    """`Mayor-Salvador-Calo-July-19-2029.pdf` says `Date of Monitoring : July 19, 2026`.

    Quarantining it on the filename year would have thrown away a valid sheet, so the index
    date only decides what to fetch and the header decides what is stored.
    """
    client = clientfactory(index_page(MISLABELLED_2029), serve={MISLABELLED_2029})

    outcome = run(session_factory, client, raw_cache)

    assert outcome.date_mismatches == 1
    assert {row.observed_on for row in observations(session)} == {dt.date(2026, 7, 19)}


def test_a_sheet_dated_after_the_run_is_quarantined(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
) -> None:
    """Judged on the sheet's own date, after parsing — the only date worth judging."""
    client = clientfactory(index_page(MISLABELLED_2029), serve={MISLABELLED_2029})

    outcome = run(session_factory, client, raw_cache, today=dt.date(2026, 7, 18))

    assert outcome.status == "partial"
    assert observations(session) == []
    reasons = [row.reason for row in quarantine(session, "validate")]
    assert any("after the run date" in reason for reason in reasons)


# -- one bad file is a skipped file, never a failed run ---------------------------


def test_a_dead_href_is_skipped_and_recorded(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
) -> None:
    """Caraga's own index lists hrefs that 404. The rest of the run must still land."""
    client = clientfactory(index_page(LUHA_JULY_28, SAN_JOSE_JULY_28), serve={LUHA_JULY_28})

    outcome = run(session_factory, client, raw_cache)

    assert outcome.status == "partial"
    assert outcome.files_failed == 1
    assert observations(session), "the file that did fetch was still stored"
    dead = quarantine(session, "index")
    assert len(dead) == 1
    assert "404" in dead[0].reason
    assert dead[0].source_url is not None
    assert dead[0].payload["filename"] == SAN_JOSE_JULY_28


def test_an_unreadable_sheet_is_quarantined_whole(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
) -> None:
    """The seven-column sheet: fetched fine, parses to nothing anyone should trust."""
    client = clientfactory(
        index_page(UNREADABLE_SHEET_NAME, LUHA_JULY_28),
        serve={UNREADABLE_SHEET_NAME, LUHA_JULY_28},
    )

    outcome = run(session_factory, client, raw_cache, lookback=dt.timedelta(days=120))

    assert outcome.status == "partial"
    parse_failures = quarantine(session, "parse")
    whole_file = [row for row in parse_failures if "no column header row" in row.reason]
    assert len(whole_file) == 1
    assert whole_file[0].source_file_sha256 is not None
    assert observations(session), "the readable sheet still landed"


def test_a_commodity_with_no_alias_is_quarantined_row_by_row(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
) -> None:
    """Never guessed. The raw strings are kept so a person can add the alias later."""
    client = clientfactory(index_page(LUHA_JULY_28), serve={LUHA_JULY_28})

    run(session_factory, client, raw_cache)

    unmapped = quarantine(session, "alias")
    assert unmapped, "this sheet has names the seed does not cover"
    assert all("no alias for" in row.reason for row in unmapped)
    assert all(row.payload["alias_key"] for row in unmapped)


def test_a_province_nobody_has_mapped_stops_at_the_sheet(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
) -> None:
    """An unseeded province cannot be given a PSGC code, so the sheet is not placed.

    Driven by handing the runner a resolver that knows one province, which is what a run
    against a region nobody has seeded would look like.
    """
    client = clientfactory(index_page(SAN_JOSE_JULY_28), serve={SAN_JOSE_JULY_28})
    only_surigao = RegionResolver(
        [SeedRegion(psgc_code="166800000", name="Surigao del Sur", level="province")]
    )

    outcome = run(session_factory, client, raw_cache, regions=only_surigao)

    assert outcome.status == "partial"
    assert observations(session) == []
    reasons = [row.reason for row in quarantine(session, "validate")]
    assert any("Dinagat Islands" in reason for reason in reasons)


# -- the same sheet published twice ----------------------------------------------


def test_a_sheet_republished_under_two_urls_is_stored_once(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
) -> None:
    """Caraga published one San Francisco sheet under two names, byte for byte identical.

    Two URLs, two cache entries, one blob — and the second is not a correction, because the
    upsert compares figures rather than filenames.
    """
    client = clientfactory(index_page(*REPUBLISHED), serve=set(REPUBLISHED))

    outcome = run(session_factory, client, raw_cache, today=APRIL_23)

    assert outcome.files_seen == 2
    assert {row.observed_on for row in observations(session)} == {APRIL_23}
    session.expire_all()
    revisions = session.scalar(select(func.count()).select_from(PriceObservation)) or 0
    assert revisions > 100
    assert all(row.revision_no == 0 for row in observations(session))


# -- failures that do stop a run -------------------------------------------------


def test_an_unreachable_index_fails_the_run_and_still_records_it(
    session_factory: sessionmaker[Session],
    session: Session,
    raw_cache: RawCache,
    reference: None,
    clock: FakeClock,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=ROBOTS_FIXTURE.read_bytes())
        return httpx.Response(503, text="down for maintenance")

    client = HttpClient(
        user_agent=TEST_USER_AGENT,
        transport=httpx.MockTransport(handle),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    outcome = run(session_factory, client, raw_cache)

    assert outcome.status == "failed"
    assert outcome.error is not None
    session.expire_all()
    row = session.scalars(select(IngestionRun)).one()
    assert row.status == "failed"
    assert row.error is not None
    assert "index page unavailable" in row.error


def test_a_robots_disallow_stops_the_run_rather_than_quarantining_everything(
    session_factory: sessionmaker[Session],
    session: Session,
    raw_cache: RawCache,
    reference: None,
    clock: FakeClock,
) -> None:
    """A source saying "do not fetch" is answered by stopping, not by knocking 500 times."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, text="should never be reached")

    client = HttpClient(
        user_agent=TEST_USER_AGENT,
        transport=httpx.MockTransport(handle),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    outcome = run(session_factory, client, raw_cache)

    assert outcome.status == "failed"
    session.expire_all()
    row = session.scalars(select(IngestionRun)).one()
    assert row.error is not None
    assert "robots.txt" in row.error
    assert quarantine(session) == []


def test_a_run_needs_the_reference_data(
    session_factory: sessionmaker[Session],
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    real_index: str,
) -> None:
    """Without a seed there is no source to attribute anything to. Fail, do not improvise."""
    with pytest.raises(LookupError, match="seed"):
        run(session_factory, clientfactory(real_index), raw_cache)


# -- the seed itself --------------------------------------------------------------


def test_seeding_creates_the_source_and_its_regions(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    with session_factory() as opened:
        counts = seed_reference_data(opened)
        opened.commit()

    session.expire_all()
    assert counts.sources_inserted == 1
    assert counts.regions_inserted == 6
    assert counts.commodities_inserted > 100
    assert counts.aliases_inserted == counts.commodities_inserted
    assert session.scalars(select(Source)).one().slug == "da-caraga"


def test_seeding_twice_changes_nothing(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    """It runs on every deploy, so it has to converge rather than accumulate."""
    with session_factory() as opened:
        seed_reference_data(opened)
        opened.commit()

    with session_factory() as opened:
        again = seed_reference_data(opened)
        opened.commit()

    assert again.changed == 0


# -- the command line -------------------------------------------------------------


def test_the_cli_offers_exactly_the_two_things_a_deploy_needs() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])
    assert parser.parse_args(["seed"]).command == "seed"
    assert parser.parse_args(["backfill"]).source == CARAGA.slug


def test_the_cli_defaults_to_the_documented_lookback() -> None:
    """A run reconciles a window, not a day. The default must not quietly become one."""
    assert parser_lookback() == DEFAULT_LOOKBACK.days


def parser_lookback() -> int:
    value = build_parser().parse_args(["backfill"]).lookback_days
    assert isinstance(value, int)
    return value


@pytest.mark.parametrize(
    ("status", "expected"),
    [("succeeded", EXIT_OK), ("partial", EXIT_PARTIAL), ("failed", EXIT_FAILED)],
)
def test_the_exit_code_distinguishes_partial_from_both_others(status: str, expected: int) -> None:
    """A scheduler reads the exit code, and "mostly worked" is its own answer.

    Collapsing partial into success hides a source that has started failing; collapsing it
    into failure makes a run that stored a fortnight of prices look broken.
    """
    codes = {"succeeded": EXIT_OK, "partial": EXIT_PARTIAL, "failed": EXIT_FAILED}

    assert codes[status] == expected
    assert len(set(codes.values())) == 3


def test_files_outside_the_window_are_skipped_not_quarantined(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
    real_index: str,
) -> None:
    """A file from last month is not a failure, and must not be counted as one.

    Caraga lists 451 datable files. Handing the lookback bound to the scraper put every one
    of them outside the window into `scrape.rejected`, and quarantining that wholesale wrote
    about 440 rows per run for files whose only sin was being old — inflating a public data
    quality figure with non-problems, every day, for ever.
    """
    served = {LUHA_JULY_28, SAN_JOSE_JULY_28, MISLABELLED_2029}
    outcome = run(session_factory, clientfactory(real_index, serve=served), raw_cache)

    index_stage = quarantine(session, "index")
    assert outcome.files_seen == 3
    assert not any("is before" in row.reason for row in index_stage)
    # What remains is the real thing: hrefs with no readable date anywhere.
    assert index_stage
    assert all("no date in link text" in row.reason for row in index_stage)


def test_an_undatable_link_is_still_quarantined(
    session_factory: sessionmaker[Session],
    session: Session,
    clientfactory: Callable[..., HttpClient],
    raw_cache: RawCache,
    reference: None,
    real_index: str,
) -> None:
    """81 of Caraga's links carry no year anywhere. Those are a real, recorded loss."""
    run(session_factory, clientfactory(real_index, serve={LUHA_JULY_28}), raw_cache)

    undatable = quarantine(session, "index")
    assert len(undatable) > 50
    assert all(row.source_url for row in undatable)
    assert all(row.payload["href"] for row in undatable)
