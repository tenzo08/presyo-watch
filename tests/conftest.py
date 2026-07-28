"""Shared fixtures.

The robots.txt files under ``fixtures/robots/`` are byte-exact captures taken from the
live hosts on 2026-07-28. They are not hand-written, and they are excluded from the
whitespace-fixing pre-commit hooks and from git's line-ending normalisation so they stay
that way — ``www.da.gov.ph`` really does serve CRLF.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
ROBOTS_FIXTURES = FIXTURES / "robots"

# Shaped like the real thing, and valid per Settings' rule-7 check.
TEST_USER_AGENT = "PresyoWatch/0.1 (+https://example.invalid; contact: tester@example.ph)"


@pytest.fixture
def robots_text() -> Callable[[str], str]:
    """Return a reader for a captured robots.txt, keyed by host."""

    def read(host: str) -> str:
        path = ROBOTS_FIXTURES / f"{host}.robots.txt"
        if not path.is_file():
            pytest.fail(f"missing robots fixture for {host}: {path}")
        # Decoded rather than read as text so the CRLF bytes survive as far as the
        # parser, which is what happens with a real response body.
        return path.read_bytes().decode("utf-8")

    return read


@dataclass
class FakeClock:
    """A monotonic clock that only advances when something sleeps.

    Lets the rate limiter and backoff be asserted exactly — both the fact that a wait
    happened and how long it was — with no real delay and no flakiness.
    """

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
