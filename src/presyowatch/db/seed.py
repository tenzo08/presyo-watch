"""Loading the committed reference data into the database.

The ingester cannot write an observation until the things it points at exist: a ``sources``
row for Caraga, ``regions`` rows for the provinces its sheets name, and ``commodities`` plus
``commodity_aliases`` for the vocabulary the resolver was built from. Those all live as CSVs
under ``presyowatch/data`` and this is what puts them in a database.

**A seed load is an upsert, not an insert.** It runs on every deploy and must converge, the
same way the observation writer does — running it twice changes nothing, and a corrected
attribution string or a newly curated alias is picked up without a hand-written migration.

**Not an Alembic data migration**, which was the other candidate. The commodity seed is
regenerated from the fixture corpus whenever that corpus grows, so pinning a snapshot of it
into a migration would mean a new migration every time a fixture is added, and a schema
history that is mostly data churn. Migrations move the schema; this moves the contents.

Markets are the exception and are not seeded at all. See :func:`resolve_market`.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from presyowatch.commodities import load_seed
from presyowatch.db.models import Commodity, CommodityAlias, Market, Region, Source
from presyowatch.log import get_logger
from presyowatch.places import load_regions, load_sources, normalise_place

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SeedCounts:
    """What one seed load inserted and updated, per table."""

    sources_inserted: int = 0
    sources_updated: int = 0
    regions_inserted: int = 0
    regions_updated: int = 0
    commodities_inserted: int = 0
    commodities_updated: int = 0
    aliases_inserted: int = 0
    aliases_updated: int = 0

    @property
    def changed(self) -> int:
        """Total rows written. Zero on a rerun that had nothing to do."""
        return (
            self.sources_inserted
            + self.sources_updated
            + self.regions_inserted
            + self.regions_updated
            + self.commodities_inserted
            + self.commodities_updated
            + self.aliases_inserted
            + self.aliases_updated
        )


def seed_reference_data(session: Session) -> SeedCounts:
    """Load sources, regions, commodities and aliases from the committed CSVs.

    Args:
        session: An open session. The caller owns the transaction; nothing here commits.

    Returns:
        Counts of what was inserted and updated, so a deploy log can say whether anything
        actually changed.
    """
    sources_inserted, sources_updated = _seed_sources(session)
    regions_inserted, regions_updated = _seed_regions(session)
    commodities_inserted, commodities_updated = _seed_commodities(session)
    session.flush()
    aliases_inserted, aliases_updated = _seed_aliases(session)
    session.flush()

    counts = SeedCounts(
        sources_inserted=sources_inserted,
        sources_updated=sources_updated,
        regions_inserted=regions_inserted,
        regions_updated=regions_updated,
        commodities_inserted=commodities_inserted,
        commodities_updated=commodities_updated,
        aliases_inserted=aliases_inserted,
        aliases_updated=aliases_updated,
    )
    logger.info("reference_data_seeded", changed=counts.changed)
    return counts


def _seed_sources(session: Session) -> tuple[int, int]:
    existing = {row.slug: row for row in session.scalars(select(Source))}
    inserted = updated = 0
    for seed in load_sources():
        current = existing.get(seed.slug)
        if current is None:
            session.add(
                Source(
                    slug=seed.slug,
                    name=seed.name,
                    base_url=seed.base_url,
                    licence=seed.licence,
                    attribution_text=seed.attribution_text,
                )
            )
            inserted += 1
            continue
        fields = (seed.name, seed.base_url, seed.licence, seed.attribution_text)
        if (current.name, current.base_url, current.licence, current.attribution_text) != fields:
            current.name, current.base_url = seed.name, seed.base_url
            current.licence, current.attribution_text = seed.licence, seed.attribution_text
            updated += 1
    return inserted, updated


def _seed_regions(session: Session) -> tuple[int, int]:
    existing = {row.psgc_code: row for row in session.scalars(select(Region))}
    inserted = updated = 0
    for seed in load_regions():
        current = existing.get(seed.psgc_code)
        if current is None:
            session.add(Region(psgc_code=seed.psgc_code, name=seed.name, level=seed.level))
            inserted += 1
        elif (current.name, current.level) != (seed.name, seed.level):
            current.name = seed.name
            # The literal type is guaranteed by the CHECK constraint behind the column; the
            # CSV is checked into this repo, not supplied by a source.
            current.level = seed.level  # type: ignore[assignment]
            updated += 1
    return inserted, updated


def _seed_commodities(session: Session) -> tuple[int, int]:
    commodities, _ = load_seed()
    existing = {row.canonical_slug: row for row in session.scalars(select(Commodity))}
    inserted = updated = 0
    for seed in commodities:
        current = existing.get(seed.canonical_slug)
        if current is None:
            session.add(
                Commodity(
                    canonical_slug=seed.canonical_slug,
                    group=seed.group,
                    name=seed.name,
                    specification=seed.specification,
                    unit=seed.unit,
                )
            )
            inserted += 1
            continue
        fields = (seed.group, seed.name, seed.specification, seed.unit)
        if (current.group, current.name, current.specification, current.unit) != fields:
            current.group, current.name = seed.group, seed.name
            current.specification, current.unit = seed.specification, seed.unit
            updated += 1
    return inserted, updated


def _seed_aliases(session: Session) -> tuple[int, int]:
    """Load the alias rows.

    Runs after commodities are flushed, because an alias needs its commodity's id. A seed
    whose alias points at a slug that was never written is a broken seed rather than a
    missing row, so it fails loudly instead of being skipped —
    :class:`presyowatch.commodities.CommodityResolver` refuses the same case for the same
    reason.

    Raises:
        ValueError: If an alias names a canonical slug that is not in ``commodities``.
    """
    _, aliases = load_seed()
    rows = session.execute(select(Commodity.canonical_slug, Commodity.id)).tuples().all()
    commodity_ids: dict[str, int] = dict(rows)
    existing = {row.raw_name: row for row in session.scalars(select(CommodityAlias))}

    inserted = updated = 0
    for raw_name, slug in aliases.items():
        commodity_id = commodity_ids.get(slug)
        if commodity_id is None:
            msg = f"alias {raw_name!r} points at unknown canonical slug {slug!r}"
            raise ValueError(msg)

        current = existing.get(raw_name)
        if current is None:
            session.add(CommodityAlias(raw_name=raw_name, commodity_id=commodity_id))
            inserted += 1
        elif current.commodity_id != commodity_id:
            current.commodity_id = commodity_id
            updated += 1
    return inserted, updated


def resolve_market(
    session: Session,
    *,
    region_psgc_code: str,
    municipality: str,
    market: str,
) -> Market:
    """Return the market for a sheet's header, creating it on first sight.

    Markets are not seeded. A province is an externally-numbered thing and an unknown one is
    an error; a market is only a place the source chose to monitor, and regional offices add
    them. Recording ``Luha Public Market, Tandag City`` because a sheet says so is not a
    guess.

    Matching is on the *normalised* names while the row keeps the spelling first seen. The
    unique constraint is on the raw columns, so it would happily accept ``Butuan City`` and
    ``butuan city`` as two markets; comparing normalised is what actually prevents that, and
    it is the same rule :mod:`presyowatch.places` applies to provinces.
    """
    wanted = (normalise_place(municipality), normalise_place(market))
    candidates = session.scalars(
        select(Market).where(Market.region_psgc_code == region_psgc_code)
    ).all()
    for candidate in candidates:
        if (normalise_place(candidate.municipality), normalise_place(candidate.name)) == wanted:
            return candidate

    created = Market(
        region_psgc_code=region_psgc_code,
        municipality=municipality,
        name=market,
    )
    session.add(created)
    session.flush()
    logger.info(
        "market_created",
        region=region_psgc_code,
        municipality=municipality,
        market=market,
    )
    return created
