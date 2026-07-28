"""Tests for the permanent raw file cache.

The bytes used here are deliberately hostile to text handling — a real PDF header, CRLF,
and embedded NULs — because a cache that quietly mangles binary content would corrupt
every downstream hash and only be noticed much later.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from presyowatch.cache import (
    CacheConflictError,
    CacheEntry,
    RawCache,
    normalise_url,
)
from presyowatch.net.errors import InvalidUrlError

PDF_BYTES = b"%PDF-1.4\r\n1 0 obj\x00\x00trailer\r\n%%EOF\n"
OTHER_BYTES = b"%PDF-1.4\r\nrevised figures\x00\n%%EOF\n"
URL = "https://caraga.da.gov.ph/wp-content/uploads/PriceMonitoring/Luha_April-2.pdf"


@pytest.fixture
def cache(tmp_path: Path) -> RawCache:
    return RawCache(tmp_path / "raw")


# -- storing and reading ---------------------------------------------------------


def test_store_then_lookup_round_trips_bytes_exactly(cache: RawCache) -> None:
    entry = cache.store(URL, PDF_BYTES, content_type="application/pdf", http_status=200)

    assert entry.sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
    assert entry.size == len(PDF_BYTES)
    assert entry.content_type == "application/pdf"
    assert entry.http_status == 200
    assert cache.read_bytes(entry) == PDF_BYTES


def test_blob_path_is_sharded_two_levels_deep(cache: RawCache) -> None:
    """A flat directory of years of daily PDFs from every region is a bad idea."""
    entry = cache.store(URL, PDF_BYTES)
    path = cache.blob_path(entry.sha256)

    assert path.name == entry.sha256
    assert path.parent.name == entry.sha256[2:4]
    assert path.parent.parent.name == entry.sha256[:2]
    assert path.is_file()


def test_lookup_returns_none_for_an_unseen_url(cache: RawCache) -> None:
    assert cache.lookup(URL) is None
    assert cache.has(URL) is False


def test_has_reports_a_stored_url(cache: RawCache) -> None:
    cache.store(URL, PDF_BYTES)

    assert cache.has(URL) is True


def test_empty_body_is_stored_faithfully(cache: RawCache) -> None:
    """A zero-byte response is what the source served; the parser can quarantine it."""
    entry = cache.store(URL, b"")

    assert entry.size == 0
    assert cache.read_bytes(entry) == b""


def test_fetched_at_is_timezone_aware(cache: RawCache) -> None:
    entry = cache.store(URL, PDF_BYTES)

    assert entry.fetched_at.tzinfo is not None


def test_explicit_fetched_at_is_preserved(cache: RawCache) -> None:
    when = datetime(2026, 7, 28, 6, 19, 1, tzinfo=UTC)

    entry = cache.store(URL, PDF_BYTES, fetched_at=when)

    assert entry.fetched_at == when


# -- content addressing ----------------------------------------------------------


def test_identical_content_at_two_urls_is_stored_once(cache: RawCache) -> None:
    """The DA's `-1` dedup suffixes really do republish identical bytes."""
    first = cache.store("https://caraga.da.gov.ph/a.pdf", PDF_BYTES)
    second = cache.store("https://caraga.da.gov.ph/a-1.pdf", PDF_BYTES)

    assert first.sha256 == second.sha256
    assert cache.blob_path(first.sha256) == cache.blob_path(second.sha256)
    blobs = [p for p in (cache.root / "blobs").rglob("*") if p.is_file()]
    assert len(blobs) == 1


def test_different_content_gets_separate_blobs(cache: RawCache) -> None:
    cache.store("https://caraga.da.gov.ph/a.pdf", PDF_BYTES)
    cache.store("https://caraga.da.gov.ph/Revised-a.pdf", OTHER_BYTES)

    blobs = [p for p in (cache.root / "blobs").rglob("*") if p.is_file()]
    assert len(blobs) == 2


# -- idempotency and integrity ---------------------------------------------------


def test_storing_the_same_url_and_content_twice_is_idempotent(cache: RawCache) -> None:
    first = cache.store(URL, PDF_BYTES, fetched_at=datetime(2026, 7, 1, tzinfo=UTC))
    second = cache.store(URL, PDF_BYTES, fetched_at=datetime(2026, 7, 28, tzinfo=UTC))

    assert second == first
    assert second.fetched_at == datetime(2026, 7, 1, tzinfo=UTC), (
        "the original fetch time is the historical fact and must not be overwritten"
    )


def test_same_url_with_different_bytes_refuses_to_overwrite(cache: RawCache) -> None:
    """Neither keeping nor replacing silently is acceptable, so stop loudly.

    Corrections arrive as new `Revised-` URLs. A source rewriting a file in place is
    something a human needs to look at, not something to paper over.
    """
    cache.store(URL, PDF_BYTES)

    with pytest.raises(CacheConflictError) as caught:
        cache.store(URL, OTHER_BYTES)

    assert caught.value.existing_sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
    assert caught.value.incoming_sha256 == hashlib.sha256(OTHER_BYTES).hexdigest()
    assert cache.read_bytes(cache.lookup(URL) or pytest.fail("entry vanished")) == PDF_BYTES


def test_no_temporary_files_are_left_behind(cache: RawCache) -> None:
    cache.store(URL, PDF_BYTES)

    leftovers = [p.name for p in cache.root.rglob(".tmp-*")]
    assert leftovers == []


def test_verify_passes_on_a_healthy_store(cache: RawCache) -> None:
    cache.store("https://caraga.da.gov.ph/a.pdf", PDF_BYTES)
    cache.store("https://caraga.da.gov.ph/b.pdf", OTHER_BYTES)

    assert cache.verify() == []


def test_verify_detects_a_corrupted_blob(cache: RawCache) -> None:
    """Bit rot found now beats a mysterious parser error in three years."""
    entry = cache.store(URL, PDF_BYTES)
    cache.blob_path(entry.sha256).write_bytes(b"corrupted")

    problems = cache.verify()

    assert len(problems) == 1
    assert problems[0].url == normalise_url(URL)
    assert "hashes to" in problems[0].reason


def test_verify_detects_a_missing_blob(cache: RawCache) -> None:
    entry = cache.store(URL, PDF_BYTES)
    cache.blob_path(entry.sha256).unlink()

    problems = cache.verify()

    assert len(problems) == 1
    assert problems[0].reason == "blob missing"


# -- persistence and enumeration -------------------------------------------------


def test_a_new_cache_instance_sees_previously_stored_files(tmp_path: Path) -> None:
    """Permanence is the entire feature: process restarts must not lose history."""
    root = tmp_path / "raw"
    RawCache(root).store(URL, PDF_BYTES)

    reopened = RawCache(root)

    entry = reopened.lookup(URL)
    assert entry is not None
    assert reopened.read_bytes(entry) == PDF_BYTES


def test_entries_yields_everything_for_a_reparse_run(cache: RawCache) -> None:
    urls = [f"https://caraga.da.gov.ph/{n}.pdf" for n in range(5)]
    for n, url in enumerate(urls):
        cache.store(url, f"file {n}".encode())

    found = {entry.url for entry in cache.entries()}

    assert found == set(urls)


def test_entries_is_empty_for_a_fresh_cache(cache: RawCache) -> None:
    assert list(cache.entries()) == []


def test_metadata_survives_as_readable_json(cache: RawCache) -> None:
    """The store should be inspectable with ordinary tools, not just by this code."""
    cache.store(URL, PDF_BYTES, content_type="application/pdf")

    files = list((cache.root / "meta").glob("*.json"))
    assert len(files) == 1
    restored = CacheEntry.model_validate_json(files[0].read_text(encoding="utf-8"))
    assert restored.url == normalise_url(URL)


# -- URL handling ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://CARAGA.da.gov.ph/a.pdf", "https://caraga.da.gov.ph/a.pdf"),
        ("HTTPS://caraga.da.gov.ph/a.pdf", "https://caraga.da.gov.ph/a.pdf"),
        ("https://caraga.da.gov.ph/a.pdf#page=2", "https://caraga.da.gov.ph/a.pdf"),
        ("https://caraga.da.gov.ph/a.pdf?v=1", "https://caraga.da.gov.ph/a.pdf?v=1"),
    ],
)
def test_normalise_url_canonicalises_case_and_drops_fragments(raw: str, expected: str) -> None:
    assert normalise_url(raw) == expected


def test_path_case_is_preserved(cache: RawCache) -> None:
    """Hosts may well serve different files at paths differing only in case."""
    upper = "https://caraga.da.gov.ph/Daily-Price-Index.pdf"
    lower = "https://caraga.da.gov.ph/daily-price-index.pdf"

    cache.store(upper, PDF_BYTES)

    assert cache.has(upper) is True
    assert cache.has(lower) is False


def test_host_case_does_not_create_a_duplicate_entry(cache: RawCache) -> None:
    cache.store("https://caraga.da.gov.ph/a.pdf", PDF_BYTES)

    assert cache.has("https://CARAGA.DA.GOV.PH/a.pdf") is True


@pytest.mark.parametrize("bad", ["/relative.pdf", "ftp://caraga.da.gov.ph/a.pdf", "", "nope"])
def test_non_absolute_urls_are_refused(bad: str, cache: RawCache) -> None:
    with pytest.raises(InvalidUrlError):
        cache.store(bad, PDF_BYTES)


@pytest.mark.parametrize(
    "hostile",
    [
        "https://caraga.da.gov.ph/../../../etc/passwd",
        "https://caraga.da.gov.ph/a%00.pdf",
        "https://caraga.da.gov.ph/CON.pdf",
        "https://caraga.da.gov.ph/a:b|c*.pdf",
    ],
)
def test_hostile_urls_cannot_escape_the_cache_directory(hostile: str, cache: RawCache) -> None:
    """URLs are source-controlled, so none of one may become a path component.

    Covers traversal, a NUL, a reserved Windows device name, and characters illegal in
    Windows filenames. All are keyed by hash, so all are harmless.
    """
    entry = cache.store(hostile, PDF_BYTES)

    for path in (cache.blob_path(entry.sha256), *(cache.root / "meta").glob("*.json")):
        assert cache.root.resolve() in path.resolve().parents
