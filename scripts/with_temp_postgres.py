"""Run a command against a throwaway PostgreSQL server.

Neither Neon nor Docker is needed to work on the schema: ``pgserver`` ships real Postgres
binaries as a wheel, so a disposable server can be started in-process on Linux, macOS, and
Windows alike.

``DATABASE_URL`` and ``PRESYOWATCH_TEST_DATABASE_URL`` are both exported into the child, so
Alembic and the gated schema tests each find what they look for.

Usage::

    uv run --with pgserver python scripts/with_temp_postgres.py pytest tests/db -q
    uv run --with pgserver python scripts/with_temp_postgres.py alembic upgrade head

Several commands can share one server by separating them with ``--``, which is how the
migration is verified end to end — ``check`` is only meaningful once ``upgrade`` has run::

    uv run --with pgserver python scripts/with_temp_postgres.py \
        alembic upgrade head -- alembic check -- alembic downgrade base

Commands run in order and stop at the first failure.

The data directory is deleted at the start of each *invocation*, so every run begins from an
empty database and nothing leaks between runs. Not a project dependency — it is deliberately
invoked through ``--with`` so that a 12 MB Postgres build is not imposed on anyone who does
not need it.

This file is not covered by ``mypy --strict``: ``pgserver`` ships no type information, and
``disallow_any_unimported`` would reject it. It is developer tooling, not shipped code.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pgserver

DATA_DIR = Path(__file__).resolve().parent.parent / ".pgdata"


def split_commands(argv: list[str]) -> list[list[str]]:
    """Split ``argv`` on ``--`` into the commands to run in sequence."""
    commands: list[list[str]] = [[]]
    for argument in argv:
        if argument == "--":
            commands.append([])
        else:
            commands[-1].append(argument)
    return [command for command in commands if command]


def main(argv: list[str]) -> int:
    commands = split_commands(argv)
    if not commands:
        print(__doc__)
        return 2

    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    server = pgserver.get_server(str(DATA_DIR))
    url = server.get_uri().replace("postgresql://", "postgresql+psycopg://")
    print(f"temporary postgres: {url}", flush=True)

    env = {
        **os.environ,
        "DATABASE_URL": url,
        "PRESYOWATCH_TEST_DATABASE_URL": url,
    }
    for command in commands:
        print(f"\n$ {' '.join(command)}", flush=True)
        # `python -m` so the command resolves inside this interpreter's environment
        # rather than depending on what happens to be on PATH.
        code = subprocess.call([sys.executable, "-m", *command], env=env)
        if code != 0:
            print(f"\nfailed: {' '.join(command)} (exit {code})", flush=True)
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
