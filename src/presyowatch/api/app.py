"""The FastAPI application and its wiring.

**One engine per process, created at startup and disposed at shutdown.** Render's free tier
stops and starts the service, and Neon's free tier suspends its compute when idle, so
connections are both precious and prone to having been dropped underneath us. The engine
built by :func:`presyowatch.db.engine.create_db_engine` already sets ``pool_pre_ping`` for
exactly that; what this module adds is making sure there is only ever one of it.

**Everything is injected through dependencies rather than reached for.** The session, the
settings and the engine all arrive as parameters, so the tests drive the real application
over the real routes against a real Postgres, with nothing patched.

**There is deliberately no module-level ``app``.** Importing this module must not require a
database URL, or the linter, the tests and ``--help`` would all fail on a missing
environment variable. Deployments name the factory instead::

    uvicorn "presyowatch.api.app:create_app" --factory --host 0.0.0.0 --port $PORT
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from presyowatch import __version__
from presyowatch.api.deps import get_session
from presyowatch.api.routes import router
from presyowatch.api.schemas import Health
from presyowatch.config import Settings
from presyowatch.db.engine import create_db_engine, create_session_factory
from presyowatch.log import get_logger

logger = get_logger(__name__)

DESCRIPTION = """
Daily retail prices for agricultural and fishery commodities in the Philippines, ingested
from the Department of Agriculture's regional *Bantay Presyo* monitoring sheets.

**Prices are strings, not numbers.** They are exact decimals, and JSON numbers are not:
`52.80` becomes `52.79999999999999716` in anything that parses them as floats.

**Gaps are gaps.** A missing price is `null` and is never interpolated. `unavailable: true`
means the source listed the commodity and published no figures for it, which is different
from there being no row at all.

Data is republished under RA 8293 § 176 with attribution; see `/meta/sources`.
"""


def create_app(
    *,
    settings: Settings | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    """Build the application.

    Args:
        settings: Read from the environment if omitted.
        engine: An engine to use instead of building one. The tests pass the same engine
            their fixtures migrated, so the API is exercised against the schema that ships.
    """
    # Settings are only consulted when an engine has to be built. An injected engine already
    # names its database, and demanding a DATABASE_URL anyway would make the tests depend on
    # an environment variable that has nothing to do with what they are testing.
    owns_engine = engine is None
    if engine is None:
        resolved = settings or Settings()  # type: ignore[call-arg]  # fields come from the env
        engine = create_db_engine(resolved.database_url)
    built = engine
    factory = create_session_factory(built)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = built
        app.state.session_factory = factory
        logger.info("api_started", version=__version__)
        yield
        # Only dispose what this app created. An injected engine belongs to its owner, and
        # disposing it would close a test's connection out from under the next test.
        if owns_engine:
            built.dispose()
        logger.info("api_stopped")

    app = FastAPI(
        title="PresyoWatch",
        version=__version__,
        description=DESCRIPTION,
        summary="Philippine commodity price time series.",
        license_info={"name": "MIT", "identifier": "MIT"},
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.engine = built
    app.state.session_factory = factory
    app.include_router(router)

    @app.get("/health", response_model=Health, tags=["meta"])
    def health(session: Annotated[Session, Depends(get_session)]) -> Health:
        """Liveness, and whether the database will actually answer.

        Reported as ``degraded`` rather than as a 5xx when the database is unreachable: the
        process is up and able to say so, and a monitor learns more from a body that names
        the failing dependency than from a connection error.
        """
        try:
            session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 — any driver failure means the same thing here
            logger.warning("health_database_unreachable", detail=str(exc))
            return Health(status="degraded", database="unreachable", version=__version__)
        return Health(status="ok", database="ok", version=__version__)

    return app
