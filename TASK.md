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
- [x] Content-addressed raw file cache (SHA-256 keyed), with a `fetch_once()` wrapper
      that returns cached bytes if the file has been seen
- [x] HTTP client: descriptive User-Agent + contact email, 1 req/sec/host limit,
      exponential backoff with jitter, explicit timeout
- [x] Postgres schema + migrations (Alembic) per PLANNING.md
- [x] Index scraper for one regional DA source: extract anchor hrefs, tolerant date
      parsing from link text with filename fallback, quarantine unparseable entries
- [x] `pdfplumber` parser producing validated Pydantic rows; group labels from positional
      extraction, blanks as NULL + `unavailable` flag, unit normalization
- [x] Commodity alias table + resolver; unmapped names quarantined, never guessed
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
- [ ] **The backfill runner must tolerate dead hrefs.** Three PDF links on Caraga's index
      return 404 (verified 2026-07-28). A 404 on one href is a skipped file, not a failed
      run — quarantine the href and carry on. The scraper does not fetch, so this belongs to
      whatever drives it. See KNOWLEDGE.md § "Caraga's directory layout is not trustworthy".
- [ ] **Recover the 81 year-less Caraga links (15% of the index).** Files like
      `Cabadbaran-City-Public-Market_July-22.pdf` carry no year in the filename or the link
      text. They are quarantined rather than guessed, because the only available year is the
      `FY####` directory and that contradicts the filename 12.6% of the time. The likely
      answer is the page's own grouping — the links sit under year/month headings — which
      means passing surrounding page context into the scraper, not just the anchor.
- [ ] ~~**Have the ingester pass `not_after=today` to `scrape_index`.**~~ **Superseded — do
      not do this.** The 2029-dated file contains `Date of Monitoring : July 19, 2026`, so
      the filename year is the typo and the PDF header is correct. Quarantining on the
      filename date would have thrown away a valid sheet. Instead: keep any `not_after`
      bound generous, and reconcile the index date against the parsed header date *after*
      fetching. A disagreement is worth recording, not worth discarding the file over.
- [ ] **Decide what to do when the index date and the sheet header date disagree.** The
      parser trusts the header. The mismatch is a real signal (a mislabelled file) and should
      probably be logged or counted on the data quality page rather than ignored.
- [ ] **Normalise province and market names before populating `regions`/`markets`.** The
      sheets write `Agusan del Norte` and `Agusan Del Norte` for the same province, and
      `Province of Dinagat Islands` for another. Matching on the raw string would create
      duplicate regions.
- [ ] **Give `is_agricultural_input` somewhere to live.** The parser flags feeds, fertiliser
      and pesticides so a food chart can exclude them, but `price_observations` has no column
      for it. Either derive it from `commodities.group` at query time or add a column —
      decide before the API filters on it.
- [ ] **Curate the 20 unseeded commodity triples.** The seed covers the 149 triples attested
      by 3+ of the 4 fixture sheets, giving 96.4% row resolution. The remaining 20 are almost
      all extraction artefacts of triples already seeded — `'pcs/kg) Male, Medium (12-14'` for
      `'Male, Medium (12-14 pcs/kg)'`, `'Habichuelas/Baguio Beans,'` truncated mid-name — and
      each needs a human to add an alias row pointing at the existing canonical commodity.
      They quarantine at stage `alias` until then, which is the intended behaviour.
      `Banana (Cardava)` is the one genuine judgement call: it is another name for
      `Banana (Saba)`, so it is a synonym rather than a new commodity.
- [ ] **Load the seed into the database.** `load_seed()` reads the committed CSVs and
      `CommodityResolver.from_seed()` builds a resolver from them, but nothing writes
      `commodities` or `commodity_aliases` rows yet. Belongs with the seed-data item above.
- [ ] **Regenerate the seed once the fixture corpus grows.** The attestation threshold is
      "more than half the sheets", so a corpus of ten will draw the line differently — and
      better — than a corpus of four.
- [ ] **Archive index snapshots.** The index page is deliberately not `fetch_once`d because it
      is mutable, so nothing keeps a copy of what the listing said on a given day. A
      content-addressed store keyed by hash alone — no URL index — would preserve the audit
      trail without breaking the never-re-fetch rule.
- [ ] **Surface `RawCache.verify()` on the data quality page.** It re-hashes every blob and
      reports corruption, but nothing calls it yet. It wants to be a scheduled check, not
      a method nobody runs.
- [ ] **Decide how `CacheConflictError` is handled by the ingester.** The cache refuses to
      overwrite a known URL whose bytes changed, which is the right default, but a run that
      hits it currently just fails. It should probably quarantine and continue.
- [x] **`scripts/with_temp_postgres.py`** — runs a command against a throwaway Postgres via
      the `pgserver` wheel, so schema work needs neither Neon nor Docker. Used to verify the
      migration applies, reverses, and does not drift from the models.
- [ ] **Seed data for `sources`, `regions`, and `commodities`.** The schema exists but is
      empty; the ingester cannot write an observation until Caraga has a `sources` row and
      its markets have `regions` rows. Probably an Alembic data migration or a seed command.
- [ ] **Wire `PRESYOWATCH_TEST_DATABASE_URL` into CI** (Phase 2) so `tests/db` stops being
      skipped there. Locally it runs via the helper script above.
