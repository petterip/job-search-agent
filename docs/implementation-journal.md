# Implementation Journal

Concise record of what is actually implemented. Keep this file current when code, runnable services, or verified behavior changes.

## 2026-07-05

### Implemented

- Five-point recommendation feedback (`rating` 1–5, optional `applied`) with upsert semantics, `scoring_snapshot`, and Finnish UI on job detail pages.
- Rating 1 hides a recommendation immediately; re-rating ≥2 un-hides. Job detail pages show inactive recommendations so hidden verdicts can be revised.
- Async feedback LLM analysis (`feedback_analysis.py`, `feedback_llm_analyses` table) on a 5-minute poll; grounded term suggestions with confidence weighting.
- Deterministic feedback learning (`feedback_learning.py`): decay-weighted net terms, hysteresis thresholds, caps, learned boosts/exclusions, lane quota overrides, few-shot examples, eval hints, and `learning_runs` audit rows.
- Learned preference/anti centroids in `learned_preference_embeddings`; semantic nudges in `merge_semantic_scores()`; exploration lane exempt from anti-penalty.
- Learned discovery queries merged into Duunitori/TMT/Laura collection via `effective_discovery_queries()`.
- Daily pipeline ordering: enrichment → drain analyses → learn → locations → match (scheduler job `run_daily_pipeline` at `LEARNER_DAILY_HOUR:MINUTE`, default 16:45).
- LLM job-fit prompt augmented with feedback few-shot examples and eval hints; `evaluation_request_hash` includes `learned.version`; pre-filter skips high anti-similarity jobs with learned exclusions.
- `make audit-db` reports feedback counts by rating, analysis status, latest learning run, learned profile summary, and offline benchmark (recall@30, exploration hit rate).

### Verified

- `make test-regression` passes.
- Backend pytest passes with `pytest --timeout=10` (167 tests).
- Senior review fixes (2026-07-05): learned exclusions are soft penalties only; comment name sanitization at analysis and API write; LLM sector suggestions wired; lane overrides recomputed statelessly; optional feedback comment + analysis hypothesis echo in UI; failed analysis re-queued on resubmit.

## 2026-06-21

### Implemented

- Worker scheduling for Raspberry Pi deployment runs enabled source collection once daily at 16:00 Europe/Helsinki by default. The ordered enrichment/learning/matching pipeline runs after collection through `run_daily_pipeline`, with `COLLECTOR_DAILY_HOUR`, `COLLECTOR_DAILY_MINUTE`, `LEARNER_DAILY_HOUR`, and `LEARNER_DAILY_MINUTE` available for deploy-time adjustment.
- Recommendation candidate ordering now uses multi-lane selection before LLM review:
  - `direct_title` for obvious title matches.
  - `application_history` for roles and duties similar to real past applications.
  - `semantic_similarity` for embedding-based matches whose wording differs from the profile.
  - `transferable_duty` for non-obvious roles with multiple duty/skill overlaps.
  - `sector_context` for suitable public-service/culture/parish/municipal contexts.
  - `exploration` for plausible hidden opportunities that should not be buried by direct-title matches.
- Deterministic recommendation records now store `candidate_lanes`, `hidden_opportunity`, `sector_matches`, and application-history match details for review/debugging.
- Candidate ranking reserves bounded review capacity per lane before appending the remaining scored jobs, so hidden opportunities can reach the LLM stage without sending every collected job to the model.
- When an LLM provider is enabled, active recommendations now require an LLM evaluation and are deactivated when the LLM returns `skip`, score below 40, `generic_customer_service_only`, or `not_applicable`.
- OpenAI embeddings are generated and cached for a positive-signal profile summary and active job summaries when `OPENAI_API_KEY` is configured. Stored pgvector similarity is used as one candidate-generation lane and as `recommendations.vector_score`; missing or failed embeddings degrade to deterministic matching.
- Duunitori, TMT, and Laura collection now merge targeted discovery searches from `DISCOVERY_SEARCH_QUERIES` into each scheduled run. The default terms protect recall for library and information-service roles that can remain open while older than the source watermark.
- Unchanged listings seen again by a source now reactivate a previously `removed` canonical job, so a still-open job is not hidden just because its content hash did not change.
- Existing source links can now be relinked to a better cross-source canonical job when improved normalization reveals a duplicate. Empty country-level locations such as `Suomi` no longer block later enrichment with a more specific city.
- Laura location inference now recognizes smaller municipalities seen in library-role discovery, including Rantasalmi, Utsjoki, and Ylöjärvi.
- The LLM profile summary now includes PII-minimized verified evidence for library qualification, Swedish B2 fluency, English B2 fluency, JET leadership qualification, and long library leadership experience. Prompt version 7 instructs evaluators not to treat ordinary 60 op / 35 ov library eligibility or ordinary Swedish second-domestic-language requirements as missing when those facts are present.
- `LLM_EVAL_MAX_JOBS` default is 800 so local, remote, nationwide, and lower-obviousness hidden matches can all receive hosted review in the same run.
- Local recommendation recall now uses a separate local candidate pool (`MATCHER_LOCAL_MAX_JOBS`) in addition to the global recency window.
- Hosted LLM matching keeps the previously approved recommendation set visible while a new run evaluates candidates; deterministic-only candidates are not exposed in the UI when LLM review is required.
- LLM evaluation commits before external provider calls and after persisted evaluations, so long hosted calls do not hold database transactions open.
- Prompt version 8 tightens local-quality review: location cannot compensate for weak fit, missing central requirements, physical care work, sales booking, or specialist work without concrete matching experience.
- Deterministic matching now hard-rejects teacher-qualification roles and C/CE driving-licence roles such as `kirjastoautonkuljettaja`, while preserving normal library instruction matches such as `kirjastonkäytön opetus`.
- Recommendation API responses now include a Finnish `recommendation_category` and `hidden_opportunity` flag derived from deterministic match evidence. The front page requests up to 15 recommendations and groups them by strength area so direct library matches remain first while transferable culture, communications, guidance, leadership, and hidden-opportunity paths stay visible.
- Profile evidence and matching now expose musicology, violin/viola, music-library cataloguing, music-event production, and published writing as a separate transferable strength path. Discovery searches now include selected music/content/culture terms so these matches can be collected even when they are not obvious library-title jobs.

### Verified

- `make test-regression` passes.
- Matching unit tests cover hidden transferable opportunities and lane-based review ordering.
- Embedding unit tests cover pgvector literal formatting, stable content hashing, minimized job embedding text, and semantic promotion of non-obvious candidates.
- Unit tests cover discovery-search collection for Duunitori, Laura municipality inference, reactivation of unchanged removed jobs, and cross-source relinking of an existing source listing.
- Unit tests cover inclusion of verified fit evidence in the minimized LLM profile summary and prompt rules for Swedish B2 and library eligibility interpretation.
- Unit tests cover hard rejection of teacher qualification requirements and library-bus driver roles without rejecting ordinary library instruction duties.
- Unit tests cover recommendation categorization for library work, leadership/admin roles, communications roles with library background keywords, and hidden opportunities. Live API verification returns 14 active recommendations for `limit=15` grouped into library, leadership/admin, culture/communications, guidance/customer work, and hidden-opportunity categories.
- Unit tests cover music/literature/publication recommendation categorization and deterministic promotion of music, literature, writing, and publication signals from inflected Finnish job text.

## 2026-06-20

### Implemented

- Docker Compose stack: `web`, `api`, `worker`, `db`.
- Host ports avoid port 80:
  - Web: `http://127.0.0.1:3080`
  - API: `http://127.0.0.1:8008`
- PostgreSQL + pgvector initial Alembic migration plus `sources.incremental_watermark`.
- FastAPI endpoints:
  - `GET /health`
  - `GET /jobs?limit=20&offset=0` with keyword, employer, source, and publication-date filters
  - `GET /jobs/{job_id}` with description, source links, attribution, and stored recommendation context
  - `GET /sources`
  - `GET /sources/status` with latest run counts and recent warning/error event totals
  - `GET /recommendations`
- Next.js portal shell that reads API health server-side.
- Finnish Next.js job browser with keyword/employer/source filters and paginated active listing feed.
- Finnish source-health panel on the portal home page for operator visibility into scheduled collection status.
- Finnish job detail pages at `/tyopaikat/{id}` with recommendation rationale, description, source attribution, and external application links.
- All verified non-blocked sources are scheduled by default through `COLLECTOR_ENABLED_SOURCES=duunitori,tmt,tmt_oulu,laura,jobly,eures_fi,kuntarekry,kirkkorekry,oulu_varbi`:
  - **Duunitori** — page walk by `-date_posted` until watermark
  - **TMT** — `publishedAfter` incremental when watermark exists; `pageSize=90`; required attribution stored
  - **Laura** — `after` for new postings and `modified_after` for edits
  - **Jobly** — sitemap `lastmod` filter + JobPosting JSON-LD fetch (capped per run)
  - **EURES (fi)** — paginated FI search; filters by `creationDate` when watermark exists (sort treated as unreliable)
  - **TMT Oulu** — same TMT API with municipality 564 filter
  - **Kuntarekry** — regional ProcessWire JSON shards + detail HTML
  - **Kirkkorekry** — regional ProcessWire JSON shards + detail HTML
  - **Oulu Varbi** — RSS + detail HTML
- Optional disabled-by-default sources are registered:
  - **Careerjet** — Publisher API v4 with `CAREERJET_API_KEY`; skips without a key
  - **LinkedIn** — guest HTTP search behind `LINKEDIN_ENABLED=false`
  - **Valtiolle** — Talentech organisation-shard harvest behind explicit `COLLECTOR_ENABLED_SOURCES`
- Shared collection runner persists `sources`, `source_runs`, `raw_listings`, `jobs`, `job_sources` idempotently.
- Durable harvester run event log in PostgreSQL:
  - `source_run_events` records start, success, failure, and skipped-run events.
  - Collector warnings/errors emitted during a run are persisted with source/run context, message, logger, file/line, args, and exception text when present.
- Worker scheduler:
  - APScheduler + PostgreSQL `SQLAlchemyJobStore`
  - One daily cron job per enabled source, defaulting to 16:00 Europe/Helsinki
  - Automatic daily pipeline via `run_daily_pipeline`, defaulting to 16:30 Europe/Helsinki, with enrichment skip/plan, location enrichment, deterministic matching, and hosted LLM evaluation in sequence
  - Overlap guard via `source_runs.status = running`
  - Pipeline overlap guard via `pipeline_runs.status = running`
  - Stale runs abandoned after `COLLECTOR_STALE_RUN_MINUTES`
- Manual CLI: `python -m app.collect <source|all>` with `--page-size`, `--max-pages`, `--max-urls`, `--force`
- Manual single-profile loader: `python -m app.profile <profile.yaml|profile.json> --name default`
- Manual deterministic matching CLI: `python -m app.match --max-jobs 1000`
- Manual database audit CLI: `python -m app.audit` or `make audit-db`
- PostgreSQL backup/restore helpers:
  - `make backup-db`
  - `make restore-db BACKUP=backups/jobsearchagent-YYYYMMDDTHHMMSSZ.dump`
- Optional hosted structured-output LLM evaluation stage for top deterministic recommendations:
  - Enabled only when `LLM_PROVIDER` selects a configured provider.
  - `LLM_PROVIDER=openai` uses `OPENAI_API_KEY` and `OPENAI_EVAL_MODEL`.
  - `LLM_PROVIDER=gemini` uses `GEMINI_API_KEY` and `GEMINI_EVAL_MODEL`.
  - Stores responses in `llm_evaluations` and links active output to `recommendations.llm_evaluation_id`.
  - Updates `recommendations.llm_score`, `fit_tier`, `suggested_action`, rationale, and concerns.
  - Uses minimized profile fields and normalized job summaries; raw CV/profile files are not logged.
  - Evaluates only active recommendations without an existing LLM evaluation.
  - Provider quota/unavailable failures create a provider-scoped local cooldown marker in `/storage` so scheduled matching does not repeat failing hosted calls every run.
- Recommendation feedback:
  - `recommendation_feedback` stores five-point `rating` (1–5), optional `applied`, legacy `action`, `scoring_snapshot`, and `analysis_status`.
  - `POST /recommendations/{recommendation_id}/feedback` (JSON body or legacy `?action=`)
  - `GET /recommendations/{recommendation_id}/feedback` and job-detail echo for inactive recommendations.
  - Finnish job detail pages expose the five-point control and applied toggle.
- Recommendations are refreshed with `(profile_id, job_id)` upserts and `is_active` state so feedback history survives matching re-runs while stale recommendations disappear from active API/UI results.
- Location enrichment:
  - Source adapters normalize city/region/country scope where available.
  - Remote and hybrid work signals are appended to the location text as `Etä` or `Hybridi`.
  - `python -m app.enrich_locations` backfills active jobs from stored raw payloads.
  - Remote/hybrid detection avoids negative source text such as "Etätyö: Ei mahdollisuutta työskennellä etänä".
- Description cleanup:
  - Talentech/KuntaRekry/KirkkoRekry, Laura, and EURES descriptions are normalized to readable plain text.
  - TMT and TMT Oulu descriptions are fetched from the public TMT detail API when collection volume is bounded, and can be backfilled from stored raw listings.
  - HTML entities are decoded and block tags are converted to line breaks before display.
  - `python -m app.enrich_descriptions` backfills stored active descriptions from raw payloads or source detail pages.
- Local secrets kept in `.env`; `.env` is gitignored.
- Safe `.env.example` with placeholder values only.

### Verified

- `make test-regression` passes (Duunitori, TMT, Laura, EURES, Jobly sitemap).
- `make test-backend` passes (42 tests).
- `make docker-config` passes without expanding secrets.
- `docker compose up --build -d` starts the stack.
- `source_run_events` verified with a constrained Duunitori run (`source_collection_started`, `source_collection_succeeded`) and a controlled collector warning probe (`verification_warning_probe`).
- Filtered jobs API verified with `q=data`; Finnish web page verified at `http://127.0.0.1:3080/?q=data&source=duunitori`.
- Cross-source dedupe now links matching source listings to an existing active job using normalized title, employer, location, and publication day.
- Conservative removal handling marks missing source-only jobs removed only after uncapped, non-watermark, non-empty refreshes.
- Deterministic single-profile recommendations are stored in `recommendations` and exposed through `GET /recommendations`; Finnish portal shows the recommendation section above the all-jobs feed.
- Laura employer enrichment derives employer names from the stable company slug in Laura listing URLs and fills missing normalized employer values on unchanged rows.
- Worker registers all nine default source collection jobs plus `analyze_feedback` and `run_daily_pipeline` without serialization errors.
- Worker prunes stale persisted scheduler jobs and syncs `sources.enabled` to the configured source set.
- `GET /jobs/{job_id}` and Finnish `/tyopaikat/{id}` verified with a stored recommendation, source attribution, and external application link.
- `GET /sources/status` verified against the live database; the Finnish home page renders source health, recommendations, and latest listings from the running stack.
- Profile loading CLI verified against the running API container and database with the private local YAML profile; output reports only metadata, not profile contents.
- Database audit CLI verified against the running stack; disabled-source-only active jobs were corrected to `removed`, leaving zero active jobs without an enabled source.
- Deterministic matching re-run after profile load and DB cleanup: 632 active jobs evaluated, 45 active recommendations stored.
- Matching re-run verified after recommendation feedback exists; it no longer fails on feedback foreign keys and preserves feedback rows.
- Location enrichment backfilled the running database: missing active locations reduced to 0. Review corrected false country-code locations and false remote markers from negative source text.
- Incremental collection removal guard verified: a TMT Oulu watermark run fetched 1 unchanged row and removed 0 existing listings.
- Description cleanup backfilled active descriptions; active description HTML entity count and HTML tag count are both 0.
- TMT detail description backfill reduced active jobs missing descriptions from 100 to 3 in the running database.
- LLM recommendation ranking now deactivates `suggested_action=skip` rows and re-ranks active recommendations by action and LLM score; active recommendations no longer include LLM skip rows.
- LLM re-evaluation now checks the current request hash for active recommendations, so description/profile/model changes are not masked by an old `llm_evaluation_id`.
- OpenAI structured-output integration verified up to provider call. Current configured API key returns `insufficient_quota`; the matcher stops the LLM stage after one provider-unavailable failure instead of retrying every candidate.
- Gemini structured-output integration verified with `LLM_PROVIDER=gemini` and `GEMINI_EVAL_MODEL=gemini-3.1-flash-lite`: live matcher runs stored 58 `llm_evaluations`, linked 58 active recommendations, and a second 30-item run completed with `llm_failed=0`.
- Gemini transient 5xx handling verified with a retry-backed provider path after live 503 responses were observed during the first run.
- Recommendation feedback endpoint and Finnish detail-page controls implemented; feedback totals are included in `make audit-db`.
- Manual collection verified:
  - Duunitori incremental watermark stop
  - TMT: 90 jobs inserted (1 page)
  - Supplemental harvesters run automatically and manually:
    - Jobly skips stale sitemap URLs returning HTTP 404.
    - EURES uses `resultsPerPage=50`, the current accepted API limit.
    - Kuntarekry/Kirkkorekry shard and detail requests tolerate HTTP 429 with backoff; exhausted Kuntarekry shards/listings are skipped for a later poll.
    - Oulu Varbi RSS + detail collection inserts the complete current catalog.

### Blocked / research sources (not in collector)

- Kuntarekry and Valtiolle.fi spike pass 2 (2026-06-20): **`organisation={id}` sharding** via `/fi/api/filters-data/` yields **1 158** Kuntarekry jobs (~61 % of ~1 900 advertised) and **187** Valtiolle jobs (full catalog). Sort sharding alone: 101 / 90. Playwright found no bulk JSON API. Spike script: `scrape-test/11_kuntarekry_valtiolle_spike.py`; `make test-spike-kuntarekry`. See `tyonhaku-rajapinnat.md` §13.
- TMT regional pages (e.g. Oulu `m=564`) map to existing TMT `municipalities` filter — 566 jobs verified; no separate adapter needed.

### Current Limits

- Cross-source deduplication is heuristic and conservative; URL canonicalization and stronger duplicate-cluster tooling are not implemented.
- Expiration/removal handling is conservative and source-refresh based; source-provided deadlines are not fully normalized yet.
- Jobly runs cap URL fetches per poll (`JOBLY_MAX_URLS_PER_RUN`); first backfill is incremental-by-`lastmod`, not full 13k import in one run.
- EURES incremental relies on `creationDate` filtering, not API sort order.
- Deterministic scoring, feedback learning, and stored recommendations are implemented and scheduled; OpenAI LLM evaluation is implemented but may be blocked by provider quota; recommendation feedback trains learned boosts/exclusions and semantic centroids when the daily pipeline runs.
- Three active Jobly rows still lack descriptions because their stored JSON-LD/detail payloads do not contain usable body text.
- Browserbase cloud session bootstrap is implemented (`browserbase_client.py`, `make browserbase-check`); Playwright CDP helper and `ENRICHMENT_ENABLED` gating are implemented for shipped browser/session paths. Durable occurrence-keyed enrichment tables, repository merge rules, `python -m app.enrich` queue/execution, upsert provenance preservation, attempt-level Browserbase metadata, Jobly browser fallback, and the daily pipeline lease are implemented. Local Playwright mode and Stagehand remain unimplemented by design.

### Blocked Sources (not implemented)

- Indeed and KIPA P67 remain blocked in `sources.yaml`. Careerjet, LinkedIn, and Valtiolle are optional registry entries (`scheduled_by_default: false`) with adapters available only when explicitly enabled/configured.
