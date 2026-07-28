"""Permanent, content-addressed cache of raw source files.

CLAUDE.md rule 3: cache every raw source file permanently before parsing. Hash it, store
the bytes, and never re-fetch a file already seen. Parser bugs get fixed by reparsing the
cache, never by re-downloading history from a government server.

**Why two indexes, not one.** Content addressing alone cannot answer the question
``fetch_once`` has to ask. "Have I seen this file?" must be answered *before* fetching,
and a file's SHA-256 is only knowable *after*. So there are two layers:

``blobs/ab/cd/<sha256>``
    The bytes, keyed by their own hash. Identical content fetched from two URLs — which
    the DA's ``-1`` dedup suffixes really do produce — is stored once.
``meta/<sha256-of-url>.json``
    One record per URL, naming the blob it resolved to. Keyed by a hash of the URL so
    that no source-controlled string ever becomes a path component, and so lookup is a
    single stat rather than a scan of a global index.

Both layers are plain files written atomically. There is no global index file to corrupt,
no lock to hold, and the whole store can be rebuilt or inspected with ``ls``. The
metadata records are self-describing — each names its own URL — so the URL index is
recoverable from the blobs' sidecars if it is ever lost.
"""

import hashlib
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from presyowatch.log import get_logger
from presyowatch.net.client import HttpClient
from presyowatch.net.errors import InvalidUrlError, NetworkError

logger = get_logger(__name__)

_SHARD_WIDTH = 2


class CacheError(NetworkError):
    """Base class for cache integrity failures."""


class CacheConflictError(CacheError):
    """A known URL was stored again with different bytes.

    Sources publish corrections under a new ``Revised-`` URL rather than by editing a
    file in place, so this should not happen. If it does, the safe response is to stop:
    silently keeping the old bytes would hide a source rewriting history, and silently
    replacing them would destroy the evidence of what was originally published.
    """

    def __init__(self, url: str, *, existing_sha256: str, incoming_sha256: str) -> None:
        self.url = url
        self.existing_sha256 = existing_sha256
        self.incoming_sha256 = incoming_sha256
        super().__init__(
            f"{url} was already cached as {existing_sha256} but was offered different "
            f"bytes hashing to {incoming_sha256}; refusing to overwrite history"
        )


class CacheEntry(BaseModel):
    """Metadata for one cached source file.

    Serialised as JSON on disk, so it is a boundary and gets a Pydantic model rather
    than a bare dict.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    """The URL as fetched, after normalisation."""

    sha256: str
    size: int
    fetched_at: datetime
    content_type: str | None = None
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class CacheProblem:
    """One integrity failure found by :meth:`RawCache.verify`."""

    url: str
    sha256: str
    reason: str


@dataclass(frozen=True, slots=True)
class FetchResult:
    """The outcome of a :meth:`CachingFetcher.fetch_once` call."""

    entry: CacheEntry
    path: Path
    from_cache: bool
    """``False`` only on the single occasion a URL is fetched from the network."""

    def read_bytes(self) -> bytes:
        """Return the cached bytes."""
        return self.path.read_bytes()


def normalise_url(url: str) -> str:
    """Return a canonical form of ``url`` for use as a cache key.

    Scheme and host are lower-cased because they are case-insensitive, and the fragment
    is dropped because it is never sent to the server. Path and query are left exactly
    as given — they *are* case-sensitive, and ``Daily-Price-Index`` is not the same path
    as ``daily-price-index`` on every server.

    Raises:
        InvalidUrlError: If ``url`` is not an absolute http(s) URL.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise InvalidUrlError(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def _atomic_write(destination: Path, body: bytes) -> None:
    """Write ``body`` to ``destination`` atomically.

    A crash mid-write must not leave a truncated file behind that later looks like a
    valid cache hit. The bytes land in a temporary file in the same directory and are
    then moved into place with ``os.replace``, which is atomic on both POSIX and Windows.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temp_name = tempfile.mkstemp(dir=str(destination.parent), prefix=".tmp-")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(destination)
    finally:
        # A successful replace has already moved the file, so this is a no-op then and a
        # cleanup on any failure.
        temp_path.unlink(missing_ok=True)


class RawCache:
    """A permanent store of raw source bytes, addressed by content.

    Nothing here touches the network. It is a store, and it is deliberately dumb: no
    expiry, no eviction, no size limit. The whole point is that a file fetched once in
    2026 is still byte-identical years later, so a parser fix can be re-run over history
    without asking a government server for anything.
    """

    def __init__(self, root: Path) -> None:
        """Initialise the cache rooted at ``root``, creating it if necessary."""
        self._root = root
        self._blobs = root / "blobs"
        self._meta = root / "meta"

    @property
    def root(self) -> Path:
        return self._root

    def blob_path(self, sha256: str) -> Path:
        """Return where the bytes hashing to ``sha256`` live.

        Sharded two levels deep. A single flat directory holding years of daily PDFs from
        every region becomes slow to list and unpleasant on some filesystems.
        """
        first, second = sha256[:_SHARD_WIDTH], sha256[_SHARD_WIDTH : _SHARD_WIDTH * 2]
        return self._blobs / first / second / sha256

    def lookup(self, url: str) -> CacheEntry | None:
        """Return the entry for ``url``, or ``None`` if it has never been fetched."""
        path = self._meta_path(url)
        if not path.is_file():
            return None
        return CacheEntry.model_validate_json(path.read_text(encoding="utf-8"))

    def has(self, url: str) -> bool:
        """Return whether ``url`` has ever been fetched."""
        return self._meta_path(url).is_file()

    def store(
        self,
        url: str,
        body: bytes,
        *,
        content_type: str | None = None,
        http_status: int | None = None,
        fetched_at: datetime | None = None,
    ) -> CacheEntry:
        """Store ``body`` as the content of ``url`` and return its entry.

        Idempotent: storing identical bytes for a known URL rewrites nothing and returns
        the original entry, preserving the original ``fetched_at``.

        Raises:
            CacheConflictError: If ``url`` is known and ``body`` hashes differently.
        """
        canonical = normalise_url(url)
        digest = hashlib.sha256(body).hexdigest()

        existing = self.lookup(canonical)
        if existing is not None:
            if existing.sha256 != digest:
                raise CacheConflictError(
                    canonical,
                    existing_sha256=existing.sha256,
                    incoming_sha256=digest,
                )
            return existing

        blob = self.blob_path(digest)
        if not blob.is_file():
            _atomic_write(blob, body)

        entry = CacheEntry(
            url=canonical,
            sha256=digest,
            size=len(body),
            fetched_at=fetched_at or datetime.now(UTC),
            content_type=content_type,
            http_status=http_status,
        )
        _atomic_write(
            self._meta_path(canonical),
            entry.model_dump_json().encode("utf-8"),
        )
        logger.info(
            "raw_file_cached",
            url=canonical,
            sha256=digest,
            size=len(body),
        )
        return entry

    def read_bytes(self, entry: CacheEntry) -> bytes:
        """Return the stored bytes for ``entry``.

        Does not re-hash. Verification is a maintenance operation
        (:meth:`verify`), not something to pay for on every read while reparsing
        thousands of files.
        """
        return self.blob_path(entry.sha256).read_bytes()

    def entries(self) -> Iterator[CacheEntry]:
        """Yield every cached entry. The basis of a reparse-from-cache run."""
        if not self._meta.is_dir():
            return
        for path in sorted(self._meta.glob("*.json")):
            yield CacheEntry.model_validate_json(path.read_text(encoding="utf-8"))

    def verify(self) -> list[CacheProblem]:
        """Re-hash every blob and report anything missing or corrupted.

        Bit rot and half-finished writes are the failure modes that would otherwise be
        discovered as a mysterious parser error years later.
        """
        problems: list[CacheProblem] = []
        for entry in self.entries():
            blob = self.blob_path(entry.sha256)
            if not blob.is_file():
                problems.append(
                    CacheProblem(url=entry.url, sha256=entry.sha256, reason="blob missing")
                )
                continue
            body = blob.read_bytes()
            actual = hashlib.sha256(body).hexdigest()
            if actual != entry.sha256:
                problems.append(
                    CacheProblem(
                        url=entry.url,
                        sha256=entry.sha256,
                        reason=f"content hashes to {actual}",
                    )
                )
        if problems:
            logger.error("raw_cache_integrity_failure", problem_count=len(problems))
        return problems

    def _meta_path(self, url: str) -> Path:
        """Return the metadata path for ``url``.

        Keyed by the hash of the URL rather than the URL itself. Source-controlled
        strings containing ``..``, ``/``, ``:`` or characters illegal on Windows must
        never reach the filesystem as path components.
        """
        canonical = normalise_url(url)
        key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return self._meta / f"{key}.json"


class CachingFetcher:
    """Fetches a URL from the network at most once, ever.

    The pairing of :class:`RawCache` with
    :class:`~presyowatch.net.client.HttpClient` that rule 3 describes: the network is
    touched once per file, and every subsequent read comes off local disk.
    """

    def __init__(self, *, client: HttpClient, cache: RawCache) -> None:
        self._client = client
        self._cache = cache

    def fetch_once(self, url: str) -> FetchResult:
        """Return ``url``'s bytes, fetching them only if they have never been seen.

        A cache hit makes no request of any kind — not even for ``robots.txt``. That is
        not a loophole: ``robots.txt`` governs what a crawler may *fetch*, and reading a
        file already on our own disk is not a fetch. Re-checking would mean a source
        going offline retroactively destroys our ability to reparse what it already gave
        us.

        Raises:
            RobotsDisallowedError: If the file is not cached and robots.txt forbids
                fetching it.
            HttpRequestError: If the fetch fails. Nothing is cached in that case; a
                failed download is not a source file.
        """
        canonical = normalise_url(url)
        cached = self._cache.lookup(canonical)
        if cached is not None:
            logger.debug("raw_cache_hit", url=canonical, sha256=cached.sha256)
            return FetchResult(
                entry=cached,
                path=self._cache.blob_path(cached.sha256),
                from_cache=True,
            )

        response = self._client.get(canonical)
        entry = self._cache.store(
            canonical,
            response.content,
            content_type=response.headers.get("Content-Type"),
            http_status=response.status_code,
        )
        return FetchResult(
            entry=entry,
            path=self._cache.blob_path(entry.sha256),
            from_cache=False,
        )
