"""Fixtures for the tests that need a real PostgreSQL.

Everything here is skipped unless ``PRESYOWATCH_TEST_DATABASE_URL`` points at a database
this suite may freely create and drop tables in. Phase 2 wires that into CI against a Neon
branch; until then it is run locally against a throwaway server::

    uv run --with pgserver python scripts/with_temp_postgres.py pytest tests/db -q

The skip lives on the ``engine`` fixture rather than on each module's ``pytestmark`` so a
test module cannot forget it — asking for a database is what makes a test need one.

**The schema is built by running the migration, never by ``metadata.create_all``.** That
distinction matters more than it looks: ``create_all`` builds from the models, so these
tests would pass against a schema no deployment has, and a migration that produced
something subtly different would go unnoticed. Migrating means every assertion below is
made against the schema that actually ships.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from presyowatch.db.models import Commodity, Market, Region, Source

DATABASE_URL = os.environ.get("PRESYOWATCH_TEST_DATABASE_URL")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """A migrated, empty database, built once for the whole session."""
    if not DATABASE_URL:
        pytest.skip("set PRESYOWATCH_TEST_DATABASE_URL to run tests against a real Postgres")

    built = create_engine(DATABASE_URL, future=True)

    # Start from genuinely nothing, whatever a previous run left behind. The skip above is
    # the contract that this database may be wiped.
    with built.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    # migrations/env.py takes the target from DATABASE_URL, deliberately not from the ini.
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = DATABASE_URL
    try:
        command.upgrade(config, "head")
        yield built
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        built.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session inside an outer transaction that is always rolled back.

    ``join_transaction_mode="create_savepoint"`` lets a test call ``session.commit()`` — the
    idempotency test needs a rerun to see committed state, not its own uncommitted work —
    while the enclosing transaction is discarded at teardown, so tests stay independent and
    the migrated schema is never rebuilt between them.
    """
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as opened:
            yield opened
    finally:
        transaction.rollback()
        connection.close()


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
