"""Request-scoped dependencies.

One function, in its own module, because both the routes and the application need it and
having the routes import from the app would be a cycle.

The session factory is read off ``app.state`` rather than captured at import time, so the
application can be built more than once in one process — which is exactly what the tests do,
and what a factory-style ASGI entry point does.
"""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker


def get_session(request: Request) -> Iterator[Session]:
    """Yield a session for one request, always closed.

    Read-only by convention: nothing in the API commits. The API serves what the ingester
    wrote, and a GET that writes is a surprise nobody needs.
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()
