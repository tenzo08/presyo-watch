"""Tests for the anomaly and data-quality endpoints.

The anomaly case is the real one from the corpus: a row whose average sits below its own
low, produced by the source dropping a digit.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from presyowatch.api.app import create_app
from presyowatch.db.models import (
    Commodity,
    IngestionRun,
    Market,
    PriceObservation,
    QuarantinedRow,
    Source,
)
from presyowatch.db.seed import seed_reference_data

START = date(2026, 7, 1)


@dataclass(frozen=True, slots=True)
class Priced:
    """What the fixture below inserted, so tests can name it rather than index a dict."""

    commodity_id: int
    slug: str
    market: int


@pytest.fixture
def client(engine: Engine, session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    app = create_app(engine=engine)
    with TestClient(app) as opened:
        app.state.session_factory = session_factory
        yield opened


@pytest.fixture
def priced(session: Session) -> Priced:
    """Eight flat days at 45.00 with one impossible row, mirroring the real Corn Cracked."""
    seed_reference_data(session)
    session.flush()

    source_id = session.scalar(select(Source.id))
    commodity = session.scalar(select(Commodity).order_by(Commodity.id))
    assert source_id is not None
    assert commodity is not None

    market = Market(
        region_psgc_code="166800000", name="Luha Public Market", municipality="Tandag City"
    )
    session.add(market)
    session.flush()

    for offset in range(8):
        # Day 4 is the source's dropped digit: low and high say 45.00, the average says 4.00.
        impossible = offset == 4
        session.add(
            PriceObservation(
                source_id=source_id,
                market_id=market.id,
                commodity_id=commodity.id,
                observed_on=START + timedelta(days=offset),
                low=Decimal("45.00"),
                high=Decimal("45.00"),
                prevailing=Decimal("45.00"),
                average=Decimal("4.00") if impossible else Decimal("45.00"),
                revision_no=0,
                source_file_sha256="a" * 64,
            )
        )
    session.add(
        IngestionRun(
            run_id="r1",
            source_id=source_id,
            status="succeeded",
            rows_upserted=8,
            rows_quarantined=3,
        )
    )
    session.add(
        QuarantinedRow(
            source_id=source_id,
            run_id="r1",
            stage="alias",
            reason="no alias for 'fish | bangus | large'",
            payload={"alias_key": "fish | bangus | large"},
        )
    )
    session.commit()
    return Priced(commodity_id=commodity.id, slug=commodity.canonical_slug, market=market.id)


# -- anomalies --------------------------------------------------------------------


def test_the_impossible_row_is_reported(client: TestClient, priced: Priced) -> None:
    body = client.get("/anomalies", params={"commodity": priced.slug}).json()

    assert body["total"] == 1
    flagged = body["items"][0]
    assert flagged["is_impossible"] is True
    assert flagged["average"] == "4.00"
    assert "below low" in flagged["reason"]


def test_the_flag_carries_the_prices_it_contradicts(client: TestClient, priced: Priced) -> None:
    """A claim a reader cannot check is not worth publishing."""
    flagged = client.get("/anomalies", params={"commodity": priced.slug}).json()["items"][0]

    assert flagged["low"] == "45.00"
    assert flagged["high"] == "45.00"
    assert flagged["market"] == "Luha Public Market"


def test_the_whole_series_can_be_asked_for(client: TestClient, priced: Priced) -> None:
    body = client.get("/anomalies", params={"commodity": priced.slug, "only_flagged": False}).json()

    assert body["total"] == 8
    assert sum(1 for item in body["items"] if item["is_impossible"]) == 1


def test_a_commodity_with_no_rows_is_empty_not_an_error(client: TestClient) -> None:
    body = client.get("/anomalies", params={"commodity": "not-a-real-slug"}).json()

    assert body == {"items": [], "total": 0, "limit": 100, "offset": 0}


def test_the_commodity_is_required(client: TestClient) -> None:
    """Flagging across every commodity would compare rice against fertiliser."""
    assert client.get("/anomalies").status_code == 422


def test_the_threshold_is_a_query_parameter(client: TestClient, priced: Priced) -> None:
    """A reader who distrusts a flag should be able to move the goalposts."""
    response = client.get(
        "/anomalies", params={"commodity": priced.slug, "threshold": 0.5, "window": 5}
    )

    assert response.status_code == 200
    assert (
        client.get("/anomalies", params={"commodity": priced.slug, "threshold": 0}).status_code
        == 422
    )


def test_an_infinite_score_travels_as_null(client: TestClient, priced: Priced) -> None:
    """JSON has no infinity, and the reason carries the meaning anyway."""
    body = client.get("/anomalies", params={"commodity": priced.slug, "only_flagged": False}).json()

    assert all(item["score"] is None or isinstance(item["score"], float) for item in body["items"])


# -- data quality -----------------------------------------------------------------


def test_quality_reports_what_did_not_load_beside_what_did(
    client: TestClient, priced: Priced
) -> None:
    body = client.get("/meta/quality").json()

    assert body["observations"] == 8
    assert body["impossible"] == 1
    assert body["commodities_seeded"] > 100
    assert body["commodities_observed"] == 1
    assert body["quarantine"][0]["stage"] == "alias"
    assert body["quarantine"][0]["rows"] == 1


def test_quality_names_the_last_successful_run(client: TestClient, priced: Priced) -> None:
    source = client.get("/meta/quality").json()["sources"][0]

    assert source["slug"] == "da-caraga"
    assert source["runs"] == 1
    assert source["succeeded"] == 1
    assert source["last_success_at"] is not None


def test_a_source_that_has_never_run_still_appears(client: TestClient, session: Session) -> None:
    """The most important thing this page can say is "nothing has ever run".

    An inner join would drop the row entirely and the page would look merely empty, which
    reads as "no data yet" rather than "ingestion is broken".
    """
    seed_reference_data(session)
    session.commit()

    body = client.get("/meta/quality").json()

    assert len(body["sources"]) == 1
    assert body["sources"][0]["runs"] == 0
    assert body["sources"][0]["last_success_at"] is None
    assert body["observations"] == 0


def test_quality_on_an_entirely_empty_database(client: TestClient) -> None:
    body = client.get("/meta/quality").json()

    assert body["sources"] == []
    assert body["observations"] == 0
    assert body["earliest_observed_on"] is None


def test_quality_reports_the_span_of_the_data(client: TestClient, priced: Priced) -> None:
    body = client.get("/meta/quality").json()

    assert body["earliest_observed_on"] == "2026-07-01"
    assert body["latest_observed_on"] == "2026-07-08"
