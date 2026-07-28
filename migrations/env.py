"""Alembic environment.

Reads ``DATABASE_URL`` from the environment rather than from ``alembic.ini`` so that no
connection string is ever committed, and so CI and production use the same code path with
different secrets.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from presyowatch.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Return the URL to migrate, from the environment.

    Raises:
        RuntimeError: If ``DATABASE_URL`` is unset. Failing here is much kinder than
            connecting to whatever a default might have pointed at.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        msg = (
            "DATABASE_URL is not set. Alembic takes the target database from the "
            "environment; see .env.example."
        )
        raise RuntimeError(msg)
    return url


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
