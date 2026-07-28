"""Engine and session construction.

Neon's free tier suspends compute when idle and resumes on the next query, so the first
connection after a quiet period is slow and can fail outright. ``pool_pre_ping`` catches
connections the server has already dropped, and the pool is kept small because a serverless
Postgres charges for connections, not for keeping them warm.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_POOL_SIZE = 5


def create_db_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Return an engine for ``database_url``.

    Args:
        database_url: A ``postgresql+psycopg://`` URL, as validated by
            :class:`presyowatch.config.Settings`.
        echo: Log every statement. Useful locally, far too noisy in production.
    """
    return create_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=DEFAULT_POOL_SIZE,
        max_overflow=0,
        future=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to ``engine``."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Run a unit of work in a transaction, committing on success.

    Rolls back and re-raises on any exception. Each source's ingestion runs in its own
    transaction so that one broken source cannot poison another's writes
    (PLANNING.md § "Fail loudly, degrade gracefully").
    """
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
