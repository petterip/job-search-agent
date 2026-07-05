# Job Search Agent

Research, regression tests, and product planning for a local Finnish job-listing portal — without private API contracts for the initial sources.

## Structure

See also [`docs/README.md`](docs/README.md) for the documentation index.
Current implemented state is tracked in [`docs/implementation-journal.md`](docs/implementation-journal.md).

```
docs/
  goal.md                  # Product goal, MVP scope, and success criteria
  tyonhaku-rajapinnat.md   # Single source of truth (APIs, scrape strategy, curl examples)
  tech-stack-plan.md       # Recommended Docker Compose product stack and implementation plan
  implementation-journal.md # What is actually implemented and verified
  sources.yaml             # Machine-readable source registry for collectors
scrape-test/
  regression_test.py       # Regression gate — run this in CI
  lib/                     # Shared HTTP helpers (stdlib)
  01–10                    # Exploratory / manual test scripts
```

## Quick start

```bash
# Regression gate (stdlib only, needs network)
make test-regression

# Quick smoke tests
make test-smoke
make test-eures
```

Optional browser/scraper scripts:

```bash
pip install -r requirements-dev.txt
playwright install chromium
python3 scrape-test/06_playwright_testi.py
```

## Local app skeleton

The Docker Compose scaffold uses non-80 host ports by default:

- Web portal: `http://127.0.0.1:3080`
- API health: `http://127.0.0.1:8008/health`

```bash
# Validate Compose
make docker-config

# Start the local stack
docker compose up --build

# Collect the first Duunitori page into PostgreSQL
docker compose exec api python -m app.collect duunitori --page-size 20

# Load the private single-profile YAML into PostgreSQL without printing profile contents
docker compose exec api python -m app.profile /path/in/container/profile.yaml --name default

# Refresh deterministic recommendations immediately
docker compose exec api python -m app.match --max-jobs 1000

# Audit normalized database consistency and recent source errors
make audit-db

# Create and restore PostgreSQL backups
make backup-db
make restore-db BACKUP=backups/jobsearchagent-YYYYMMDDTHHMMSSZ.dump
```

If `LLM_PROVIDER=openai` and `OPENAI_API_KEY` are configured, matching also evaluates the top candidates with OpenAI structured outputs and stores the result in `llm_evaluations`. Provider quota or API failures are logged and counted, but deterministic recommendations remain available.

The default bind address is `127.0.0.1` because the app will contain private profile and recommendation data. Change `WEB_BIND`, `API_BIND`, `WEB_PORT`, or `API_PORT` in `.env` only when intentionally exposing services beyond the local machine.

Use `make docker-config` rather than raw `docker compose config`; the raw command expands `.env` and may print local secrets.

The current backend exposes:

- `GET /health`
- `GET /jobs?limit=20&offset=0`
- `GET /jobs/{job_id}`
- `GET /sources`
- `GET /sources/status`
- `GET /recommendations`

Scheduled collection defaults to all verified non-blocked harvesters:

```env
COLLECTOR_DAILY_HOUR=16
COLLECTOR_DAILY_MINUTE=0
MATCHER_DAILY_HOUR=16
MATCHER_DAILY_MINUTE=0
COLLECTOR_ENABLED_SOURCES=duunitori,tmt,tmt_oulu,laura,jobly,eures_fi,kuntarekry,kirkkorekry,oulu_varbi
```

Remove sources from `COLLECTOR_ENABLED_SOURCES` only when intentionally narrowing the automatic schedule.
The worker uses `Europe/Helsinki` time; by default collection and matching/LLM evaluation each run once daily at 16:00.

## Recommended collection stack

The worker runs all verified non-blocked sources by default. Duunitori, Työmarkkinatori, and Laura remain the broadest MVP-grade sources; Jobly, EURES, TMT Oulu, Kuntarekry, Kirkkorekry, and Oulu Varbi add supplementary coverage.

1. **Duunitori** — `GET /api/v1/jobentries` (~17k listings, full text in `descr`)
2. **Työmarkkinatori** — `POST .../search/v2/search` + `LATEST` (~11k, official data)
3. **Laura** — WordPress REST `job-listings` (~12k)
4. **Jobly** — sitemap pages 1+2 + JSON-LD per URL (~14k)
5. **EURES** — supplementary; do not trust `MOST_RECENT` for incrementals

Dedup across sources is required. See `docs/tyonhaku-rajapinnat.md` §12.

## Recommended runtime stack

For the first single-user portal implementation, use the Docker Compose product stack described in [`docs/tech-stack-plan.md`](docs/tech-stack-plan.md):

- Next.js + TypeScript portal
- FastAPI backend/API
- Python worker with APScheduler and a persistent PostgreSQL job store
- PostgreSQL + pgvector for normalized data, raw JSONB payloads, full-text search, and embeddings
- Docker volumes for database data and local CV/source artifacts

Do not start with SQLite, Redis, Celery, OpenSearch, or a separate vector database unless measured needs appear.

## Status

See [`docs/implementation-journal.md`](docs/implementation-journal.md) for the up-to-date implementation record.
