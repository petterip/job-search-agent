# Browser Enrichment and Extended Collection Implementation Plan

**Updated:** 2026-07-05  
**Status:** partial: Browserbase bootstrap only. Enrichment runner, enrichment tables, Playwright extraction, and new sources are not shipped.  
**Related:** [`goal.md`](goal.md), [`architecture.md`](architecture.md), [`tyonhaku-rajapinnat.md`](tyonhaku-rajapinnat.md), [`sources.yaml`](sources.yaml), [`implementation-journal.md`](implementation-journal.md)

HTTP-first collection remains the default. Browser automation is only a bounded detail-enrichment fallback for rows that HTTP, JSON-LD, metadata, or source APIs cannot describe well enough.

---

## 1. Verified Current State

| Item | State | Notes |
|---|---|---|
| `BROWSERBASE_API_KEY` and config | Done | `backend/app/config.py`, `.env.example`, `AGENTS.md` |
| `browserbase` SDK dependency | Done | Currently required in `backend/pyproject.toml`; decide in Phase A whether it stays required or moves to an enrichment extra |
| `browserbase_client.py` | Done | Creates/releases sessions; sync SDK calls |
| `make browserbase-check` | Done | Creates/releases one live cloud session |
| `app/enrichers/browser.py` | Partial | Creates a cloud session only; no Playwright CDP connection or page extraction |
| `ENRICHMENT_ENABLED` | Unused | Parsed by config, not enforced by browser/session/worker code |
| `python -m app.enrich` | Missing | No unified enrichment CLI |
| DB `enrichment_*` tables | Missing | No durable queue/run/attempt state |
| Worker enrichment job | Missing | Scheduler currently registers source collection and matching at the same configured time |
| Kuntarekry org-shard | Missing | Current adapter uses regional shards; research proves `organisation={id}` org loop has much higher coverage |
| Valtiolle adapter | Missing | Documented as research only |
| Jobly HTML fallback | Missing | JSON-LD miss falls back to title-only payload |
| Careerjet / LinkedIn | Missing | Careerjet is still under `blocked`; LinkedIn is not registered |
| Registry/doc sync | Missing | `sources.yaml`, `tyonhaku-rajapinnat.md`, scheduler, adapter registry, and tests are not aligned for new sources |
| `implementation-journal.md` | Stale | Current limits still say embeddings are not implemented, while embedding code and schema exist |

---

## 2. Implementation Guardrails

Build the plan under these constraints:

- Source collection and enrichment have separate ownership. Source collection owns `raw_listings.payload`, `raw_listings.content_hash`, and `job_sources.last_content_hash`; enrichment owns derived text, structured fields, attempts, and artifacts.
- Do not use a mutated `raw_listings.content_hash` as an enrichment input hash. Compute enrichment input hashes from stable source-owned fields plus enricher name/version.
- A normal source re-collection must preserve an existing enriched description when the source still does not provide a usable body. If the source later provides a better body, mark the current description as source-provided.
- Track description provenance explicitly in `job_description_state`; implementation must be able to query whether `jobs.description` is source-provided or enriched.
- Add ordered pipeline execution or a DB-backed pipeline lease before scheduling enrichment. Matching must run after collection and enrichment finish or explicitly skip.
- Keep new sources disabled by default unless the source is intentionally part of the default collector set.
- Add source registry consistency tests before adding new adapters. Adapter registry, scheduler registry, config defaults, and `sources.yaml` must agree intentionally.
- `ENRICHMENT_ENABLED=false` must be side-effect free: no queue writes, no cloud browser session, no browser attempt.
- Keep browser automation last. Static HTTP/API/detail extraction must run before Playwright.
- Make the Browserbase/Playwright packaging decision explicit before adding Playwright. Do not add local Chromium to the default Docker image.

---

## 3. Source Registry Plan

| Source | Implementation status | Method |
|---|---|---|
| `careerjet` | Add as optional source requiring `CAREERJET_API_KEY` | Publisher API v4; skip with warning when key is absent |
| `linkedin` | Add as disabled-by-default supplementary source | Guest HTTP search first; browser detail only through enrichment queue for short descriptions |
| `indeed` | Remains blocked | No adapter |
| `kipa_p67` | Remains blocked | No adapter |

Registry tasks:

1. Move `careerjet` from `blocked` to `sources` with `scheduled_by_default: false`, `requires_api_key: true`, and conservative `poll_interval_min`.
2. Add `linkedin` under `sources` with `scheduled_by_default: false`, `enabled_by_env: LINKEDIN_ENABLED`, and conservative `poll_interval_min`.
3. Update `tyonhaku-rajapinnat.md` sections that still describe Careerjet as “not MVP” or blocked.
4. Add tests that compare `SOURCE_NAMES`, adapter factories, scheduled-source registry, and docs registry entries for enabled/default sources.
5. Journal source-status changes when an adapter ships.

---

## 4. Enrichment Architecture

### 4.1 Ownership Boundaries

Source collection owns:

- `raw_listings.payload` exactly as normalized from the source fetch.
- `raw_listings.content_hash` as the hash of source-normalization input only.
- `job_sources.last_content_hash` as the source-observed hash.
- Source-provided `jobs.description` when the source gives a usable body.

Enrichment owns:

- Derived detail text, extraction method, confidence, and input hash.
- Browser/session metadata.
- Retry state and errors.
- Optional HTML/text artifacts under storage.

Enrichment must not rewrite source payload hashes unless the adapter source payload genuinely changed.

### 4.2 Tables

Add these in Phase C before enabling any worker job:

| Table | Purpose |
|---|---|
| `enrichment_runs` | One runner execution: status, started/finished, counts, error summary, metadata JSONB |
| `enrichment_queue` | Durable desired work: `job_id`, `enricher`, `input_hash`, status, priority, attempts, `next_attempt_at`, `last_error` |
| `enrichment_attempts` | Per-attempt audit: queue item, run id, status, provider, started/finished, session id, dashboard URL, artifact paths, error |
| `job_enrichments` | Latest successful derived output per `(job_id, enricher, input_hash)`: description, structured fields JSONB, method, confidence, `applied_to_job` |
| `job_description_state` | One row per job tracking current `jobs.description` provenance: `job_id`, `provenance` (`source` or `enriched`), `source_id`, `enricher`, `input_hash`, `confidence`, `applied_at` |

Required constraints:

- Unique active queue item on `(job_id, enricher, input_hash)` for statuses `queued`, `running`, `retry`.
- Unique latest enrichment result on `(job_id, enricher, input_hash)`.
- Check constraints for queue/run/attempt statuses.
- Foreign keys to `jobs`, `enrichment_runs`, and `enrichment_queue`.
- A queryable way to tell whether the current `jobs.description` came from source collection or enrichment.

### 4.3 Input Hash

Compute `input_hash` from stable source-owned fields only:

- `job_sources.last_content_hash`
- `job_sources.application_url`
- `raw_listings.canonical_source_url`
- `jobs.title`
- `jobs.employer`
- `jobs.published_at`
- enricher name and version

Do not include previous enrichment output, `updated_at`, retry counters, or `raw_listings.payload` after enrichment metadata has been added.

### 4.4 Description Merge Rule

Add a helper that writes enriched descriptions without corrupting source state:

```text
apply_enrichment_result(job_id, result)
  insert job_enrichments
  read current description provenance
  update jobs.description only when:
    current description is null, blank, or shorter than threshold
    OR current description is enriched and the new result has higher confidence/source priority
  never lower-quality overwrite source-provided body text
  update description provenance when result is applied
```

The existing `enrich_descriptions.py` functions should be refactored to return `EnrichmentResult` objects and call this helper. They must stop directly mutating `raw_listings.content_hash`.

When source collection itself provides a usable description, `upsert_listing()` must mark the description provenance as source-provided. When source collection does not provide a usable description, it must preserve any existing enriched description and provenance.

### 4.5 Queue Eligibility

Queue a job only when all are true:

- `jobs.status = 'active'`
- at least one enabled `job_sources` row exists for an enricher-supported source
- current description is null, blank, or shorter than `ENRICHMENT_SHORT_DESCRIPTION_CHARS` default 200
- no successful `job_enrichments` row exists for the same `(job_id, enricher, input_hash)`
- no non-terminal queue item exists for the same `(job_id, enricher, input_hash)`

### 4.6 Browser Last

Extraction order per job:

1. Source adapter normalization or detail API.
2. Existing non-browser backfills: TMT detail API, Talentech detail HTML, Laura/EURES normalization.
3. Jobly static HTML metadata/microdata fallback in the existing adapter fetch path.
4. Browser CDP extraction for the small residual queue.
5. Optional Stagehand path only when explicitly enabled.

Duunitori is excluded from browser enrichment because its API already returns full `descr`.

---

## 5. Runtime Flow

```text
scheduled or manual pipeline
  │
  ├─ collect enabled sources
  │    └─ source_runs/source_run_events
  │
  ├─ enqueue enrichment candidates
  │    └─ enrichment_queue(input_hash)
  │
  ├─ run enabled HTTP/static enrichers
  │    └─ job_enrichments + enrichment_attempts
  │
  ├─ run bounded browser enrichers
  │    └─ Browserbase session metadata per attempt
  │
  ├─ enrich_locations
  │
  └─ matching + embeddings + LLM evaluation
```

The worker should prefer this ordered pipeline over independent cron jobs. If separate scheduler jobs remain, they must use a DB-backed pipeline lease and skip when a previous stage is still running.

---

## 6. Modules

| Path | Role |
|---|---|
| `app/browserbase_client.py` | Cloud session lifecycle; sync SDK wrapped by async callers |
| `app/enrichers/browser.py` | `browser_session()` gating, `asyncio.to_thread`, CDP connection helper |
| `app/enrichers/base.py` | `DetailEnricher`, `EnrichmentResult`, `EnrichmentInput`, method/confidence enums |
| `app/enrichers/repository.py` | Queue/run/attempt/result persistence and merge helper |
| `app/enrichers/jobly.py` | Jobly static fallback and browser extractor |
| `app/enrichers/talentech.py` | Browser retry only when existing `<article>` parser returns empty |
| `app/enrichers/runner.py` | Queue creation, caps, leases, HTTP/static/browser orchestration |
| `app/enrich.py` | Manual CLI with `--dry-run`, `--enqueue-only`, `--source`, `--enricher`, `--max-jobs` |
| `app/adapters/talentech_org_shard.py` | Shared `filters-data` + `organisation={id}` loop |
| `app/adapters/careerjet.py`, `app/adapters/linkedin.py` | Optional aggregate adapters |

---

## 7. Config

```env
BROWSERBASE_API_KEY=
ENRICHMENT_ENABLED=false
ENRICHMENT_PROVIDER=browserbase    # browserbase | local | off
ENRICHMENT_MAX_JOBS_PER_RUN=50
ENRICHMENT_SHORT_DESCRIPTION_CHARS=200
ENRICHMENT_DAILY_HOUR=16
ENRICHMENT_DAILY_MINUTE=30
JOBLY_BROWSER_ENRICH_MAX_PER_RUN=20
APPLICATION_ENRICH_MAX_PER_RUN=10
CAREERJET_API_KEY=
CAREERJET_MAX_RESULTS_PER_RUN=50
LINKEDIN_ENABLED=false
LINKEDIN_MAX_RESULTS_PER_RUN=100
LINKEDIN_BROWSER_ENRICH_MAX_PER_RUN=10
```

Packaging decision:

- Keep `browserbase` in required dependencies only if the base backend image is expected to run `make browserbase-check` without extras.
- Put `playwright` in `[project.optional-dependencies].enrichment`.
- Do not install local Chromium in the default Docker image. Add a Compose `enrichment` profile only if local browser mode is implemented.

Disabled behavior:

- `ENRICHMENT_ENABLED=false` means `python -m app.enrich` logs/skips unless `--dry-run` is used.
- The worker does not enqueue or run enrichment.
- `browser_session()` raises before creating a Browserbase session.
- Unit tests must prove `create_cloud_session()` is not called when disabled.

---

## 8. Phases

### Phase A: Finish Browser Bootstrap

| Task | Acceptance |
|---|---|
| A.1 Gate `browser_session()` on `ENRICHMENT_ENABLED` before provider/key checks | Disabled test proves no cloud session is created |
| A.2 Wrap sync Browserbase create/release calls with `asyncio.to_thread()` | Async unit test proves create/release are awaited and release runs on exceptions |
| A.3 Add `playwright` to optional `enrichment` extra and document package decision for `browserbase` | `pip install '.[enrichment]'` installs browser dependencies |
| A.4 Add `connect_playwright_over_cdp(connect_url)` helper | Unit-tested with mocked Playwright async API; returns browser/page handle and closes cleanly |
| A.5 Fix `implementation-journal.md` embedding contradiction and note Browserbase bootstrap state | Journal no longer says embeddings are not implemented |

### Phase B: HTTP Coverage And Registry

| Task | Acceptance |
|---|---|
| B.1 Add source-registry consistency tests | Adapter registry, scheduled-source registry, default config, and `sources.yaml` intentionally agree |
| B.2 Update `sources.yaml` and `tyonhaku-rajapinnat.md` for `careerjet` and `linkedin` optional entries | `make test-regression` passes; blocked list remains only truly blocked entries |
| B.3 Implement `TalentechOrgShardAdapter` | Fixture tests cover `filters-data`, singular `organisation={id}`, dedupe, 429 backoff, and detail HTML |
| B.4 Switch or configure `KuntarekryAdapter` to org-shard national mode while keeping regional fast path available | Live probe reaches expected current range; fixture tests are deterministic |
| B.5 Add `ValtiolleAdapter`, adapter registry entry, scheduler entry disabled by default | `python -m app.collect valtiolle --max-pages/--max-urls` is idempotent |
| B.6 Add Jobly static fallback during existing detail fetch | Fixtures cover meta description, OpenGraph, microdata/visible body fallback; missing-description audit drops without browser |
| B.7 Make `collect_all_sources()` honor `COLLECTOR_ENABLED_SOURCES` | CLI `all` matches worker enabled source set |

### Phase C: Enrichment Framework

| Task | Acceptance |
|---|---|
| C.1 Add Alembic migration for enrichment tables and constraints | Clean upgrade/downgrade; no existing table semantics changed |
| C.2 Implement `EnrichmentResult`, repository helpers, input hash, queue creation, provenance tracking, and merge rules | Unit tests prove enriched descriptions survive source re-upserts and do not mutate source hashes |
| C.3 Refactor existing `enrich_descriptions.py` logic into runner-compatible HTTP/static enrichers | `python -m app.enrich --dry-run` lists candidates and planned actions |
| C.4 Add DB-backed enrichment lease and ordered worker integration | Matching cannot run while collection/enrichment stage is active |
| C.5 Expose last enrichment run status in `GET /sources/status` or `make audit-db` | Operator can see last run, counts, failures, and stale queued attempts |
| C.6 Store Browserbase session id/dashboard URL per attempt, not only per run | Failed browser attempts are debuggable by job/enricher |

### Phase D: Jobly Browser Extract

| Task | Acceptance |
|---|---|
| D.1 Implement deterministic Playwright extraction through CDP helper | Fixture tests cover selectors and body cleanup; residual Jobly missing descriptions drop or are documented |
| D.2 Enforce `JOBLY_BROWSER_ENRICH_MAX_PER_RUN` and global run cap | Tests prove caps are honored |
| D.3 Add `scrape-test/12_jobly_enrichment_probe.py` | Live opt-in probe emits JSON and fails on empty/challenge/short-body results |
| D.4 Optional Stagehand behind `JOBLY_ENRICH_USE_STAGEHAND=false` | Disabled by default; cache artifacts under storage only when enabled |

### Phase E: Careerjet Adapter

| Task | Acceptance |
|---|---|
| E.1 Implement `CareerjetAdapter` using API key config | Missing key skips with visible warning and no failed run noise |
| E.2 Bound requests and detail fetches | Max 50/run by default; at least 3 seconds between detail requests |
| E.3 Deduplicate against existing jobs by canonical URL and normalized title/employer/location | Unit tests cover duplicate and new-listing cases |

### Phase F: LinkedIn Adapter

| Task | Acceptance |
|---|---|
| F.1 Create live probe and fixture from observed guest HTTP behavior | Probe documents shape before adapter is enabled |
| F.2 Implement disabled-by-default `LinkedinAdapter` | `LINKEDIN_ENABLED=false` default; scheduler does not register it unless enabled |
| F.3 Route short descriptions through enrichment queue | HTTP first, browser only for capped residual candidates |
| F.4 Add `scrape-test/13_linkedin_harvest_probe.py` | Live opt-in probe fails loudly on zero jobs or challenge response |

### Phase G: Structured Fields And Application Pages

| Task | Acceptance |
|---|---|
| G.1 Extract deadline, languages, licences, and structured detail fields from TMT/detail payloads without browser | Matching consumes fields; reproducible tests |
| G.2 Add application-page enricher only for recommendation candidates | Cap 10/run; no form submissions; no raw form bodies exposed through API |

### Phase H: Production Ops

| Task | Acceptance |
|---|---|
| H.1 Document rebuild requirements after dependency changes | `raspberry-pi-deployment.md` says to run `docker compose up --build` |
| H.2 Document Browserbase-only path vs local Playwright profile | Default stack does not install Chromium |
| H.3 Add `make browserbase-check` to smoke/deploy runbook | Not part of `test-regression` |
| H.4 Link this plan from `architecture.md` normalization/enrichment section | Architecture docs point to current plan |

---

## 9. Implementation Order

```text
A (safe browser bootstrap)
  -> B.1 + B.7 (registry/CLI consistency)
  -> B.2-B.6 (HTTP coverage)
  -> C (durable enrichment framework)
  -> D (Jobly browser residual)
  -> E/F (optional aggregate sources)
  -> G (structured fields)
  -> H (ops docs)
```

Do not set `ENRICHMENT_ENABLED=true` in production until Phase C and the relevant Phase D browser smoke pass.

Do not run matching immediately after collection once enrichment is enabled. Matching should run only after the ordered pipeline finishes or a lease proves enrichment was skipped.

---

## 10. Regression Controls

Required before merging each implementation phase:

| Layer | Command/test | Required for |
|---|---|---|
| Source API regression | `make test-regression` | Any adapter or source docs change |
| Backend unit tests | `make test-backend` | Every backend change |
| Source registry consistency | New pytest module | Phases B-E |
| Enrichment persistence tests | New pytest module | Phase C and later |
| Upsert preservation tests | New tests around `upsert_listing()` + enrichment merge | Phase C |
| Scheduler ordering tests | Scheduler/runner pytest | Phase C |
| Kuntarekry/Valtiolle spike | `make test-spike-kuntarekry` | Org-shard implementation and live verification |
| Browserbase smoke | `make browserbase-check` | Deploy/key rotation only |
| Jobly/LinkedIn browser probes | `scrape-test/12_*`, `13_*` | Manual opt-in; CI only with `RUN_LIVE_BROWSER_TESTS=1` |
| Docker config | `make docker-config` | Config/Compose changes |

Regression invariants:

- A normal source re-collection must not delete an enriched description unless the source now provides a better description.
- Enrichment must not change `raw_listings.content_hash`.
- The system must be able to query whether the current `jobs.description` is source-provided or enriched.
- `ENRICHMENT_ENABLED=false` must be side-effect free.
- `python -m app.collect all` and worker scheduled sources must use the same enabled-source set.
- New sources default to disabled unless explicitly intended otherwise.
- Matching must not run concurrently with an active collection/enrichment pipeline.
- Live browser checks are opt-in and cannot make default CI flaky.

---

## 11. Risks And Mitigations

| Risk | Mitigation |
|---|---|
| Source-hash churn from enrichment | Separate `job_enrichments` from `raw_listings`; source hashes remain source-owned |
| Enriched description overwritten by later collection | Merge helper and upsert preservation tests |
| Scheduler overlap | Ordered pipeline lease; matching waits until collection/enrichment finish or skip |
| Org-shard runtime and rate limits | Backoff, caps, live spike, regional fast path for Oulu-oriented runs |
| Optional aggregate duplicate inflation | Keep disabled by default; dedupe by URL plus normalized title/employer/location |
| Browser provider/session failures | Attempt-level records, caps, retries with `next_attempt_at`, dashboard URL per attempt |
| Pi RAM pressure | Browserbase default; local Chromium only under optional profile |
| Registry drift | Consistency tests spanning adapter registry, scheduler registry, config defaults, and `sources.yaml` |

---

## 12. Non-Goals

- Browser-based list harvest for Duunitori, TMT, Laura, or EURES.
- Indeed adapter.
- LinkedIn authenticated sessions or credential storage.
- Public exposure of replay URLs or HTML artifacts through the portal API.
- Replacing source adapters with browser scraping.

---

## 13. Success Metrics

- Enrichment framework records runs, queue state, attempts, and latest results.
- Source re-collection after enrichment keeps enriched descriptions and stable source hashes.
- Kuntarekry org-shard reaches the expected live range and Valtiolle reaches the expected current catalog range, with fixture tests independent of live counts.
- Active Jobly missing descriptions fall to zero or each residual has a recorded attempt/error reason.
- Optional Careerjet/LinkedIn sources remain disabled by default, collect cleanly when enabled, and dedupe against existing jobs.
- `make test-regression`, `make test-backend`, and relevant phase-specific tests pass before enabling production enrichment.
- `implementation-journal.md`, `sources.yaml`, and `tyonhaku-rajapinnat.md` are updated when each phase ships.
