"""Upsert and correction handling against a real Postgres.

The centrepiece is ``test_running_the_same_batch_twice_leaves_the_database_identical``,
which PLANNING.md § "Idempotent by construction" asks for by name. It compares *every*
column of both tables rather than a chosen few, so a rerun that quietly rewrites a column
nobody thought to assert still fails.

Skipped unless ``PRESYOWATCH_TEST_DATABASE_URL`` is set; ``conftest.py`` owns the migrated
database and the transaction each test runs in.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from presyowatch.db.models import Commodity, ObservationRevision, PriceObservation
from presyowatch.db.upsert import PendingObservation, UpsertOutcome, upsert_observations

OBSERVED_ON = date(2026, 7, 28)
ORIGINAL_FILE = "a" * 64
REVISED_FILE = "b" * 64

LOW = Decimal("52.00")
HIGH = Decimal("53.00")
PREVAILING = Decimal("53.00")
AVERAGE = Decimal("52.80")


@pytest.fixture
def second_commodity(session: Session) -> int:
    """A second commodity, so "only the changed row is revised" can be asserted."""
    commodity = Commodity(
        canonical_slug="rice-well-milled-local",
        group="LOCAL COMMERCIAL RICE",
        name="Well Milled",
        specification=None,
        unit="kg",
    )
    session.add(commodity)
    session.flush()
    return commodity.id


def pending(
    ids: tuple[int, int, int],
    *,
    observed_on: date = OBSERVED_ON,
    low: Decimal | None = LOW,
    high: Decimal | None = HIGH,
    prevailing: Decimal | None = PREVAILING,
    average: Decimal | None = AVERAGE,
    unavailable: bool = False,
    source_file_sha256: str = ORIGINAL_FILE,
) -> PendingObservation:
    """One observation for the seeded ids, with every field overridable by name."""
    source_id, market_id, commodity_id = ids
    return PendingObservation(
        source_id=source_id,
        market_id=market_id,
        commodity_id=commodity_id,
        observed_on=observed_on,
        low=low,
        high=high,
        prevailing=prevailing,
        average=average,
        unavailable=unavailable,
        source_file_sha256=source_file_sha256,
    )


def snapshot(session: Session) -> list[tuple[object, ...]]:
    """Every column of both tables, in a stable order.

    Deliberately not a list of chosen columns: the assertion is that *nothing* changed, and
    naming columns would quietly stop covering any added later.
    """
    session.expire_all()
    observations = session.execute(
        select(PriceObservation.__table__).order_by(PriceObservation.id)
    ).all()
    revisions = session.execute(
        select(ObservationRevision.__table__).order_by(ObservationRevision.id)
    ).all()
    return [tuple(row) for row in observations] + [tuple(row) for row in revisions]


def stored_observations(session: Session) -> Sequence[PriceObservation]:
    session.expire_all()
    return session.scalars(select(PriceObservation).order_by(PriceObservation.id)).all()


def stored_revisions(session: Session) -> Sequence[ObservationRevision]:
    session.expire_all()
    return session.scalars(select(ObservationRevision).order_by(ObservationRevision.id)).all()


def test_a_new_observation_is_inserted(session: Session, seeded: tuple[int, int, int]) -> None:
    outcome = upsert_observations(session, [pending(seeded)])

    stored = stored_observations(session)
    assert outcome == UpsertOutcome(inserted=1)
    assert len(stored) == 1
    assert stored[0].average == Decimal("52.80")
    assert stored[0].revision_no == 0
    assert stored[0].source_file_sha256 == ORIGINAL_FILE


def test_an_empty_batch_writes_nothing(session: Session, seeded: tuple[int, int, int]) -> None:
    outcome = upsert_observations(session, [])

    assert outcome.written == 0
    assert stored_observations(session) == []


def test_running_the_same_batch_twice_leaves_the_database_identical(
    session: Session, seeded: tuple[int, int, int], second_commodity: int
) -> None:
    """PLANNING.md § "Idempotent by construction". The whole point of the natural key."""
    source_id, market_id, _ = seeded
    batch = [pending(seeded), pending((source_id, market_id, second_commodity))]
    upsert_observations(session, batch)
    session.commit()

    before = snapshot(session)
    outcome = upsert_observations(session, batch)
    session.commit()

    assert snapshot(session) == before
    assert outcome == UpsertOutcome(unchanged=2)


def test_an_unchanged_row_is_not_rewritten(session: Session, seeded: tuple[int, int, int]) -> None:
    """Not even ``ingested_at``.

    Backdated first, because ``now()`` is the transaction's timestamp and would not move
    within a test even if the row *were* rewritten — this makes a spurious write visible.
    """
    upsert_observations(session, [pending(seeded)])
    backdated = datetime(2026, 1, 1, tzinfo=UTC)
    session.execute(update(PriceObservation).values(ingested_at=backdated))
    session.commit()

    upsert_observations(session, [pending(seeded)])

    assert stored_observations(session)[0].ingested_at == backdated


def test_the_same_figures_from_another_file_do_not_supersede(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    """A ``Revised-`` sheet reprints every row; only the ones that moved are corrections.

    ``source_file_sha256`` keeps pointing at the file the figures came from, so a rerun
    stays a genuine no-op and one corrected row does not make the other 152 look revised.
    """
    upsert_observations(session, [pending(seeded)])
    session.commit()

    outcome = upsert_observations(session, [pending(seeded, source_file_sha256=REVISED_FILE)])

    stored = stored_observations(session)[0]
    assert outcome.unchanged == 1
    assert stored.source_file_sha256 == ORIGINAL_FILE
    assert stored.revision_no == 0
    assert stored_revisions(session) == []


def test_a_correction_updates_in_place_and_keeps_what_it_replaced(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    """Rule 4: update the row, append the old values — never a second row for the same day."""
    upsert_observations(session, [pending(seeded)])
    session.commit()

    outcome = upsert_observations(
        session,
        [pending(seeded, average=Decimal("49.00"), source_file_sha256=REVISED_FILE)],
    )

    stored = stored_observations(session)
    revisions = stored_revisions(session)
    assert outcome == UpsertOutcome(revised=1)
    assert len(stored) == 1
    assert stored[0].average == Decimal("49.00")
    assert stored[0].revision_no == 1
    assert stored[0].source_file_sha256 == REVISED_FILE
    assert len(revisions) == 1
    assert revisions[0].observation_id == stored[0].id
    assert revisions[0].average == Decimal("52.80")


def test_a_revision_records_the_number_and_file_of_the_values_it_holds(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    """History reads "revision 0 said this, and it came from that file"."""
    upsert_observations(session, [pending(seeded)])
    upsert_observations(
        session,
        [pending(seeded, low=Decimal("50.00"), source_file_sha256=REVISED_FILE)],
    )

    revision = stored_revisions(session)[0]

    assert revision.revision_no == 0
    assert revision.source_file_sha256 == ORIGINAL_FILE


def test_a_second_correction_appends_a_second_revision(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    upsert_observations(session, [pending(seeded)])
    upsert_observations(session, [pending(seeded, average=Decimal("49.00"))])
    session.commit()

    upsert_observations(session, [pending(seeded, average=Decimal("47.00"))])

    stored = stored_observations(session)[0]
    revisions = stored_revisions(session)
    assert stored.revision_no == 2
    assert [revision.revision_no for revision in revisions] == [0, 1]
    assert [revision.average for revision in revisions] == [Decimal("52.80"), Decimal("49.00")]


def test_only_the_rows_that_changed_are_revised(
    session: Session, seeded: tuple[int, int, int], second_commodity: int
) -> None:
    source_id, market_id, _ = seeded
    unchanged_key = (source_id, market_id, second_commodity)
    upsert_observations(session, [pending(seeded), pending(unchanged_key)])
    session.commit()

    outcome = upsert_observations(
        session,
        [pending(seeded, average=Decimal("49.00")), pending(unchanged_key)],
    )

    assert outcome == UpsertOutcome(revised=1, unchanged=1)
    assert len(stored_revisions(session)) == 1


def test_a_row_becoming_unavailable_is_a_correction(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    """A commodity dropping out of monitoring is a change, not a blank to ignore."""
    upsert_observations(session, [pending(seeded)])
    session.commit()

    outcome = upsert_observations(
        session,
        [
            pending(
                seeded,
                low=None,
                high=None,
                prevailing=None,
                average=None,
                unavailable=True,
            )
        ],
    )

    stored = stored_observations(session)[0]
    assert outcome.revised == 1
    assert stored.unavailable is True
    assert stored.low is None
    assert stored_revisions(session)[0].unavailable is False


def test_conflicting_duplicates_are_reported_and_nothing_is_written(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    """Two source rows resolving to one commodity with different prices settle nothing."""
    outcome = upsert_observations(
        session, [pending(seeded), pending(seeded, average=Decimal("49.00"))]
    )

    assert outcome.written == 0
    assert outcome.rejected == 2
    assert len(outcome.conflicts) == 1
    assert stored_observations(session) == []


def test_a_conflict_does_not_stop_the_rest_of_the_batch(
    session: Session, seeded: tuple[int, int, int], second_commodity: int
) -> None:
    """One unresolvable commodity must not cost a sheet its other 152 rows."""
    source_id, market_id, _ = seeded
    good = pending((source_id, market_id, second_commodity))

    outcome = upsert_observations(
        session, [pending(seeded), pending(seeded, average=Decimal("49.00")), good]
    )

    assert outcome.inserted == 1
    assert len(outcome.conflicts) == 1
    assert [row.commodity_id for row in stored_observations(session)] == [second_commodity]


def test_an_existing_row_survives_a_conflict_about_it(
    session: Session, seeded: tuple[int, int, int]
) -> None:
    """A batch that cannot agree with itself must not disturb what is already stored."""
    upsert_observations(session, [pending(seeded)])
    session.commit()
    before = snapshot(session)

    upsert_observations(session, [pending(seeded, average=Decimal("49.00")), pending(seeded)])

    assert snapshot(session) == before
