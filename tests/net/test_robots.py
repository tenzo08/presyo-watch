"""Tests for robots.txt parsing, caching, and RFC 9309 status semantics.

The allow/deny cases run against real captured robots.txt files, not invented ones, so
that a change in what these hosts publish shows up as a test failure rather than as a
quiet violation in production.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from presyowatch.net.errors import InvalidUrlError, RobotsDisallowedError
from presyowatch.net.robots import (
    RobotsFetchOutcome,
    RobotsGate,
    RobotsStatus,
    origin_of,
    robots_url_for,
)
from tests.conftest import TEST_USER_AGENT

DA_PDF = "https://www.da.gov.ph/wp-content/uploads/2026/07/Daily-Price-Index-July-24-2026.pdf"
DA_INDEX = "https://www.da.gov.ph/price-monitoring/"
CARAGA_PDF = (
    "https://caraga.da.gov.ph/wp-content/uploads/PriceMonitoring/FY2025/"
    "Tandag/April/Luha_April-2.pdf"
)


def gate_serving(
    body: str | None,
    *,
    status_code: int | None = 200,
    user_agent: str = TEST_USER_AGENT,
    ttl: timedelta = timedelta(hours=24),
    now: Callable[[], datetime] | None = None,
    calls: list[str] | None = None,
) -> RobotsGate:
    """Build a gate whose fetch returns a fixed outcome, recording each call."""

    def fetch(url: str) -> RobotsFetchOutcome:
        if calls is not None:
            calls.append(url)
        return RobotsFetchOutcome(status_code=status_code, body=body)

    return RobotsGate(user_agent=user_agent, fetch=fetch, ttl=ttl, now=now)


# -- origin handling -------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.da.gov.ph/a/b.pdf?x=1#frag", "https://www.da.gov.ph"),
        ("https://WWW.DA.GOV.PH/a", "https://www.da.gov.ph"),
        ("http://caraga.da.gov.ph", "http://caraga.da.gov.ph"),
        ("https://example.ph:8443/x", "https://example.ph:8443"),
    ],
)
def test_origin_of_strips_path_and_normalises_case(url: str, expected: str) -> None:
    assert origin_of(url) == expected


@pytest.mark.parametrize("url", ["/relative/path.pdf", "ftp://da.gov.ph/x", "not a url", ""])
def test_origin_of_rejects_non_absolute_http_urls(url: str) -> None:
    with pytest.raises(InvalidUrlError):
        origin_of(url)


def test_robots_url_is_origin_scoped() -> None:
    """Subdomains are separate policies: caraga's rules never speak for www's."""
    assert robots_url_for(CARAGA_PDF) == "https://caraga.da.gov.ph/robots.txt"
    assert robots_url_for(DA_PDF) == "https://www.da.gov.ph/robots.txt"


# -- real robots.txt files -------------------------------------------------------


def test_da_national_pdfs_are_disallowed(robots_text: Callable[[str], str]) -> None:
    """The load-bearing case: `Disallow: /*.pdf$` must actually block a PDF.

    A prefix-only parser reads that rule as permitting this URL. If this test ever
    passes with `allowed=True`, the parser has regressed to failing open and Phase 1
    would start scraping files the host has asked us not to touch.
    """
    gate = gate_serving(robots_text("www.da.gov.ph"))

    decision = gate.check(DA_PDF)

    assert decision.allowed is False
    assert decision.status == "parsed"
    assert "Disallow" in decision.reason


def test_da_national_html_index_is_allowed(robots_text: Callable[[str], str]) -> None:
    """Only the PDFs are off limits; the index page is fair game (KNOWLEDGE.md)."""
    gate = gate_serving(robots_text("www.da.gov.ph"))

    assert gate.check(DA_INDEX).allowed is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.da.gov.ph/wp-admin/",
        "https://www.da.gov.ph/author/someone/",
        "https://www.da.gov.ph/deeply/nested/report.pdf",
    ],
)
def test_da_national_other_disallows(url: str, robots_text: Callable[[str], str]) -> None:
    gate = gate_serving(robots_text("www.da.gov.ph"))

    assert gate.check(url).allowed is False


def test_caraga_pdfs_are_allowed(robots_text: Callable[[str], str]) -> None:
    """Caraga only protects /wp-admin/, which is why it is the automatable source."""
    gate = gate_serving(robots_text("caraga.da.gov.ph"))

    assert gate.check(CARAGA_PDF).allowed is True
    assert gate.check("https://caraga.da.gov.ph/wp-admin/").allowed is False


def test_openstat_allows_our_agent_but_blocks_named_ai_crawlers(
    robots_text: Callable[[str], str],
) -> None:
    """PSA's Cloudflare block disallows a list of AI crawlers by name.

    PresyoWatch is not one of them and matches `User-agent: *` with `Allow: /`. This
    test pins that distinction so the difference is a deliberate, documented reading of
    the file rather than an accident.
    """
    api = "https://openstat.psa.gov.ph/PXWeb/api/v1/en/DB/"
    body = robots_text("openstat.psa.gov.ph")

    assert gate_serving(body).check(api).allowed is True
    assert gate_serving(body, user_agent="ClaudeBot").check(api).allowed is False
    assert gate_serving(body, user_agent="GPTBot").check(api).allowed is False


def test_crlf_line_endings_parse(robots_text: Callable[[str], str]) -> None:
    """www.da.gov.ph serves CRLF. Confirm the capture kept them and parsing copes."""
    body = robots_text("www.da.gov.ph")

    assert "\r\n" in body
    assert gate_serving(body).check(DA_PDF).allowed is False


# -- RFC 9309 status semantics ---------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_allowed"),
    [
        (200, "parsed", False),
        (404, "unavailable", True),
        (401, "unavailable", True),
        (403, "unavailable", True),
        (429, "unreachable", False),
        (500, "unreachable", False),
        (503, "unreachable", False),
        (None, "unreachable", False),
    ],
)
def test_status_code_maps_to_rfc_9309_semantics(
    status_code: int | None,
    expected_status: RobotsStatus,
    expected_allowed: bool,
    robots_text: Callable[[str], str],
) -> None:
    """4xx means "no rules, go ahead"; 5xx/429/transport failure means "stop".

    The asymmetry is easy to get backwards, and getting it backwards means a flaky
    server silently licenses us to ignore rules we merely failed to read.
    """
    body = robots_text("www.da.gov.ph") if status_code == 200 else None
    gate = gate_serving(body, status_code=status_code)

    decision = gate.check(DA_PDF)

    assert decision.status == expected_status
    assert decision.allowed is expected_allowed


def test_empty_robots_txt_allows_everything() -> None:
    """A published-but-empty robots.txt parses to zero rules, permitting everything."""
    gate = gate_serving("", status_code=200)

    decision = gate.check(DA_PDF)

    assert decision.status == "parsed"
    assert decision.allowed is True


def test_success_status_with_no_body_at_all_fails_closed() -> None:
    """A 2xx with a `None` body is an impossible state, so refuse rather than allow.

    Guards the tempting shortcut of treating a bodyless success as "no rules
    published", which would hand out permission on the strength of a bug.
    """
    gate = gate_serving(None, status_code=200)

    decision = gate.check(DA_PDF)

    assert decision.status == "unreachable"
    assert decision.allowed is False


@pytest.mark.parametrize(
    ("status_code", "expected_phrase"),
    [
        (404, "nothing is restricted"),
        (503, "RFC 9309"),
        (None, "RFC 9309"),
    ],
)
def test_reason_explains_the_verdict(status_code: int | None, expected_phrase: str) -> None:
    """The reason string ends up in error messages and on the data quality page."""
    gate = gate_serving(None, status_code=status_code)

    assert expected_phrase in gate.check(DA_PDF).reason


def test_reason_distinguishes_allowed_from_disallowed(
    robots_text: Callable[[str], str],
) -> None:
    gate = gate_serving(robots_text("www.da.gov.ph"))

    assert gate.check(DA_INDEX).reason == "allowed by robots.txt"
    assert gate.check(DA_PDF).reason == "matched a Disallow rule"


def test_unreachable_robots_blocks_even_an_innocuous_url() -> None:
    """Unreachable is a *complete* disallow, not a per-path one."""
    gate = gate_serving(None, status_code=503)

    assert gate.check("https://caraga.da.gov.ph/anything").allowed is False


# -- caching ---------------------------------------------------------------------


def test_policy_is_fetched_once_per_origin(robots_text: Callable[[str], str]) -> None:
    calls: list[str] = []
    gate = gate_serving(robots_text("www.da.gov.ph"), calls=calls)

    for _ in range(5):
        gate.check(DA_PDF)
        gate.check(DA_INDEX)

    assert calls == ["https://www.da.gov.ph/robots.txt"]


def test_distinct_origins_are_fetched_separately(robots_text: Callable[[str], str]) -> None:
    calls: list[str] = []
    gate = gate_serving(robots_text("caraga.da.gov.ph"), calls=calls)

    gate.check(CARAGA_PDF)
    gate.check("https://cagayanvalley.da.gov.ph/x.pdf")

    assert calls == [
        "https://caraga.da.gov.ph/robots.txt",
        "https://cagayanvalley.da.gov.ph/robots.txt",
    ]


def test_policy_is_refetched_once_the_ttl_expires(robots_text: Callable[[str], str]) -> None:
    calls: list[str] = []
    current = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    gate = gate_serving(
        robots_text("www.da.gov.ph"),
        ttl=timedelta(hours=24),
        now=lambda: current,
        calls=calls,
    )

    gate.check(DA_PDF)
    current += timedelta(hours=23, minutes=59)
    gate.check(DA_PDF)
    assert len(calls) == 1, "policy re-read before its TTL elapsed"

    current += timedelta(minutes=2)
    gate.check(DA_PDF)
    assert len(calls) == 2


def test_checked_at_records_when_the_policy_was_read(robots_text: Callable[[str], str]) -> None:
    """`sources.robots_checked_at` needs this, and it must be timezone-aware."""
    read_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    gate = gate_serving(robots_text("caraga.da.gov.ph"), now=lambda: read_at)

    decision = gate.check(CARAGA_PDF)

    assert decision.checked_at == read_at
    assert decision.checked_at.tzinfo is not None


# -- enforcement -----------------------------------------------------------------


def test_require_allowed_raises_with_the_origin_and_reason(
    robots_text: Callable[[str], str],
) -> None:
    gate = gate_serving(robots_text("www.da.gov.ph"))

    with pytest.raises(RobotsDisallowedError) as caught:
        gate.require_allowed(DA_PDF)

    assert caught.value.origin == "https://www.da.gov.ph"
    assert caught.value.url == DA_PDF
    assert "www.da.gov.ph" in str(caught.value)


def test_require_allowed_returns_the_decision_when_permitted(
    robots_text: Callable[[str], str],
) -> None:
    gate = gate_serving(robots_text("caraga.da.gov.ph"))

    assert gate.require_allowed(CARAGA_PDF).allowed is True


def test_crawl_delay_is_surfaced_when_stated() -> None:
    gate = gate_serving("User-agent: *\nCrawl-delay: 7\nDisallow: /private/\n")

    assert gate.check("https://example.ph/ok").crawl_delay == pytest.approx(7.0)


def test_crawl_delay_is_none_when_unstated(robots_text: Callable[[str], str]) -> None:
    gate = gate_serving(robots_text("caraga.da.gov.ph"))

    assert gate.check(CARAGA_PDF).crawl_delay is None
