# PresyoWatch

A free, public data platform that ingests Philippine agricultural and commodity price
data daily, stores it as a time series, serves it through an API, and presents it as a
dashboard with anomaly detection and forecasting.

> **Status: scaffold only.** No ingestion, API, or dashboard yet. See
> [`TASK.md`](TASK.md) for the build order and what is actually done.

## Development

Dependencies are managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync                        # create .venv and install everything, incl. dev tools
cp .env.example .env           # then fill in real values — never commit .env
uv run pre-commit install      # enable the pre-commit hooks

uv run ruff check .            # lint
uv run ruff format .           # format
uv run mypy                    # type check (strict, per pyproject.toml)
uv run pytest                  # tests
```

### Database

Migrations are Alembic and take their target from `DATABASE_URL` — no connection string is
committed. Schema tests are skipped unless `PRESYOWATCH_TEST_DATABASE_URL` is set.

Neither Neon nor Docker is needed to work on the schema. `pgserver` ships real PostgreSQL
binaries as a wheel, so a disposable server can be started on Linux, macOS, or Windows:

```bash
# Apply the migration, confirm the models have not drifted from it, run the schema tests,
# and prove the migration reverses.
uv run --with pgserver python scripts/with_temp_postgres.py \
    alembic upgrade head -- alembic check -- pytest tests/db -q -- alembic downgrade base
```

`alembic check` is the important one: it fails if the models and the migration disagree.
The schema tests build their database by *running the migration* rather than by
`metadata.create_all`, so they assert against the schema that actually ships.

## Documentation

| File | Contents |
|---|---|
| [`PLANNING.md`](PLANNING.md) | Architecture, stack rationale, schema, design principles |
| [`TASK.md`](TASK.md) | Phased task list — the single source of scope |
| [`KNOWLEDGE.md`](KNOWLEDGE.md) | Verified facts about the data sources; do not contradict without re-verifying |
| [`CLAUDE.md`](CLAUDE.md) | Non-negotiable engineering rules for this repo |

## Data sources and attribution

This project redistributes public data published by agencies of the Government of the
Philippines. Full attribution is a condition of use, not a courtesy.

- **Philippine Statistics Authority (PSA)** — [OpenStat](https://openstat.psa.gov.ph/).
  Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- **Department of Agriculture (DA)** — Bantay Presyo price monitoring, published by the
  national portal and regional field offices.

Automated collection respects `robots.txt` on a per-host basis. `www.da.gov.ph`
disallows every PDF on the host (`Disallow: /*.pdf$`), so national PDFs are **not**
fetched automatically, wherever they are served from. See [`KNOWLEDGE.md`](KNOWLEDGE.md) for the verified position on
each host and the legal basis under RA 8293 §§ 175–176.

Prices shown are as published by the source agency. This project does not correct,
smooth, or interpolate source data; gaps are shown as gaps.

## License

Code is MIT licensed. Data carries the license of its originating agency, above.
