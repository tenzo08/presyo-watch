"""Index page scraping: discover source files from anchor hrefs.

CLAUDE.md rule 1: never construct source URLs from a date pattern. Scrape the index page's
anchor hrefs and parse dates from link text, with the filename as fallback. The evidence for
that rule is in KNOWLEDGE.md and it is overwhelming — for Caraga alone, a June 2026 file is
filed under ``FY2025``, month directories vary in case, and two files in the same series use
different filename shapes. A generated URL would miss most of the archive and silently
report the gaps as missing data.

**The index page is fetched, not ``fetch_once``d.** This is the one place the
never-re-fetch rule does not apply, and getting it wrong breaks everything downstream. A
price PDF is immutable: fetched once, cached forever. An index page is the opposite — it
gains a link every day, and its whole purpose is to tell us what is new. Passing it to
:meth:`presyowatch.cache.CachingFetcher.fetch_once` would return yesterday's snapshot for
ever, so the ingester would never discover another file. Source *files* go through the cache;
the *listing* does not.
"""

import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit

from presyowatch.cache import normalise_url
from presyowatch.db.models import QuarantinedRow
from presyowatch.log import get_logger
from presyowatch.net.client import HttpClient
from presyowatch.sources.dates import read_date

logger = get_logger(__name__)

DateSource = Literal["link_text", "filename"]

_REVISION = re.compile(r"\brevised\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class IndexSpec:
    """Which page to read, and which of its links are source files.

    The ``href_pattern`` matters more than it looks. Caraga's index carries 538 PDF links,
    and some of them are job postings. Without a filter every unrelated link that has no
    date in it would be quarantined as unparseable, burying the real failures under noise.
    Links that do not match are simply not ours; only *candidates* that fail to yield a date
    are quarantined.
    """

    slug: str
    index_url: str
    href_pattern: re.Pattern[str]


CARAGA = IndexSpec(
    slug="da-caraga",
    index_url="https://caraga.da.gov.ph/price-monitoring",
    # Verified 2026-07-28: price sheets live under this directory; job postings and other
    # PDFs on the same page do not.
    href_pattern=re.compile(r"/PriceMonitoring/", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One source file discovered on an index page."""

    url: str
    link_text: str
    filename: str
    observed_on: date
    date_source: DateSource
    """Which string the date came from. Reported so reliance on the fallback is visible."""

    is_revision: bool
    """A ``Revised-`` republication, which supersedes an earlier file for the same date."""

    month_was_corrected: bool = False
    corrected_from: str | None = None


@dataclass(frozen=True, slots=True)
class RejectedEntry:
    """A candidate link that could not be understood, kept with the raw href."""

    url: str
    link_text: str
    filename: str
    reason: str


@dataclass(frozen=True, slots=True)
class IndexScrape:
    """Everything one pass over an index page found."""

    entries: tuple[IndexEntry, ...]
    rejected: tuple[RejectedEntry, ...]
    anchors_seen: int
    candidates_seen: int
    duplicates_skipped: int

    @property
    def rejection_rate(self) -> float:
        """Share of candidates that could not be read. Belongs on the data quality page."""
        considered = len(self.entries) + len(self.rejected)
        return len(self.rejected) / considered if considered else 0.0


class _AnchorExtractor(HTMLParser):
    """Collects ``(href, text)`` for every anchor.

    Uses the standard library rather than adding a parsing dependency: the requirement is
    only to read anchors out of WordPress output, and ``HTMLParser`` is lenient about the
    unclosed tags that come with it. Text is accumulated across nested elements, so
    ``<a href="x"><strong>July 24</strong></a>`` yields ``July 24``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        # A nested <a> is invalid HTML but occurs in the wild; close the outer one first
        # rather than blending their text together.
        self._finish_anchor()
        self._href = dict(attrs).get("href") or ""
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._finish_anchor()

    def close(self) -> None:
        super().close()
        # Flush an anchor left open by truncated or malformed markup.
        self._finish_anchor()

    def _finish_anchor(self) -> None:
        if self._href is not None:
            text = " ".join("".join(self._text).split())
            self.anchors.append((self._href, text))
        self._href = None
        self._text = []


def _filename_of(url: str) -> str:
    return urlsplit(url).path.rsplit("/", 1)[-1]


def _stem_of(filename: str) -> str:
    return filename.rsplit(".", 1)[0] if "." in filename else filename


def scrape_index(
    html: str,
    *,
    page_url: str,
    spec: IndexSpec,
    not_before: date | None = None,
    not_after: date | None = None,
) -> IndexScrape:
    """Find every source file linked from ``html``.

    Args:
        html: The index page's markup, as served.
        page_url: The URL it was fetched from, used to resolve relative hrefs.
        spec: Which links count as source files.
        not_before: Reject dates earlier than this.
        not_after: Reject dates later than this. Pass today's date: a price monitoring
            file cannot describe a day that has not happened. Caraga's index really does
            contain ``Mayor-Salvador-Calo-July-19-2029.pdf`` (verified 2026-07-28), whose
            year is a typo in the source. Read literally it would plant a fabricated point
            three years to the right of every chart, so it is better quarantined than
            trusted. Both bounds default to ``None``, leaving the reading unjudged.

    Returns:
        The entries understood and the candidates that were not, with counts.
    """
    extractor = _AnchorExtractor()
    extractor.feed(html)
    extractor.close()

    entries: list[IndexEntry] = []
    rejected: list[RejectedEntry] = []
    seen: set[str] = set()
    candidates = 0
    duplicates = 0

    for href, link_text in extractor.anchors:
        if not href:
            continue
        absolute = urljoin(page_url, href)
        if urlsplit(absolute).scheme not in {"http", "https"}:
            # mailto:, javascript:, tel: and bare fragments are not source files.
            continue
        if not spec.href_pattern.search(absolute):
            continue

        candidates += 1
        canonical = normalise_url(absolute)
        if canonical in seen:
            duplicates += 1
            continue
        seen.add(canonical)

        filename = _filename_of(canonical)
        resolved = _resolve_entry(canonical, link_text=link_text, filename=filename)
        if isinstance(resolved, RejectedEntry):
            rejected.append(resolved)
            continue

        outside = _outside_bounds(resolved.observed_on, not_before, not_after)
        if outside is not None:
            rejected.append(
                RejectedEntry(
                    url=resolved.url,
                    link_text=resolved.link_text,
                    filename=resolved.filename,
                    reason=outside,
                )
            )
            continue
        entries.append(resolved)

    logger.info(
        "index_scraped",
        source=spec.slug,
        page_url=page_url,
        anchors_seen=len(extractor.anchors),
        candidates_seen=candidates,
        entries=len(entries),
        rejected=len(rejected),
        duplicates_skipped=duplicates,
    )
    return IndexScrape(
        entries=tuple(entries),
        rejected=tuple(rejected),
        anchors_seen=len(extractor.anchors),
        candidates_seen=candidates,
        duplicates_skipped=duplicates,
    )


def _outside_bounds(
    observed_on: date, not_before: date | None, not_after: date | None
) -> str | None:
    """Return why ``observed_on`` is implausible, or ``None`` if it is fine."""
    if not_before is not None and observed_on < not_before:
        return f"date {observed_on.isoformat()} is before {not_before.isoformat()}"
    if not_after is not None and observed_on > not_after:
        return f"date {observed_on.isoformat()} is after {not_after.isoformat()}"
    return None


def _resolve_entry(url: str, *, link_text: str, filename: str) -> IndexEntry | RejectedEntry:
    """Read a date for one candidate link, preferring the link text.

    Link text first because a human wrote it for a human and it is usually the clearer of
    the two. The filename is the fallback, and plenty of links have empty or decorative
    text, so the fallback is the common path rather than an edge case.
    """
    from_text = read_date(link_text)
    if from_text.ok and from_text.value is not None:
        return IndexEntry(
            url=url,
            link_text=link_text,
            filename=filename,
            observed_on=from_text.value,
            date_source="link_text",
            is_revision=bool(_REVISION.search(filename)),
            month_was_corrected=from_text.month_was_corrected,
            corrected_from=from_text.corrected_from,
        )

    from_filename = read_date(_stem_of(filename))
    if from_filename.ok and from_filename.value is not None:
        return IndexEntry(
            url=url,
            link_text=link_text,
            filename=filename,
            observed_on=from_filename.value,
            date_source="filename",
            is_revision=bool(_REVISION.search(filename)),
            month_was_corrected=from_filename.month_was_corrected,
            corrected_from=from_filename.corrected_from,
        )

    return RejectedEntry(
        url=url,
        link_text=link_text,
        filename=filename,
        reason=(
            f"no date in link text ({from_text.reason}) nor in filename ({from_filename.reason})"
        ),
    )


def to_quarantine_row(
    rejected: RejectedEntry,
    *,
    source_id: int | None = None,
    run_id: str | None = None,
) -> QuarantinedRow:
    """Turn a rejected link into a row for the quarantine table.

    Index-stage failures have no file and no SHA-256 — only an href that could not be
    understood. The raw href and link text are both kept verbatim so the entry can be
    reprocessed after the date parser is improved, rather than needing to be rediscovered.
    """
    return QuarantinedRow(
        source_id=source_id,
        run_id=run_id,
        stage="index",
        reason=rejected.reason,
        source_url=rejected.url,
        payload={
            "href": rejected.url,
            "link_text": rejected.link_text,
            "filename": rejected.filename,
        },
    )


def fetch_index(client: HttpClient, spec: IndexSpec) -> str:
    """Fetch an index page's markup.

    Deliberately bypasses the content-addressed cache. See the module docstring: the
    listing is mutable and must be re-read every run, unlike the immutable files it points
    at.

    Raises:
        RobotsDisallowedError: If robots.txt forbids reading the index.
        HttpRequestError: If the page cannot be fetched.
    """
    return client.get(spec.index_url).text
