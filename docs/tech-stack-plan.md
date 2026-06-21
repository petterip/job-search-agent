# Tech Stack Plan

**Updated:** 2026-06-20  
**Goal:** choose a Docker Compose based stack for a polished Finnish job portal with automated collection, deduplication, search, one-job-seeker recommendations, deterministic matching, and LLM-assisted ranking.

---

## Re-Evaluation Summary

The earlier recommendation was a single Python/FastAPI container with SQLite, FTS5, and APScheduler. That was a good fit for a small collector and local dataset browser.

The goal has changed. The target product is now:

- a beautiful, professional web portal
- running on a local server with Docker Compose
- showing both all new jobs and personalized recommendations
- using one job seeker's CV, profile, location, and preferences
- combining deterministic filtering, machine scoring, embeddings/semantic matching, and LLM scoring
- running the data pipeline automatically without manual triage

Given those requirements, SQLite and a single app process are no longer the best default. They would still work for a prototype, but they would force an early migration once recommendation scoring, embeddings, structured LLM outputs, portal views, and background jobs become central.

## Recommendation

**Use Option B: Docker Compose with Next.js, FastAPI, PostgreSQL + pgvector, and a Python worker/scheduler.**

**Build-readiness verdict:** the stack decision is ready; the implementation plan needs the hardening below before coding starts. In particular, the first build slice must prove one source end to end before adding every adapter, the Compose defaults must protect private profile data, and schema/API contracts must be explicit enough that later portal work does not force a rewrite.

Recommended MVP stack:

| Layer | Choice | Rationale |
|---|---|---|
| Frontend portal | Next.js + TypeScript | Best fit for a polished responsive portal, detail pages, dashboard views, and Docker standalone deployment. |
| Styling/UI | Tailwind CSS + component primitives | Fast path to a professional, consistent operational UI without a large design system. |
| Backend/API | FastAPI + Pydantic | Fits existing Python source probes and provides a typed API for portal, collector status, jobs, and recommendations. |
| Worker | Python worker service, same codebase as API | Keeps collection, normalization, dedup, matching, embeddings, and LLM scoring out of the web request path. |
| Scheduler | APScheduler inside the worker with SQLAlchemyJobStore | Good enough for one local server; persists scheduled jobs in PostgreSQL without Redis/Celery. |
| Database | PostgreSQL | Better default now because the app needs JSONB raw payloads, full-text search, durable state, worker coordination, and future growth. |
| Vector search | pgvector | Store job/profile embeddings in PostgreSQL and avoid a separate vector database. |
| Text search | PostgreSQL full-text search | Good enough for title/description search without OpenSearch. |
| LLM provider | Provider abstraction: OpenAI first, Ollama optional later | Hosted LLMs can provide strong structured scoring; local models can be evaluated for privacy/cost after the hosted path works. See [`llm-hosted.md`](llm-hosted.md). |
| Migrations | Alembic | Standard migration path for SQLAlchemy-based Python apps; needed for ordered `CREATE EXTENSION vector` and schema changes. |
| Files | Docker volume for uploaded CV/source artifacts | Keep original CV files and derived text under controlled local storage. |
| Deployment | Docker Compose | One local-server stack with web, api, worker, database, and optional model service. |
| Testing | pytest + Playwright + existing regression gate | Covers adapters, matching logic, API behavior, and portal UI. |

## Compose Shape

The MVP should use separate services even if some share the same image:

```yaml
x-backend: &backend
  image: job-search-agent-backend:${APP_VERSION:-local}
  env_file: .env
  depends_on:
    db:
      condition: service_healthy
  volumes:
    - app_storage:/storage

services:
  web:
    build: ./web
    env_file: .env
    depends_on:
      - api
    ports:
      - "${WEB_BIND:-127.0.0.1}:${WEB_PORT:-3080}:3000"

  api:
    <<: *backend
    build: ./backend
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    ports:
      - "${API_BIND:-127.0.0.1}:${API_PORT:-8008}:8000"

  worker:
    <<: *backend
    command: python -m app.worker

  db:
    image: pgvector/pgvector:pg18
    env_file: .env
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-jobsearchagent}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-change-me}
      POSTGRES_DB: ${POSTGRES_DB:-jobsearchagent}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5
    volumes:
      - postgres_data:/var/lib/postgresql

volumes:
  postgres_data:
  app_storage:
```

The `pgvector/pgvector:pg18` tag is intentionally pinned to a PostgreSQL major version and is an official pgvector Docker tag. PostgreSQL 18 images should mount the database volume at `/var/lib/postgresql`, not `/var/lib/postgresql/data`, so major-version subdirectories work correctly. If the project standardizes on PostgreSQL 17 or 16 instead, change the image tag deliberately and document the reason. For maximum reproducibility on the local server, the release deployment may pin a full pgvector tag such as `0.8.3-pg18` after verifying compatibility.

Credentials in `.env.example` must be development defaults only; local-server deployments should set real values in `.env`. Compose interpolation uses the project `.env` file or shell environment, so required variables must be present there, not only in `env_file`.

The default Compose port bindings should be loopback-only (`127.0.0.1`) because the stack contains a private job seeker profile and LLM-derived recommendation history. The default host ports are `WEB_PORT=3080` and `API_PORT=8008`, intentionally avoiding port 80. Set `WEB_BIND=0.0.0.0` and, only if needed, `API_BIND=0.0.0.0` deliberately for LAN access. Public internet exposure requires a separate HTTPS, authentication, source-terms, and privacy review.

The API service builds `job-search-agent-backend:${APP_VERSION:-local}` and the worker service runs the same image with a different command. This keeps API and worker dependencies identical and avoids drift between two backend builds.

Because the worker uses the shared backend image without its own `build:` block, `docker compose up --build` should be the normal startup path on a clean host. Running `docker compose up worker` alone before the API image has been built can fail with an image-not-found error; build `api` or the backend image first if starting the worker standalone.

Next.js needs two API base URLs. Server-side code inside the Docker network should use an internal URL such as `http://api:8000`; browser-side code should use the host-visible public URL such as `http://localhost:8008`. `.env.example` should document these separately, for example `INTERNAL_API_BASE_URL=http://api:8000` and `NEXT_PUBLIC_API_BASE_URL=http://localhost:8008`.

The web app must not call privileged operator endpoints directly from public browser code unless an authentication layer exists. For the MVP, keep operator actions local-only and avoid exposing mutation endpoints beyond the local machine.

Add later only when needed:

- `ollama` for local LLM/embedding experiments.
- `caddy` or another reverse proxy when exposing the portal beyond LAN.
- `redis` only if real queueing/retry visibility becomes necessary.
- `minio` only if local object storage becomes more useful than a plain volume.

## Why This Replaces SQLite-First

PostgreSQL is the better default for the updated goal:

- JSONB is a natural fit for raw source payloads and normalized extracted fields.
- Full-text search is built in and can rank natural-language job documents.
- pgvector lets the project store embeddings in the same database as jobs, profiles, and recommendation records.
- Worker coordination can use database state, row locks, or advisory locks without adding Redis.
- Backups are standard and predictable for a server-running Docker Compose stack.
- Moving to multiple users later is easier than migrating away from a SQLite-shaped data model.

SQLite remains useful for small experiments, but it should no longer be the main product database.

## LLM and Matching Architecture

The matching system should be staged so expensive and privacy-sensitive LLM calls happen only after cheaper filters:

1. Collect and normalize source listings.
2. Deduplicate listings into canonical jobs.
3. Extract structured fields such as skills, language, location, seniority, role type, and deadline.
4. Apply hard deterministic filters from the single job seeker profile.
5. Compute machine scores from structured fields and preference weights.
6. Generate or update embeddings for jobs and profile/CV text.
7. Use vector similarity as one signal, not the only ranking mechanism.
8. Send only the strongest candidate jobs to the LLM.
9. Store structured LLM output: score, rationale, concerns, suggested action, model, prompt version, and timestamp.
10. Publish recommendations into the portal.

The LLM provider should be replaceable. The first implementation can use OpenAI structured outputs for reliable JSON scoring, but that is a conscious privacy trade-off: CV-derived profile data and selected job facts may leave the local server unless the provider is configured to a local model. Hosted LLM calls must be opt-in through environment configuration; a missing provider/API key should disable LLM scoring rather than silently sending data elsewhere. The matching prompt must send the minimum useful profile summary, not the full raw CV, unless explicitly enabled. Ollama can be added as an optional local provider for embeddings or scoring experiments when privacy or cost requires it.

Embedding dimensions are a schema commitment. The MVP should pick one embedding model and one vector size before creating `job_embeddings` and `profile_embeddings`; for example, OpenAI `text-embedding-3-small` uses 1536 dimensions, while common local models may use different sizes. Changing embedding model/dimension is an Alembic migration plus full re-embed of jobs and profiles, not a configuration-only change.

## Scheduling and Durability

The worker should use APScheduler with `SQLAlchemyJobStore` backed by PostgreSQL, not the default in-memory job store. This keeps scheduled jobs visible and restart-tolerant while avoiding Redis/Celery for the MVP.

The scheduler job store is not a substitute for pipeline state. Each collection and matching execution must still create durable `source_runs`, `pipeline_runs`, and error records. On startup, the worker should reconcile missed or incomplete runs from database state before scheduling the next interval.

Worker execution must also prevent overlapping runs. Use a database-backed run lease, advisory lock, or equivalent `pipeline_runs` state transition so a manual trigger and a scheduled trigger cannot collect the same source concurrently. A stuck run must become visible as `failed` or `abandoned` after a timeout rather than blocking future work forever.

## Minimal Data Model

| Table | Purpose |
|---|---|
| `sources` | Source registry: Duunitori, TMT, Laura, Jobly, EURES. |
| `source_runs` | Every poll run, status, timing, counts, and error summary. |
| `raw_listings` | Source payload JSONB, source URL, external ID, content hash. |
| `jobs` | Canonical normalized listing: title, employer, description, dates, status. |
| `job_sources` | Source-specific occurrences linked to one canonical job, including per-source attribution text. |
| `job_fts` or generated `tsvector` | Full-text search document for title, employer, and description. |
| `job_embeddings` | Vector representation of job content for semantic matching, using the fixed MVP embedding dimension. |
| `job_seeker_profiles` | Exactly one MVP profile: CV text, location, preferences, exclusions. |
| `profile_embeddings` | Vector representation of CV/profile/preference text, using the same fixed embedding dimension. |
| `recommendations` | Deterministic score, vector score, LLM score, rank, rationale, concerns. |
| `pipeline_runs` | End-to-end collector/matcher run records. |
| `llm_evaluations` | Raw structured LLM request/response metadata for audit/debugging. |
| `apscheduler_jobs` | Persistent APScheduler job records managed by SQLAlchemyJobStore. |

### Data Model Invariants

These rules should be encoded in migrations, constraints, or tests rather than left to adapter convention:

- `sources.name` is unique and seeded from [`sources.yaml`](sources.yaml).
- `raw_listings` is unique on `(source_id, external_id)` when an external ID exists, otherwise `(source_id, canonical_source_url)`.
- `raw_listings.content_hash` changes only when the source payload relevant to normalization changes.
- `jobs.status` is an enum-like value: `active`, `expired`, `removed`, `superseded`.
- `job_sources` keeps source-specific attribution, application URL, external ID, first/last seen timestamps, and last content hash.
- TMT `job_sources.attribution` must preserve `Lähde: Työmarkkinatorin asiakastietojärjestelmä`.
- `recommendations` stores deterministic filter result, machine score, vector score, active LLM evaluation ID, rank, and explanation fields used by the portal.
- `llm_evaluations` stores request hash and model metadata so unchanged job/profile inputs do not trigger repeat paid evaluations.
- Profile-derived text, embeddings, recommendations, and LLM evaluation payloads must be deletable together.

## API Shape

Keep the API boring and explicit:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Service health. |
| `GET /jobs` | Paginated all-jobs feed with filters and full-text search. |
| `GET /jobs/{id}` | Job detail with source occurrences and attribution. |
| `GET /recommendations` | Ranked recommendations for the single profile. |
| `GET /recommendations/{id}` | Recommendation detail with scoring explanation. |
| `GET /profile` / `PUT /profile` | Single job seeker profile and preferences. |
| `GET /sources/status` | Source health, last run, counts, and failures. |
| `POST /pipeline/run` | Manual operator trigger for collection + matching. |

The portal should call the API rather than read the database directly.

For the MVP, treat the API as local-private. If any API route is exposed on a LAN, mutation routes such as `PUT /profile` and `POST /pipeline/run` need at least a shared operator token before exposure. Public exposure is out of scope.

### API Contract Rules

- List endpoints must be paginated from the first implementation; default page size should be conservative.
- API responses should use stable Pydantic response models rather than leaking ORM objects.
- Dates should be timezone-aware ISO 8601 strings.
- IDs exposed to the portal should be stable internal UUIDs or opaque strings, not database row assumptions.
- Error responses should be structured enough for the portal to distinguish validation errors, source failures, and unavailable recommendation data.

## Options Considered

### Option A - FastAPI + SQLite + APScheduler

**Assessment:** no longer recommended as the main product stack.

Good for a collector prototype, but weak for the updated goal because recommendations, embeddings, LLM audit records, and a professional portal benefit from PostgreSQL and service separation.

### Option B - Next.js + FastAPI + PostgreSQL + pgvector + Python Worker

**Assessment:** recommended.

This is the smallest stack that fits the real target product without immediately adding queue infrastructure or a separate search/vector database.

### Option B2 - Vite + React SPA + FastAPI Static Hosting

**Assessment:** viable lighter alternative, not the primary recommendation.

A Vite React SPA would reduce Compose complexity by removing the separate web runtime service: FastAPI could serve a static bundle, and the portal would still call the same API. That is attractive for a single-user LAN app. The trade-off is that Next.js gives stronger routing conventions, production Docker standalone support, and a better path if the portal grows into richer pages. Keep Next.js as the product default, but revisit Vite if frontend complexity starts to dominate the project.

### Option C - FastAPI Templates + React Admin Only

**Assessment:** useful for internal tools, not enough for the desired portal.

An admin-first UI would help operations, but the product goal needs a polished candidate-facing portal with recommendations and all-new-job browsing.

### Option D - Django + PostgreSQL

**Assessment:** viable but not preferred.

Django Admin is attractive for operations, but the existing code and source probes are already plain Python scripts, and the portal/API split is cleaner with FastAPI + Next.js. Choose Django only if built-in admin becomes the dominant requirement.

### Option E - PostgreSQL + Redis + Celery + OpenSearch

**Assessment:** too heavy for MVP.

Keep this as the scale path. Add Redis/Celery when retry queues, concurrent workers, or operational queue visibility become real needs. Add OpenSearch only if PostgreSQL full-text search is measured and found insufficient.

## Decision

**Adopt Option B for implementation.**

Build the MVP as a Docker Compose application with:

- `web`: Next.js portal
- `api`: FastAPI service
- `worker`: Python collection/matching scheduler
- `db`: PostgreSQL with pgvector
- `app_storage`: local Docker volume for CV and source artifacts

Do not start with SQLite, Redis, Celery, OpenSearch, or a separate vector database.

## Implementation Plan

The plan below is now a build plan, not only a stack comparison. It is intentionally sliced so each phase leaves the product runnable and reviewable. A task is not complete until its verification commands and manual checks pass.

### Phase 0 - Build Readiness

#### Task 0: Project Scaffolding Contract

**Description:** Establish the repository conventions needed before application code starts.

**Acceptance criteria:**

- [ ] `.env.example` exists and contains development-safe placeholders only.
- [ ] `.env` is ignored and documented in `AGENTS.md`.
- [ ] Backend dependency management is chosen (`pyproject.toml` preferred) and includes FastAPI, SQLAlchemy, Alembic, Pydantic, APScheduler, PostgreSQL driver, pytest, and HTTP client dependencies.
- [ ] Frontend package manager is chosen and locked (`package-lock.json`, `pnpm-lock.yaml`, or equivalent).
- [ ] Standard commands are documented: backend tests, frontend tests, lint/typecheck, regression tests, and Compose startup.
- [ ] `make test-regression` still passes before collector/source changes.

### Phase 1 - Compose Foundation

#### Task 1: Compose Skeleton

**Description:** Add `compose.yaml`, `web`, `backend`, `api`, `worker`, and `db` service structure.

**Acceptance criteria:**

- [ ] `docker compose config` succeeds.
- [ ] `docker compose up --build` starts `web`, `api`, `worker`, and `db`.
- [ ] `GET /health` returns OK from the API.
- [ ] The portal loads locally and can call the API health endpoint.
- [ ] `.env.example` distinguishes internal Docker API URL from browser-visible public API URL.
- [ ] Compose ports bind to `127.0.0.1` by default and require explicit env changes for LAN exposure.
- [ ] Documentation notes that the backend image must be built before starting the worker standalone.

#### Task 2: PostgreSQL Schema and Migrations

**Description:** Add Alembic migrations and initial tables for sources, runs, raw listings, jobs, profile, recommendations, and scheduler state.

**Acceptance criteria:**

- [ ] PostgreSQL volume is created.
- [ ] First Alembic migration enables `CREATE EXTENSION IF NOT EXISTS vector` before vector columns are created.
- [ ] First Alembic migration fixes the MVP embedding dimension for `job_embeddings` and `profile_embeddings`.
- [ ] Initial schema can be applied from a clean database.
- [ ] The APScheduler SQLAlchemy job table exists in PostgreSQL.
- [ ] Migrations are repeatable in CI.
- [ ] Uniqueness constraints and enum-like status checks cover the data model invariants above.

#### Task 3: API and Worker Shell

**Description:** Add enough backend structure that later tasks plug into stable module boundaries.

**Acceptance criteria:**

- [ ] API has `/health` and structured error response helpers.
- [ ] Worker starts, connects to the database, and exits cleanly on SIGTERM.
- [ ] Logging is structured enough to include component, source, run ID, status, duration, and error summary.
- [ ] No profile text, raw CV content, API keys, or full LLM prompts are logged.

### Phase 2 - Collection and Normalization

#### Task 4: Adapter Interface

**Description:** Define a source adapter contract for fetching raw payloads and yielding normalized listing candidates.

**Acceptance criteria:**

- [ ] The interface separates `fetch`, `normalize`, and `watermark` concerns.
- [ ] All adapters use a shared HTTP client with user-agent, timeout, retry, and source-aware poll interval behavior.
- [ ] Normalized listing candidates include source ID/URL, title, employer, description, application URL, dates, location, category, language hints, content hash, and raw payload reference.
- [ ] Adapter tests can run without live network calls by using fixtures.
- [ ] Live source behavior remains covered by `make test-regression`.

#### Task 5: First Vertical Source - Duunitori

**Description:** Implement one high-volume adapter end to end before adding more sources.

**Acceptance criteria:**

- [ ] Duunitori collection walks pages ordered by `date_posted` until the stored watermark.
- [ ] Raw payloads and normalized candidates are persisted idempotently.
- [ ] A repeated run creates no duplicate jobs or raw listings.
- [ ] Source run metrics include fetched, inserted, updated, unchanged, failed, and duration counts.
- [ ] Unit tests cover normalization from saved Duunitori fixtures.

#### Task 6: Worker Scheduler

**Description:** Move scheduled collection into the worker service using APScheduler with PostgreSQL-backed SQLAlchemyJobStore.

**Acceptance criteria:**

- [ ] Worker can run one source manually.
- [ ] Worker can run all enabled sources automatically.
- [ ] Scheduled jobs survive worker container restart.
- [ ] Missed or incomplete pipeline runs are detected from database state at startup.
- [ ] API remains responsive while collection runs.
- [ ] Failures are recorded in `source_runs`.
- [ ] Overlapping runs for the same source are prevented by database state or advisory locks.

### Phase 3 - Jobs, Search, and Dedup

#### Task 7: Additional MVP Sources

**Description:** Add TMT and Laura after the Duunitori path is proven.

**Acceptance criteria:**

- [ ] TMT uses `publishedAfter` and `pageSize=90`, and preserves required attribution.
- [ ] Laura uses `after` for new postings and `modified_after` for edits.
- [ ] Each adapter has fixture-based normalization tests.
- [ ] Each adapter respects `sources.yaml` poll intervals and blocked-source policy.

#### Task 8: Idempotent Storage and State Transitions

**Description:** Store source payloads and normalized jobs without duplicating repeated fetches; deactivate stale listings predictably.

**Acceptance criteria:**

- [ ] Re-running the same source does not duplicate raw rows.
- [ ] Canonical `jobs` preserve all source occurrences through `job_sources`.
- [ ] Raw JSONB payloads remain inspectable.
- [ ] Expired listings deactivate based on explicit deadline when available.
- [ ] Removed listings deactivate after source-specific absence rules, not after a single transient source failure.

#### Task 9: Deduplication

**Description:** Merge obvious duplicates using deterministic keys first, then add fuzzy and embedding-assisted candidate detection for cross-source duplicates that differ in title/formatting.

**Acceptance criteria:**

- [ ] Same job from multiple sources links to one `jobs` row.
- [ ] Dedup decisions can be inspected.
- [ ] False-positive dedup cases are test-covered.
- [ ] Cross-source candidates can be generated from normalized employer/title/location plus text similarity.
- [ ] Embeddings may be reused as a dedup signal, but dedup does not automatically merge solely on vector similarity.
- [ ] Ambiguous dedup decisions remain explainable and reversible.

#### Task 10: Search API

**Description:** Add PostgreSQL full-text search and paginated `/jobs`.

**Acceptance criteria:**

- [ ] `GET /jobs` returns newest jobs.
- [ ] `GET /jobs?q=...` searches title, employer, and description.
- [ ] Results are paginated and filterable.
- [ ] Filters cover source, location, employer, publication date, active/expired status, and freshness.
- [ ] Query plans are acceptable on a realistic fixture dataset.

### Phase 4 - Single Job Seeker Matching

#### Task 11: Profile and Preferences

**Description:** Add the single job seeker profile, CV text, location, hard filters, and preference weights.

**Acceptance criteria:**

- [ ] `GET /profile` and `PUT /profile` work.
- [ ] CV text and preference notes can be stored locally.
- [ ] Profile data is not logged accidentally.
- [ ] API mutation is local-only or protected by an operator token before LAN exposure.
- [ ] `llm_allowed_fields` and `llm_forbidden_fields` are represented in the profile model.

#### Task 12: Deterministic and Machine Scoring

**Description:** Score jobs using hard filters, structured fields, weights, and vector similarity.

**Acceptance criteria:**

- [ ] Hard exclusions remove jobs before LLM evaluation.
- [ ] Scores are reproducible from stored inputs.
- [ ] Embeddings are stored in PostgreSQL through pgvector.
- [ ] The selected embedding model and vector dimension are documented.
- [ ] Scoring output records enough intermediate fields to explain why a job was promoted or rejected.
- [ ] Missing embeddings degrade gracefully rather than blocking deterministic recommendations.

#### Task 13: LLM Evaluation

**Description:** Evaluate strongest candidates with a structured LLM response.

**Acceptance criteria:**

- [ ] LLM calls are capped per run.
- [ ] Response schema includes score, rationale, concerns, and suggested action.
- [ ] Results are stored and reused by the portal.
- [ ] Provider can be configured without changing recommendation logic.
- [ ] `llm_evaluations.request_hash` prevents repeat paid calls for unchanged job/profile/prompt inputs.
- [ ] Provider startup validation reports disabled/unavailable hosted scoring clearly.
- [ ] `docs/llm-hosted.md` remains the source of truth for hosted model IDs and prompt schema.

### Phase 5 - Professional Portal

#### Task 14: API Client and Portal Shell

**Description:** Build the portal shell, typed API client, navigation, and shared UI primitives before full feature views.

**Acceptance criteria:**

- [ ] Next.js standalone Docker build succeeds.
- [ ] Server-side API calls use `INTERNAL_API_BASE_URL`; browser-side calls use `NEXT_PUBLIC_API_BASE_URL`.
- [ ] Portal has stable routes for recommendations, all jobs, job detail, and source status.
- [ ] Loading, error, and empty states are available as shared components.

#### Task 15: Portal Views

**Description:** Build polished views for recommendations, all new jobs, job details, and source health.

**Acceptance criteria:**

- [ ] Recommendations and all-new-jobs are clearly separate.
- [ ] Job detail pages show source attribution and application links.
- [ ] Source-specific attribution text is rendered from stored `job_sources` data, including Työmarkkinatori attribution where required.
- [ ] Recommendation cards show match score, rationale, concerns, source freshness, and primary action.
- [ ] Empty, loading, no-match, and source-failure states are designed deliberately.
- [ ] UI works on desktop and mobile.
- [ ] Keyboard navigation and focus states work for search, filters, cards, and detail navigation.
- [ ] Layout has no obvious text overflow or overlap defects.
- [ ] Playwright checks cover desktop and mobile widths for core views.

#### Task 16: Local Server Operations

**Description:** Document and test local-server operation.

**Acceptance criteria:**

- [ ] `docker compose up -d` starts the full stack.
- [ ] Backup and restore commands are documented.
- [ ] Environment variables are documented in `.env.example`.
- [ ] `.env.example` includes `INTERNAL_API_BASE_URL`, `NEXT_PUBLIC_API_BASE_URL`, selected embedding model, and embedding dimension.
- [ ] Logs identify collection, matching, and LLM failures clearly.
- [ ] Restore is tested into a clean Docker volume before the build is considered server-ready.
- [ ] Source terms and privacy review are documented before any non-local exposure.

## Build-Ready Gate

The plan is ready to implement only when all of the following are true:

- [ ] `.env.example`, `.gitignore`, and `AGENTS.md` agree on secret handling.
- [ ] The first migration defines the tables and invariants needed for one source, not a throwaway prototype schema.
- [ ] The Duunitori vertical slice can run from source fetch to `/jobs` response without portal-specific hacks.
- [ ] `make test-regression` passes before adapter changes and is wired into CI.
- [ ] Unit tests use fixtures for adapter normalization; live tests are reserved for regression probes.
- [ ] Compose starts locally with private loopback bindings by default.
- [ ] LLM scoring can be disabled without breaking deterministic recommendations.
- [ ] A backup/restore command pair exists before storing private profile data.

## Upgrade Triggers

Add Redis/Celery or another queue when:

- worker jobs need concurrent execution
- retry/dead-letter visibility becomes operationally important
- LLM calls or source collection regularly exceed one worker's capacity
- persisted APScheduler jobs plus database pipeline state are not enough for retry/dead-letter requirements

Add OpenSearch when:

- PostgreSQL full-text search is measured and fails the portal's relevance/performance needs
- ranking logic becomes too complex for PostgreSQL
- multi-field faceting and analytics become central product features

Add a dedicated vector database only when:

- pgvector query latency is measured and insufficient
- embedding volume grows beyond what the local PostgreSQL server can handle
- multiple embedding collections/models need independent lifecycle management
- changing embedding model/dimension becomes frequent enough that schema migrations and full re-embeds are too costly

Revisit Next.js versus Vite SPA when:

- the portal remains a LAN-only single-user app
- SSR, file-based routing, and standalone web runtime add more maintenance cost than value
- a static bundle served by FastAPI would simplify deployment without hurting UX

## Sources

- Docker Compose defines and runs multi-container applications with services, networks, and volumes: https://docs.docker.com/compose/
- Docker Compose services are independently defined containerized application components: https://docs.docker.com/reference/compose-file/services/
- FastAPI documents building Docker images from the official Python image: https://fastapi.tiangolo.com/deployment/docker/
- Next.js documents Docker standalone output for minimal production Docker images: https://nextjs.org/docs/app/getting-started/deploying
- Next.js `output: "standalone"` copies only required runtime files for deployment: https://nextjs.org/docs/pages/api-reference/config/next-config-js/output
- PostgreSQL JSONB supports efficient processing of JSON data compared with reparsing JSON text: https://www.postgresql.org/docs/current/datatype-json.html
- PostgreSQL full-text search supports natural-language document matching and relevance ranking: https://www.postgresql.org/docs/current/textsearch.html
- PostgreSQL text search data types include `tsvector` and `tsquery`: https://www.postgresql.org/docs/current/datatype-textsearch.html
- pgvector stores vectors in PostgreSQL and supports similarity search: https://github.com/pgvector/pgvector
- OpenAI structured outputs support JSON Schema shaped model responses: https://developers.openai.com/api/docs/guides/structured-outputs
- OpenAI embeddings are intended for search, clustering, recommendations, classification, and related use cases: https://developers.openai.com/api/docs/concepts
- Ollama embeddings can generate local text embeddings for semantic search and retrieval: https://docs.ollama.com/capabilities/embeddings
- Ollama exposes an embedding API at `/api/embed`: https://docs.ollama.com/api/embed
- APScheduler supports triggers for scheduling jobs at later or recurring times: https://apscheduler.readthedocs.io/en/3.x/userguide.html
- APScheduler SQLAlchemyJobStore stores scheduled jobs in a database table: https://apscheduler.readthedocs.io/en/3.x/modules/jobstores/sqlalchemy.html
- Alembic is a lightweight database migration tool for SQLAlchemy: https://alembic.sqlalchemy.org/
