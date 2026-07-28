"""Resolving the place names on a sheet onto the regions the schema knows about.

The same problem as :mod:`presyowatch.commodities`, one level up: the sheets write a province
name as free text and it varies. All three of these appear in the committed corpus, for two
provinces:

``Agusan del Norte`` / ``Agusan Del Norte``
    Case differs between markets, and between sheets from the same market.
``Province of Dinagat Islands``
    A prefix no other province carries.

Matching on the raw string would create a separate ``regions`` row for each spelling, and
every chart would then show two half-populated series for one province. So a province is
looked up by a normalised key, and — as with commodities — a name nobody has mapped is
**refused rather than guessed**. An unrecognised province means a sheet from outside the
seeded region, or a spelling nobody has seen; inventing a PSGC code for it would put a
fabricated identifier into a public dataset.

**Markets are created, not looked up.** A province is a fixed, externally-numbered thing, so
an unknown one is an error. A market is just a place the source decided to monitor, and new
ones appear whenever a regional office adds one. Recording ``(province, municipality, market)``
as it is written is not a guess — it is what the sheet says — so markets are created on first
sight and matched on their normalised triple thereafter.

**The PSGC codes are the one thing in this project not verified by direct fetch.** PSA serves
``https://psa.gov.ph/classification/psgc`` but returns **HTTP 403** to this project's client
(checked 2026-07-28, after its ``robots.txt`` allowed the path). Spoofing a browser
User-Agent to get around that would be routing around an access control, so the codes in
``data/regions.csv`` are transcribed from the PSGC rather than fetched, and KNOWLEDGE.md
records that they are unconfirmed. A wrong code here mislabels a region; it does not corrupt
a price.
"""

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from typing import Final

from presyowatch.log import get_logger

logger = get_logger(__name__)

REGIONS_FILE: Final = "regions.csv"
SOURCES_FILE: Final = "sources.csv"
_DATA_PACKAGE: Final = "presyowatch.data"

_PROVINCE_PREFIX = re.compile(r"^province\s+of\s+", re.IGNORECASE)


def normalise_place(name: str) -> str:
    """Return the lookup key for a place name.

    Case and internal whitespace are normalised, and a leading ``Province of`` is dropped —
    the sheets write ``Province of Dinagat Islands`` where the PSGC says ``Dinagat Islands``.
    Nothing else is stripped: ``San Jose`` and ``San Jose de Buenavista`` are different
    municipalities and a more aggressive normaliser would eventually merge two such names.
    """
    collapsed = " ".join(name.split())
    return _PROVINCE_PREFIX.sub("", collapsed).casefold()


@dataclass(frozen=True, slots=True)
class SeedRegion:
    """One row of the committed region list."""

    psgc_code: str
    name: str
    level: str


@dataclass(frozen=True, slots=True)
class SeedSource:
    """One row of the committed source list."""

    slug: str
    name: str
    base_url: str
    licence: str | None
    attribution_text: str


class RegionResolver:
    """Maps a province name as written on a sheet onto a PSGC code."""

    def __init__(self, regions: Iterable[SeedRegion]) -> None:
        self._regions = tuple(regions)
        self._by_key = {
            normalise_place(region.name): region
            for region in self._regions
            if region.level == "province"
        }

    def __len__(self) -> int:
        return len(self._by_key)

    @property
    def regions(self) -> tuple[SeedRegion, ...]:
        """Every seeded region, including the non-province rows the resolver ignores."""
        return self._regions

    def resolve(self, province: str) -> SeedRegion | None:
        """Return the region ``province`` names, or ``None`` if it is not seeded.

        ``None`` is an ordinary outcome the caller quarantines, not an error. A sheet from a
        region nobody has added yet is data we cannot place, and saying so is the honest
        response.
        """
        found = self._by_key.get(normalise_place(province))
        if found is None:
            logger.info("province_unmapped", province=province)
        return found

    @classmethod
    def from_seed(cls) -> "RegionResolver":
        """Build a resolver from the region list shipped with the package."""
        return cls(load_regions())


def _read_rows(filename: str) -> list[dict[str, str]]:
    """Read a committed CSV through ``importlib.resources``.

    By resource rather than by path, so this keeps working from an installed wheel and not
    only from a checkout — the same reason :func:`presyowatch.commodities.load_seed` does it.
    """
    data = resources.files(_DATA_PACKAGE)
    with (
        resources.as_file(data / filename) as path,
        path.open(encoding="utf-8", newline="") as handle,
    ):
        return list(csv.DictReader(handle))


def load_regions() -> list[SeedRegion]:
    """Read the committed region list."""
    return [
        SeedRegion(
            psgc_code=record["psgc_code"],
            name=record["name"],
            level=record["level"],
        )
        for record in _read_rows(REGIONS_FILE)
    ]


def load_sources() -> list[SeedSource]:
    """Read the committed source list."""
    return [
        SeedSource(
            slug=record["slug"],
            name=record["name"],
            base_url=record["base_url"],
            licence=record["licence"] or None,
            attribution_text=record["attribution_text"],
        )
        for record in _read_rows(SOURCES_FILE)
    ]
