# PresyoWatch

A free, public data platform that ingests Philippine agricultural and commodity price
data daily, stores it as a time series, serves it through an API, and presents it as a
dashboard with anomaly detection and forecasting.

> **Status: ingestion, API and dashboard all built; API deployed.** A run scrapes a regional
> DA index, fetches each sheet at most once ever, parses it, and upserts observations
> idempotently; a FastAPI app serves them; a static Next.js dashboard charts them. See
> [`TASK.md`](TASK.md) for the build order and what is actually done.

## Running the ingester

```bash
python -m presyowatch seed        # load the committed reference data (idempotent)
python -m presyowatch backfill    # reconcile the last 14 days for the Caraga source
```

`backfill --lookback-days N` changes the window. Exit codes are `0` succeeded, `1` failed,
`2` partial — some files landed and some were quarantined. "Mostly worked" is a real
outcome and a scheduler should be able to see it.

Every run writes an `ingestion_runs` row, including when it fails, so a missed or broken run
is visible rather than absent. Anything that cannot be stored — a dead link, an unreadable
sheet, a commodity name with no alias — goes to the `quarantine` table with its reason and
enough of the raw payload to be reprocessed later. Nothing is dropped silently.

### What it will not do

- **Fetch a file twice.** Source bytes are cached permanently, addressed by SHA-256. A
  parser fix is applied by reparsing the cache, never by re-downloading history from a
  government server.
- **Guess a commodity.** Names are resolved by exact lookup against an explicit alias table.
  About 87% of parsed rows resolve today; the rest quarantine until a human maps them,
  because the heuristic that would merge `Well-Milled` into `Well Milled` would also merge
  feed-grade corn grits into food-grade.
- **Route around `robots.txt`.** A disallow ends the run rather than being worked around.

## The API

```bash
uv run uvicorn "presyowatch.api.app:create_app" --factory --reload
```

Interactive docs at `/docs`. The endpoints are `/health`, `/meta/sources`, `/meta/runs`,
`/regions`, `/markets`, `/commodities`, `/commodities/{slug}` and `/observations`, all
read-only and all paginated with a hard ceiling on page size.

Two things about the responses are deliberate and will surprise somebody:

- **Prices are JSON strings, not numbers.** They are exact decimals, and JSON numbers are
  IEEE-754 doubles: `52.80` arrives as `52.79999999999999716` in anything that parses them
  as floats, and ends up on a chart with fifteen decimal places. A string round-trips
  exactly and the client decides what to do with it.
- **`/meta/runs` is public.** Every ingestion run, including the failed ones. PLANNING.md
  treats observability as a feature rather than an afterthought, and a run that broke is
  more informative to a reader than one that worked.

### Deploying

[`render.yaml`](render.yaml) is a Render blueprint for the free web service plan. **The
free plan spins down after 15 minutes of inactivity and takes roughly 30–60 seconds to wake
up.** That is a real constraint, not a footnote: the dashboard has to render a skeleton for
a slow first request rather than a spinner that hangs forever. There is deliberately no cron
job pinging the service to keep it warm — free instance hours are finite, and defeating the
idling would be pretending the constraint is not there.

`DATABASE_URL` and `HTTP_USER_AGENT` are set in the Render dashboard and are never in this
repository.

## The dashboard

A static Next.js export in [`web/`](web/) — no server of its own, so it deploys free to
Vercel or Cloudflare Pages and the reader sees a painted page immediately while the sleeping
API wakes up behind a labelled loading state. See [`web/README.md`](web/README.md) for the
chart's design constraints, which are stricter than they look: the series palette is
validated for colour-blindness by script rather than chosen by eye, gaps in monitoring break
the line rather than being bridged, and nothing is ever averaged across markets.

## Automation

| Workflow | Trigger | What it does |
|---|---|---|
| [`ci.yml`](.github/workflows/ci.yml) | push, PR | ruff, `mypy --strict`, pytest against a real PostgreSQL service container, and `alembic check` to prove the models have not drifted from the migrations |
| [`ingest.yml`](.github/workflows/ingest.yml) | daily cron + manual | migrate, seed, reconcile a 14-day window |

The ingestion workflow pairs `schedule:` with `workflow_dispatch:` because a GitHub cron has
no timing SLA, is dropped under load, and is disabled outright after 60 days without a
commit. A run reconciles a window rather than a day, so a missed firing costs nothing.

It treats exit code 2 — *partial*, some files landed and some were quarantined — as a
success with a note, not a failed job. A source that publishes dead links produces that
outcome routinely, and a red X every morning trains everyone to ignore red X's. A real
failure still fails.

The raw source cache is restored from `actions/cache` between runs so rule 3 holds across
ephemeral runners. That is a stopgap: Actions caches are evicted after 7 days without a read.
PLANNING.md names Cloudflare R2 as the durable home.

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

### Fixtures

`tests/fixtures/pdf/` holds thirteen real monitoring sheets fetched from `caraga.da.gov.ph`
and committed byte-exact, plus the index page they were listed on. They were chosen for
what is wrong with them: a collapsed table row, two filenames that disagree with the date
printed inside the sheet, header labels the source truncated mid-word, a header block that
extracts one line per row, a doubled `.xlsx.pdf` extension, one sheet published twice under
two URLs, and one with seven columns that the parser refuses outright.

That last group matters more than the coverage does. Widening this corpus from four sheets
to twelve found two real parser bugs that had been filing commodities under the wrong groups
— caught by a cross-sheet agreement check rather than by anything anyone thought to assert.
Fixtures are real files, including the malformed ones, for exactly this reason.

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
