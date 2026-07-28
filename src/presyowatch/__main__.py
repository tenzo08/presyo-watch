"""Command line entry point: ``python -m presyowatch``.

Two subcommands, because a deployment needs exactly two things done and neither should be
implicit:

``seed``
    Load the committed reference data. Idempotent, so a deploy can run it every time.
``backfill``
    Reconcile a source's lookback window.

Phase 2's scheduled workflow calls ``backfill``; until then it is how a run is made by hand.
The exit code is what a scheduler reads, so it distinguishes the outcomes: ``0`` for a clean
run, ``1`` for a failure, and ``2`` for a partial one — some files landed, some were
quarantined — because "mostly worked" is neither success nor failure and a cron job that
treated it as either would be lying.
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta

from presyowatch.cache import RawCache
from presyowatch.config import Settings
from presyowatch.db.engine import create_db_engine, create_session_factory, session_scope
from presyowatch.db.seed import seed_reference_data
from presyowatch.ingest import DEFAULT_LOOKBACK, run_backfill
from presyowatch.log import configure_logging, get_logger
from presyowatch.net.client import HttpClient
from presyowatch.sources.index import CARAGA

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 2

SPECS = {CARAGA.slug: CARAGA}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="presyowatch", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("seed", help="load the committed reference data")

    backfill = commands.add_parser("backfill", help="reconcile a source's lookback window")
    backfill.add_argument("--source", default=CARAGA.slug, choices=sorted(SPECS))
    backfill.add_argument(
        "--lookback-days",
        type=int,
        default=int(DEFAULT_LOOKBACK.days),
        help="how many days back to reconcile (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    configure_logging()
    settings = Settings()  # type: ignore[call-arg]  # every field comes from the environment
    engine = create_db_engine(settings.database_url)
    factory = create_session_factory(engine)

    try:
        if arguments.command == "seed":
            with session_scope(factory) as session:
                counts = seed_reference_data(session)
            logger.info("seed_complete", changed=counts.changed)
            return EXIT_OK

        with HttpClient(user_agent=settings.http_user_agent) as client:
            outcome = run_backfill(
                session_factory=factory,
                client=client,
                cache=RawCache(settings.raw_cache_dir),
                spec=SPECS[arguments.source],
                today=datetime.now(UTC).date(),
                lookback=timedelta(days=arguments.lookback_days),
            )
    finally:
        engine.dispose()

    if outcome.status == "failed":
        return EXIT_FAILED
    return EXIT_PARTIAL if outcome.status == "partial" else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
