"""Flagging prices that do not belong, without deciding they are wrong.

Two different jobs live here, and conflating them would be a mistake.

**Impossible rows** are arithmetic, not statistics. If a row says LOW 45.00, HIGH 45.00 and
AVERAGE 4.00, no amount of context is needed to know something is broken — an average
outside its own low-to-high range cannot happen. That exact row is in the committed corpus
(KNOWLEDGE.md § "A row can be internally impossible"), it is the source's dropped digit
rather than our parser's fault, and it currently produces the largest price movement in the
dataset. Catching it needs no window, no threshold and no tuning.

**Outliers** are statistical and are never claimed to be wrong — only unusual. A price can
genuinely triple after a typhoon, and a system that quietly deleted that would be worse than
useless. So this module *annotates* and never filters, and nothing here writes to the
database.

**Median and MAD, not mean and standard deviation.** PLANNING.md is explicit and the reason
is that a price spike is precisely the observation that corrupts a mean-based threshold: one
₱450 reading drags the mean up and inflates the standard deviation, so the spike ends up
inside its own confidence interval and hides. The median barely moves and the MAD barely
grows, so the spike stands out.

**A point is never judged against itself.** Its comparison window is its neighbours,
excluding it. With a robust estimator this matters less than it would with a mean, but a
single wild value can still drag a short window's median, and leaving it out costs nothing.

**Constant runs are the common case here, and they break the textbook formula.** A market
that reports ₱45.00 for eight days running has a MAD of exactly zero, and the standard
modified z-score divides by it. This is not a rare edge case in this dataset — most
commodities at most markets are flat for days at a time — so it is handled explicitly rather
than guarded against. See :func:`_deviation_scale`.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from presyowatch.log import get_logger

logger = get_logger(__name__)

MAD_TO_SIGMA: Final = 0.6745
"""Scales a median absolute deviation onto the standard-deviation scale for a normal
distribution, so the threshold below reads like a z-score."""

MEAN_AD_TO_SIGMA: Final = 1.253314
"""The equivalent constant for a *mean* absolute deviation, used when the MAD is zero."""

DEFAULT_WINDOW: Final = 9
DEFAULT_THRESHOLD: Final = 3.5
"""Iglewicz and Hoaglin's recommended cut-off for the modified z-score. Configurable, per
PLANNING.md — "window and threshold are config, not magic numbers"."""

DEFAULT_MIN_RELATIVE_DEVIATION: Final = 0.10
"""How far from the local median a price must sit before the word "anomaly" is used at all.

A pure z-score is scale-free, and on price data that is a liability. When a window holds
50.00, 51.00, 50.00, 50.00 the MAD is zero and even the mean-absolute-deviation fallback is
0.25, so an entirely ordinary one-peso move scores over six deviations. Formally correct;
practically it would flag routine variation on every stable commodity in the dataset and
bury the real spikes in noise.

Ten percent is a floor, not a second test: a value must clear *both* it and the threshold.
The failures worth catching here are not subtle — the real Corn Cracked row is 91% below its
neighbours — so the cost of this floor is small and the noise it prevents is large.
"""

DEFAULT_MIN_NEIGHBOURS: Final = 4
"""Below this many neighbours, no judgement is offered at all.

Three prices are not a distribution. Flagging against two neighbours would produce confident
nonsense on exactly the sparse series where a reader is least able to check it.
"""


MIN_USABLE_WINDOW: Final = 3
"""Below three there is no middle to take a median of."""

MIN_USABLE_NEIGHBOURS: Final = 2
"""A floor on the floor. One neighbour is a comparison, not a distribution."""


@dataclass(frozen=True, slots=True)
class AnomalyConfig:
    """Tunables. Defaults are documented above, not chosen for taste."""

    window: int = DEFAULT_WINDOW
    threshold: float = DEFAULT_THRESHOLD
    min_neighbours: int = DEFAULT_MIN_NEIGHBOURS
    min_relative_deviation: float = DEFAULT_MIN_RELATIVE_DEVIATION

    def __post_init__(self) -> None:
        """Validate the tunables.

        Raises:
            ValueError: If any is outside its usable range.
        """
        if self.window < MIN_USABLE_WINDOW:
            msg = f"window must be at least {MIN_USABLE_WINDOW}, got {self.window}"
            raise ValueError(msg)
        if self.threshold <= 0:
            msg = f"threshold must be positive, got {self.threshold}"
            raise ValueError(msg)
        if self.min_neighbours < MIN_USABLE_NEIGHBOURS:
            msg = (
                f"min_neighbours must be at least {MIN_USABLE_NEIGHBOURS}, "
                f"got {self.min_neighbours}"
            )
            raise ValueError(msg)
        if self.min_relative_deviation < 0:
            msg = f"min_relative_deviation must not be negative, got {self.min_relative_deviation}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Point:
    """One observation, reduced to what flagging needs."""

    observed_on: date
    average: Decimal | None
    low: Decimal | None = None
    high: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Flag:
    """One annotated observation.

    ``score`` is the modified z-score, ``None`` when there were too few neighbours to judge.
    """

    observed_on: date
    average: Decimal | None
    score: float | None
    is_anomaly: bool
    is_impossible: bool
    reason: str | None = None


def is_impossible(point: Point) -> str | None:
    """Return why a row is arithmetically impossible, or ``None`` if it is fine.

    Nothing statistical happens here. These are claims that cannot all be true at once, so
    they are caught with certainty rather than with a threshold.
    """
    if point.low is not None and point.high is not None and point.low > point.high:
        return f"low {point.low} is above high {point.high}"
    if point.average is None:
        return None
    if point.low is not None and point.average < point.low:
        return f"average {point.average} is below low {point.low}"
    if point.high is not None and point.average > point.high:
        return f"average {point.average} is above high {point.high}"
    return None


def _deviation_scale(neighbours: Sequence[float], centre: float) -> float | None:
    """Return the spread of ``neighbours`` about ``centre``, on a standard-deviation scale.

    The MAD is used when it is non-zero. When it is zero — which happens whenever more than
    half the window holds the same price, and in this dataset that is most of the time — the
    mean absolute deviation is used instead, following Iglewicz and Hoaglin.

    When *both* are zero the window is perfectly flat: every neighbour is the same price.
    ``None`` is returned to say "no spread at all", and the caller decides what that means,
    because dividing by it is exactly the bug this function exists to avoid.
    """
    absolute = [abs(value - centre) for value in neighbours]
    mad = statistics.median(absolute)
    if mad > 0:
        return mad / MAD_TO_SIGMA

    mean_ad = statistics.fmean(absolute)
    if mean_ad > 0:
        return mean_ad * MEAN_AD_TO_SIGMA
    return None


def _neighbours(values: Sequence[float | None], index: int, window: int) -> list[float]:
    """Return the priced neighbours nearest ``index``, excluding it.

    Centred on the point where there is room on both sides, and sliding to whichever side
    has data at the ends of a series. Blanks are skipped rather than treated as zero: a day
    with no monitoring is not a day the price was nothing.
    """
    reach = window // 2
    start = max(0, index - reach)
    stop = min(len(values), index + reach + 1)

    # Slide the window inwards at the edges so the first and last points are judged against
    # a full-sized sample rather than half of one.
    if stop - start < window:
        if start == 0:
            stop = min(len(values), start + window)
        else:
            start = max(0, stop - window)

    return [
        value
        for position in range(start, stop)
        if position != index and (value := values[position]) is not None
    ]


def flag_series(points: Sequence[Point], config: AnomalyConfig | None = None) -> list[Flag]:
    """Annotate a price series, oldest first.

    Args:
        points: One market's observations of one commodity, in date order. Passing several
            markets' prices as one series would compare a Butuan price against a Tandag one
            and call the difference an anomaly.
        config: Window, threshold and the floor on how much context is needed.

    Returns:
        One :class:`Flag` per input point, in the same order. Nothing is dropped: a caller
        that wants only the anomalies filters them itself, and one that wants the whole
        series annotated already has it.
    """
    settings = config or AnomalyConfig()
    values: list[float | None] = [
        None if point.average is None else float(point.average) for point in points
    ]

    flags: list[Flag] = []
    for index, point in enumerate(points):
        impossible = is_impossible(point)
        value = values[index]

        if value is None:
            # A blank is not an anomaly. It is a day nobody looked.
            flags.append(
                Flag(
                    observed_on=point.observed_on,
                    average=None,
                    score=None,
                    is_anomaly=False,
                    is_impossible=impossible is not None,
                    reason=impossible,
                )
            )
            continue

        neighbours = _neighbours(values, index, settings.window)
        score: float | None = None
        anomalous = False

        if len(neighbours) >= settings.min_neighbours:
            centre = statistics.median(neighbours)
            scale = _deviation_scale(neighbours, centre)
            if scale is None:
                # Every neighbour is the same price. Any difference at all is then as
                # unusual as it is possible to be, and no difference is entirely ordinary.
                # Reporting an infinite score would be arithmetically true and useless.
                anomalous = value != centre
                score = None if not anomalous else float("inf")
            else:
                score = (value - centre) / scale
                anomalous = abs(score) > settings.threshold

            # Both gates, always. A price that is statistically far from a very tight
            # cluster is still only a few pesos from it, and a few pesos is not news.
            if anomalous and not _far_enough(value, centre, settings.min_relative_deviation):
                anomalous = False

        reason = impossible
        if reason is None and anomalous:
            reason = _describe(value, neighbours, score)

        flags.append(
            Flag(
                observed_on=point.observed_on,
                average=point.average,
                score=score,
                is_anomaly=anomalous,
                is_impossible=impossible is not None,
                reason=reason,
            )
        )

    return flags


def _far_enough(value: float, centre: float, minimum: float) -> bool:
    """Return whether ``value`` differs from ``centre`` by at least ``minimum``, relatively.

    Measured against the centre rather than the value, so a drop to near zero — which is
    what a dropped digit looks like — is not divided by its own tiny magnitude.
    """
    if centre == 0:
        return value != 0
    return abs(value - centre) / abs(centre) >= minimum


def _describe(value: float, neighbours: Sequence[float], score: float | None) -> str:
    """Say what is unusual in words, with the comparison that produced it.

    A bare score tells a reader nothing they can check. The surrounding median is what makes
    the claim falsifiable by eye.
    """
    centre = statistics.median(neighbours)
    direction = "above" if value > centre else "below"
    if score is None or score == float("inf"):
        return f"{value:.2f} differs from {len(neighbours)} neighbours all at {centre:.2f}"
    return (
        f"{value:.2f} is {abs(score):.1f} deviations {direction} "
        f"the surrounding median of {centre:.2f}"
    )
