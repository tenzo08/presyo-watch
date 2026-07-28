"""Tests for `fetch_once` — the point where the cache meets the network.

The property under test throughout: the network is touched exactly once per URL, ever.
Asserted by counting what the transport actually saw, not by trusting a return flag.
"""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from presyowatch.cache import CachingFetcher, RawCache
from presyowatch.net.client import HttpClient, HttpConfig
from presyowatch.net.errors import HttpRequestError, RobotsDisallowedError
from tests.conftest import TEST_USER_AGENT

PDF_BYTES = b"%PDF-1.4\r\nprices\x00\n%%EOF\n"
CARAGA_PDF = "https://caraga.da.gov.ph/wp-content/uploads/PriceMonitoring/Luha.pdf"
DA_PDF = "https://www.da.gov.ph/wp-content/uploads/2026/07/Daily-Price-Index-July-24-2026.pdf"
ALLOW_ALL = "User-agent: *\nAllow: /\n"


class Recorder:
    """Serves robots.txt and a fixed body, counting every request."""

    def __init__(self, *, robots: str = ALLOW_ALL, body: bytes = PDF_BYTES) -> None:
        self.robots = robots
        self.body = body
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        return httpx.Response(200, content=self.body, headers={"Content-Type": "application/pdf"})

    def content_requests(self) -> list[str]:
        return [u for u in self.urls if not u.endswith("/robots.txt")]


def build(tmp_path: Path, recorder: Recorder) -> tuple[CachingFetcher, RawCache]:
    cache = RawCache(tmp_path / "raw")
    client = HttpClient(
        user_agent=TEST_USER_AGENT,
        config=HttpConfig(min_interval_per_host=0.0),
        transport=httpx.MockTransport(recorder),
    )
    return CachingFetcher(client=client, cache=cache), cache


def test_first_call_fetches_and_caches(tmp_path: Path) -> None:
    recorder = Recorder()
    fetcher, cache = build(tmp_path, recorder)

    result = fetcher.fetch_once(CARAGA_PDF)

    assert result.from_cache is False
    assert result.read_bytes() == PDF_BYTES
    assert result.entry.content_type == "application/pdf"
    assert result.entry.http_status == 200
    assert cache.has(CARAGA_PDF) is True
    assert recorder.content_requests() == [CARAGA_PDF]


def test_second_call_makes_no_request_at_all(tmp_path: Path) -> None:
    """Not even a robots.txt request: reading our own disk is not a fetch."""
    recorder = Recorder()
    fetcher, _ = build(tmp_path, recorder)

    fetcher.fetch_once(CARAGA_PDF)
    requests_after_first = len(recorder.urls)
    result = fetcher.fetch_once(CARAGA_PDF)

    assert result.from_cache is True
    assert result.read_bytes() == PDF_BYTES
    assert len(recorder.urls) == requests_after_first


def test_repeated_calls_never_refetch(tmp_path: Path) -> None:
    recorder = Recorder()
    fetcher, _ = build(tmp_path, recorder)

    for _ in range(10):
        fetcher.fetch_once(CARAGA_PDF)

    assert recorder.content_requests() == [CARAGA_PDF]


def test_a_new_process_still_does_not_refetch(tmp_path: Path) -> None:
    """The reason the cache is on disk: yesterday's run counts as "already seen"."""
    first_recorder = Recorder()
    first_fetcher, _ = build(tmp_path, first_recorder)
    first_fetcher.fetch_once(CARAGA_PDF)

    second_recorder = Recorder()
    second_fetcher, _ = build(tmp_path, second_recorder)
    result = second_fetcher.fetch_once(CARAGA_PDF)

    assert result.from_cache is True
    assert second_recorder.urls == []


def test_url_variants_resolve_to_one_fetch(tmp_path: Path) -> None:
    recorder = Recorder()
    fetcher, _ = build(tmp_path, recorder)

    fetcher.fetch_once(CARAGA_PDF)
    fetcher.fetch_once(CARAGA_PDF.replace("caraga", "CARAGA"))
    fetcher.fetch_once(CARAGA_PDF + "#page=2")

    assert recorder.content_requests() == [CARAGA_PDF]


def test_robots_disallowed_url_is_not_fetched_or_cached(
    tmp_path: Path, robots_text: Callable[[str], str]
) -> None:
    """A disallowed URL must leave no trace: no request, no blob, no metadata.

    Runs against the real `www.da.gov.ph` rules, so this is the actual `Disallow: /*.pdf$`
    being enforced end to end through the cache layer.
    """
    recorder = Recorder(robots=robots_text("www.da.gov.ph"))
    fetcher, cache = build(tmp_path, recorder)

    with pytest.raises(RobotsDisallowedError):
        fetcher.fetch_once(DA_PDF)

    assert recorder.content_requests() == []
    assert cache.has(DA_PDF) is False
    assert list(cache.entries()) == []


def test_a_failed_fetch_caches_nothing(tmp_path: Path) -> None:
    """A failed download is not a source file, and must not look like one later."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        return httpx.Response(404, text="gone")

    cache = RawCache(tmp_path / "raw")
    client = HttpClient(
        user_agent=TEST_USER_AGENT,
        config=HttpConfig(min_interval_per_host=0.0),
        transport=httpx.MockTransport(handler),
    )
    fetcher = CachingFetcher(client=client, cache=cache)

    with pytest.raises(HttpRequestError):
        fetcher.fetch_once(CARAGA_PDF)

    assert cache.has(CARAGA_PDF) is False
    assert list(cache.entries()) == []


def test_identical_files_from_two_urls_share_one_blob(tmp_path: Path) -> None:
    recorder = Recorder()
    fetcher, cache = build(tmp_path, recorder)

    first = fetcher.fetch_once("https://caraga.da.gov.ph/a.pdf")
    second = fetcher.fetch_once("https://caraga.da.gov.ph/a-1.pdf")

    assert first.entry.sha256 == second.entry.sha256
    assert len(recorder.content_requests()) == 2, "two distinct URLs are two fetches"
    blobs = [p for p in (cache.root / "blobs").rglob("*") if p.is_file()]
    assert len(blobs) == 1


def test_cached_bytes_are_reparseable_without_the_network(tmp_path: Path) -> None:
    """Rule 3's payoff: a parser fix re-runs over history with the source unavailable."""
    recorder = Recorder()
    fetcher, cache = build(tmp_path, recorder)
    for n in range(3):
        fetcher.fetch_once(f"https://caraga.da.gov.ph/{n}.pdf")

    offline_cache = RawCache(cache.root)
    reparsed = [offline_cache.read_bytes(entry) for entry in offline_cache.entries()]

    assert len(reparsed) == 3
    assert all(body == PDF_BYTES for body in reparsed)
    assert offline_cache.verify() == []
