# PLANNING.md — Architecture & Design

## Goal

Ingest Philippine commodity price data daily, without human intervention, and expose it
as an API and dashboard with anomaly detection and forecasting. Must run at zero cost.

## Architecture

```
  Scheduled ingestion (GitHub Actions cron)
      |
      |-- PSA OpenSTAT  (PxWeb JSON API, CC BY 4.0)   <- backbone: CPI / inflation
      |-- Regional DA sites (text-extractable PDFs)    <- detail: per-market prices
      |-- DOE Oil Monitor (weekly PDFs)                <- optional third source
      |
      v
  Raw object cache (content-addressed by SHA-256)
      |
      v
  Parse -> validate (Pydantic) -> idempotent upsert
      |
      v
  Postgres (Neon)  --  price_observations, revisions, ingestion_runs, quarantine
      |
      +---> FastAPI on Render        ---> Next.js dashboard on Vercel / Cloudflare Pages
      +---> Analytics jobs (anomaly detection, forecast backtest)
```

## Stack

| Layer | Choice | Why |
|---|---|---|
| Ingestion | Python 3.12, httpx, pdfplumber | PDFs are text-based; no OCR needed |
| Scheduler | GitHub Actions cron | Free for public repos, doubles as CI |
| Database | Neon Postgres (free tier) | Scale-to-zero, resumes on query, branching for CI |
| API | FastAPI on Render (free web service) | Postgres + web service + static, no card |
| Frontend | Next.js on Vercel or Cloudflare Pages | Genuinely free, no expiring credits |
| Charts | Recharts | Simple, no license cost |
| Object cache | Repo-committed fixtures + Cloudflare R2 free tier | Never re-fetch a source file |

Do **not** use Fly.io or Heroku — neither has a free tier anymore.

## Schema (first cut)

- `sources` — id, name, base_url, robots_checked_at, license, attribution_text
- `regions` — psgc_code, name, level
- `markets` — id, region_id, name, municipality
- `commodities` — id, group, name, specification, unit, canonical_slug
- `price_observations` — source_id, market_id, commodity_id, observed_on, low, high,
  prevailing, average, revision_no, source_file_sha256, ingested_at
  - natural key: (source_id, market_id, commodity_id, observed_on)
- `observation_revisions` — append-only history of superseded values
- `ingestion_runs` — run_id, source_id, started_at, finished_at, status, files_seen,
  rows_upserted, rows_quarantined, error
- `quarantine` — raw row payload, source_file, reason, created_at

Commodity names vary across regions and over time. Maintain an explicit alias table
mapping raw strings to `canonical_slug`. Unmapped names go to quarantine, never guessed.

## Design principles

1. **Idempotent by construction.** Re-running the ingester over the same date range must
   produce byte-identical database state. This is testable — write that test.
2. **Fail loudly, degrade gracefully.** One broken source must not stop the others. Each
   source runs in its own transaction and records its own run row.
3. **Reparse from cache, never from the network.** The network is the slow, rude,
   unreliable part. Touch it once per file, ever.
4. **Observability is a feature, not an afterthought.** The data quality page is public
   and shows ingestion success rate, null rate per commodity, quarantine count, and
   last-successful-run per source. This is the single highest-signal thing to a reviewer.
5. **Honest analytics.** Every forecast ships with a backtest against a naive seasonal
   baseline and the comparison is displayed. If the model loses, say so on the site.

## Analytics decisions

- **Anomaly detection**: rolling median + MAD, not mean + standard deviation. Price spikes
  are precisely the outliers that corrupt a mean-based threshold. Window and threshold are
  config, not magic numbers.
- **Forecasting**: seasonal ETS (statsmodels) with a walk-forward backtest, evaluated by
  MAE against a seasonal-naive baseline. Prophet is out — too heavy for the free tier and
  not better here.
- **Missing data**: never interpolate silently. Gaps stay gaps in storage; any smoothing
  happens at the presentation layer and is labelled.

## Deployment constraints to design around

- Render free web services cold-start. The frontend must handle a slow first request
  gracefully — skeleton states, not spinners that hang forever. Document the tradeoff in
  the README rather than hiding it.
- Neon free-tier compute suspends when idle; the daily cron keeps it warm.
- GitHub Actions scheduled workflows have no timing SLA, enforce a 5-minute minimum
  interval, only fire from the default branch, and are auto-disabled after 60 days without
  a commit. Design: pair `schedule:` with `workflow_dispatch:`, and make every run
  reconcile a lookback window rather than assuming it fires once per day.

## Out of scope (for now)

User accounts, alerting/notifications, mobile app, any paid infrastructure, scraping
private retailer websites. Revisit only after Phases 1–3 are live.
