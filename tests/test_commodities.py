"""Tests for commodity resolution.

The point of these is mostly negative: proving the resolver *refuses* to do things. Every
near miss it declines is a price series it did not silently corrupt.
"""

from collections.abc import Iterable
from decimal import Decimal

import pytest

from presyowatch.commodities import (
    CanonicalCommodity,
    CommodityResolver,
    Resolution,
    alias_key,
    load_seed,
    to_quarantine_row,
)
from presyowatch.sources.bantay_presyo import PriceRow
from tests.conftest import SHEET_NAMES, load_sheet

# Two real triples that differ only by group, and are different products.
IMPORTED_PREMIUM = CanonicalCommodity(
    canonical_slug="imported-commercial-rice-premium-5-broken",
    group="IMPORTED COMMERCIAL RICE",
    name="Premium",
    specification="5% Broken",
    unit="kg",
)
LOCAL_PREMIUM = CanonicalCommodity(
    canonical_slug="local-commercial-rice-premium-5-broken",
    group="LOCAL COMMERCIAL RICE",
    name="Premium",
    specification="5% broken",
    unit="kg",
)
SACKED_FEED = CanonicalCommodity(
    canonical_slug="feeds-hog-booster",
    group="LIVESTOCK & POULTRY FEEDS",
    name="Hog Booster",
    specification=None,
    unit="bag",
)


def resolver_for(commodities: Iterable[CanonicalCommodity]) -> CommodityResolver:
    items = list(commodities)
    aliases = {
        alias_key(item.group, item.name, item.specification): item.canonical_slug for item in items
    }
    return CommodityResolver(items, aliases)


def price_row(
    group: str,
    commodity: str,
    specification: str | None = None,
    *,
    unit: str = "kg",
) -> PriceRow:
    return PriceRow(
        group=group,
        commodity=commodity,
        specification=specification,
        unit=unit,
        low=Decimal("48.00"),
        high=Decimal("56.00"),
        prevailing=Decimal("50.00"),
        average=Decimal("52.00"),
        unavailable=False,
        is_agricultural_input=False,
    )


# -- the key ---------------------------------------------------------------------


def test_the_key_is_the_whole_triple() -> None:
    assert alias_key("FISH", "Bangus", "Large") == "fish | bangus | large"


def test_case_and_spacing_do_not_change_the_key() -> None:
    """Sheets disagree on both: `5% Broken` on one, `5% broken` on another."""
    assert alias_key("FISH", "Bangus", "5% Broken") == alias_key("fish", "bangus", "5% broken")
    assert alias_key("FISH", " Bangus  Large ", None) == alias_key("FISH", "Bangus Large", "")


def test_a_missing_specification_is_an_empty_part_not_a_missing_one() -> None:
    """`None` and `""` mean the same thing — no specification recorded."""
    assert alias_key("FRUITS", "Melon", None) == alias_key("FRUITS", "Melon", "")
    assert alias_key("FRUITS", "Melon", None).endswith(" | ")


def test_punctuation_is_preserved_because_it_carries_meaning() -> None:
    """`5%` and `20-40%` are the only thing separating two rice grades."""
    assert alias_key("RICE", "Well Milled", "1-19% Bran Streak") != alias_key(
        "RICE", "Well Milled", "20-40% Bran Streak"
    )


def test_the_group_is_part_of_the_key() -> None:
    """`Premium` is a different product in each rice group; the key must separate them."""
    assert alias_key("IMPORTED COMMERCIAL RICE", "Premium", "5% Broken") != alias_key(
        "LOCAL COMMERCIAL RICE", "Premium", "5% Broken"
    )


# -- resolving -------------------------------------------------------------------


def test_a_mapped_triple_resolves() -> None:
    resolver = resolver_for([IMPORTED_PREMIUM])

    outcome = resolver.resolve(price_row("IMPORTED COMMERCIAL RICE", "Premium", "5% Broken"))

    assert outcome.resolved
    assert outcome.commodity == IMPORTED_PREMIUM
    assert outcome.reason is None


def test_case_differences_still_resolve() -> None:
    resolver = resolver_for([IMPORTED_PREMIUM])

    outcome = resolver.resolve(price_row("imported commercial rice", "premium", "5% BROKEN"))

    assert outcome.resolved


def test_the_two_premiums_resolve_to_different_commodities() -> None:
    """Imported and local rice must not collapse into one series."""
    resolver = resolver_for([IMPORTED_PREMIUM, LOCAL_PREMIUM])

    imported = resolver.resolve(price_row("IMPORTED COMMERCIAL RICE", "Premium", "5% Broken"))
    local = resolver.resolve(price_row("LOCAL COMMERCIAL RICE", "Premium", "5% broken"))

    assert imported.commodity == IMPORTED_PREMIUM
    assert local.commodity == LOCAL_PREMIUM


def test_an_unmapped_triple_does_not_resolve() -> None:
    resolver = resolver_for([IMPORTED_PREMIUM])

    outcome = resolver.resolve(price_row("FISH", "Something New", None))

    assert not outcome.resolved
    assert outcome.reason is not None
    assert "no alias" in outcome.reason


# -- the refusals that matter ----------------------------------------------------


@pytest.mark.parametrize(
    ("group", "commodity", "specification"),
    [
        # A truncated specification — the real artefact shape.
        ("IMPORTED COMMERCIAL RICE", "Premium", "5%"),
        # A specification with text leaked in from the row above.
        ("IMPORTED COMMERCIAL RICE", "Premium", "bran streak 5% Broken"),
        # A truncated commodity name, as in `Habichuelas/Baguio Beans,`.
        ("IMPORTED COMMERCIAL RICE", "Premiu", "5% Broken"),
        # A missing specification where one is recorded.
        ("IMPORTED COMMERCIAL RICE", "Premium", None),
        # A near-miss group.
        ("IMPORTED COMMERCIAL RIC", "Premium", "5% Broken"),
    ],
)
def test_near_misses_are_refused_rather_than_guessed(
    group: str, commodity: str, specification: str | None
) -> None:
    """Every one of these is obviously "the same thing" to a person, and is still refused.

    A prefix or fuzzy match would resolve them, and the same rule would merge
    `Corn Grits | Feed Grade` into `Corn Grits | White, Food Grade` — averaging animal feed
    into the price of food on a public chart, with nothing downstream to flag it. A
    quarantined row is visible; a wrongly resolved one is not.
    """
    resolver = resolver_for([IMPORTED_PREMIUM])

    assert not resolver.resolve(price_row(group, commodity, specification)).resolved


def test_a_changed_unit_is_refused() -> None:
    """A sack price recorded against a kilo commodity is a fiftyfold error.

    Nothing else about the row would look wrong, so this cannot be absorbed quietly.
    """
    resolver = resolver_for([SACKED_FEED])

    outcome = resolver.resolve(
        price_row("LIVESTOCK & POULTRY FEEDS", "Hog Booster", None, unit="kg")
    )

    assert not outcome.resolved
    assert outcome.reason is not None
    assert "does not match" in outcome.reason
    assert "bag" in outcome.reason


def test_the_matching_unit_resolves() -> None:
    resolver = resolver_for([SACKED_FEED])

    outcome = resolver.resolve(
        price_row("LIVESTOCK & POULTRY FEEDS", "Hog Booster", None, unit="bag")
    )

    assert outcome.resolved


def test_a_dangling_alias_is_refused_at_construction() -> None:
    """An alias pointing nowhere would look exactly like an unmapped name at runtime.

    Failing loudly at startup beats a broken seed hiding behind a plausible symptom.
    """
    with pytest.raises(ValueError, match="unknown canonical slugs"):
        CommodityResolver([IMPORTED_PREMIUM], {"fish | bangus | ": "no-such-slug"})


# -- quarantine ------------------------------------------------------------------


def test_an_unresolved_row_becomes_a_quarantine_record() -> None:
    resolver = resolver_for([IMPORTED_PREMIUM])
    row = price_row("FISH", "Galunggong, Local", "Male, Medium (12-14")
    outcome = resolver.resolve(row)

    record = to_quarantine_row(
        row, outcome, source_id=3, run_id="run-1", source_file_sha256="a" * 64
    )

    assert record.stage == "alias"
    assert record.source_id == 3
    assert record.run_id == "run-1"
    assert record.source_file_sha256 == "a" * 64
    assert record.reason
    assert record.payload["commodity"] == "Galunggong, Local"
    assert record.payload["specification"] == "Male, Medium (12-14"
    assert record.payload["alias_key"] == alias_key(
        "FISH", "Galunggong, Local", "Male, Medium (12-14"
    )


def test_the_raw_strings_survive_verbatim() -> None:
    """Reprocessing after an alias is added must not need the PDF parsed again."""
    resolver = resolver_for([IMPORTED_PREMIUM])
    row = price_row("HIGHLAND VEGETABLES", "Habichuelas/Baguio Beans,", None)

    record = to_quarantine_row(row, resolver.resolve(row))

    assert record.payload["group"] == "HIGHLAND VEGETABLES"
    assert record.payload["commodity"] == "Habichuelas/Baguio Beans,"
    assert record.payload["unit"] == "kg"


def test_quarantining_a_resolved_row_is_refused() -> None:
    """Would understate the failure count in the one direction nobody checks."""
    row = price_row("IMPORTED COMMERCIAL RICE", "Premium", "5% Broken")

    with pytest.raises(ValueError, match="refusing to quarantine"):
        to_quarantine_row(row, Resolution(commodity=IMPORTED_PREMIUM))


# -- the committed seed ----------------------------------------------------------


def test_the_seed_loads() -> None:
    commodities, aliases = load_seed()

    assert len(commodities) > 100
    assert len(aliases) == len(commodities)


def test_seed_slugs_are_unique() -> None:
    commodities, _ = load_seed()

    slugs = [item.canonical_slug for item in commodities]
    assert len(slugs) == len(set(slugs))


def test_every_seeded_commodity_has_a_group_name_and_unit() -> None:
    commodities, _ = load_seed()

    for item in commodities:
        assert item.canonical_slug
        assert item.group
        assert item.name
        assert item.unit


def test_seed_slugs_fit_the_database_column() -> None:
    """`commodities.canonical_slug` is `String(128)`."""
    commodities, _ = load_seed()

    assert all(len(item.canonical_slug) <= 128 for item in commodities)


def test_the_seed_builds_a_resolver() -> None:
    resolver = CommodityResolver.from_seed()

    assert len(resolver) == resolver.commodity_count > 100


def test_every_seeded_commodity_resolves_its_own_triple() -> None:
    """Self-consistency: the seed's own rows must round-trip through the resolver."""
    resolver = CommodityResolver.from_seed()
    commodities, _ = load_seed()

    for item in commodities:
        row = price_row(item.group, item.name, item.specification, unit=item.unit)
        outcome = resolver.resolve(row)
        assert outcome.resolved, f"{item.canonical_slug} does not resolve itself"
        assert outcome.commodity is not None
        assert outcome.commodity.canonical_slug == item.canonical_slug


# -- the seed against the real sheets --------------------------------------------


def test_most_real_rows_resolve() -> None:
    """About 87% of rows across the twelve sheets resolve unattended.

    That is *down* from 96% on the original four, and the drop is the corpus telling the
    truth rather than a regression. Two of the added sheets use a different vocabulary for
    the same products — ``Well-Milled`` for ``Well Milled``, ``Special (blue tag)``,
    ``Bangus | Large`` for ``Bangus, Large`` — and each of those is an alias a person has to
    write. Guessing them is exactly what :mod:`presyowatch.commodities` refuses to do, so
    they quarantine until curated.

    A drop below this floor means the seed went stale or the parser changed what it emits.
    """
    resolver = CommodityResolver.from_seed()
    total = resolved = 0
    for name in SHEET_NAMES:
        for row in load_sheet(name).rows:
            total += 1
            resolved += resolver.resolve(row).resolved

    assert total > 1500
    assert resolved / total > 0.85, f"only {resolved}/{total} resolved"


def test_every_unresolved_real_row_can_be_quarantined() -> None:
    """Nothing is dropped: each failure produces a record a human can act on."""
    resolver = CommodityResolver.from_seed()

    for name in SHEET_NAMES:
        for row in load_sheet(name).rows:
            outcome = resolver.resolve(row)
            if not outcome.resolved:
                record = to_quarantine_row(row, outcome, source_id=1, run_id="r")
                assert record.stage == "alias"
                assert record.payload["alias_key"]


def test_the_seed_matches_the_fixtures_it_was_generated_from() -> None:
    """Drift check, in the spirit of `alembic check`.

    Re-derives the seed's alias keys from the fixtures using the documented attestation
    threshold and compares. Fails if the fixtures changed, the parser changed what it
    emits, or the seed was hand-edited without regenerating — any of which would leave the
    committed seed quietly describing a vocabulary that no longer exists.
    """
    attestation: dict[str, int] = {}
    for name in SHEET_NAMES:
        for row in load_sheet(name).rows:
            key = alias_key(row.group, row.commodity, row.specification)
            attestation[key] = attestation.get(key, 0) + 1

    threshold = len(SHEET_NAMES) // 2 + 1
    expected = {key for key, count in attestation.items() if count >= threshold}
    _, aliases = load_seed()

    assert set(aliases) == expected, (
        "seed is out of step with the fixtures; run scripts/generate_commodity_seed.py"
    )


def test_known_extraction_artefacts_are_not_in_the_seed() -> None:
    """The mangled specifications must stay unmapped so they surface for curation."""
    _, aliases = load_seed()

    for artefact in (
        alias_key("FISH", "Galunggong, Imported", "pcs/kg) Male, Medium (12-14"),
        alias_key("FISH", "Salmon Head, Local", "pcs/kg)"),
        alias_key("HIGHLAND VEGETABLES", "Broccoli, Local", "Medium (8-10 cm"),
        alias_key("HIGHLAND VEGETABLES", "Habichuelas/Baguio Beans,", None),
    ):
        assert artefact not in aliases
