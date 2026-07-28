"""The parts of the upsert that hold before anything touches a database.

Quantization, the validators mirroring the check constraints, and duplicate collapsing are
all pure, so they are tested without a server and therefore run in CI today rather than
waiting for Phase 2 to provision one.
"""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from presyowatch.db.upsert import PendingObservation, collapse_duplicates

OBSERVED_ON = date(2026, 7, 28)
SHA = "a" * 64

LOW = Decimal("52.00")
HIGH = Decimal("53.00")
PREVAILING = Decimal("53.00")
AVERAGE = Decimal("52.80")


def pending(
    *,
    commodity_id: int = 3,
    low: Decimal | None = LOW,
    high: Decimal | None = HIGH,
    prevailing: Decimal | None = PREVAILING,
    average: Decimal | None = AVERAGE,
    unavailable: bool = False,
    source_file_sha256: str = SHA,
) -> PendingObservation:
    """One observation, with every field a test varies overridable by name."""
    return PendingObservation(
        source_id=1,
        market_id=2,
        commodity_id=commodity_id,
        observed_on=OBSERVED_ON,
        low=low,
        high=high,
        prevailing=prevailing,
        average=average,
        unavailable=unavailable,
        source_file_sha256=source_file_sha256,
    )


def test_natural_key_is_the_upsert_target() -> None:
    assert pending().natural_key == (1, 2, 3, OBSERVED_ON)


def test_a_trailing_zero_is_not_a_difference() -> None:
    """``52.8`` and ``52.80`` are the same price, and Decimal agrees."""
    assert pending(average=Decimal("52.8")).figures == pending(average=Decimal("52.80")).figures


def test_a_price_is_quantized_to_the_scale_it_will_be_stored_at() -> None:
    """Otherwise the row differs from itself after a round trip and is revised forever.

    ``Numeric(12, 2)`` would store ``52.805`` as ``52.81``; comparing the unrounded value
    against that on the next run would supersede the row on every single run.
    """
    assert pending(low=Decimal("52.805")).low == Decimal("52.81")


def test_quantization_agrees_with_postgres_on_a_half() -> None:
    """Postgres rounds numeric half away from zero, so ``ROUND_HALF_UP`` must too."""
    assert pending(low=Decimal("52.005")).low == Decimal("52.01")


def test_a_missing_price_stays_missing() -> None:
    """Gaps stay gaps: quantization must not turn ``None`` into a number."""
    assert pending(low=None).low is None


def test_low_above_high_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"low 99\.00 is above high 10\.00"):
        pending(low=Decimal("99.00"), high=Decimal("10.00"))


def test_unavailable_may_not_carry_prices() -> None:
    """ "Not monitored" and "priced at 52.00" cannot both be true of one row."""
    with pytest.raises(ValidationError, match="marked unavailable but carries prices"):
        pending(unavailable=True)


def test_unavailable_with_no_prices_is_fine() -> None:
    row = pending(unavailable=True, low=None, high=None, prevailing=None, average=None)

    assert row.figures == (None, None, None, None, True)


def test_an_empty_batch_collapses_to_nothing() -> None:
    settled, conflicts = collapse_duplicates([])

    assert settled == {}
    assert conflicts == ()


def test_repeats_that_agree_collapse_to_one() -> None:
    """Two source rows resolving to one commodity with the same figures are not a conflict."""
    settled, conflicts = collapse_duplicates([pending(), pending(average=Decimal("52.800"))])

    assert list(settled) == [(1, 2, 3, OBSERVED_ON)]
    assert conflicts == ()


def test_repeats_that_disagree_are_pulled_out_entirely() -> None:
    """Including the first one seen — keeping it would be a guess dressed up as a rule."""
    first = pending()
    second = pending(average=Decimal("49.00"))

    settled, conflicts = collapse_duplicates([first, second])

    assert settled == {}
    assert len(conflicts) == 1
    assert conflicts[0].key == (1, 2, 3, OBSERVED_ON)
    assert conflicts[0].observations == (first, second)


def test_a_third_disagreeing_row_joins_the_same_conflict() -> None:
    rows = [pending(), pending(average=Decimal("49.00")), pending(average=Decimal("48.00"))]

    _, conflicts = collapse_duplicates(rows)

    assert len(conflicts) == 1
    assert conflicts[0].observations == tuple(rows)


def test_a_repeat_matching_a_disputed_key_does_not_resurrect_it() -> None:
    """A later row agreeing with the first must not settle a key already in dispute."""
    settled, conflicts = collapse_duplicates(
        [pending(), pending(average=Decimal("49.00")), pending()]
    )

    assert settled == {}
    assert len(conflicts[0].observations) == 3


def test_one_conflict_does_not_take_out_the_rest_of_the_batch() -> None:
    other = pending(commodity_id=4)

    settled, conflicts = collapse_duplicates([pending(), pending(average=Decimal("49.00")), other])

    assert settled == {(1, 2, 4, OBSERVED_ON): other}
    assert len(conflicts) == 1


def test_the_conflict_explains_itself_for_quarantine() -> None:
    _, conflicts = collapse_duplicates([pending(), pending(average=Decimal("49.00"))])

    reason = conflicts[0].reason

    assert "2 rows" in reason
    assert "2026-07-28" in reason
