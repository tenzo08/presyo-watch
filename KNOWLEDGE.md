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

### They are *ruled* tables, and that solves the group-label problem

Verified 2026-07-28 across four sheets from four markets, all committed under
`tests/fixtures/pdf/`.

`page.find_tables()` reconstructs the grid from the ruling lines and gives an 8-column
table: `Commodity Group | Commodities | Specifications | Unit | LOW | HIGH | PREVAILING |
AVERAGE`. Two facts make this far better than clustering words by y-coordinate:

- A group label is **one tall cell** spanning its block, and `extract()` places it in the
  **first** row of that span. Group assignment is a forward fill.
- Continuation rows return **`None`** for the group column, whereas a genuinely empty cell
  returns **`""`**. That distinction is load-bearing: `None` means "same group as above",
  `""` in a price column means "not monitored".

Anatomy of a sheet: 3–4 pages, ~153 data rows, **21 commodity groups**. The "blank means not
available" note is itself a row of the table and must be skipped.

**Not every sheet repeats its header.** The three-page sheets put the header block and column
header on page 1 only. The four-page Mayor Salvador Calo sheet repeats **both on every page**,
and worse, a group heading gets absorbed onto the end of that repeated metadata cell:
`...Date of Monitoring : July 19, 2026\nLIVESTOCK MEAT\nPRODUCTS`. Treating any first-column
text as a heading would set the group to the whole header block.

### An empty group cell means a page break, and its meaning depends on which side

This is the subtlest thing in these files. When a group's block straddles a page break, the
heading cell is split and one half extracts as `""` — a real closed cell with no text — while
the heading renders on the other half. **Both directions occur:**

| Situation | Where the `""` cells are | Correct heading |
|---|---|---|
| Block *starts* near the bottom of a page | end of that page | the **next** heading (drawn overleaf) |
| Block *continues* onto a new page | first data rows of the new page | the **previous** heading |

Real examples: `Avocado` and three bananas begin the `FRUITS` block at the foot of a page, so
their heading appears overleaf. `Porkchop`, `Pork ribs` and `Pork pata (hind)` are the tail of
`LIVESTOCK MEAT PRODUCTS` at the top of a page, where the next heading is the unrelated
`POULTRY MEAT PRODUCTS`.

So: **an empty heading cell on the first data row of a page keeps the previous heading;
anywhere else it takes the next one.** Forward-filling everything put Avocado under `SPICES`
and pork under `POULTRY`.

**How to catch this class of bug:** the four sheets publish the same 21 groups, so a commodity
name appearing under different groups *on different sheets* means the parser is inconsistent,
not the source. That cross-sheet check is what exposed it, and it is now a test. The only
legitimate cross-group names are the seven rice varieties — `Premium`, `Basmati`, `Glutinous`,
`Japonica/Jasponica`, `Other Special Rice`, `Regular Milled`, `Well Milled` — which really do
appear under both `IMPORTED` and `LOCAL COMMERCIAL RICE`. **A commodity's identity therefore
needs its group, not just its name.**

### The header block is not always a single cell, and that broke two things

Verified 2026-07-28 on `Mayor-Salvador-Calo-July-23-2026.xlsx.pdf` (five pages).

Most sheets extract their metadata block as one tall cell that never reaches the table body.
This one repeats the block on **every page** and extracts it **one line per row**, split
across the first two columns:

```
['Province', ': Agusan Del Norte', '', '', '', '', '', '']
['Municipality/City', ': Butuan City', '', '', '', '', '', '']
```

That is indistinguishable from a commodity row carrying a group heading, and treating it as
one caused two unrelated failures on the same sheet:

- `Province` became a commodity group, and the seven highland vegetables whose real heading
  is drawn overleaf were filed under it;
- those rows counted as the *first data row of their page*, which cost
  `Cooking Oil (Coconut)` its page-opening status and moved it out of
  `OTHER BASIC COMMODITIES` into `LIVESTOCK & POULTRY FEEDS`.

**Therefore:** a row whose first column names a header field is metadata, not data, and is
skipped rather than rejected. And an empty group cell only means "a page break split this
heading" **on a commodity row** — the header block's blank rows have one too.

Neither bug was visible on the original four fixtures. Both were found by the cross-sheet
agreement check when the corpus grew to twelve, which is the strongest argument in this file
for keeping real, awkward fixtures rather than convenient ones.

### The source truncates its own header labels

Verified 2026-07-28 on `Libertad-Public-Market-July-22-2026.pdf`:

```
Province : Agusan Del Norte
Municipality/Ci: Butuan City
Market Monitor: Libertad Public Market
Date of Monitor: July 22, 2026
```

The spreadsheet column was too narrow and the export clipped the **label**, not the value.
The data is complete and unambiguous. Header labels are therefore matched by prefix, with a
six-character minimum and ambiguity refused rather than resolved by order.

### One real sheet has seven columns, and is quarantined

`Libertad-Public-Market.xlsx-June-3-2026.pdf` has no `Specifications` column:

```
Commodity Group | Commodities | Unit | LOW | HIGH | PREVAILING | AVERAGE
```

Otherwise it is a perfectly ordinary, readable sheet. It is **quarantined whole** rather than
read, because reading it would shift every price one column left, and because supporting the
layout changes what identifies a commodity — the triple `(group, commodity, specification)`
loses a third of itself. Other Libertad files from January and April 2026 use the normal
eight-column layout, so this is an occasional variant rather than a market-wide one.

### The commodity vocabulary differs between markets and over time

Measured 2026-07-28 across the twelve committed sheets: **87.1%** of parsed rows resolve
against the seeded alias table, down from 96.4% on the original four. The drop is real
coverage information, not a regression.

Two sheets sit far below the rest — `Surigao-City-Public-Market` (64%) and the January 2025
Libertad sheet (63%) — and they fail on the same kind of thing:

| Seeded | Also published as |
|---|---|
| `Well Milled` | `Well-Milled`, `Well-Milled (white tag)` |
| `Regular Milled` | `Regular-Milled`, `Regular-Milled (white tag)` |
| `Bangus, Large` | `Bangus` + specification `Large` |
| `Premium` | `Premium (yellow tag)`, `Premium (Imported)` |

Each of those is obvious to a person and unsafe for a machine: the same hyphen-insensitive
rule that merges `Well-Milled` into `Well Milled` would merge `Corn Grits Feed Grade` into
`Corn Grits Food Grade`. They stay unmapped and quarantine at stage `alias` until curated.

### The same sheet is published under two URLs

`SanFranciscoADS/April/ADS-April-23-2026.pdf` and
`SanFranciscoADS/April/SanFracisco-April-23-2026.pdf` (the source's own typo) are **byte for
byte identical** — 83,840 bytes, same SHA-256. Verified 2026-07-28.

This is the case the content-addressed cache was built for: two URL records, one blob. It is
also why the upsert compares figures rather than filenames — a republication is not a
correction.

**No `Revised-` file is obtainable from a permitted host.** The Caraga index contains no
file matching `revised` (checked over all 535 links). The `Revised-` files are published by
`www.da.gov.ph`, whose robots.txt disallows every PDF, so one can only reach the fixture
corpus by manual download.

### The sheet's own date beats the filename

`Mayor-Salvador-Calo-July-19-2029.pdf` contains `Date of Monitoring : July 19, 2026`.

The filename year is a typo; the header is correct. **Therefore the PDF header is the
observation date, and the index scraper's date is provisional** — good only for deciding
what to fetch and for spotting files worth a second look.

It is not a one-off, and it goes both ways. `Libertad-Public-Market_Jan-07-2025.pdf`
contains `Date of Monitoring : January 7, 2026` — the ordinary new-year slip, with the
filename a year behind rather than three ahead. Two of twelve committed sheets disagree with
their own filenames.

This corrects an earlier plan to quarantine future-dated files at index stage: doing so
would have discarded this perfectly valid 2026 sheet. Any `not_after` bound on the scraper
must be generous, and the filename/header disagreement should be reconciled *after*
parsing.

### Further extraction quirks, all observed

- **Thousands separators**: feed and fertiliser prices are written `1,791.00`. Strip commas.
- **Collapsed rows**: in the Cabadbaran sheet one cell contains three stacked values,
  `1,865.00\n1,805.00\n40.00`. There is no way to know which belongs to the commodity, so
  the row is quarantined. The collapse also robs the two rows below of their `LOW`, and that
  damage is *not* detectable from our side — those rows look legitimately partial.
- **Units observed**: `kg`, `g`/`gram`, `L`/`Liter`, `ml`, `pc`, `box`, `bag`. Casing and
  pluralisation vary between sheets, so normalise.
- **Province names vary in case and form**: `Agusan del Norte` and `Agusan Del Norte` both
  appear, and Dinagat Islands is written `Province of Dinagat Islands`. This matters when
  populating `regions` and `markets` — match on a normalised key, not the raw string.
- **The non-food tail is a deliberate keep-and-flag.** `LIVESTOCK & POULTRY FEEDS`,
  `FERTILIZER`, `INSECTICIDE`, `HERBICIDE`, `FUNGICIDE`, `MOLLUSCIDE`, `RODENTICIDE` —
  about 35 rows a sheet. They are parsed (dropping published data silently is not
  acceptable) and flagged, so a food-price chart can exclude them rather than quietly
  averaging fungicide into the cost of rice.
- **Blanks are frequent, not exceptional**: 55–75 of ~153 rows per sheet are unmonitored.
  Any "coverage" figure on the data quality page has to expect that.

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

## PSGC codes are the one thing here not verified by fetch

Everything else in this file was checked by direct request. The region codes in
`src/presyowatch/data/regions.csv` were not, and that is recorded rather than hidden.

`https://psa.gov.ph/robots.txt` **allows** `/classification/psgc` (checked 2026-07-28), but
the page itself returns **HTTP 403** to this project's client. Getting past that would mean
presenting a browser User-Agent, which is routing around an access control and is not
something this project does — for the same reason it does not route around a `Disallow`.

So the six Caraga codes are transcribed from the PSGC rather than fetched:

| PSGC | Name | Level |
|---|---|---|
| 160000000 | Caraga | region |
| 160200000 | Agusan del Norte | province |
| 160300000 | Agusan del Sur | province |
| 166700000 | Surigao del Norte | province |
| 166800000 | Surigao del Sur | province |
| 168500000 | Dinagat Islands | province |

**Treat these as unconfirmed until someone checks them against the PSA publication by hand.**
A wrong code mislabels a region; it does not corrupt a price, and it is fixable with a data
migration. Note also that Butuan City's sheets name **Agusan del Norte** as their province,
which is where the city sits geographically even though it is administratively independent
of the province.

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
