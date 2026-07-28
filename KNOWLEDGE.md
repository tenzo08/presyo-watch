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

### Department of Agriculture — national portal
- Index page: `https://www.da.gov.ph/price-monitoring/` — **fetches fine**.
- Publishes three daily/weekly series as PDFs:
  - Daily Price Index — one PDF per day, current through at least 2026-07-25
  - Weekly Average Prices — one PDF per week, back to August 2023
  - Daily Cigarette Price Monitoring — one PDF per day
- **`www.da.gov.ph` disallows automated access to `/wp-content/uploads/`.** A direct fetch
  of a Daily Price Index PDF returns a robots disallow. The HTML index page is fine.
  → Treat national PDFs as **manual download only**. Do not automate. Do not route around.

### Department of Agriculture — regional subdomains, AUTOMATABLE
- `caraga.da.gov.ph`, `cagayanvalley.da.gov.ph`, `rfo7.da.gov.ph`,
  `cagayandeoro.da.gov.ph`, `easternvisayas.da.gov.ph`, and others.
- Caraga serves PDFs without a robots disallow — verified by direct fetch.
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
