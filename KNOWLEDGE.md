# KNOWLEDGE.md — Verified facts about the data sources

Everything below was verified by direct fetch, not assumed. Do not re-derive it, and do
not contradict it without re-verifying and updating this file with the date.

Last verified: 2026-07-28

---

## Source status

### PSA OpenSTAT — PRIMARY, use this first
- `https://openstat.psa.gov.ph/`
- Powered by PC-Axis / PxWeb, which exposes a documented JSON query API.
- Licensed CC BY 4.0 — free use, reuse, and redistribution with attribution.
- This is the clean, unambiguous, no-scraping-required source. Use it as the backbone for
  CPI, inflation, and regional index data.
- Related portals: PSA Data Archive (`psada.psa.gov.ph`) for microdata (request-gated),
  `mapstat-psa.opendata.arcgis.com` for geospatial.
- `robots.txt` verified 2026-07-28. Cloudflare-managed, and worth reading carefully:
  `User-agent: *` gets `Allow: /`, but a list of named AI crawlers — `GPTBot`,
  `ClaudeBot`, `CCBot`, `Google-Extended`, `Bytespider`, `Amazonbot` and others — get
  `Disallow: /`. PresyoWatch is none of them and matches `*`, so it is permitted. The
  file also carries `Content-Signal: search=yes,ai-train=no,use=reference`, an express
  reservation of rights against AI training. This project stores and republishes the data
  with attribution and does not train models on it, which is consistent with that signal.
  Captured at `tests/fixtures/robots/openstat.psa.gov.ph.robots.txt`.

### Department of Agriculture — national portal
- Index page: `https://www.da.gov.ph/price-monitoring/` — **fetches fine**.
- Publishes three daily/weekly series as PDFs:
  - Daily Price Index — one PDF per day, current through at least 2026-07-25
  - Weekly Average Prices — one PDF per week, back to August 2023
  - Daily Cigarette Price Monitoring — one PDF per day
- **`www.da.gov.ph` disallows *every* PDF on the host — not just `/wp-content/uploads/`.**
  Corrected 2026-07-28 by fetching `robots.txt` directly. The whole file is:

  ```
  User-agent: *
  Disallow: /wp-admin/
  Disallow: /*.pdf$
  Disallow: /author/
  ```

  The operative rule is `Disallow: /*.pdf$`, which matches any URL ending in `.pdf`
  anywhere on the host. An earlier version of this file described the disallow as being
  on `/wp-content/uploads/`; that was **wrong and too narrow** — a crawler checking only
  that prefix would wrongly fetch a PDF served from any other path.
  → Conclusion unchanged and now broader: national PDFs are **manual download only,
  wherever they live**. The HTML index page (`/price-monitoring/`) is allowed.
  → Served with **CRLF** line endings. Captured byte-exact at
  `tests/fixtures/robots/www.da.gov.ph.robots.txt`.

### Department of Agriculture — regional subdomains, AUTOMATABLE
- `caraga.da.gov.ph`, `cagayanvalley.da.gov.ph`, `rfo7.da.gov.ph`,
  `cagayandeoro.da.gov.ph`, `easternvisayas.da.gov.ph`, and others.
- Caraga serves PDFs without a robots disallow — re-verified 2026-07-28. Its entire
  `robots.txt` is `Disallow: /wp-admin/` plus an `Allow` for `admin-ajax.php`. No
  `Crawl-delay`. Captured at `tests/fixtures/robots/caraga.da.gov.ph.robots.txt`.
- **Caraga is slow: roughly 8 seconds per request** for its home page (measured
  2026-07-28). This matters more than it looks — `httpx`'s default timeout is 5 seconds,
  so a client using library defaults fails against this host every time. Always set the
  timeout explicitly; `HttpConfig.timeout` defaults to 30s for this reason.
- **Check `robots.txt` for each regional host before adding it as a source.** Record the
  result and the check date in the `sources` table.
- Caraga uses a highly predictable directory layout:
  `/wp-content/uploads/PriceMonitoring/FY{year}/{City}/{Month}/{Market}_{Month}-{Day}.pdf`
  — this gives per-market, per-date granularity, which is better than regional averages
  for anomaly detection.
- Regional coverage is uneven. Some regions publish consistently, some don't. Verify per
  region rather than assuming national coverage.

### DOE Oil Monitor — optional
- `https://doe.gov.ph/articles/group/liquid-fuels?category=Oil+Monitor`
- Weekly PDFs, current (Jul 2026). Attachment names like `Oil Monitor as of 21 July 2026.pdf.pdf`
  (note the doubled extension — real).
- Crowded space: fuelprice.ph, gaswatchph.com, metrofueltracker.com all already do this.
  Fine as a third source for demonstrating multi-source handling; weak as a headline feature.

### bantaypresyo.da.gov.ph — DEAD, do not use
- Still loads, has a region dropdown including Region VIII (Eastern Visayas).
- The vegetables table is dated **"As of May 28, 2025"** — over a year stale.
- 2013-era PHP app, superseded by the PDF publishing workflow. Ignore it.

---

## The PDFs are text-based, not scans

Verified by extracting a Caraga regional file. Output is clean structured text:

```
Bantay Presyo Monitoring
Retail Price of Selected Agri-Fishery Commodities
Province : Surigao del Sur
Municipality/City : Tandag City
Market Monitored : Luha Public Market
Date of Monitoring : April 2, 2025
Commodity Group Commodities Specifications Unit LOW HIGH PREVAILING AVERAGE
Premium 5% Broken kg 52.00 53.00 53.00 52.80
...
```

Use `pdfplumber`. **No OCR required.** Do not add tesseract to the stack.

### Known extraction quirks
- Commodity **group** labels (`IMPORTED COMMERCIAL RICE`, `HIGHLAND VEGETABLES`,
  `SPICES`, `FERTILIZER`, …) appear *out of order*, often collected at the end of a
  block, because of the multi-column layout. Group assignment must come from positional
  extraction, not reading order.
- **Blank values mean "not available during monitoring"**, not zero and not null-because-
  broken. The footer states this explicitly. Store as NULL with a distinct
  `unavailable` flag; never coerce to 0.
- Duplicated unit tokens appear (`Porkchop kg kg`, `Ginger kg kg`) — the unit is repeated
  in both the specification and unit columns. Normalize.
- The tail of these files includes non-food items (feeds, fertilizer, insecticide,
  rodenticide). Decide explicitly whether they're in scope; don't let them silently
  pollute a "food prices" chart.

---

## `urllib.robotparser` fails open on the DA's rules — do not use it

Verified 2026-07-28 against the real `www.da.gov.ph` file.

Python's standard-library parser matches a rule by **literal prefix only**:
`RuleLine.applies_to` reduces to `path.startswith(rule)`. It understands neither a `*`
inside a pattern nor a `$` end-anchor. Given the real `Disallow: /*.pdf$`:

| URL | `urllib.robotparser` | `protego` | Correct |
|---|---|---|---|
| `/wp-content/uploads/2026/07/Daily-Price-Index-July-24-2026.pdf` | **allowed** | disallowed | disallowed |
| `/price-monitoring/` | allowed | allowed | allowed |
| `/wp-admin/` | disallowed | disallowed | disallowed |

The stdlib parser grants permission to fetch precisely the files this project must not
fetch. It fails in the permissive direction, which is the unacceptable one.

**Therefore:** robots parsing uses `protego` (the parser Scrapy uses), which implements
the RFC 9309 matching rules including wildcards, end-anchors, and longest-match
precedence. This is a correctness requirement, not a preference.

## Unreachable robots.txt means disallow everything

RFC 9309 §§ 2.3.1.3–2.3.1.4 draws a distinction that intuition gets backwards:

- **`4xx`** — robots.txt is *unavailable*. No rules exist, so everything is allowed.
- **`5xx`, `429`, timeouts, connection failures** — robots.txt is *unreachable*. The
  rules exist but could not be read, so a crawler **must assume complete disallow**.

Getting this backwards means a flaky government server silently licenses us to ignore
rules we merely failed to read. Implemented in `net/robots.py` and pinned by tests.

## URL naming is inconsistent — never build URLs from patterns

Observed in the live DA index, all in one page:

- `Daily-Price-Index-July-24-2026.pdf` — the common form
- `July-25-2026-DPI-AFC.pdf` — completely different form, same series
- `Revised-Daily-Price-Index-May-29-2026.pdf` — corrections republished later
- `Daily-Price-Index-July-14-2026-1.pdf` — `-1` dedup suffix from the CMS
- Files for one month stored under a different month's upload folder
  (e.g. a May file under `/2026/06/`)
- Typos in **link text**: "JUne 6, 2026", "Marhc 20, 2025", "Janauary 19-24"
- Missing dates outright (several days absent from 2025 and 2026)

**Therefore:** scrape anchor hrefs from the index page. Parse the date from link text with
a tolerant parser, fall back to the filename, and quarantine anything unparseable with the
raw href recorded. Never generate a URL and hope.

The `Revised-` files are the reason the schema needs `observation_revisions`. This is a
real correction-handling problem, not a hypothetical one.

### Caraga's directory layout is not trustworthy either

Verified 2026-07-28 from `https://caraga.da.gov.ph/price-monitoring`, which lists
**538 PDF links on a single page** — the whole index arrives in one request, which is
comfortable under a one-request-per-second budget.

The layout in this file's "Source status" section is *approximately* right and must not
be used to generate URLs. Real hrefs from that one page:

```
/wp-content/uploads/PriceMonitoring/FY2025/CabadbaranCity/june/Cabadbaran-City-Public-Market_June-24-2026.pdf
/wp-content/uploads/PriceMonitoring/FY2026/ButuanCity/April/April-2026-April-10.pdf
```

- A **June 2026** file filed under **`FY2025`**. Fetched successfully (HTTP 200,
  62,680 bytes), so the path is real, not a broken link. The fiscal-year directory does
  not reliably match the date in the filename.
- Month directory casing is inconsistent: `june` in one path, `April` in another.
- Filename forms differ for the same series: `{Market}_{Month}-{Day}-{Year}.pdf` in one,
  `{Month}-{Year}-{Month}-{Day}.pdf` in another, with no market name at all.
- **Dead links are mixed into the index.** Three `/Downloads/JobOpportunity/*.pdf`
  hrefs on both the home page and the index return **404**. An index scraper must treat
  a 404 on one href as a skipped file, not a failed run.

Together these mean the date and market must be parsed from link text and filename with
a tolerant parser, exactly as for the national source, and the directory path must not be
used to infer either.

### Measured, over all 535 price-monitoring links on that page

The index page is committed as a fixture
(`tests/fixtures/index/caraga.da.gov.ph_price-monitoring.html`, captured 2026-07-28), so
these numbers are reproducible rather than anecdotal:

| Observation | Count |
|---|---|
| Price-monitoring PDF links (after filtering out job postings etc.) | 535 |
| Dates read successfully | 451 |
| **Rejected: no year in link text *or* filename** | **81 (15%)** |
| Rejected for other reasons | 3 |
| **Read successfully but whose filename year contradicts its `FY####` directory** | **57 of 451 (12.6%)** |

Two conclusions follow, and they are the reason the scraper behaves as it does.

**The `FY` directory cannot supply the missing year.** All 81 year-less links sit under a
`FY####` directory, so the year looks available. But among the entries where the filename
*does* state a year, 12.6% contradict their directory — February 2025 sheets filed under
`FY2026`. Inferring from the directory would therefore misdate roughly one in eight of them.
They are quarantined instead, with the reason recorded. Losing 15% honestly beats publishing
12% of it wrong, and the quarantine count makes the loss visible.

**One file is dated in the future.**
`FY2026/ButuanCity/July/Mayor-Salvador-Calo-July-19-2029.pdf`, link text "July 19". The
year is a typo at the source. Read literally it plants a point three years to the right of
every chart, so the scraper takes an optional `not_after` bound and the ingester should pass
today's date.

Two incidental notes from the same page: `Mayor Salvador Calo` is a real Butuan market, so
`Mayor` is a legitimate filename token that a careless fuzzy month matcher reads as `May`;
and doubled extensions are a habit here too (`...July-23-2026.xlsx.pdf`), matching the DOE
oil monitor's `.pdf.pdf`.

---

## Legal position

- Philippine IP Code (RA 8293) § 176: **no copyright subsists in any work of the
  Government of the Philippines.** Prior approval from the originating agency is required
  to exploit such work *for profit*.
- § 175: copyright does not extend to **mere data as such**, even when embodied in a work.
- PSA OpenSTAT is explicitly CC BY 4.0.

**Conclusion:** a free, public, non-commercial portfolio project is clearly permitted.
Attribute sources prominently. If this is ever monetized, DA approval is required first.
Respecting `robots.txt` is separate from copyright — it's the professional norm and a
condition of this project regardless of what the law permits.

---

## GitHub Actions scheduling caveats

- Scheduled workflows are **silently disabled after 60 days with no commit** on the
  default branch. Only commits reset the timer — releases, tags, and issues do not.
- **Minimum interval is 5 minutes.** More frequent cron expressions are silently ignored.
- **Delays of 5–30 minutes are common**, longer under load. There is no timing SLA.
- Schedule triggers **only fire from the default branch**. A cron on a feature branch
  never runs.
- Scheduled workflows are disabled by default in forks.

**Design response:** always pair `schedule:` with `workflow_dispatch:`; make each run
reconcile a lookback window (e.g. last 14 days) rather than fetching "today"; record every
run in `ingestion_runs` so a skipped run is visible on the data quality page rather than
silently invisible.

---

## Prior art / competitors

- `lowpricedito.com` already aggregates Bantay Presyo alongside supermarket prices.
- Fuel trackers: fuelprice.ph, gaswatchph.com, metrofueltracker.com.

Not blockers. But do not claim in the README to be the first to do this.
