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
- [x] Idempotent upsert on the natural key; `Revised-` files update in place and append to
      `observation_revisions`
- [x] Backfill runner over a date range; `ingestion_runs` row per source per run
- [x] Fixture corpus: commit ~10 real PDFs including at least one revised file, one with
      missing values, and one that fails to parse
      — **13 committed, but no revised file.** See "no `Revised-` file is reachable" below.
- [x] Tests: parser unit tests against fixtures; **idempotency test** (run twice, assert
      identical DB state); revision-handling test

## Phase 2 — API + CI (target: 1 week)

- [x] FastAPI app: typed responses, pagination, filters for commodity / region / market /
      date range; `/health` and `/meta/sources` endpoints
      — plus `/meta/runs`, `/regions`, `/markets`, `/commodities`, `/commodities/{slug}`
- [x] OpenAPI docs served at `/docs` with real examples
      — examples copied from a live run, and a test validates them against their own schemas
      so the docs cannot quietly start lying
- [x] Pytest against a test database (Neon branch or dockerized Postgres)
      — a `postgres:17` service container in CI; the `engine` fixture *fails* rather than
      skips when `CI` is set, so a typo in the workflow environment cannot turn the suite
      green while testing nothing
- [x] GitHub Actions CI: ruff, mypy --strict, pytest on every PR
      — plus a third job proving the migration applies, does not drift from the models
      (`alembic check`), seeds twice without changing anything, and reverses
- [x] GitHub Actions ingestion workflow: `schedule:` + `workflow_dispatch:`, reconciles a
      14-day lookback window, writes an `ingestion_runs` row even on failure
- [ ] **Deploy API to Render.** `render.yaml` is written and the cold-start tradeoff is in
      the README, but the deploy itself needs an account: connect the repo to Render, set
      `DATABASE_URL` (a Neon connection string) and `HTTP_USER_AGENT` in the dashboard, then
      add the same two as repository secrets so the ingestion workflow can run. **Nothing
      else in Phase 2 is blocked on this.**

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
- [ ] **Record `robots_checked_at` and the robots verdict per source.** The columns exist and
      the seed leaves them null; `HttpClient.robots_decision()` returns everything needed and
      the backfill runner is the natural place to write it, once per run.
- [ ] **Decide what the ingester does with a source that requests a large `Crawl-delay`.**
      The client honours it in full and logs `large_crawl_delay_requested`; no caller acts
      on that yet, so a source asking for 300s would silently make a run very long.
- [ ] **Optional: a network-marked integration test** for the live hosts. Verified manually
      on 2026-07-28 (DA PDF refused, Caraga fetched, rate limiter observed); not automated,
      because CI should not depend on a government server being up.
- [x] **The backfill runner must tolerate dead hrefs.** Done: a 404 quarantines the href at
      stage `index` and the run continues, ending `partial` rather than `failed`.
- [ ] **Recover the 81 year-less Caraga links (15% of the index).** Files like
      `Cabadbaran-City-Public-Market_July-22.pdf` carry no year in the filename or the link
      text. They are quarantined rather than guessed, because the only available year is the
      `FY####` directory and that contradicts the filename 12.6% of the time. The likely
      answer is the page's own grouping — the links sit under year/month headings — which
      means passing surrounding page context into the scraper, not just the anchor.
- [x] ~~**Have the ingester pass `not_after=today` to `scrape_index`.**~~ **Superseded — done
      the other way.** The runner applies no `not_after` at index stage and checks the parsed
      header date against the run date instead. The 2029-dated file contains `Date of Monitoring : July 19, 2026`, so
      the filename year is the typo and the PDF header is correct. Quarantining on the
      filename date would have thrown away a valid sheet. Instead: keep any `not_after`
      bound generous, and reconcile the index date against the parsed header date *after*
      fetching. A disagreement is worth recording, not worth discarding the file over.
- [x] **Decide what to do when the index date and the sheet header date disagree.** Decided:
      the header wins, the disagreement is logged as `index_date_disagrees_with_sheet` and
      counted as `BackfillOutcome.date_mismatches`. Two of twelve fixtures disagree. Still to
      do: surface the count on the data quality page (Phase 4).
- [x] **Normalise province and market names before populating `regions`/`markets`.** Done in
      `presyowatch.places`: a normalised key drops a leading `Province of` and folds case.
      Provinces resolve against the seed or quarantine; markets are created on first sight and
      matched on the normalised triple.
- [ ] **Give `is_agricultural_input` somewhere to live.** The parser flags feeds, fertiliser
      and pesticides so a food chart can exclude them, but `price_observations` has no column
      for it. Either derive it from `commodities.group` at query time or add a column —
      decide before the API filters on it.
- [ ] **Curate the unseeded commodity triples — now 103, not 20.** The seed covers the 149 triples attested
      by 7+ of the 12 fixture sheets, giving 87.1% row resolution. Many are still extraction
      artefacts of triples already seeded — `'pcs/kg) Male, Medium (12-14'` for
      `'Male, Medium (12-14 pcs/kg)'`, `'Habichuelas/Baguio Beans,'` truncated mid-name — and
      each needs a human to add an alias row pointing at the existing canonical commodity.
      They quarantine at stage `alias` until then, which is the intended behaviour.
      `Banana (Cardava)` is one genuine judgement call: it is another name for `Banana (Saba)`,
      so it is a synonym rather than a new commodity. The wider corpus added a second, larger
      class of them — whole vocabularies that differ between markets and over time
      (`Well-Milled` for `Well Milled`, `Bangus` + spec `Large` for `Bangus, Large`,
      `Premium (yellow tag)`). See KNOWLEDGE.md § "The commodity vocabulary differs between
      markets and over time". These are the two low-resolution sheets and are worth curating
      first: it is roughly 30 alias rows for about 10 points of coverage.
- [x] **Load the seed into the database.** `presyowatch.db.seed.seed_reference_data` upserts
      sources, regions, commodities and aliases; `python -m presyowatch seed` runs it.
- [x] **Regenerate the seed once the fixture corpus grows.** Done at twelve sheets: threshold
      7 of 12, 143 triples seeded, 103 left for curation. Do it again next time the corpus
      grows.
- [ ] **Archive index snapshots.** The index page is deliberately not `fetch_once`d because it
      is mutable, so nothing keeps a copy of what the listing said on a given day. A
      content-addressed store keyed by hash alone — no URL index — would preserve the audit
      trail without breaking the never-re-fetch rule.
- [ ] **Surface `RawCache.verify()` on the data quality page.** It re-hashes every blob and
      reports corruption, but nothing calls it yet. It wants to be a scheduled check, not
      a method nobody runs.
- [x] **Decide how `CacheConflictError` is handled by the ingester.** Decided: quarantine the
      file at stage `index` with the two hashes in the reason, and continue. Untested against a
      real occurrence, because no source has done it yet.
- [x] **`scripts/with_temp_postgres.py`** — runs a command against a throwaway Postgres via
      the `pgserver` wheel, so schema work needs neither Neon nor Docker. Used to verify the
      migration applies, reverses, and does not drift from the models.
- [x] **Seed data for `sources`, `regions`, and `commodities`.** Committed as CSVs under
      `presyowatch/data`, loaded by `python -m presyowatch seed`. A seed command rather than a
      data migration, because the commodity seed is regenerated whenever the corpus grows.
- [ ] **Wire `PRESYOWATCH_TEST_DATABASE_URL` into CI** (Phase 2) so `tests/db` stops being
      skipped there. Locally it runs via the helper script above.
- [ ] **The backfill runner must ingest a date's files in index order.** `upsert_observations`
      is last-write-wins by design — it compares figures, not filenames — so feeding it a
      `Revised-` sheet *before* the original would record the original as the correction.
      Nothing in the upsert can tell which of two files was published first; the runner has to
      preserve the index's order.
- [ ] **Quarantine `UpsertOutcome.conflicts`.** When two raw rows on one sheet resolve to the
      same commodity with different figures, neither is written and the pair comes back with a
      ready-made `reason`. Nothing writes them to `quarantine` (stage `validate`) yet, so today
      they would be dropped silently — which is exactly what CLAUDE.md forbids. Belongs with
      the backfill runner.
- [ ] **Reconsider the revision model if a source ever revises a *deletion*.** A commodity that
      vanishes from a corrected sheet is currently invisible: the upsert only ever sees rows
      that are present, so the stale observation stays as published. Publishing a row as
      `unavailable` is handled; removing it entirely is not, and it is not yet known whether
      the DA does that.
- [ ] **No `Revised-` file is reachable, so end-to-end revision handling is untested.** The
      `Revised-` files are published only by `www.da.gov.ph`, whose robots.txt disallows every
      PDF on the host; Caraga's 535-link index contains none (checked 2026-07-28). Revision
      handling *is* tested at the schema level in `tests/db/test_upsert.py`, but nothing
      exercises it through the runner on a real pair of files. **This needs a human to
      download one `Revised-*.pdf` by hand** — permitted, since robots.txt governs automated
      access — and commit it beside its original. Until then the corpus has the byte-identical
      San Francisco republication, which proves the *non*-correction path only.
- [ ] **Support the seven-column sheet, or decide not to.** One committed fixture omits the
      `Specifications` column entirely and is quarantined whole. Supporting it means deciding
      what identifies a commodity when a third of the triple is missing — probably a
      per-source flag saying the specification is absent rather than empty. Quantify first:
      four Libertad sheets were sampled and only one was seven-column, so this may be rare.
- [ ] **Verify the PSGC codes by hand.** `data/regions.csv` is the only thing in this project
      not confirmed by direct fetch; `psa.gov.ph` returns 403 to our client and spoofing a
      browser User-Agent is not something we do. See KNOWLEDGE.md § "PSGC codes are the one
      thing here not verified by fetch".
- [ ] **`markets` needs a normalised uniqueness constraint.** `resolve_market` matches on
      normalised names, but the unique constraint is on the raw columns, so two processes
      racing on a market's first sighting could still insert `Butuan City` and `butuan city`.
      Single-flight ingestion makes this theoretical today. A generated column plus a unique
      index on it would make the database enforce what the code intends.
- [ ] **Nothing reprocesses quarantine.** Rows are written with enough raw payload to be
      replayed after a parser or alias fix, which was the point, but there is no command that
      replays them. Roughly 240 rows per run currently sit there awaiting curation.
- [ ] **Quarantine re-records the same undatable links on every run.** The 81 year-less
      Caraga hrefs are quarantined afresh each time the index is read, so the table grows by
      about 84 rows a day for a fixed, known problem. Either key index-stage quarantine on
      `(source_id, source_url)` and upsert it, or record a `last_seen_at` instead of a new
      row. Worth settling before the data quality page counts these (Phase 4).
- [ ] **The raw cache on GitHub Actions is a stopgap.** `ingest.yml` restores it with
      `actions/cache`, which is evicted after 7 days without a read and capped at 10 GB per
      repository — so the permanent archive rule 3 promises is not actually permanent there.
      PLANNING.md names Cloudflare R2; wire it up before the archive matters.
- [ ] **`/observations` pages with `LIMIT`/`OFFSET` and counts with a second query.** Both
      are fine at today's size and both degrade on a large time series: a deep offset scans
      everything it skips, and `COUNT` over a filtered join is not free. Keyset pagination on
      `(observed_on, id)` is the fix when it starts to matter, and it is a breaking change to
      the response shape, so decide before anyone depends on `offset`.
