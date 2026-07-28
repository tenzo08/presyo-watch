"""The only sanctioned way for this project to make an outbound request.

Three of the project's rules are structural here rather than advisory:

- **Rule 2** — every ``get()`` passes through :class:`~presyowatch.net.robots.RobotsGate`
  first. The underlying ``httpx.Client`` is private and the robots-exempt path is private
  and used solely to fetch ``/robots.txt`` itself, which cannot be robots-checked without
  infinite regress. There is no public method that skips the gate.
- **Rule 7** — the User-Agent is required at construction, and at most one request per
  second per host is issued, counting robots.txt requests against the same budget.
- **Fail loudly** — an exhausted retry budget raises. Nothing returns a sentinel that a
  caller might mistake for an empty result.
"""

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from types import TracebackType
from urllib.parse import urlsplit

import httpx

from presyowatch.log import get_logger
from presyowatch.net.errors import HttpRequestError
from presyowatch.net.robots import RobotsDecision, RobotsFetchOutcome, RobotsGate

logger = get_logger(__name__)

_HTTP_STATUS_RATE_LIMITED = 429
_HTTP_STATUS_SERVER_ERROR = 500
_HTTP_STATUS_CLIENT_ERROR = 400
_NOTEWORTHY_CRAWL_DELAY_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class HttpConfig:
    """Transport tunables.

    These live here rather than in :class:`presyowatch.config.Settings` because none of
    them differ between deployments; they are engineering defaults, not configuration.

    Attributes:
        timeout: Per-request timeout in seconds. Always explicit — httpx's default is
            to wait 5 seconds, and a government CMS under load can exceed that.
        min_interval_per_host: Floor on the gap between requests to one host.
        max_attempts: Total tries per URL, including the first.
        backoff_base: First retry waits up to this many seconds.
        backoff_max: Ceiling on computed backoff.
        retry_after_max: Ceiling on an honoured ``Retry-After`` header, so a stray
            header value cannot stall a run indefinitely.
        robots_ttl: How long a robots.txt policy is trusted before re-reading.
    """

    timeout: float = 30.0
    min_interval_per_host: float = 1.0
    max_attempts: int = 4
    backoff_base: float = 0.5
    backoff_max: float = 30.0
    retry_after_max: float = 120.0
    robots_ttl: timedelta = field(default_factory=lambda: timedelta(hours=24))

    def __post_init__(self) -> None:
        """Reject nonsense up front rather than behaving strangely later.

        Raises:
            ValueError: If any tunable is outside its usable range.
        """
        if self.max_attempts < 1:
            msg = f"max_attempts must be at least 1, got {self.max_attempts}"
            raise ValueError(msg)
        if self.timeout <= 0:
            msg = f"timeout must be positive, got {self.timeout}"
            raise ValueError(msg)
        if self.min_interval_per_host < 0:
            msg = f"min_interval_per_host must not be negative, got {self.min_interval_per_host}"
            raise ValueError(msg)


class HttpClient:
    """A polite, robots-respecting, rate-limited HTTP client.

    Synchronous by design. The one-request-per-second-per-host floor dominates any
    gain from concurrency within a host, and a single-threaded client makes the rate
    limiter and the retry loop straightforward to reason about and to test. If
    cross-host parallelism is ever needed, run one client per source process.

    Example:
        >>> with HttpClient(user_agent="PresyoWatch/0.1 (contact: a@b.ph)") as client:
        ...     response = client.get("https://caraga.da.gov.ph/robots.txt")
    """

    def __init__(
        self,
        *,
        user_agent: str,
        config: HttpConfig | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        """Initialise the client.

        Args:
            user_agent: Sent on every request. Must be non-empty;
                :class:`presyowatch.config.Settings` is what enforces that it also
                carries a contact email.
            config: Transport tunables.
            transport: Injectable transport. Tests pass an ``httpx.MockTransport``.
            sleep: Injectable sleep, so rate-limit and backoff behaviour can be
                asserted without real delays.
            monotonic: Injectable monotonic clock.

        Raises:
            ValueError: If ``user_agent`` is empty.
        """
        if not user_agent.strip():
            msg = "user_agent must not be empty (CLAUDE.md rule 7)"
            raise ValueError(msg)

        self._config = config or HttpConfig()
        self._user_agent = user_agent
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._last_request_at: dict[str, float] = {}

        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(self._config.timeout),
            follow_redirects=True,
            transport=transport,
        )
        self._robots = RobotsGate(
            user_agent=user_agent,
            fetch=self._fetch_robots,
            ttl=self._config.robots_ttl,
        )

    # -- public API ---------------------------------------------------------------

    def get(self, url: str) -> httpx.Response:
        """Fetch ``url`` after confirming robots.txt permits it.

        Raises:
            RobotsDisallowedError: If robots.txt forbids the fetch or is unreachable.
            HttpRequestError: If the request fails and retrying does not help.
        """
        decision = self._robots.require_allowed(url)
        return self._request(url, min_interval=self._interval_for(decision))

    def robots_decision(self, url: str) -> RobotsDecision:
        """Return the robots.txt verdict for ``url`` without fetching it.

        Useful for recording ``sources.robots_checked_at`` and for reporting on the data
        quality page why a source was skipped.
        """
        return self._robots.check(url)

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- internals ---------------------------------------------------------------

    def _interval_for(self, decision: RobotsDecision) -> float:
        """Return the gap to leave before this request.

        A ``Crawl-delay`` longer than our own floor is honoured in full and never
        capped: it is the origin telling us its limit, and quietly ignoring it would be
        routing around robots.txt by another name. It is logged when unusually large so
        that an unworkably slow source is visible rather than mysterious.
        """
        floor = self._config.min_interval_per_host
        requested = decision.crawl_delay
        if requested is None or requested <= floor:
            return floor
        if requested >= _NOTEWORTHY_CRAWL_DELAY_SECONDS:
            logger.warning(
                "large_crawl_delay_requested",
                origin=decision.origin,
                crawl_delay_seconds=requested,
            )
        return requested

    def _fetch_robots(self, robots_url: str) -> RobotsFetchOutcome:
        """Fetch a robots.txt, translating every failure into an outcome.

        The only request path that is not robots-checked, because checking it would
        require the file it is fetching. It is still rate limited, still carries the
        User-Agent, and still retries.
        """
        try:
            response = self._request(
                robots_url,
                min_interval=self._config.min_interval_per_host,
                allow_client_errors=True,
            )
        except HttpRequestError as exc:
            # Distinguishing "server said 5xx" from "connection died" is not useful
            # here: RFC 9309 treats both as unreachable, i.e. disallow everything.
            logger.warning("robots_fetch_failed", url=robots_url, detail=str(exc))
            return RobotsFetchOutcome(status_code=exc.status_code, body=None)
        return RobotsFetchOutcome(status_code=response.status_code, body=response.text)

    def _request(
        self,
        url: str,
        *,
        min_interval: float,
        allow_client_errors: bool = False,
    ) -> httpx.Response:
        """Issue a rate-limited GET with retries. Performs no robots check.

        Args:
            url: Absolute URL to fetch.
            min_interval: Seconds to leave since the last request to this host.
            allow_client_errors: Return ``4xx`` responses instead of raising. Used for
                robots.txt, where a ``404`` is a meaningful answer rather than a
                failure.

        Raises:
            HttpRequestError: If every attempt failed.
        """
        attempts = self._config.max_attempts
        for attempt in range(1, attempts + 1):
            self._throttle(url, min_interval)
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                detail = f"{type(exc).__name__}: {exc}"
                if attempt == attempts:
                    raise HttpRequestError(url, attempts=attempts, detail=detail) from exc
                logger.info("http_retry", url=url, attempt=attempt, detail=detail)
                self._back_off(attempt, retry_after=None)
                continue

            status = response.status_code
            if status < _HTTP_STATUS_CLIENT_ERROR:
                return response

            retryable = status == _HTTP_STATUS_RATE_LIMITED or status >= _HTTP_STATUS_SERVER_ERROR
            if not retryable:
                if allow_client_errors:
                    return response
                raise HttpRequestError(url, attempts=attempt, status_code=status)
            if attempt == attempts:
                raise HttpRequestError(url, attempts=attempts, status_code=status)

            logger.info("http_retry", url=url, attempt=attempt, status_code=status)
            self._back_off(attempt, retry_after=_retry_after_seconds(response))

        # Unreachable: HttpConfig guarantees max_attempts >= 1, so the loop above always
        # returns or raises. Present because the function must not fall off the end.
        raise HttpRequestError(  # pragma: no cover
            url, attempts=attempts, detail="no attempts were made"
        )

    def _throttle(self, url: str, min_interval: float) -> None:
        """Block until at least ``min_interval`` has passed since this host was hit."""
        host = urlsplit(url).netloc.lower()
        last = self._last_request_at.get(host)
        if last is not None:
            remaining = min_interval - (self._monotonic() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at[host] = self._monotonic()

    def _back_off(self, attempt: int, *, retry_after: float | None) -> None:
        """Wait before the next attempt.

        Full jitter — a uniform draw from ``[0, computed_delay]`` rather than the delay
        itself — so that several sources retrying after a shared outage do not
        resynchronise into a thundering herd. An explicit ``Retry-After`` wins, since
        the server has told us what it wants.
        """
        if retry_after is not None:
            self._sleep(min(retry_after, self._config.retry_after_max))
            return
        ceiling = min(
            self._config.backoff_base * (2 ** (attempt - 1)),
            self._config.backoff_max,
        )
        self._sleep(random.uniform(0, ceiling))  # noqa: S311 — jitter, not cryptography


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header expressed in seconds.

    The HTTP-date form is permitted by the spec and ignored here: it is rare in
    practice, and falling back to jittered backoff is a safe response to a header we do
    not understand.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None
