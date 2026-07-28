"""Province and market name normalisation.

Every variant asserted here is one the committed sheets actually write. The reason this
module exists at all is that matching on the raw string would give one province two
``regions`` rows and every chart two half-populated series.
"""

import pytest

from presyowatch.places import (
    RegionResolver,
    SeedRegion,
    load_regions,
    load_sources,
    normalise_place,
)


def test_case_differences_are_the_same_place() -> None:
    """Both spellings appear in the corpus, sometimes for the same market."""
    assert normalise_place("Agusan del Norte") == normalise_place("Agusan Del Norte")


def test_the_province_of_prefix_is_dropped() -> None:
    """The San Jose sheet writes `Province of Dinagat Islands`; the PSGC does not."""
    assert normalise_place("Province of Dinagat Islands") == normalise_place("Dinagat Islands")


def test_surrounding_and_internal_whitespace_is_collapsed() -> None:
    assert normalise_place("  Surigao   del  Sur ") == normalise_place("Surigao del Sur")


def test_distinct_places_stay_distinct() -> None:
    """A more aggressive normaliser would eventually merge two real municipalities."""
    assert normalise_place("San Jose") != normalise_place("San Jose de Buenavista")


@pytest.mark.parametrize(
    "written",
    [
        "Agusan del Norte",
        "Agusan Del Norte",
        "Agusan del Sur",
        "Surigao del Norte",
        "Surigao del Sur",
        "Province of Dinagat Islands",
    ],
)
def test_every_province_the_fixtures_name_resolves(written: str) -> None:
    """The five provinces across the twelve committed sheets, as they are written."""
    assert RegionResolver.from_seed().resolve(written) is not None


def test_an_unseeded_province_is_refused_rather_than_invented() -> None:
    """A PSGC code cannot be guessed, and a fabricated one would be a public identifier."""
    assert RegionResolver.from_seed().resolve("Bukidnon") is None


def test_the_region_row_itself_is_not_a_province() -> None:
    """Caraga is in the seed as the region; a sheet naming it would not be a province."""
    assert RegionResolver.from_seed().resolve("Caraga") is None


def test_the_resolver_returns_the_psgc_code() -> None:
    found = RegionResolver.from_seed().resolve("Surigao del Sur")

    assert found is not None
    assert found.psgc_code == "166800000"
    assert found.level == "province"


def test_the_seed_holds_the_region_and_its_provinces() -> None:
    regions = load_regions()

    assert sum(1 for region in regions if region.level == "region") == 1
    assert sum(1 for region in regions if region.level == "province") == 5


def test_every_seeded_psgc_code_is_distinct() -> None:
    codes = [region.psgc_code for region in load_regions()]

    assert len(set(codes)) == len(codes)


def test_a_resolver_built_from_one_province_knows_only_that_one() -> None:
    resolver = RegionResolver(
        [SeedRegion(psgc_code="166800000", name="Surigao del Sur", level="province")]
    )

    assert resolver.resolve("Surigao del Sur") is not None
    assert resolver.resolve("Agusan del Norte") is None
    assert len(resolver) == 1


def test_the_source_seed_carries_attribution() -> None:
    """Rule 8: attribution is a condition of use, so it cannot be blank."""
    sources = load_sources()

    assert len(sources) == 1
    assert sources[0].slug == "da-caraga"
    assert "Department of Agriculture" in sources[0].attribution_text
