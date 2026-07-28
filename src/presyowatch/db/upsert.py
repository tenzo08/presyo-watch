"""Writing observations idempotently, and keeping whatever a correction replaced.

Rule 4: every write to ``price_observations`` is an upsert on the natural key
``(source_id, market_id, commodity_id, observed_on)``, and a corrected figure updates the
row in place while the superseded one is appended to ``observation_revisions``. Rerunning
the ingester over a date range must converge, not accumulate — PLANNING.md calls that
"idempotent by construction" and asks for a test, which ``tests/db/test_upsert.py`` is.

**A change is a change in the figures, not in the provenance.** Two files can carry the
same numbers for the same commodity on the same day: the DA republishes a whole corrected
sheet as ``Revised-...pdf`` even when one row moved, and the CMS re-uploads files under a
``-1`` suffix. If a row's figures are identical to what is stored, nothing is written at
all — not ``ingested_at``, not ``source_file_sha256``, not ``revision_no``. So
``source_file_sha256`` means "the file these figures first came from", a rerun is a genuine
no-op at row level, and a correction to one row does not make the other 152 look revised.

**Detection is by value, never by filename.** ``Revised-`` is how the DA happens to name
corrections; KNOWLEDGE.md § "URL naming is inconsistent" is a catalogue of why the filename
cannot be trusted to say what a file is. Comparing figures needs no such trust, and it also
catches a correction republished under an ordinary name.

**Why not ``INSERT ... ON CONFLICT DO UPDATE``.** Postgres' upsert is the obvious tool and
it cannot do this job: the values being overwritten have to be archived, and ``RETURNING``
on a conflicting insert reports the row as it now is, not the row it replaced. Reading the
existing rows first — under a row lock — is what makes the previous figures available to
copy into ``observation_revisions``. The unique constraint remains the backstop, so a
concurrent writer produces an ``IntegrityError`` rather than a silently lost revision.

**Ordering is the caller's problem.** Last write wins, so ingesting a ``Revised-`` sheet
*before* the original it corrects would record the original as the correction. The backfill
runner must process a date's files in index order; nothing here can tell which of two files
was published first.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from presyowatch.db.models import ObservationRevision, PriceObservation
from presyowatch.log import get_logger

logger = get_logger(__name__)

NaturalKey = tuple[int, int, int, date]
"""``(source_id, market_id, commodity_id, observed_on)`` — the upsert target."""

Figures = tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None, bool]
"""Everything a correction can change: the four prices and the unavailable flag."""

STORED_SCALE: Final = Decimal("0.01")
"""The scale of the ``Numeric(12, 2)`` price columns.

Incoming prices are quantized to it *before* being compared with what is stored, which is
not cosmetic: a sheet quoting ``52.805`` would be stored as ``52.81`` and then differ from
itself on every rerun, superseding the row forever and growing a revision per run. Postgres
rounds half away from zero, so ``ROUND_HALF_UP`` on non-negative prices agrees with it.
"""


class PendingObservation(BaseModel):
    """One resolved observation, ready to be written.

    The parsed row (:class:`presyowatch.sources.bantay_presyo.PriceRow`) plus the identity
    the resolvers established for it. Validated again here rather than trusted, because
    this is the boundary at which a mis-assembled row stops being a Python object and
    becomes a public number: the two validators mirror the ``price_observations`` check
    constraints, so a bad row is rejected with a reason instead of aborting a flush of 153.
    """

    model_config = ConfigDict(frozen=True)

    source_id: int
    market_id: int
    commodity_id: int
    observed_on: date

    low: Decimal | None = None
    high: Decimal | None = None
    prevailing: Decimal | None = None
    average: Decimal | None = None
    unavailable: bool = False

    source_file_sha256: str

    @field_validator("low", "high", "prevailing", "average")
    @classmethod
    def _quantize(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return value.quantize(STORED_SCALE, rounding=ROUND_HALF_UP)

    @model_validator(mode="after")
    def _check_range(self) -> "PendingObservation":
        if self.low is not None and self.high is not None and self.low > self.high:
            msg = f"low {self.low} is above high {self.high}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_unavailable(self) -> "PendingObservation":
        if self.unavailable and any(price is not None for price in self.figures[:4]):
            msg = "row is marked unavailable but carries prices"
            raise ValueError(msg)
        return self

    @property
    def natural_key(self) -> NaturalKey:
        return (self.source_id, self.market_id, self.commodity_id, self.observed_on)

    @property
    def figures(self) -> Figures:
        return (self.low, self.high, self.prevailing, self.average, self.unavailable)


@dataclass(frozen=True, slots=True)
class ObservationConflict:
    """One natural key that a single batch described in more than one way.

    Two raw rows on a sheet can resolve to the same canonical commodity — the alias table
    maps many source strings onto one — and if they disagree about the price there is no
    honest way to pick. Neither is written, and the caller quarantines them at stage
    ``validate`` with the reason below.
    """

    key: NaturalKey
    observations: tuple[PendingObservation, ...]

    @property
    def reason(self) -> str:
        return (
            f"{len(self.observations)} rows in one batch resolve to "
            f"commodity {self.key[2]} at market {self.key[1]} on {self.key[3]:%Y-%m-%d} "
            f"with different figures"
        )


@dataclass(frozen=True, slots=True)
class UpsertOutcome:
    """What one call to :func:`upsert_observations` did.

    ``unchanged`` is reported rather than folded into a single "upserted" number because it
    is the interesting one: on a healthy rerun it should be everything, and a run where it
    is unexpectedly zero means something is rewriting rows that did not change.
    """

    inserted: int = 0
    revised: int = 0
    unchanged: int = 0
    conflicts: tuple[ObservationConflict, ...] = ()

    @property
    def written(self) -> int:
        """Rows the call actually wrote — what ``ingestion_runs.rows_upserted`` counts."""
        return self.inserted + self.revised

    @property
    def rejected(self) -> int:
        """Rows dropped as conflicting, which the caller must quarantine."""
        return sum(len(conflict.observations) for conflict in self.conflicts)


def collapse_duplicates(
    observations: Iterable[PendingObservation],
) -> tuple[dict[NaturalKey, PendingObservation], tuple[ObservationConflict, ...]]:
    """Reduce a batch to at most one observation per natural key.

    Repeats that agree are collapsed silently — the same figures written twice are the same
    figures. Repeats that disagree are pulled out entirely, including the first one seen:
    keeping whichever arrived first would be a guess dressed up as a rule.

    Returns:
        The observations to write, keyed by natural key, and the keys that could not be
        settled.
    """
    settled: dict[NaturalKey, PendingObservation] = {}
    disputed: dict[NaturalKey, list[PendingObservation]] = {}

    for observation in observations:
        key = observation.natural_key
        if key in disputed:
            disputed[key].append(observation)
            continue

        first = settled.get(key)
        if first is None:
            settled[key] = observation
        elif first.figures != observation.figures:
            disputed[key] = [first, observation]
            del settled[key]

    conflicts = tuple(
        ObservationConflict(key=key, observations=tuple(rows)) for key, rows in disputed.items()
    )
    return settled, conflicts


def upsert_observations(
    session: Session,
    observations: Iterable[PendingObservation],
) -> UpsertOutcome:
    """Reconcile ``observations`` with what is stored.

    A row that is not there is inserted; a row whose figures changed is superseded — its
    previous values appended to ``observation_revisions`` and ``revision_no`` incremented —
    and a row that agrees with what is stored is left completely alone.

    Args:
        session: An open session. Nothing here commits: the caller owns the transaction, so
            that one source's writes succeed or fail as a unit
            (PLANNING.md § "Fail loudly, degrade gracefully").
        observations: The batch to write, in any order.

    Returns:
        Counts of what happened, plus any natural key the batch itself disagreed about.
    """
    pending, conflicts = collapse_duplicates(observations)
    stored = _lock_existing(session, pending)

    inserted = revised = unchanged = 0
    for key, incoming in pending.items():
        current = stored.get(key)
        if current is None:
            session.add(_as_observation(incoming))
            inserted += 1
        elif _figures_of(current) == incoming.figures:
            unchanged += 1
        else:
            session.add(_superseded(current))
            _apply(current, incoming)
            revised += 1

    # Flushed here so that a constraint violation is raised inside the call that caused it,
    # rather than surfacing later against whatever the caller happened to be doing.
    session.flush()

    logger.info(
        "observations_upserted",
        inserted=inserted,
        revised=revised,
        unchanged=unchanged,
        conflicts=len(conflicts),
    )
    return UpsertOutcome(
        inserted=inserted,
        revised=revised,
        unchanged=unchanged,
        conflicts=conflicts,
    )


def _lock_existing(
    session: Session,
    pending: Mapping[NaturalKey, PendingObservation],
) -> dict[NaturalKey, PriceObservation]:
    """Return the stored rows for ``pending``'s keys, locked for update.

    The lock closes the window between deciding a row is unchanged and writing it. Keys are
    ordered so that two runs over overlapping ranges queue behind each other in the same
    order; Postgres may lock after sorting, so this narrows the deadlock window rather than
    closing it. The real guarantee is that a source's ingestion is single-flight — the
    unique constraint is what catches it if that ever stops being true.
    """
    if not pending:
        return {}

    statement = (
        select(PriceObservation)
        .where(
            tuple_(
                PriceObservation.source_id,
                PriceObservation.market_id,
                PriceObservation.commodity_id,
                PriceObservation.observed_on,
            ).in_(sorted(pending))
        )
        .order_by(
            PriceObservation.source_id,
            PriceObservation.market_id,
            PriceObservation.commodity_id,
            PriceObservation.observed_on,
        )
        .with_for_update()
    )
    return {
        (row.source_id, row.market_id, row.commodity_id, row.observed_on): row
        for row in session.scalars(statement)
    }


def _figures_of(observation: PriceObservation) -> Figures:
    return (
        observation.low,
        observation.high,
        observation.prevailing,
        observation.average,
        observation.unavailable,
    )


def _as_observation(incoming: PendingObservation) -> PriceObservation:
    """Build a first-revision row. Defaults are set explicitly, not left to the server."""
    return PriceObservation(
        source_id=incoming.source_id,
        market_id=incoming.market_id,
        commodity_id=incoming.commodity_id,
        observed_on=incoming.observed_on,
        low=incoming.low,
        high=incoming.high,
        prevailing=incoming.prevailing,
        average=incoming.average,
        unavailable=incoming.unavailable,
        revision_no=0,
        source_file_sha256=incoming.source_file_sha256,
    )


def _superseded(observation: PriceObservation) -> ObservationRevision:
    """Copy an observation's current values into an append-only revision row.

    ``revision_no`` is the number these values *had*, so the history reads as
    "revision 0 said this", and the file recorded is the one they came from — not the one
    replacing them.
    """
    return ObservationRevision(
        observation_id=observation.id,
        revision_no=observation.revision_no,
        low=observation.low,
        high=observation.high,
        prevailing=observation.prevailing,
        average=observation.average,
        unavailable=observation.unavailable,
        source_file_sha256=observation.source_file_sha256,
    )


def _apply(observation: PriceObservation, incoming: PendingObservation) -> None:
    """Overwrite an observation in place with a correction's figures."""
    observation.low = incoming.low
    observation.high = incoming.high
    observation.prevailing = incoming.prevailing
    observation.average = incoming.average
    observation.unavailable = incoming.unavailable
    observation.revision_no += 1
    observation.source_file_sha256 = incoming.source_file_sha256
    # The database clock, matching the server default the insert path uses. Two rows written
    # by one transaction should not disagree about when the transaction happened.
    observation.ingested_at = func.now()
