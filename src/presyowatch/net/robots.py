"""robots.txt fetching, parsing, caching, and enforcement.

CLAUDE.md rule 2: check ``robots.txt`` per host before automating any fetch, and obey
it. Different hosts, different rules. Never route around a disallow.

Two decisions here are load-bearing.

**Why not `urllib.robotparser`.** The standard library matches a rule only by literal
prefix: ``RuleLine.applies_to`` reduces to ``path.startswith(rule)``. It understands
neither ``*`` inside a pattern nor a ``$`` end-anchor. ``www.da.gov.ph`` publishes::

    Disallow: /*.pdf$

Verified 2026-07-28. Against that real file, the stdlib parser returns *allowed* for
``/wp-content/uploads/2026/07/Daily-Price-Index-July-24-2026.pdf`` — it fails **open**
on the exact rule this project must respect most. ``protego`` implements the RFC 9309
matching rules, including wildcards and longest-match precedence, and returns
*disallowed*. A parser that errs toward permission is not usable here.

**Why unreachable means "disallow everything".** RFC 9309 sections 2.3.1.3 and 2.3.1.4
draw a line the intuition gets backwards:

- ``4xx`` — robots.txt is *unavailable*: there are no rules, so everything is allowed.
- ``5xx``, ``429``, timeouts, connection failures — robots.txt is *unreachable*: the
  rules exist but we could not read them, so a crawler must assume complete disallow.

So a flaky server stops ingestion rather than silently licensing us to ignore rules we
simply failed to read.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from protego import Protego

from presyowatch.log import get_logger
from presyowatch.net.errors import InvalidUrlError, RobotsDisallowedError

logger = get_logger(__name__)

RobotsStatus = Literal["parsed", "unavailable", "unreachable"]
"""Outcome of trying to read an origin's robots.txt.

``parsed``
    Rules were retrieved and understood.
``unavailable``
    The server said there are no rules (``4xx``). Everything is permitted.
``unreachable``
    The rules could not be read (``5xx``, ``429``, transport failure). Nothing is
    permitted.
"""

_HTTP_STATUS_RATE_LIMITED = 429
_HTTP_STATUS_CLIENT_ERROR = 400
_HTTP_STATUS_SERVER_ERROR = 500
_DEFAULT_TTL = timedelta(hours=24)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def origin_of(url: str) -> str:
    """Return the scheme-and-authority prefix of ``url``.

    robots.txt is scoped to an origin, so ``https://caraga.da.gov.ph`` and
    ``https://www.da.gov.ph`` are separate policies even though both are ``da.gov.ph``.

    Raises:
        InvalidUrlError: If ``url`` is not an absolute http(s) URL.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise InvalidUrlError(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), "", "", ""))


def robots_url_for(url: str) -> str:
    """Return the robots.txt URL governing ``url``."""
    return f"{origin_of(url)}/robots.txt"


@dataclass(frozen=True, slots=True)
class RobotsFetchOutcome:
    """What the transport saw when asking for a robots.txt.

    Attributes:
        status_code: The HTTP status, or ``None`` if the request never completed
            (DNS failure, connection reset, timeout).
        body: The response text, when there was one.
    """

    status_code: int | None
    body: str | None = None


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    """A parsed robots.txt for one origin, with the time it was read.

    ``fetched_at`` is what populates ``sources.robots_checked_at`` — the schema asks us
    to record not just the verdict but when we last earned the right to it.
    """

    origin: str
    status: RobotsStatus
    fetched_at: datetime
    rules: Protego | None = None

    def can_fetch(self, url: str, user_agent: str) -> bool:
        """Return whether ``user_agent`` may fetch ``url`` under this policy."""
        if self.status == "unavailable":
            return True
        if self.status == "unreachable" or self.rules is None:
            # `rules is None` with status "parsed" should be unreachable; if it ever
            # happens, refusing is the only safe reading.
            return False
        return bool(self.rules.can_fetch(url, user_agent))

    def crawl_delay(self, user_agent: str) -> float | None:
        """Return the origin's requested seconds between requests, if it states one."""
        if self.rules is None:
            return None
        delay = self.rules.crawl_delay(user_agent)
        return None if delay is None else float(delay)


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    """The verdict for one URL, and the evidence behind it."""

    url: str
    origin: str
    allowed: bool
    status: RobotsStatus
    checked_at: datetime
    crawl_delay: float | None = None

    @property
    def reason(self) -> str:
        """A short explanation suitable for a log line or an error message."""
        match self.status:
            case "unavailable":
                return "no robots.txt published (4xx); nothing is restricted"
            case "unreachable":
                return "robots.txt could not be read; assuming complete disallow per RFC 9309"
            case "parsed":
                return "allowed by robots.txt" if self.allowed else "matched a Disallow rule"


class RobotsGate:
    """Answers "may we fetch this?" for one user agent, caching per origin.

    The gate never touches the network itself. It is handed a ``fetch`` callable, which
    keeps it a pure decision component — testable against real robots.txt files with no
    transport involved — and lets the caller apply its own rate limiting and retries to
    the robots.txt request like any other.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        fetch: Callable[[str], RobotsFetchOutcome],
        ttl: timedelta = _DEFAULT_TTL,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialise the gate.

        Args:
            user_agent: The agent string that rules are matched against.
            fetch: Retrieves a robots.txt. Must not itself consult a ``RobotsGate``.
            ttl: How long a policy stays fresh. RFC 9309 suggests 24 hours.
            now: Wall clock, injectable for tests.
        """
        self._user_agent = user_agent
        self._fetch = fetch
        self._ttl = ttl
        self._now = now or _utc_now
        self._policies: dict[str, RobotsPolicy] = {}

    def policy_for(self, url: str) -> RobotsPolicy:
        """Return the cached policy for ``url``'s origin, fetching if stale or absent."""
        origin = origin_of(url)
        cached = self._policies.get(origin)
        if cached is not None and self._now() - cached.fetched_at < self._ttl:
            return cached

        policy = self._load(origin)
        self._policies[origin] = policy
        return policy

    def check(self, url: str) -> RobotsDecision:
        """Evaluate ``url`` without raising."""
        policy = self.policy_for(url)
        return RobotsDecision(
            url=url,
            origin=policy.origin,
            allowed=policy.can_fetch(url, self._user_agent),
            status=policy.status,
            checked_at=policy.fetched_at,
            crawl_delay=policy.crawl_delay(self._user_agent),
        )

    def require_allowed(self, url: str) -> RobotsDecision:
        """Evaluate ``url`` and refuse if it is disallowed.

        Returns:
            The decision, so the caller can honour any ``Crawl-delay``.

        Raises:
            RobotsDisallowedError: If robots.txt forbids the fetch, or could not be
                read at all.
        """
        decision = self.check(url)
        if not decision.allowed:
            logger.warning(
                "robots_disallowed",
                url=url,
                origin=decision.origin,
                robots_status=decision.status,
            )
            raise RobotsDisallowedError(url, origin=decision.origin, reason=decision.reason)
        return decision

    def _load(self, origin: str) -> RobotsPolicy:
        robots_url = f"{origin}/robots.txt"
        outcome = self._fetch(robots_url)
        status = _classify(outcome)
        rules: Protego | None = None
        if status == "parsed":
            if outcome.body is None:
                # A 2xx carrying no body at all is not something a real server does.
                # Refusing is the only safe reading of an impossible state; treating it
                # as "no rules published" would fail open.
                status = "unreachable"
            else:
                # An empty body parses to zero rules, which legitimately permits
                # everything. That is a parsed policy, not a missing one.
                rules = Protego.parse(outcome.body)

        logger.info(
            "robots_fetched",
            origin=origin,
            robots_status=status,
            status_code=outcome.status_code,
        )
        return RobotsPolicy(
            origin=origin,
            status=status,
            fetched_at=self._now(),
            rules=rules,
        )


def _classify(outcome: RobotsFetchOutcome) -> RobotsStatus:
    """Map a fetch outcome onto RFC 9309 § 2.3.1 semantics."""
    code = outcome.status_code
    if code is None:
        return "unreachable"
    if code == _HTTP_STATUS_RATE_LIMITED or code >= _HTTP_STATUS_SERVER_ERROR:
        return "unreachable"
    if code >= _HTTP_STATUS_CLIENT_ERROR:
        return "unavailable"
    return "parsed"
