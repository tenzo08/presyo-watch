"""Tests for the read API, driven over real HTTP routing against a real Postgres.

``TestClient`` exercises the actual application — real routing, real dependency injection,
real response models — so what is asserted here is what a client receives. The only thing
swapped is the session factory, replaced after startup with the one whose transaction the
test rolls back; the engine, schema and queries are the ones that ship.
"""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from presyowatch.api.app import create_app
from presyowatch.api.schemas import CommodityOut, ObservationOut, SourceOut
from presyowatch.db.models import (
    Commodity,
    IngestionRun,
    Market,
    PriceObservation,
    Source,
)
from presyowatch.db.seed import seed_reference_data
from presyowatch.ingest import DEFAULT_LOOKBACK

OBSERVED = date(2026, 7, 28)


@pytest.fixture
def client(engine: Engine, session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """The real app, reading through the test's transaction.

    The factory is replaced *after* startup because the lifespan sets its own; doing it
    before would be silently undone.
    """
    app = create_app(engine=engine)
    with TestClient(app) as opened:
        app.state.session_factory = session_factory
        yield opened


@pytest.fixture
def data(session: Session) -> dict[str, int]:
    """Reference data plus a handful of observations, committed into the test transaction."""
    seed_reference_data(session)
    session.flush()

    source_id = session.scalar(select(Source.id))
    commodity_id = session.scalar(select(Commodity.id).order_by(Commodity.id))
    assert source_id is not None
    assert commodity_id is not None

    market = Market(
        region_psgc_code="166800000", name="Luha Public Market", municipality="Tandag City"
    )
    session.add(market)
    session.flush()

    session.add_all(
        [
            PriceObservation(
                source_id=source_id,
                market_id=market.id,
                commodity_id=commodity_id,
                observed_on=day,
                low=Decimal("52.00"),
                high=Decimal("53.00"),
                prevailing=Decimal("53.00"),
                average=Decimal("52.80"),
                revision_no=0,
                source_file_sha256="a" * 64,
            )
            for day in (date(2026, 7, 26), date(2026, 7, 27), OBSERVED)
        ]
    )
    session.add(IngestionRun(run_id="r1", source_id=source_id, status="succeeded", rows_upserted=3))
    session.commit()
    return {"source_id": source_id, "market_id": market.id, "commodity_id": commodity_id}


# -- meta -------------------------------------------------------------------------


def test_health_reports_the_database_it_actually_queried(client: TestClient) -> None:
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"]


def test_sources_carry_their_attribution(client: TestClient, data: dict[str, int]) -> None:
    """Rule 8: attribution travels with the data, not only in a README."""
    body = client.get("/meta/sources").json()

    assert len(body) == 1
    assert body[0]["slug"] == "da-caraga"
    assert "Department of Agriculture" in body[0]["attribution_text"]


def test_runs_are_public(client: TestClient, data: dict[str, int]) -> None:
    """A failed run, or a day with no run, should be visible to anyone."""
    body = client.get("/meta/runs").json()

    assert body["total"] == 1
    assert body["items"][0]["run_id"] == "r1"
    assert body["items"][0]["status"] == "succeeded"


# -- reference --------------------------------------------------------------------


def test_regions_come_from_the_seed(client: TestClient, data: dict[str, int]) -> None:
    body = client.get("/regions").json()

    codes = {region["psgc_code"] for region in body}
    assert "166800000" in codes
    assert len(body) == 6


def test_commodities_can_be_searched_by_name(client: TestClient, data: dict[str, int]) -> None:
    body = client.get("/commodities", params={"q": "corn"}).json()

    assert body["total"] > 0
    assert all("corn" in item["name"].lower() for item in body["items"])


def test_farm_inputs_are_flagged_and_can_be_excluded(
    client: TestClient, data: dict[str, int]
) -> None:
    """Derived from the group at read time, so there is one definition of "farm input"."""
    everything = client.get("/commodities", params={"limit": 500}).json()
    food_only = client.get(
        "/commodities", params={"limit": 500, "include_agricultural_inputs": False}
    ).json()

    flagged = [item for item in everything["items"] if item["is_agricultural_input"]]
    assert flagged, "the seed contains feeds and fertiliser"
    assert food_only["total"] < everything["total"]
    assert not any(item["is_agricultural_input"] for item in food_only["items"])


def test_an_unknown_commodity_is_a_404(client: TestClient, data: dict[str, int]) -> None:
    assert client.get("/commodities/not-a-real-slug").status_code == 404


def test_a_commodity_can_be_fetched_by_its_stable_slug(
    client: TestClient, session: Session, data: dict[str, int]
) -> None:
    listed = client.get("/commodities", params={"limit": 1}).json()["items"][0]

    fetched = client.get(f"/commodities/{listed['canonical_slug']}").json()

    assert fetched == listed


# -- observations -----------------------------------------------------------------


def test_prices_are_strings_so_they_survive_json(client: TestClient, data: dict[str, int]) -> None:
    """The whole reason `Money` exists.

    As a JSON number, `52.80` reaches a JavaScript client as 52.79999999999999716 and ends
    up on a chart with fifteen decimal places. As a string it round-trips exactly.
    """
    item = client.get("/observations").json()["items"][0]

    assert item["average"] == "52.80"
    assert isinstance(item["average"], str)


def test_observations_come_back_oldest_first(client: TestClient, data: dict[str, int]) -> None:
    dates = [item["observed_on"] for item in client.get("/observations").json()["items"]]

    assert dates == sorted(dates)


def test_a_date_range_filters_both_ends(client: TestClient, data: dict[str, int]) -> None:
    body = client.get(
        "/observations", params={"date_from": "2026-07-27", "date_to": "2026-07-27"}
    ).json()

    assert body["total"] == 1
    assert body["items"][0]["observed_on"] == "2026-07-27"


def test_a_backwards_date_range_is_refused_rather_than_empty(
    client: TestClient, data: dict[str, int]
) -> None:
    """Returning nothing would look like "no data", which is a different statement."""
    response = client.get(
        "/observations", params={"date_from": "2026-07-28", "date_to": "2026-07-01"}
    )

    assert response.status_code == 422
    assert "is after" in response.json()["detail"]


def test_filtering_by_region_reaches_through_the_market(
    client: TestClient, data: dict[str, int]
) -> None:
    matching = client.get("/observations", params={"region": "166800000"}).json()
    other = client.get("/observations", params={"region": "160200000"}).json()

    assert matching["total"] == 3
    assert other["total"] == 0


def test_filtering_by_a_commodity_that_has_no_rows_is_empty_not_an_error(
    client: TestClient, data: dict[str, int]
) -> None:
    body = client.get("/observations", params={"commodity": "not-a-real-slug"}).json()

    assert body["total"] == 0
    assert body["items"] == []


def test_paging_is_stable_across_pages(client: TestClient, data: dict[str, int]) -> None:
    """Every list orders by something unique last, so page 2 cannot repeat page 1.

    Ordering only by `observed_on` leaves ties to the query plan, and the resulting
    duplicate-and-skip only shows up once there is real data.
    """
    first = client.get("/observations", params={"limit": 2, "offset": 0}).json()
    second = client.get("/observations", params={"limit": 2, "offset": 2}).json()

    assert first["total"] == second["total"] == 3
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    seen = [item["observed_on"] for item in first["items"] + second["items"]]
    assert len(set(seen)) == 3


def test_the_page_limit_is_capped(client: TestClient, data: dict[str, int]) -> None:
    """An unbounded query is the easiest way to time out a free-tier database."""
    assert client.get("/observations", params={"limit": 100_000}).status_code == 422
    assert client.get("/observations", params={"limit": 0}).status_code == 422


def test_unavailable_rows_are_excluded_unless_asked_for(
    client: TestClient, session: Session, data: dict[str, int]
) -> None:
    """They are real data and stay available, but a price series usually wants prices."""
    session.add(
        PriceObservation(
            source_id=data["source_id"],
            market_id=data["market_id"],
            commodity_id=data["commodity_id"],
            observed_on=date(2026, 7, 25),
            unavailable=True,
            revision_no=0,
            source_file_sha256="b" * 64,
        )
    )
    session.commit()

    default = client.get("/observations").json()
    included = client.get("/observations", params={"include_unavailable": True}).json()

    assert default["total"] == 3
    assert included["total"] == 4


def test_an_observation_names_where_it_came_from(client: TestClient, data: dict[str, int]) -> None:
    """A price with no market or source attached is not much use to a chart."""
    item = client.get("/observations").json()["items"][0]

    assert item["market"] == "Luha Public Market"
    assert item["municipality"] == "Tandag City"
    assert item["region_psgc_code"] == "166800000"
    assert item["source_slug"] == "da-caraga"
    assert item["revision_no"] == 0


# -- the documented surface -------------------------------------------------------


def test_openapi_documents_every_route(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths) >= {
        "/health",
        "/meta/sources",
        "/meta/runs",
        "/regions",
        "/markets",
        "/commodities",
        "/observations",
    }


def test_the_docs_page_is_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200


def test_the_schema_warns_that_prices_are_strings(client: TestClient) -> None:
    """The surprising choice has to be documented where a client will actually see it."""
    description = client.get("/openapi.json").json()["info"]["description"]

    assert "strings, not numbers" in description


def test_the_ingest_lookback_is_what_the_workflow_claims() -> None:
    """The scheduled workflow's comment and the code must not drift apart."""
    assert DEFAULT_LOOKBACK.days == 14


@pytest.mark.parametrize("model", [ObservationOut, CommodityOut, SourceOut])
def test_the_documented_examples_are_valid_instances(model: type[BaseModel]) -> None:
    """An example that no longer matches its schema is worse than none at all.

    They are copied from a live run rather than invented, so they drift the moment a field
    is renamed. Validating them here means the docs cannot quietly start lying.
    """
    extra = model.model_config.get("json_schema_extra")
    assert isinstance(extra, dict)
    examples = extra["examples"]
    assert isinstance(examples, list)

    for example in examples:
        assert isinstance(example, dict)
        model.model_validate(example)


# -- biggest movers ---------------------------------------------------------------


@pytest.fixture
def movement(session: Session, data: dict[str, int]) -> int:
    """A second commodity that falls while the first one holds steady."""
    other = session.scalar(
        select(Commodity.id).where(Commodity.id != data["commodity_id"]).order_by(Commodity.id)
    )
    assert other is not None
    session.add_all(
        [
            PriceObservation(
                source_id=data["source_id"],
                market_id=data["market_id"],
                commodity_id=other,
                observed_on=day,
                average=average,
                revision_no=0,
                source_file_sha256="c" * 64,
            )
            for day, average in (
                (date(2026, 7, 26), Decimal("100.00")),
                (date(2026, 7, 28), Decimal("75.00")),
            )
        ]
    )
    session.commit()
    return other


def test_a_mover_reports_what_it_actually_compared(client: TestClient, movement: int) -> None:
    """The dates are returned because the source does not publish every day.

    A window of "7 days" compared here is really "the first and last figures inside those 7
    days", and pretending otherwise would be interpolation with extra steps.
    """
    body = client.get("/movers", params={"window_days": 7}).json()

    top = body["items"][0]
    assert top["first_observed_on"] == "2026-07-26"
    assert top["last_observed_on"] == "2026-07-28"
    assert top["first_average"] == "100.00"
    assert top["last_average"] == "75.00"
    assert top["change"] == "-25.00"
    assert top["percent_change"] == pytest.approx(-25.0)
    assert top["observations"] == 2


def test_movers_are_ordered_by_the_size_of_the_move_either_way(
    client: TestClient, movement: int
) -> None:
    """A 25% fall is a bigger story than a 1% rise, and both are stories."""
    items = client.get("/movers", params={"window_days": 7}).json()["items"]

    magnitudes = [abs(item["percent_change"]) for item in items]
    assert magnitudes == sorted(magnitudes, reverse=True)
    assert items[0]["percent_change"] < 0


def test_a_commodity_that_did_not_move_is_still_a_row(client: TestClient, movement: int) -> None:
    """Zero is a real answer. The steady one has three identical observations."""
    items = client.get("/movers", params={"window_days": 7}).json()["items"]

    steady = [item for item in items if item["percent_change"] == 0]
    assert steady
    assert steady[0]["observations"] == 3


def test_one_observation_in_the_window_is_not_a_movement(
    client: TestClient, session: Session, data: dict[str, int]
) -> None:
    """A single price is a price, not a change. Reporting it as 0% would be a claim."""
    body = client.get("/movers", params={"window_days": 1}).json()

    assert body["total"] == 0


def test_the_window_anchors_on_the_latest_data_not_the_wall_clock(
    client: TestClient, movement: int
) -> None:
    """A late ingestion run must not make the table read "nothing moved"."""
    body = client.get("/movers", params={"window_days": 7}).json()

    assert body["total"] > 0
    assert body["items"][0]["last_observed_on"] == "2026-07-28"


def test_an_explicit_as_of_narrows_the_window(client: TestClient, movement: int) -> None:
    body = client.get("/movers", params={"window_days": 7, "as_of": "2026-07-27"}).json()

    assert all(item["last_observed_on"] <= "2026-07-27" for item in body["items"])


def test_farm_inputs_are_excluded_from_movers_by_default(client: TestClient, movement: int) -> None:
    """A "biggest movers" table reads as a story about food."""
    default = client.get("/movers", params={"window_days": 7}).json()

    assert not any(item["group"] in {"FERTILIZER", "INSECTICIDE"} for item in default["items"])


def test_movers_on_an_empty_database_is_empty_not_an_error(client: TestClient) -> None:
    """Before the first ingestion run there is nothing to compare, which is not a fault."""
    body = client.get("/movers").json()

    assert body == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_movers_names_the_market_rather_than_averaging_across_them(
    client: TestClient, movement: int
) -> None:
    """Averaging Butuan and Tandag would produce a number that is nobody's price."""
    top = client.get("/movers", params={"window_days": 7}).json()["items"][0]

    assert top["market"] == "Luha Public Market"
    assert top["municipality"] == "Tandag City"
