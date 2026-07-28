"""Schema behaviour against a real Postgres.

Skipped unless ``PRESYOWATCH_TEST_DATABASE_URL`` is set; see ``tests/db/conftest.py``,
which owns the migrated database and the transaction each test runs in.

These assert what only a live server can: that the migration applies, that the models have
not drifted from it, and that the check constraints actually reject bad rows rather than
merely being declared.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from presyowatch.db.models import Market, PriceObservation


def test_the_schema_under_test_came_from_the_migration(engine: Engine) -> None:
    """Proves the fixture migrated rather than silently creating from the models."""
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert version, "alembic_version is empty, so no migration ran"


def observation(ids: tuple[int, int, int], **overrides: object) -> PriceObservation:
    source_id, market_id, commodity_id = ids
    values: dict[str, object] = {
        "source_id": source_id,
        "market_id": market_id,
        "commodity_id": commodity_id,
        "observed_on": date(2026, 4, 2),
        "low": Decimal("52.00"),
        "high": Decimal("53.00"),
        "prevailing": Decimal("53.00"),
        "average": Decimal("52.80"),
        "source_file_sha256": "a" * 64,
    }
    values.update(overrides)
    return PriceObservation(**values)


def test_prices_round_trip_as_exact_decimals(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    """The point of Numeric: 52.80 comes back as 52.80, not 52.799999999999997."""
    session.add(observation(seeded))
    session.flush()
    session.expire_all()

    stored = session.scalars(select(PriceObservation)).one()

    assert stored.average == Decimal("52.80")
    assert isinstance(stored.average, Decimal)


def test_natural_key_rejects_a_duplicate(session: Session, seeded: tuple[int, int, int]) -> None:
    """Rule 4's upsert target. Without this the ingester would append, not converge."""
    session.add(observation(seeded))
    session.flush()

    # Differs only in `average`, so the natural key is the sole thing that can reject it.
    # Varying `low` instead would trip the low<=high check first and pass for the wrong
    # reason.
    session.add(observation(seeded, average=Decimal("49.00")))
    with pytest.raises(IntegrityError, match="uq_price_observations_natural_key"):
        session.flush()


def test_the_same_day_at_another_market_is_a_different_observation(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    source_id, _, commodity_id = seeded
    other = Market(region_psgc_code="160000000", name="Public Market", municipality="Butuan City")
    session.add(other)
    session.flush()

    session.add(observation(seeded))
    session.add(observation((source_id, other.id, commodity_id)))
    session.flush()

    assert len(session.scalars(select(PriceObservation)).all()) == 2


def test_low_above_high_is_rejected(session: Session, seeded: tuple[int, int, int]) -> None:
    session.add(observation(seeded, low=Decimal("99.00"), high=Decimal("10.00")))

    with pytest.raises(IntegrityError, match="low_not_above_high"):
        session.flush()


def test_unavailable_row_may_not_carry_prices(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    """ "Not monitored" and "priced at 52.00" cannot both be true of one row."""
    session.add(observation(seeded, unavailable=True))

    with pytest.raises(IntegrityError, match="unavailable_implies_no_prices"):
        session.flush()


def test_unavailable_row_with_no_prices_is_accepted(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    session.add(
        observation(
            seeded,
            unavailable=True,
            low=None,
            high=None,
            prevailing=None,
            average=None,
        )
    )
    session.flush()

    stored = session.scalars(select(PriceObservation)).one()
    assert stored.unavailable is True
    assert stored.low is None


def test_partial_prices_are_allowed(session: Session, seeded: tuple[int, int, int]) -> None:
    """A source may publish a prevailing price and leave low and high blank."""
    session.add(observation(seeded, low=None, high=None))
    session.flush()

    stored = session.scalars(select(PriceObservation)).one()
    assert stored.prevailing == Decimal("53.00")
    assert stored.low is None


def test_timestamps_come_back_timezone_aware(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    session.add(observation(seeded))
    session.flush()
    session.expire_all()

    stored = session.scalars(select(PriceObservation)).one()

    assert stored.ingested_at.tzinfo is not None
    assert stored.ingested_at <= datetime.now(UTC)


def test_an_observation_needs_a_real_market(session: Session, seeded: tuple[int, int, int]) -> None:
    source_id, _, commodity_id = seeded
    session.add(observation((source_id, 999_999, commodity_id)))

    with pytest.raises(IntegrityError, match="fk_price_observations_market_id_markets"):
        session.flush()


def test_run_status_rejects_an_unknown_value(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    """The CHECK behind the non-native enum has to actually be enforced.

    A non-native enum is only as good as its constraint; without this the column would
    accept any string and the "typed" status would be decorative.
    """
    source_id, _, _ = seeded
    statement = text(
        "INSERT INTO ingestion_runs (run_id, source_id, status, started_at) "
        "VALUES ('r1', :source_id, 'bogus', now())"
    ).bindparams(source_id=source_id)

    with pytest.raises(IntegrityError, match="ingestion_status"):
        session.execute(statement)
