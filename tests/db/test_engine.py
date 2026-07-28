"""Tests for engine and session construction.

``create_db_engine`` is lazy — SQLAlchemy does not connect until a connection is asked for
— so its configuration can be asserted without a database. ``session_scope``'s transaction
semantics are exercised against in-memory SQLite: the unit under test is our wrapper's
commit, rollback, and close behaviour, which is dialect-independent.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import (
    Column,
    Engine,
    Integer,
    MetaData,
    Table,
    create_engine,
    select,
    text,
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from presyowatch.db.engine import (
    DEFAULT_POOL_SIZE,
    create_db_engine,
    create_session_factory,
    session_scope,
)

POSTGRES_URL = "postgresql+psycopg://user:pass@host.neon.tech/db?sslmode=require"

_metadata = MetaData()
WIDGETS = Table("widgets", _metadata, Column("id", Integer, primary_key=True))


def queue_pool(engine: Engine) -> QueuePool:
    """Narrow ``engine.pool`` to the concrete type that exposes sizing."""
    pool = engine.pool
    assert isinstance(pool, QueuePool)
    return pool


def test_engine_uses_the_given_url() -> None:
    engine = create_db_engine(POSTGRES_URL)

    assert engine.url.drivername == "postgresql+psycopg"
    assert engine.url.host == "host.neon.tech"


def test_engine_does_not_connect_on_construction() -> None:
    """A bad host must not blow up at import time, only when a query is attempted."""
    engine = create_db_engine("postgresql+psycopg://u:p@nonexistent.invalid/db")

    assert queue_pool(engine).checkedout() == 0


def test_pre_ping_is_enabled_for_a_database_that_suspends() -> None:
    """Neon's free tier drops idle connections; without pre-ping the first query fails.

    Reaches for a private attribute because it is the only exposure SQLAlchemy offers. If a
    future version renames it, this fails loudly and gets updated — which is preferable to
    not checking a setting the deployment depends on.
    """
    assert queue_pool(create_db_engine(POSTGRES_URL))._pre_ping is True


def test_pool_is_small() -> None:
    """Serverless Postgres charges for connections, not for keeping them warm."""
    assert queue_pool(create_db_engine(POSTGRES_URL)).size() == DEFAULT_POOL_SIZE


def test_echo_is_off_by_default_and_can_be_turned_on() -> None:
    assert create_db_engine(POSTGRES_URL).echo is False
    assert create_db_engine(POSTGRES_URL, echo=True).echo is True


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite://", future=True)
    _metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def test_session_factory_is_bound_to_its_engine(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        assert session.execute(select(text("1"))).scalar_one() == 1


def test_session_scope_commits_on_success(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        session.execute(WIDGETS.insert().values(id=1))

    with factory() as check:
        assert check.execute(select(WIDGETS.c.id)).scalars().all() == [1]


def _insert_then_fail(factory: sessionmaker[Session], message: str) -> None:
    """Write, then raise, inside one scope — the shape of a mid-ingest parser failure."""
    with session_scope(factory) as session:
        session.execute(WIDGETS.insert().values(id=1))
        raise RuntimeError(message)


def test_session_scope_rolls_back_on_failure(factory: sessionmaker[Session]) -> None:
    """One source's failure must not leave a half-written transaction behind.

    PLANNING.md § "Fail loudly, degrade gracefully": each source runs in its own
    transaction so a broken one cannot poison another's writes.
    """
    message = "parser exploded"

    with pytest.raises(RuntimeError, match=message):
        _insert_then_fail(factory, message)

    with factory() as check:
        assert check.execute(select(WIDGETS.c.id)).scalars().all() == []


def test_session_scope_reraises_rather_than_swallowing(factory: sessionmaker[Session]) -> None:
    """Rolling back quietly and returning would hide the failure from the run record."""
    message = "boom"

    with pytest.raises(RuntimeError, match=message):
        _insert_then_fail(factory, message)


def test_session_scope_closes_the_session(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        opened = session

    assert opened.get_transaction() is None


def test_session_scope_closes_the_session_after_a_failure(
    factory: sessionmaker[Session],
) -> None:
    opened: list[Session] = []

    def capture() -> None:
        with session_scope(factory) as session:
            opened.append(session)
            message = "boom"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        capture()

    assert len(opened) == 1
    assert opened[0].get_transaction() is None
