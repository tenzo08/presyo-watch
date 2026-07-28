"""Tests for the HTTP client: robots enforcement, rate limiting, retries, backoff.

Uses ``httpx.MockTransport``, so a real ``httpx.Client`` and the real retry and
throttling code run — only the socket is replaced. Waiting is asserted through an
injected clock rather than actually waited on.
"""

from collections.abc import Callable

import httpx
import pytest

from presyowatch.net.client import HttpClient, HttpConfig
from presyowatch.net.errors import HttpRequestError, RobotsDisallowedError
from tests.conftest import TEST_USER_AGENT, FakeClock

DA_PDF = "https://www.da.gov.ph/wp-content/uploads/2026/07/Daily-Price-Index-July-24-2026.pdf"
CARAGA_PDF = "https://caraga.da.gov.ph/wp-content/uploads/PriceMonitoring/x.pdf"

Handler = Callable[[httpx.Request], httpx.Response]


class RecordingTransport:
    """A MockTransport wrapper that remembers every request it served."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def build_client(
    handler: Handler,
    *,
    clock: FakeClock | None = None,
    config: HttpConfig | None = None,
) -> tuple[HttpClient, RecordingTransport]:
    recorder = RecordingTransport(handler)
    the_clock = clock or FakeClock()
    client = HttpClient(
        user_agent=TEST_USER_AGENT,
        config=config,
        transport=recorder.transport(),
        sleep=the_clock.sleep,
        monotonic=the_clock.monotonic,
    )
    return client, recorder


def serve(robots: str, *, robots_status: int = 200, body: str = "payload") -> Handler:
    """Serve a robots.txt for any origin, and ``body`` for everything else."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status, text=robots)
        return httpx.Response(200, text=body)

    return handler


ALLOW_ALL = "User-agent: *\nAllow: /\n"


# -- robots enforcement cannot be bypassed ---------------------------------------


def test_disallowed_url_is_never_requested(robots_text: Callable[[str], str]) -> None:
    """The whole point of the design: refusal happens *before* any content request.

    Asserting the exception is not enough — a client that fetched the PDF and then
    complained would satisfy that. This asserts the socket never saw the URL.
    """
    client, recorder = build_client(serve(robots_text("www.da.gov.ph")))

    with client, pytest.raises(RobotsDisallowedError):
        client.get(DA_PDF)

    assert recorder.urls == ["https://www.da.gov.ph/robots.txt"]
    assert DA_PDF not in recorder.urls


def test_allowed_url_is_fetched_after_the_robots_check(
    robots_text: Callable[[str], str],
) -> None:
    client, recorder = build_client(serve(robots_text("caraga.da.gov.ph")))

    with client:
        response = client.get(CARAGA_PDF)

    assert response.status_code == 200
    assert response.text == "payload"
    assert recorder.urls == ["https://caraga.da.gov.ph/robots.txt", CARAGA_PDF]


def test_unreachable_robots_refuses_the_fetch() -> None:
    """A 5xx on robots.txt must stop ingestion, not wave it through."""
    client, recorder = build_client(
        serve("", robots_status=503),
        config=HttpConfig(max_attempts=1, min_interval_per_host=0.0),
    )

    with client, pytest.raises(RobotsDisallowedError, match="RFC 9309"):
        client.get(CARAGA_PDF)

    assert CARAGA_PDF not in recorder.urls


def test_missing_robots_permits_the_fetch() -> None:
    """404 means no rules were published, so there is nothing to obey."""
    client, recorder = build_client(serve("", robots_status=404))

    with client:
        assert client.get(CARAGA_PDF).status_code == 200

    assert CARAGA_PDF in recorder.urls


def test_robots_is_read_once_across_many_fetches() -> None:
    client, recorder = build_client(serve(ALLOW_ALL))

    with client:
        for n in range(3):
            client.get(f"https://caraga.da.gov.ph/{n}.pdf")

    assert recorder.urls.count("https://caraga.da.gov.ph/robots.txt") == 1


def test_robots_decision_does_not_fetch_the_url() -> None:
    """Exposed for `sources.robots_checked_at`; must stay side-effect free."""
    client, recorder = build_client(serve(ALLOW_ALL))

    with client:
        decision = client.robots_decision(CARAGA_PDF)

    assert decision.allowed is True
    assert recorder.urls == ["https://caraga.da.gov.ph/robots.txt"]


# -- identification (rule 7) -----------------------------------------------------


def test_user_agent_is_sent_on_every_request_including_robots() -> None:
    client, recorder = build_client(serve(ALLOW_ALL))

    with client:
        client.get(CARAGA_PDF)

    assert len(recorder.requests) == 2
    assert all(r.headers["User-Agent"] == TEST_USER_AGENT for r in recorder.requests)


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_empty_user_agent_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="rule 7"):
        HttpClient(user_agent=bad)


# -- rate limiting ---------------------------------------------------------------


def test_requests_to_one_host_are_spaced_by_at_least_a_second(clock: FakeClock) -> None:
    client, _ = build_client(serve(ALLOW_ALL), clock=clock)

    with client:
        client.get("https://caraga.da.gov.ph/a.pdf")
        client.get("https://caraga.da.gov.ph/b.pdf")

    # robots.txt, a.pdf, b.pdf — three requests, so two gaps of one second.
    assert clock.sleeps == [1.0, 1.0]


def test_robots_request_counts_against_the_host_budget(clock: FakeClock) -> None:
    """Politeness is per host, not per purpose."""
    client, _ = build_client(serve(ALLOW_ALL), clock=clock)

    with client:
        client.get(CARAGA_PDF)

    assert clock.sleeps == [1.0]


def test_separate_hosts_do_not_throttle_each_other(clock: FakeClock) -> None:
    client, _ = build_client(serve(ALLOW_ALL), clock=clock)

    with client:
        client.get("https://caraga.da.gov.ph/a.pdf")
        client.get("https://cagayanvalley.da.gov.ph/b.pdf")

    # Each host: one robots fetch then one content fetch, one gap apiece.
    assert clock.sleeps == [1.0, 1.0]


def test_crawl_delay_longer_than_our_floor_is_honoured(clock: FakeClock) -> None:
    """The origin's stated limit wins over our own; ignoring it would route around it."""
    client, _ = build_client(
        serve("User-agent: *\nCrawl-delay: 5\nAllow: /\n"),
        clock=clock,
    )

    with client:
        client.get(CARAGA_PDF)

    assert clock.sleeps == [5.0]


def test_unusually_large_crawl_delay_is_still_honoured(clock: FakeClock) -> None:
    """Never silently capped: quietly ignoring Crawl-delay is routing around robots.txt.

    It is logged as noteworthy so an unworkably slow source is visible rather than
    mysterious, and the ingester can decide to drop it.
    """
    client, _ = build_client(
        serve("User-agent: *\nCrawl-delay: 45\nAllow: /\n"),
        clock=clock,
    )

    with client:
        client.get(CARAGA_PDF)

    assert clock.sleeps == [45.0]


def test_crawl_delay_shorter_than_our_floor_does_not_speed_us_up(clock: FakeClock) -> None:
    client, _ = build_client(
        serve("User-agent: *\nCrawl-delay: 0.2\nAllow: /\n"),
        clock=clock,
    )

    with client:
        client.get(CARAGA_PDF)

    assert clock.sleeps == [1.0]


# -- configuration validation ----------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": -1},
        {"timeout": 0.0},
        {"timeout": -5.0},
        {"min_interval_per_host": -1.0},
    ],
)
def test_nonsense_config_is_refused(kwargs: dict[str, float]) -> None:
    """Caught at construction, so no request is ever made under a broken setting."""
    with pytest.raises(ValueError, match="must"):
        HttpConfig(**kwargs)  # type: ignore[arg-type]


# -- retries and backoff ---------------------------------------------------------

NO_THROTTLE = HttpConfig(min_interval_per_host=0.0)


def flaky(statuses: list[int]) -> Handler:
    """Serve robots, then walk through ``statuses`` for content requests."""
    remaining = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        return httpx.Response(remaining.pop(0) if remaining else 200, text="payload")

    return handler


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_retryable_statuses_are_retried_then_succeed(status: int, clock: FakeClock) -> None:
    client, recorder = build_client(flaky([status]), clock=clock, config=NO_THROTTLE)

    with client:
        assert client.get(CARAGA_PDF).status_code == 200

    assert recorder.urls.count(CARAGA_PDF) == 2


def test_retries_are_exhausted_and_then_raise(clock: FakeClock) -> None:
    client, recorder = build_client(
        flaky([503, 503, 503, 503]),
        clock=clock,
        config=HttpConfig(min_interval_per_host=0.0, max_attempts=4),
    )

    with client, pytest.raises(HttpRequestError) as caught:
        client.get(CARAGA_PDF)

    assert caught.value.status_code == 503
    assert caught.value.attempts == 4
    assert recorder.urls.count(CARAGA_PDF) == 4


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_client_errors_are_not_retried(status: int, clock: FakeClock) -> None:
    """A 404 will not become a 200 by asking again; retrying is just rudeness."""
    client, recorder = build_client(flaky([status]), clock=clock, config=NO_THROTTLE)

    with client, pytest.raises(HttpRequestError) as caught:
        client.get(CARAGA_PDF)

    assert caught.value.status_code == status
    assert recorder.urls.count(CARAGA_PDF) == 1


def test_transport_errors_are_retried_then_reported(clock: FakeClock) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        attempts["n"] += 1
        msg = "connection reset"
        raise httpx.ConnectError(msg, request=request)

    client, _ = build_client(
        handler, clock=clock, config=HttpConfig(min_interval_per_host=0.0, max_attempts=3)
    )

    with client, pytest.raises(HttpRequestError) as caught:
        client.get(CARAGA_PDF)

    assert attempts["n"] == 3
    assert caught.value.status_code is None
    assert "ConnectError" in str(caught.value)


def test_retry_after_header_is_honoured_exactly(clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        if len(clock.sleeps) == 0:
            return httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")
        return httpx.Response(200, text="payload")

    client, _ = build_client(handler, clock=clock, config=NO_THROTTLE)

    with client:
        assert client.get(CARAGA_PDF).status_code == 200

    assert clock.sleeps == [7.0]


def test_absurd_retry_after_is_capped(clock: FakeClock) -> None:
    """A server is allowed to ask for a week. A daily job cannot give it one."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        if not clock.sleeps:
            one_week = 7 * 24 * 60 * 60
            return httpx.Response(503, headers={"Retry-After": str(one_week)})
        return httpx.Response(200, text="payload")

    client, _ = build_client(
        handler,
        clock=clock,
        config=HttpConfig(min_interval_per_host=0.0, retry_after_max=120.0),
    )

    with client:
        assert client.get(CARAGA_PDF).status_code == 200

    assert clock.sleeps == [120.0]


@pytest.mark.parametrize("header", ["Wed, 21 Oct 2026 07:28:00 GMT", "not-a-number", "-5"])
def test_unparseable_retry_after_falls_back_to_jittered_backoff(
    header: str, clock: FakeClock
) -> None:
    """The HTTP-date form is legal and unsupported; backoff must still be sane."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        if not clock.sleeps:
            return httpx.Response(503, headers={"Retry-After": header})
        return httpx.Response(200, text="payload")

    client, _ = build_client(
        handler,
        clock=clock,
        config=HttpConfig(min_interval_per_host=0.0, backoff_base=0.5),
    )

    with client:
        assert client.get(CARAGA_PDF).status_code == 200

    assert len(clock.sleeps) == 1
    assert 0.0 <= clock.sleeps[0] <= 0.5


def test_backoff_is_jittered_within_bounds(clock: FakeClock) -> None:
    """Full jitter: the wait is a draw from [0, ceiling], not the ceiling itself.

    Bounds rather than an exact value, because the whole point is that it is random.
    """
    config = HttpConfig(min_interval_per_host=0.0, backoff_base=0.5, max_attempts=4)
    client, _ = build_client(flaky([503, 503, 503]), clock=clock, config=config)

    with client:
        client.get(CARAGA_PDF)

    ceilings = [0.5, 1.0, 2.0]
    assert len(clock.sleeps) == len(ceilings)
    for slept, ceiling in zip(clock.sleeps, ceilings, strict=True):
        assert 0.0 <= slept <= ceiling


def test_backoff_respects_its_ceiling(clock: FakeClock) -> None:
    config = HttpConfig(
        min_interval_per_host=0.0, backoff_base=10.0, backoff_max=15.0, max_attempts=3
    )
    client, _ = build_client(flaky([503, 503]), clock=clock, config=config)

    with client:
        client.get(CARAGA_PDF)

    assert all(slept <= 15.0 for slept in clock.sleeps)
