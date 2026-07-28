# CLAUDE.md — PresyoWatch

Read `PLANNING.md` (architecture + absolute rules), `TASK.md` (current work), and
`KNOWLEDGE.md` (verified facts about the data sources) before doing anything.

## What this project is

A public, free-to-run data platform that ingests Philippine agricultural and commodity
price data every day, stores it as a time series, serves it through an API, and presents
it as a dashboard with anomaly detection and forecasting.

This is a portfolio project. Its purpose is to demonstrate production engineering
judgment, not feature count. A smaller system that is deployed, monitored, tested, and
honest about its failures beats a larger one that only runs locally.

## Non-negotiable rules

1. **Never construct source URLs from a date pattern.** Scrape the index page's anchor
   hrefs and parse dates from link text, with the filename as fallback. See
   KNOWLEDGE.md § "URL naming is inconsistent" for the evidence.
2. **Check `robots.txt` per host before automating any fetch, and obey it.**
   `www.da.gov.ph` disallows automated access to its uploads; regional subdomains do not.
   Different hosts, different rules. Never route around a disallow.
3. **Cache every raw source file permanently before parsing.** Hash it, store the bytes,
   and never re-fetch a file already seen. Parser bugs get fixed by reparsing the cache,
   never by re-downloading history from a government server.
4. **All writes to `price_observations` are idempotent upserts** on a composite natural
   key. Sources republish corrected figures under a `Revised-` prefix; corrections must
   update the row and append to a revision history table, never duplicate.
5. **The ingester backfills.** Each run reconciles all missing dates in a lookback
   window. Never assume one run equals one day — scheduled runs get delayed and skipped.
6. **No secrets in the repo.** Everything through env vars, `.env.example` checked in
   with placeholder values only.
7. **Set a descriptive User-Agent** with a contact email on every outbound request, and
   rate-limit to at most one request per second per host.
8. **Attribute data sources prominently** in the UI and README. PSA data is CC BY 4.0.

## Definition of done for any task

- Tests pass, types check, linter clean
- The change is covered by a test that would fail without it
- Errors are handled explicitly — no bare `except:`, no swallowed exceptions
- Anything user-visible is reflected in the README
- Committed with a message explaining *why*, not *what*

## Style

- Python: 3.12+, `ruff` for lint and format, `mypy --strict` on `src/`
- Typed function signatures everywhere. Pydantic for anything crossing a boundary.
- Structured logging (JSON) with a run ID, never bare `print()`
- Small pure functions for parsing; I/O isolated at the edges so parsers are unit-testable
  against fixture files committed to the repo

## Git
- Commit freely; group changes into logical units with why-focused messages.
- Never push without being asked. Never force-push. Never rewrite published history.
- Never commit .env, credentials, or anything under data/cache/.
- Run the full test suite before any commit that touches src/.

## What not to do

- Do not add a feature that is not in `TASK.md`. Propose it there first.
- Do not mock the data sources in a way that hides real-world messiness. Fixtures must be
  real files pulled from the actual sources, including the malformed ones.
- Do not silently drop rows that fail validation. Quarantine them in a table with the
  reason, and surface the count on the data quality page.
