"""Structured JSON logging.

Every log line carries a ``run_id`` so that one ingestion run can be reconstructed
from a day of output. Bound via ``contextvars``, so it propagates without being
threaded through every function signature.

Named ``log`` rather than ``logging`` to avoid any ambiguity with the standard library.
"""

import logging
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

if TYPE_CHECKING:
    from structlog.typing import Processor


def new_run_id() -> str:
    """Return an identifier for one ingestion run."""
    return uuid.uuid4().hex[:16]


def bind_run_id(run_id: str) -> None:
    """Attach ``run_id`` to every subsequent log line in this context."""
    bind_contextvars(run_id=run_id)


def clear_run_context() -> None:
    """Drop all context-bound values. Call between runs in a long-lived process."""
    clear_contextvars()


def configure_logging(*, level: int = logging.INFO, json_output: bool = True) -> None:
    """Configure structlog process-wide.

    Args:
        level: Minimum level to emit.
        json_output: Emit one JSON object per line. Set ``False`` for a
            human-readable console renderer when developing locally.
    """
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:  # noqa: ANN401
    """Return a bound logger.

    The return type is ``Any`` because structlog's bound loggers are dynamically
    constructed and have no useful static type; annotating it more tightly would be
    a lie that mypy cannot check.
    """
    return structlog.get_logger(name)
