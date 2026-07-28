"""Tests for anomaly flagging.

The headline case is real: `Mayor-Salvador-Calo-July-19-2029.pdf` publishes Corn Cracked as
low 45.00, high 45.00, prevailing 45.00, **average 4.00**. It is the source's dropped digit,
it is in the committed corpus, and before this module existed it was the largest price
movement in the dataset at +1025%.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from presyowatch.analytics.anomalies import (
    AnomalyConfig,
    Flag,
    Point,
    flag_series,
    is_impossible,
)


def series(*averages: str | None, start: date = date(2026, 7, 1)) -> list[Point]:
    """A daily series of averages, blanks written as None."""
    return [
        Point(
            observed_on=start + timedelta(days=offset),
            average=None if value is None else Decimal(value),
        )
        for offset, value in enumerate(averages)
    ]


def anomalies(flags: list[Flag]) -> list[Flag]:
    return [flag for flag in flags if flag.is_anomaly]


# -- arithmetic impossibility -----------------------------------------------------


def test_the_real_corn_cracked_row_is_caught_without_statistics() -> None:
    """low 45.00, high 45.00, average 4.00. No window, no threshold, no tuning."""
    point = Point(
        observed_on=date(2026, 7, 19),
        average=Decimal("4.00"),
        low=Decimal("45.00"),
        high=Decimal("45.00"),
    )

    assert is_impossible(point) == "average 4.00 is below low 45.00"


def test_an_average_above_the_high_is_impossible_too() -> None:
    point = Point(
        observed_on=date(2026, 7, 19),
        average=Decimal("99.00"),
        low=Decimal("40.00"),
        high=Decimal("45.00"),
    )

    assert is_impossible(point) is not None


def test_an_ordinary_row_is_not_impossible() -> None:
    point = Point(
        observed_on=date(2026, 7, 19),
        average=Decimal("42.50"),
        low=Decimal("40.00"),
        high=Decimal("45.00"),
    )

    assert is_impossible(point) is None


def test_a_missing_average_is_not_impossible() -> None:
    """A blank is a day nobody looked, not a contradiction."""
    point = Point(date(2026, 7, 19), average=None, low=Decimal("40.00"), high=Decimal("45.00"))

    assert is_impossible(point) is None


def test_an_impossible_row_is_flagged_even_when_statistically_unremarkable() -> None:
    """The two checks are independent, and this is why both exist.

    A row can sit comfortably inside its neighbours' spread and still contradict itself.
    """
    points = [
        Point(date(2026, 7, day), Decimal("45.00"), Decimal("44.00"), Decimal("46.00"))
        for day in range(1, 8)
    ]
    points[3] = Point(date(2026, 7, 4), Decimal("45.00"), Decimal("46.00"), Decimal("46.00"))

    flags = flag_series(points)

    assert flags[3].is_impossible is True
    assert flags[3].is_anomaly is False
    assert flags[3].reason is not None


# -- outliers ---------------------------------------------------------------------

NOISY_WITH_SPIKE = (
    "50.00",
    "52.00",
    "51.00",
    "49.00",
    "53.00",
    "450.00",
    "50.00",
    "51.00",
    "52.00",
)
NOISY = ("50.00", "52.00", "51.00", "49.00", "53.00", "50.00", "51.00", "52.00", "50.00")


def test_a_spike_in_a_noisy_series_is_flagged() -> None:
    found = anomalies(flag_series(series(*NOISY_WITH_SPIKE)))

    assert len(found) == 1
    assert found[0].average == Decimal("450.00")


def test_the_spike_does_not_hide_inside_its_own_threshold() -> None:
    """The reason PLANNING.md insists on median and MAD over mean and standard deviation.

    One 450 drags a mean upwards and inflates a standard deviation enough that the spike
    lands inside its own confidence interval. The median barely moves.
    """
    flags = flag_series(series(*NOISY_WITH_SPIKE))

    assert flags[5].score is not None
    assert flags[5].score > 3.5


def test_ordinary_variation_is_not_flagged() -> None:
    assert anomalies(flag_series(series(*NOISY))) == []


def test_a_point_is_not_judged_against_itself() -> None:
    """Included in its own window, a lone spike pulls the centre towards itself."""
    points = series(
        "50.00", "50.00", "50.00", "50.00", "200.00", "50.00", "50.00", "50.00", "50.00"
    )

    assert flag_series(points)[4].is_anomaly is True


# -- flat runs, which are the common case in this dataset -------------------------


def test_a_perfectly_flat_series_has_no_anomalies() -> None:
    """Most commodities at most markets hold one price for days. That is not suspicious."""
    assert anomalies(flag_series(series(*["45.00"] * 9))) == []


def test_one_different_price_among_identical_neighbours_is_flagged() -> None:
    """MAD is zero here, and the textbook modified z-score divides by it.

    Not an edge case in this dataset — it is the normal shape of a price series — so it has
    to work rather than merely not crash.
    """
    points = series("45.00", "45.00", "45.00", "45.00", "4.00", "45.00", "45.00", "45.00", "45.00")

    found = anomalies(flag_series(points))

    assert len(found) == 1
    assert found[0].average == Decimal("4.00")
    assert found[0].reason is not None
    assert "all at 45.00" in found[0].reason


def test_a_flat_run_with_one_step_change_does_not_flag_everything_after_it() -> None:
    """A price that moves once and stays there has changed, not misbehaved."""
    points = series("45.00", "45.00", "45.00", "45.00", "50.00", "50.00", "50.00", "50.00", "50.00")

    found = anomalies(flag_series(points))

    assert len(found) <= 1, [flag.observed_on for flag in found]


# -- refusing to judge ------------------------------------------------------------


def test_too_few_neighbours_means_no_score_and_no_claim() -> None:
    """Three prices are not a distribution."""
    flags = flag_series(series("50.00", "500.00", "50.00"))

    assert all(flag.score is None for flag in flags)
    assert anomalies(flags) == []


def test_blanks_are_never_anomalies() -> None:
    points = series("50.00", "51.00", None, "50.00", "52.00", None, "51.00", "50.00", "49.00")

    blanks = [flag for flag in flag_series(points) if flag.average is None]

    assert len(blanks) == 2
    assert not any(flag.is_anomaly for flag in blanks)


def test_blanks_do_not_count_as_zero_when_judging_their_neighbours() -> None:
    """Treating a gap as zero pesos would make every real price look like a spike."""
    points = series("50.00", None, "51.00", None, "50.00", None, "52.00", None, "50.00")

    assert anomalies(flag_series(points)) == []


def test_a_small_move_against_a_tight_cluster_is_not_an_anomaly() -> None:
    """The floor earning its keep, on the case that first exposed the need for it.

    Neighbours of 50, 51, 50, 50 give a mean absolute deviation of 0.25, so an ordinary
    two-peso move scores over six deviations. Statistically true, practically noise — and
    this is the ordinary shape of a stable price series, not a contrived one.
    """
    points = series("50.00", None, "51.00", None, "50.00", None, "52.00", None, "50.00")

    flags = flag_series(points)

    assert flags[6].score is not None
    assert flags[6].score > 3.5, "the score is genuinely large"
    assert flags[6].is_anomaly is False, "but two pesos on fifty is not news"


def test_the_floor_can_be_lowered_for_a_caller_that_wants_everything() -> None:
    points = series("50.00", None, "51.00", None, "50.00", None, "52.00", None, "50.00")

    found = anomalies(flag_series(points, AnomalyConfig(min_relative_deviation=0.0)))

    assert len(found) == 1


def test_every_input_point_comes_back() -> None:
    """Nothing is dropped. Analysis annotates; it does not filter."""
    points = series("50.00", None, "450.00", "51.00", "50.00")

    flags = flag_series(points)

    assert len(flags) == len(points)
    assert [flag.observed_on for flag in flags] == [point.observed_on for point in points]


# -- configuration ----------------------------------------------------------------


def test_the_threshold_is_configurable_not_magic() -> None:
    points = series("50.00", "52.00", "51.00", "49.00", "53.00", "70.00", "50.00", "51.00", "52.00")

    lenient = anomalies(flag_series(points, AnomalyConfig(threshold=50.0)))
    strict = anomalies(flag_series(points, AnomalyConfig(threshold=1.5)))

    assert lenient == []
    assert len(strict) >= 1


@pytest.mark.parametrize(
    ("window", "threshold", "min_neighbours"),
    [(2, 3.5, 4), (9, 0, 4), (9, -1, 4), (9, 3.5, 1)],
)
def test_nonsense_configuration_is_refused_up_front(
    window: int, threshold: float, min_neighbours: int
) -> None:
    with pytest.raises(ValueError, match="must be"):
        AnomalyConfig(window=window, threshold=threshold, min_neighbours=min_neighbours)


def test_an_empty_series_is_an_empty_answer() -> None:
    assert flag_series([]) == []
