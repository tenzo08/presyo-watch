"""Fixtures for the schema tests.

The database itself — engine, connection, session factory — lives in
``tests/conftest.py``, because the ingester's tests need it too.
"""

import pytest
from sqlalchemy.orm import Session

from presyowatch.db.models import Commodity, Market, Region, Source


@pytest.fixture
def seeded(session: Session) -> tuple[int, int, int]:
    """Insert the minimum referenced rows and return ``(source_id, market_id, commodity_id)``."""
    region = Region(psgc_code="160000000", name="Caraga", level="region")
    source = Source(
        slug="da-caraga",
        name="DA Regional Field Office XIII (Caraga)",
        base_url="https://caraga.da.gov.ph",
        attribution_text="Department of Agriculture RFO XIII (Caraga)",
    )
    commodity = Commodity(
        canonical_slug="rice-premium",
        group="IMPORTED COMMERCIAL RICE",
        name="Premium",
        specification="5% Broken",
        unit="kg",
    )
    session.add_all([region, source, commodity])
    session.flush()
    market = Market(
        region_psgc_code=region.psgc_code,
        name="Luha Public Market",
        municipality="Tandag City",
    )
    session.add(market)
    session.flush()
    return source.id, market.id, commodity.id
