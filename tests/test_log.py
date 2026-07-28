"""Tests for structured logging.

Observability is treated as a feature here (PLANNING.md § Design principles), so the
log format is asserted rather than assumed. If ``run_id`` silently stopped being
emitted, reconstructing a single ingestion run from a day of output would quietly
become impossible.
"""

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from presyowatch.log import (
    bind_run_id,
    clear_run_context,
    configure_logging,
    get_logger,
    new_run_id,
)


@pytest.fixture(autouse=True)
def _isolate_structlog() -> Iterator[None]:
    """Keep process-wide logging configuration from leaking between tests."""
    clear_run_context()
    structlog.reset_defaults()
    yield
    clear_run_context()
    structlog.reset_defaults()


def emitted(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    out = capsys.readouterr().out.strip()
    return [json.loads(line) for line in out.splitlines() if line]


def test_run_id_is_attached_to_every_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    bind_run_id("run-abc123")
    log = get_logger("test")

    log.info("ingestion_started", source="caraga")
    log.info("file_cached", sha256="deadbeef")

    lines = emitted(capsys)
    assert [line["run_id"] for line in lines] == ["run-abc123", "run-abc123"]


def test_output_is_one_json_object_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    bind_run_id("run-1")

    get_logger("test").warning("robots_disallowed", url="https://www.da.gov.ph/x.pdf")

    (line,) = emitted(capsys)
    assert line["event"] == "robots_disallowed"
    assert line["level"] == "warning"
    assert line["url"] == "https://www.da.gov.ph/x.pdf"
    assert "timestamp" in line


def test_timestamps_are_utc_iso8601(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()

    get_logger("test").info("tick")

    (line,) = emitted(capsys)
    assert isinstance(line["timestamp"], str)
    assert line["timestamp"].endswith("Z")


def test_level_filtering_suppresses_quieter_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level=logging.WARNING)
    log = get_logger("test")

    log.info("not_emitted")
    log.warning("emitted")

    assert [line["event"] for line in emitted(capsys)] == ["emitted"]


def test_clear_run_context_detaches_the_run_id(capsys: pytest.CaptureFixture[str]) -> None:
    """A long-lived process must not stamp one run's id onto the next run's lines."""
    configure_logging()
    bind_run_id("run-1")
    log = get_logger("test")

    log.info("first")
    clear_run_context()
    log.info("second")

    first, second = emitted(capsys)
    assert first["run_id"] == "run-1"
    assert "run_id" not in second


def test_console_renderer_is_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    """The local-development path should stay human-readable."""
    configure_logging(json_output=False)

    get_logger("test").info("hello", key="value")

    out = capsys.readouterr().out
    assert "hello" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip())


def test_run_ids_are_distinct() -> None:
    assert len({new_run_id() for _ in range(200)}) == 200


def test_run_id_is_short_enough_to_read_in_a_log_line() -> None:
    run_id = new_run_id()

    assert len(run_id) == 16
    assert run_id.isalnum()
