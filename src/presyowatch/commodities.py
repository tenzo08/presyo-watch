"""Resolving the source's commodity names onto canonical commodities.

PLANNING.md: "Commodity names vary across regions and over time. Maintain an explicit alias
table mapping raw strings to ``canonical_slug``. Unmapped names go to quarantine, never
guessed."

This module is the "never guessed" half, and it is deliberately dull. Resolution is an exact
dictionary lookup on a normalised key. There is no fuzzy matching, no prefix matching, no
nearest-neighbour fallback. A name nobody has mapped comes back unresolved, and the caller
quarantines the row with its raw strings intact so a person can decide.

That restraint is the whole point. Silently mapping ``Corn Grits | Feed Grade`` onto
``Corn Grits | White, Food Grade`` would average animal feed into the price of food on a
public chart, and nothing downstream would ever flag it. A quarantined row is visible; a
wrongly resolved row is not.

**Identity is the triple, not the name.** Seven rice varieties — ``Premium``, ``Basmati``,
``Glutinous``, ``Japonica/Jasponica``, ``Other Special Rice``, ``Regular Milled``,
``Well Milled`` — appear under both ``IMPORTED COMMERCIAL RICE`` and
``LOCAL COMMERCIAL RICE`` and are genuinely different products (verified across four sheets,
2026-07-28). Keying on the commodity name alone would merge imported and local rice prices
into one series.

**The unit is checked but is not part of the key.** A commodity's unit belongs to the
commodity, so it does not identify it. But a source that starts quoting a sack price where it
used to quote a kilo price would multiply a series by fifty with no other symptom, so a unit
that disagrees with the canonical record is treated as a reason to quarantine rather than
something to absorb quietly.
"""

import csv
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Final

from presyowatch.db.models import QuarantinedRow
from presyowatch.log import get_logger
from presyowatch.sources.bantay_presyo import PriceRow

logger = get_logger(__name__)

_KEY_SEPARATOR: Final = " | "
COMMODITIES_FILE: Final = "commodities.csv"
ALIASES_FILE: Final = "commodity_aliases.csv"
_DATA_PACKAGE: Final = "presyowatch.data"


def alias_key(group: str, commodity: str, specification: str | None) -> str:
    """Return the lookup key for a raw source triple.

    Case and internal whitespace are normalised because sheets differ on both — the same
    rice is ``5% Broken`` on one and ``5% broken`` on another. Nothing else is normalised:
    punctuation carries meaning here (``5%``, ``20-40%``, ``(12-14 pcs/kg)``) and stripping
    it would merge specifications that differ for real.
    """
    parts = (group, commodity, specification or "")
    return _KEY_SEPARATOR.join(" ".join(part.split()).casefold() for part in parts)


@dataclass(frozen=True, slots=True)
class CanonicalCommodity:
    """One row of the canonical commodity list."""

    canonical_slug: str
    group: str
    name: str
    specification: str | None
    unit: str


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of resolving one parsed row.

    Exactly one of ``commodity`` and ``reason`` is set. An unresolved row is an ordinary,
    expected outcome — not an error — and carries the reason it is going to quarantine.
    """

    commodity: CanonicalCommodity | None = None
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.commodity is not None


class CommodityResolver:
    """Maps raw source triples onto canonical commodities by exact lookup."""

    def __init__(
        self,
        commodities: Iterable[CanonicalCommodity],
        aliases: Mapping[str, str],
    ) -> None:
        """Initialise the resolver.

        Args:
            commodities: The canonical list, keyed internally by ``canonical_slug``.
            aliases: ``alias_key`` to ``canonical_slug``.

        Raises:
            ValueError: If an alias points at a slug that is not in ``commodities``. A
                dangling alias would resolve to nothing at runtime and look like an
                unmapped name, hiding a broken seed behind a plausible symptom.
        """
        self._commodities = {item.canonical_slug: item for item in commodities}
        self._aliases = dict(aliases)

        dangling = sorted(
            {slug for slug in self._aliases.values() if slug not in self._commodities}
        )
        if dangling:
            msg = f"aliases point at unknown canonical slugs: {dangling}"
            raise ValueError(msg)

    def __len__(self) -> int:
        return len(self._aliases)

    @property
    def commodity_count(self) -> int:
        return len(self._commodities)

    def resolve(self, row: PriceRow) -> Resolution:
        """Resolve one parsed row.

        Returns:
            A :class:`Resolution` carrying either the canonical commodity or the reason the
            row cannot be attributed to one.
        """
        key = alias_key(row.group, row.commodity, row.specification)
        slug = self._aliases.get(key)
        if slug is None:
            logger.info(
                "commodity_unmapped",
                group=row.group,
                commodity=row.commodity,
                specification=row.specification,
            )
            return Resolution(reason=f"no alias for {key!r}")

        commodity = self._commodities[slug]
        if commodity.unit != row.unit:
            # Not absorbed silently: a kilo price recorded against a sack commodity is a
            # fiftyfold error with no other symptom.
            return Resolution(
                reason=(
                    f"unit {row.unit!r} does not match {commodity.unit!r} recorded for {slug!r}"
                )
            )
        return Resolution(commodity=commodity)

    @classmethod
    def from_seed(cls) -> "CommodityResolver":
        """Build a resolver from the seed shipped with the package."""
        commodities, aliases = load_seed()
        return cls(commodities, aliases)


def load_seed() -> tuple[list[CanonicalCommodity], dict[str, str]]:
    """Read the committed seed files.

    Read through ``importlib.resources`` rather than by path so this keeps working from an
    installed wheel, not only from a checkout.
    """
    data = resources.files(_DATA_PACKAGE)

    commodities: list[CanonicalCommodity] = []
    with (
        resources.as_file(data / COMMODITIES_FILE) as path,
        path.open(encoding="utf-8", newline="") as handle,
    ):
        commodities.extend(
            CanonicalCommodity(
                canonical_slug=record["canonical_slug"],
                group=record["group"],
                name=record["name"],
                specification=record["specification"] or None,
                unit=record["unit"],
            )
            for record in csv.DictReader(handle)
        )

    aliases: dict[str, str] = {}
    with (
        resources.as_file(data / ALIASES_FILE) as path,
        path.open(encoding="utf-8", newline="") as handle,
    ):
        for record in csv.DictReader(handle):
            aliases[record["alias_key"]] = record["canonical_slug"]

    return commodities, aliases


def to_quarantine_row(
    row: PriceRow,
    resolution: Resolution,
    *,
    source_id: int | None = None,
    run_id: str | None = None,
    source_file_sha256: str | None = None,
) -> QuarantinedRow:
    """Turn an unresolved row into a quarantine record.

    The raw strings are kept verbatim so the row can be reprocessed once an alias exists,
    rather than needing the source file to be parsed again.

    Raises:
        ValueError: If ``resolution`` actually resolved. Quarantining a good row would
            understate the data quality figures in the one direction nobody would check.
    """
    if resolution.resolved:
        msg = "refusing to quarantine a row that resolved"
        raise ValueError(msg)

    return QuarantinedRow(
        source_id=source_id,
        run_id=run_id,
        stage="alias",
        reason=resolution.reason or "unresolved",
        source_file_sha256=source_file_sha256,
        payload={
            "group": row.group,
            "commodity": row.commodity,
            "specification": row.specification,
            "unit": row.unit,
            "alias_key": alias_key(row.group, row.commodity, row.specification),
        },
    )
