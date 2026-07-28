# TASK.md

Work top to bottom. Do not start a phase until the previous one is deployed and green.
Mark items `[x]` as they land. Add discovered work under "Discovered during work".

**Deploy publicly at the end of Phase 3.** A live half-built system beats a polished
localhost one.

---

## Phase 1 — Ingestion (target: 2 weeks)

- [x] Repo scaffold: `src/`, `tests/`, `pyproject.toml`, ruff + mypy config, pre-commit
- [x] `.env.example` with placeholders; real `.env` gitignored
- [x] `robots.txt` checker utility — fetches and parses per host, caches result, refuses
      to fetch disallowed paths. Wire it into the HTTP client so it cannot be bypassed.
- [ ] Content-addressed raw file cache (SHA-256 keyed), with a `fetch_once()` wrapper
      that returns cached bytes if the file has been seen
- [x] HTTP client: descriptive User-Agent + contact email, 1 req/sec/host limit,
      exponential backoff with jitter, explicit timeout
- [ ] Postgres schema + migrations (Alembic) per PLANNING.md
- [ ] Index scraper for one regional DA source: extract anchor hrefs, tolerant date
      parsing from link text with filename fallback, quarantine unparseable entries
- [ ] `pdfplumber` parser producing validated Pydantic rows; group labels from positional
      extraction, blanks as NULL + `unavailable` flag, unit normalization
- [ ] Commodity alias table + resolver; unmapped names quarantined, never guessed
- [ ] Idempotent upsert on the natural key; `Revised-` files update in place and append to
      `observation_revisions`
- [ ] Backfill runner over a date range; `ingestion_runs` row per source per run
- [ ] Fixture corpus: commit ~10 real PDFs including at least one revised file, one with
      missing values, and one that fails to parse
- [ ] Tests: parser unit tests against fixtures; **idempotency test** (run twice, assert
      identical DB state); revision-handling test

## Phase 2 — API + CI (target: 1 week)

- [ ] FastAPI app: typed responses, pagination, filters for commodity / region / market /
      date range; `/health` and `/meta/sources` endpoints
- [ ] OpenAPI docs served at `/docs` with real examples
- [ ] Pytest against a test database (Neon branch or dockerized Postgres)
- [ ] GitHub Actions CI: ruff, mypy --strict, pytest on every PR
- [ ] GitHub Actions ingestion workflow: `schedule:` + `workflow_dispatch:`, reconciles a
      14-day lookback window, writes an `ingestion_runs` row even on failure
- [ ] Deploy API to Render; document the cold-start tradeoff in the README

## Phase 3 — Dashboard (target: 2 weeks)

- [ ] Next.js app, deployed to Vercel or Cloudflare Pages
- [ ] Time series chart with region/market comparison
- [ ] "Biggest movers" table over a selectable window
- [ ] Commodity search
- [ ] Proper loading skeletons and error states — assume the API is cold and slow
- [ ] Source attribution footer (PSA CC BY 4.0, DA)
- [ ] **Ship it publicly. Put the URL at the top of your CV.**

## Phase 4 — Analytics (target: 2 weeks)

- [ ] PSA OpenSTAT PxWeb API client — CPI / inflation series as a second source
- [ ] Anomaly detection: rolling median + MAD, configurable window and threshold
- [ ] Anomaly flags surfaced in the API and highlighted on charts
- [ ] Seasonal ETS forecast with walk-forward backtest
- [ ] Seasonal-naive baseline, MAE comparison, **displayed on the site even when the model
      loses**
- [ ] Public data quality page: ingestion success rate, null rate per commodity,
      quarantine count, last-successful-run per source

## Phase 5 — Polish (target: 1 week)

- [ ] README: what it does, live link, architecture diagram, the three hardest problems
      and how they were solved, what would change at 100x data
- [ ] `docker compose up` runs the whole stack locally with seed data
- [ ] Written incident log: one real failure, how it was detected, how it was fixed
- [ ] Repo made public, description and topics set

---

## Discovered during work

_(append new tasks here as they surface — do not silently expand scope in other phases)_

- [x] **Added `protego` as a dependency.** `urllib.robotparser` matches rules by literal
      prefix and cannot handle `Disallow: /*.pdf$`, which is what `www.da.gov.ph` actually
      publishes — it returned *allowed* for a DA price-index PDF. See KNOWLEDGE.md
      § "`urllib.robotparser` fails open".
- [x] **Corrected KNOWLEDGE.md on the DA disallow.** The rule is `/*.pdf$` (all PDFs on
      the host), not `/wp-content/uploads/`. Conclusion unchanged but broader.
- [x] **`.gitattributes`** — force LF, mark PDF/XLSX binary, and keep `tests/fixtures/**`
      byte-exact. Fixture hashes are load-bearing for the content-addressed cache, and
      `www.da.gov.ph` serves CRLF.
- [ ] **Record `robots_checked_at` and the robots verdict per source** when the `sources`
      table lands. `HttpClient.robots_decision()` already returns everything needed;
      nothing persists it yet.
- [ ] **Decide what the ingester does with a source that requests a large `Crawl-delay`.**
      The client honours it in full and logs `large_crawl_delay_requested`; no caller acts
      on that yet, so a source asking for 300s would silently make a run very long.
- [ ] **Optional: a network-marked integration test** for the live hosts. Verified manually
      on 2026-07-28 (DA PDF refused, Caraga fetched, rate limiter observed); not automated,
      because CI should not depend on a government server being up.
