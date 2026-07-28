"""Exceptions raised by the networking layer.

Each class builds its own message so call sites stay short and every failure carries
the same structured detail.
"""


class NetworkError(Exception):
    """Base class for every failure originating in the networking layer."""


class InvalidUrlError(NetworkError):
    """A URL was not absolute, so no origin could be determined for a robots check."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"expected an absolute http(s) URL, got {url!r}")


class RobotsDisallowedError(NetworkError):
    """robots.txt forbids fetching this URL.

    Not retryable, and never to be caught in order to make the request anyway.
    Routing around a disallow is explicitly forbidden (CLAUDE.md rule 2).
    """

    def __init__(self, url: str, *, origin: str, reason: str) -> None:
        self.url = url
        self.origin = origin
        self.reason = reason
        super().__init__(f"robots.txt for {origin} disallows {url} ({reason})")


class HttpRequestError(NetworkError):
    """A request failed and retrying did not help."""

    def __init__(
        self,
        url: str,
        *,
        attempts: int,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        self.url = url
        self.attempts = attempts
        self.status_code = status_code
        self.detail = detail
        what = f"HTTP {status_code}" if status_code is not None else (detail or "transport error")
        plural = "attempt" if attempts == 1 else "attempts"
        super().__init__(f"GET {url} failed after {attempts} {plural}: {what}")
