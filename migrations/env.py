"""Alembic environment.

Reads ``DATABASE_URL`` from the environment rather than from ``alembic.ini`` so that no
connection string is ever committed, and so CI and production use the same code path with
different secrets.
"""

import os
import pathlib
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from presyowatch.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Return the URL to migrate: the real environment first, then ``.env``.

    The environment wins, so a deployment or a CI job that exports ``DATABASE_URL`` is never
    quietly overridden by a stray file. ``.env`` is consulted only as a fallback, because the
    README tells a developer to create one and it was then the one command that ignored it —
    ``alembic upgrade head`` failed with "DATABASE_URL is not set" while the application
    running beside it read the same file happily.

    Raises:
        RuntimeError: If neither supplies it. Failing here is much kinder than connecting to
            whatever a default might have pointed at.
    """
    url = os.environ.get("DATABASE_URL") or _from_dotenv("DATABASE_URL")
    if not url:
        msg = (
            "DATABASE_URL is not set, in the environment or in .env. Alembic takes the "
            "target database from there; see .env.example."
        )
        raise RuntimeError(msg)
    return url


def _from_dotenv(key: str) -> str | None:
    """Read one value from the repository's ``.env``, if there is one.

    A deliberately small reader rather than a dependency: this needs to handle
    ``KEY=value``, optional quotes and comments, and nothing else. ``pydantic-settings``
    does the real job for the application, but importing the app's settings here would make
    a migration depend on the code it is migrating for.
    """
    env_file = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'") or None
    return None


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting.

    Useful for reviewing exactly what a migration will do to production before it does it.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without these, a changed column type or default is silently missed by
            # autogenerate and the models drift away from the schema.
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
