"""Tests for index page scraping.

The bulk of these run against a byte-exact capture of
``https://caraga.da.gov.ph/price-monitoring`` taken 2026-07-28 — 203 KB of real WordPress
output with 538 PDF links, inconsistent filename shapes, and dead links among them. Hand-
written HTML would not exercise any of that, which is why CLAUDE.md insists fixtures be real
files including the malformed ones.
"""

import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from presyowatch.net.client import HttpClient, HttpConfig
from presyowatch.sources.index import (
    CARAGA,
    IndexScrape,
    IndexSpec,
    fetch_index,
    scrape_index,
    to_quarantine_row,
)
from tests.conftest import TEST_USER_AGENT

FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "index" / "caraga.da.gov.ph_price-monitoring.html"
)
PAGE_URL = CARAGA.index_url

ANY_PDF = IndexSpec(
    slug="test", index_url="https://example.ph/i", href_pattern=re.compile(r"\.pdf$")
)


@pytest.fixture(scope="module")
def caraga_html() -> str:
    return FIXTURE.read_bytes().decode("utf-8", errors="replace")


@pytest.fixture(scope="module")
def caraga_scrape(caraga_html: str) -> IndexScrape:
    return scrape_index(caraga_html, page_url=PAGE_URL, spec=CARAGA)


# -- the real index page ----------------------------------------------------------


def test_the_real_index_yields_hundreds_of_files(caraga_scrape: IndexScrape) -> None:
    """One request produces the whole archive, which is why this is affordable to poll."""
    assert len(caraga_scrape.entries) > 400
    assert caraga_scrape.anchors_seen > 200


def test_unrelated_pdfs_are_not_candidates(caraga_scrape: IndexScrape) -> None:
    """The same page links job postings. They are not ours and must not be quarantined.

    Without the href filter these would fail date parsing and bury the real failures.
    """
    urls = [entry.url for entry in caraga_scrape.entries]
    rejected = [entry.url for entry in caraga_scrape.rejected]

    assert not any("JobOpportunity" in url for url in urls + rejected)
    assert all("/PriceMonitoring/" in url for url in urls)


def test_every_entry_has_an_absolute_url(caraga_scrape: IndexScrape) -> None:
    assert all(entry.url.startswith("https://caraga.da.gov.ph/") for entry in caraga_scrape.entries)


def test_dates_are_not_absurd(caraga_scrape: IndexScrape) -> None:
    """No date should predate the series. The upper bound is a separate case, below."""
    dates = [entry.observed_on for entry in caraga_scrape.entries]

    assert min(dates) >= date(2020, 1, 1)


def test_the_source_really_publishes_a_future_dated_file(caraga_scrape: IndexScrape) -> None:
    """Read literally, Caraga's index contains a file dated 2029.

    `Mayor-Salvador-Calo-July-19-2029.pdf` sits under `FY2026/ButuanCity/July/` with link
    text "July 19". The year is a typo at the source. Pinned here so the reason the
    `not_after` bound exists stays visible, rather than looking like defensive noise.
    """
    future = [entry for entry in caraga_scrape.entries if entry.observed_on.year == 2029]

    assert len(future) == 1
    assert future[0].filename == "Mayor-Salvador-Calo-July-19-2029.pdf"


def test_a_future_dated_file_is_quarantined_when_a_bound_is_given(caraga_html: str) -> None:
    """A price sheet cannot describe a day that has not happened yet.

    Without this the 2029 typo becomes a fabricated point three years to the right of
    every chart — far more damaging than one quarantined row.
    """
    scrape = scrape_index(
        caraga_html,
        page_url=PAGE_URL,
        spec=CARAGA,
        not_after=date(2026, 7, 28),
    )

    assert all(entry.observed_on <= date(2026, 7, 28) for entry in scrape.entries)
    assert any("is after 2026-07-28" in rejected.reason for rejected in scrape.rejected)


def test_a_file_dated_before_the_series_began_is_quarantined() -> None:
    """The lower bound catches the other direction: a year misread as 1999, say."""
    html = '<a href="https://e.ph/a_July-24-1999.pdf">July 24, 1999</a>'

    scrape = scrape_index(
        html,
        page_url="https://e.ph/i",
        spec=ANY_PDF,
        not_before=date(2020, 1, 1),
    )

    assert scrape.entries == ()
    assert "is before 2020-01-01" in scrape.rejected[0].reason


def test_the_market_named_mayor_takes_its_month_from_a_real_month_token(
    caraga_scrape: IndexScrape,
) -> None:
    """ "Mayor Salvador Calo" is a real Butuan market, so `Mayor` is a real filename token.

    65 of its files are in the fixture, spanning April to July — including genuine May
    sheets, where `Mayor` and `May` sit side by side in one filename. The month must come
    from the `May` token and never from `Mayor`, which scores 0.750 against `may` and would
    be admitted if three-letter months were fuzzy-matchable.

    Substring matching would not test this: `"may" in "mayor"` is trivially true. So the
    month name has to appear as its own token.
    """
    month_names = [
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ]
    mayor_files = [entry for entry in caraga_scrape.entries if "Mayor" in entry.filename]

    assert len(mayor_files) > 60, "expected the Mayor Salvador Calo series in the fixture"
    for entry in mayor_files:
        tokens = set(re.split(r"[-_.]+", entry.filename.lower()))
        expected = month_names[entry.observed_on.month - 1]
        assert expected in tokens, f"{entry.filename} parsed as {entry.observed_on}"


def test_a_doubled_file_extension_still_parses(caraga_scrape: IndexScrape) -> None:
    """`...July-23-2026.xlsx.pdf` is real: someone renamed a spreadsheet to PDF.

    KNOWLEDGE.md records the same shape on the DOE oil monitor (`....pdf.pdf`), so it is a
    habit rather than a one-off.
    """
    doubled = [entry for entry in caraga_scrape.entries if entry.filename.count(".") > 1]

    assert doubled, "expected at least one doubled extension in the fixture"
    assert any(
        entry.filename == "Mayor-Salvador-Calo-July-23-2026.xlsx.pdf"
        and entry.observed_on == date(2026, 7, 23)
        for entry in doubled
    )


def test_the_fiscal_year_directory_does_not_become_the_year(caraga_scrape: IndexScrape) -> None:
    """A June 2026 file really is filed under `FY2025` (KNOWLEDGE.md, fetched and verified).

    Parsing the whole path would read 2025 from the directory and misdate the file by a
    year. Only the filename is parsed, which is why this passes.
    """
    mismatched = [
        entry
        for entry in caraga_scrape.entries
        if "/FY2025/" in entry.url and entry.observed_on.year == 2026
    ]

    assert mismatched, "expected at least one 2026 file under FY2025"
    for entry in mismatched:
        assert str(entry.observed_on.year) not in entry.url.split("/PriceMonitoring/")[0]


def test_most_candidates_are_understood(caraga_scrape: IndexScrape) -> None:
    """451 of 535 candidates read cleanly; the ~16% rejected is the source, not the parser.

    The bound is deliberately just above the measured rate. If it climbs, either the source
    changed shape or the parser regressed, and either is worth a failing test.
    """
    assert caraga_scrape.rejection_rate < 0.18, (
        f"{len(caraga_scrape.rejected)} of "
        f"{len(caraga_scrape.entries) + len(caraga_scrape.rejected)} rejected: "
        f"{[r.reason for r in caraga_scrape.rejected[:5]]}"
    )


def test_the_year_is_never_inferred_from_the_fiscal_year_directory(
    caraga_scrape: IndexScrape,
) -> None:
    """About 15% of Caraga's links carry no year at all, and guessing one would be wrong.

    Files like `Cabadbaran-City-Public-Market_July-22.pdf` have no year in the filename and
    none in the link text ("July 22"). The only year available is the `FY####` directory,
    and the fixture shows exactly how unreliable that is: of the entries where the filename
    *does* state a year, 57 of 451 contradict their directory — February 2025 sheets filed
    under `FY2026`. Inferring from the directory would therefore misdate roughly one in
    eight of these.

    So they are quarantined with the reason recorded. Losing 15% honestly beats publishing
    12% of it wrong, and the quarantine count makes the loss visible instead of invisible.
    """
    missing_year = [
        rejected
        for rejected in caraga_scrape.rejected
        if "no four-digit year found) nor in filename (no four-digit year found)" in rejected.reason
    ]

    assert len(missing_year) > 50, "the missing-year cohort should be substantial"
    # Every one of them sits under a directory that would have offered a year.
    assert all(re.search(r"/FY\d{4}/", rejected.url) for rejected in missing_year)


def test_the_fiscal_year_directory_contradicts_the_filename_often(
    caraga_scrape: IndexScrape,
) -> None:
    """Quantifies the evidence behind refusing to trust the directory.

    KNOWLEDGE.md recorded one example of this. The fixture shows it is systematic.
    """
    mismatched = [
        entry
        for entry in caraga_scrape.entries
        if (found := re.search(r"/FY(\d{4})/", entry.url))
        and int(found.group(1)) != entry.observed_on.year
    ]

    assert len(mismatched) > 40


def test_no_duplicate_urls_survive(caraga_scrape: IndexScrape) -> None:
    urls = [entry.url for entry in caraga_scrape.entries]

    assert len(urls) == len(set(urls))


def test_rejections_keep_the_raw_href_and_a_reason(caraga_scrape: IndexScrape) -> None:
    for rejected in caraga_scrape.rejected:
        assert rejected.url.startswith("https://")
        assert rejected.reason


# -- anchor extraction ------------------------------------------------------------


def test_relative_hrefs_are_resolved_against_the_page() -> None:
    html = '<a href="/wp-content/uploads/x_July-24-2026.pdf">July 24, 2026</a>'

    scrape = scrape_index(html, page_url="https://example.ph/price-monitoring", spec=ANY_PDF)

    assert scrape.entries[0].url == "https://example.ph/wp-content/uploads/x_July-24-2026.pdf"


def test_link_text_inside_nested_tags_is_recovered() -> None:
    """WordPress wraps link text in `<strong>`, `<span>`, and friends."""
    html = '<a href="https://e.ph/a.pdf"><strong>July</strong> <em>24, 2026</em></a>'

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    assert scrape.entries[0].link_text == "July 24, 2026"
    assert scrape.entries[0].date_source == "link_text"


def test_html_entities_in_link_text_are_decoded() -> None:
    html = '<a href="https://e.ph/a.pdf">July&nbsp;24, 2026</a>'

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    assert scrape.entries[0].observed_on == date(2026, 7, 24)


@pytest.mark.parametrize(
    "href",
    [
        "mailto:someone@da.gov.ph",
        "javascript:void(0)",
        "#section",
        "tel:+63212345678",
    ],
)
def test_non_http_hrefs_are_ignored(href: str) -> None:
    html = f'<a href="{href}">July 24, 2026</a>'

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    assert scrape.entries == ()
    assert scrape.rejected == ()


def test_anchors_without_an_href_are_ignored() -> None:
    html = '<a name="anchor">July 24, 2026</a><a href="https://e.ph/a_July-24-2026.pdf">x</a>'

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    assert len(scrape.entries) == 1


def test_an_unclosed_anchor_is_still_collected() -> None:
    """Truncated or malformed markup should not swallow the last link on the page."""
    html = '<a href="https://e.ph/a.pdf">July 24, 2026'

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    assert scrape.entries[0].observed_on == date(2026, 7, 24)


def test_nested_anchors_do_not_merge_their_text() -> None:
    html = (
        '<a href="https://e.ph/outer_July-24-2026.pdf">July 24, 2026'
        '<a href="https://e.ph/inner_May-1-2026.pdf">May 1, 2026</a>'
    )

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    dates = {entry.observed_on for entry in scrape.entries}
    assert dates == {date(2026, 7, 24), date(2026, 5, 1)}


def test_the_same_url_twice_is_counted_once() -> None:
    html = (
        '<a href="https://e.ph/a_July-24-2026.pdf">July 24, 2026</a>'
        '<a href="https://e.ph/a_July-24-2026.pdf">July 24, 2026 (again)</a>'
    )

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    assert len(scrape.entries) == 1
    assert scrape.duplicates_skipped == 1
    assert scrape.candidates_seen == 2


# -- date sourcing ----------------------------------------------------------------


def test_link_text_is_preferred_over_the_filename() -> None:
    """When both parse, the human-written text wins — and the two can disagree."""
    html = '<a href="https://e.ph/x_May-1-2026.pdf">July 24, 2026</a>'

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    assert scrape.entries[0].observed_on == date(2026, 7, 24)
    assert scrape.entries[0].date_source == "link_text"


def test_the_filename_is_used_when_link_text_has_no_date() -> None:
    html = '<a href="https://e.ph/Daily-Price-Index-July-24-2026.pdf">Download PDF</a>'

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    assert scrape.entries[0].observed_on == date(2026, 7, 24)
    assert scrape.entries[0].date_source == "filename"


def test_a_repaired_month_is_reported_on_the_entry() -> None:
    """A typo that had to be fixed is visible downstream, not silently absorbed."""
    html = '<a href="https://e.ph/a.pdf">Marhc 20, 2025</a>'

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    assert scrape.entries[0].observed_on == date(2025, 3, 20)
    assert scrape.entries[0].month_was_corrected is True
    assert scrape.entries[0].corrected_from == "marhc"


def test_a_link_with_no_readable_date_anywhere_is_rejected() -> None:
    html = '<a href="https://e.ph/Publication-AO-V-Et-Al_A.pdf">Job posting</a>'

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    assert scrape.entries == ()
    assert len(scrape.rejected) == 1
    assert "no date in link text" in scrape.rejected[0].reason
    assert "nor in filename" in scrape.rejected[0].reason


def test_revisions_are_flagged() -> None:
    html = '<a href="https://e.ph/Revised-Daily-Price-Index-May-29-2026.pdf">May 29, 2026</a>'

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    assert scrape.entries[0].is_revision is True


def test_ordinary_files_are_not_flagged_as_revisions() -> None:
    html = '<a href="https://e.ph/Daily-Price-Index-May-29-2026.pdf">May 29, 2026</a>'

    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    assert scrape.entries[0].is_revision is False


# -- quarantine ------------------------------------------------------------------


def test_a_rejected_link_becomes_a_quarantine_row() -> None:
    """Nothing is dropped: an unreadable href is kept with everything needed to retry."""
    html = '<a href="https://e.ph/mystery.pdf">Untitled</a>'
    scrape = scrape_index(html, page_url="https://e.ph/i", spec=ANY_PDF)

    row = to_quarantine_row(scrape.rejected[0], source_id=7, run_id="abc123")

    assert row.stage == "index"
    assert row.source_id == 7
    assert row.run_id == "abc123"
    assert row.source_url == "https://e.ph/mystery.pdf"
    assert row.source_file_sha256 is None, "there is no file yet, only a bad link"
    assert row.payload["href"] == "https://e.ph/mystery.pdf"
    assert row.payload["link_text"] == "Untitled"
    assert row.reason


def test_every_real_rejection_can_be_quarantined(caraga_scrape: IndexScrape) -> None:
    rows = [to_quarantine_row(r, source_id=1, run_id="r") for r in caraga_scrape.rejected]

    assert all(row.stage == "index" for row in rows)
    assert all(row.payload["href"] for row in rows)


# -- fetching --------------------------------------------------------------------


def test_fetch_index_reads_the_page_through_the_robots_gate(caraga_html: str) -> None:
    """The index goes through HttpClient, so robots.txt still governs reading it."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /wp-admin/\n")
        return httpx.Response(200, text=caraga_html)

    client = HttpClient(
        user_agent=TEST_USER_AGENT,
        config=HttpConfig(min_interval_per_host=0.0),
        transport=httpx.MockTransport(handler),
    )
    with client:
        html = fetch_index(client, CARAGA)

    assert CARAGA.index_url in requested
    assert "https://caraga.da.gov.ph/robots.txt" in requested
    assert len(scrape_index(html, page_url=CARAGA.index_url, spec=CARAGA).entries) > 400


def test_repeated_index_fetches_hit_the_network_every_time(caraga_html: str) -> None:
    """The index is mutable and must never be served from the permanent cache.

    A price PDF is immutable and fetched once. The listing is the opposite: its entire job
    is to reveal what is new, so caching it would freeze discovery at the first run and the
    ingester would never see another file.
    """
    content_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        content_requests.append(str(request.url))
        return httpx.Response(200, text=caraga_html)

    client = HttpClient(
        user_agent=TEST_USER_AGENT,
        config=HttpConfig(min_interval_per_host=0.0),
        transport=httpx.MockTransport(handler),
    )
    with client:
        fetch_index(client, CARAGA)
        fetch_index(client, CARAGA)
        fetch_index(client, CARAGA)

    assert len(content_requests) == 3
