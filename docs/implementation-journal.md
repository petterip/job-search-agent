# Implementation Journal

Concise record of what is actually implemented. Keep this file current when code, runnable services, or verified behavior changes.

## 2026-09-16

### Production deployment (Pi, inkeri.etto.fi)

- **Deployed commit:** `518e045` (initial `451c32c`, plus the audit-serialization
  fix and the `app.match --inventory` flag).
- **Backups taken before deploy:** `jobsearchagent-20260915T223008Z.dump`
  (+ `.sha256`) and `.env.bak.20260915T222900Z` on the host.
- **`.env` changes (names only):** added `OPERATOR_API_TOKEN` (generated),
  `PUBLIC_ORIGIN=https://inkeri.etto.fi` and an explicit
  `RECOMMENDATION_COMMUTE_LIMIT_MINUTES=120` matching the code default.
- **Migrations:** `alembic current` = `20260916_0018 (head)` (0016 index,
  0017 run ownership, 0018 usage accounting).
- **Build/health:** backend and web images rebuilt; api/db healthy, web and
  worker up; `/health` ok with `transit_distance_enabled: true`.
- **Access boundary verified:** unauthenticated `GET /recommendations` and
  `GET /jobs/{id}` return 401, authenticated reads return 200, `/health` and the
  web pages stay public. The web container receives only the operator token and
  public origin; the feedback route returns 403 for cross-origin and
  origin-less POSTs and redirects non-numeric ids.
- **`make audit-db`:** now runs; the pre-existing `RowMapping is not JSON
  serializable` failure was fixed. It reports the usual data-quality counters
  (for example `broken_duunitori_urls`), which are pre-existing and not caused
  by this deploy.
- **Measured review backlog (dry run, `python -m app.match --inventory`):**
  18,457 eligible stale requests — 2,822 commutable / 508 remote / 15,127
  nationwide; 9,828 without an evaluation and 8,629 with a changed request
  identity; 71 currently published of 18,457 eligible rows. The frozen cohort
  hash is `1a4a2670…`. Estimated paid calls: 18,457, which is why the executor
  refuses to spend without an explicit authorization flag and budget. This
  supersedes the audit's earlier `2134` deterministic-pass figure as the current
  measured backlog.
- **Not verified:** the Cloudflare tunnel ingress/Access policy itself is
  remote-managed (`cloudflared tunnel run --token-file`) and was not
  inspectable locally; the application-level token and origin checks are the
  enforced boundary. No paid backfill, profile import or destructive action was
  run.



### Recommendation pipeline remediation — Stage A/B/C implementation

Implemented from `docs/recommendation-pipeline-remediation-plan.md`. Verified
with backend unit tests plus a disposable PostgreSQL 18 + pgvector database
(`alembic upgrade head`, `TEST_DATABASE_URL=...`); the web app passed
`npm run typecheck` and `npm run build`. See the plan's "Implementation status"
table for per-task evidence; the summary below distinguishes implemented,
verified and blocked work.

**Implemented and covered by tests**

- **P0-1 outbound privacy.** New `app/privacy.py`: supported `llm_allowed_fields`
  directives map to explicit structured projections, `llm_forbidden_fields` win
  at every depth, unknown directives fail closed, `llm_allowed=false` disables
  hosted profile/embedding calls. Job travel evidence, feedback snapshots,
  learned examples/hints and embedding input are sanitized; the API response
  model no longer exposes `travel_origin_address`. Synthetic sentinels only in
  committed tests.
- **P0-2 exact evaluation identity.** `evaluation_request_hash` now covers the
  actual prompt instructions/task, canonical response schema, provider, model,
  job and profile ownership, and the sanitized learned input; `returned_model`
  is response provenance only. Invalid cached output is a recoverable miss, and
  lookup/link updates are ownership-scoped.
- **P0-4 stable learned state.** The learner bumps its version only when the
  effective learned content changes; `updated_at`, counters and diagnostic
  provenance do not change evaluation identity.
- **P0-5 publication coherence.** Hard eligibility is immutable through
  semantic promotion and travel; `reconcile_recommendation_publication`
  re-checks existing recommendations against current hard rules outside the
  retrieval window; deterministic upsert no longer overwrites a linked LLM
  rationale; cooldown/misconfiguration never publishes unreviewed rows;
  prompt-version mismatch marks `awaiting_refresh` instead of silently
  reactivating.
- **P0-6 freshness.** New `app/freshness.py` pure policy: one captured UTC
  `as_of`, date-only deadlines interpreted through the Helsinki end of day,
  first-seen fallback tagged, future/malformed dates flagged, `drop_past_deadline`
  honored. The gate runs before embeddings and again on API reads; catalogue
  `jobs.status` is never expired for a profile age preference.
- **P0-7 feedback visibility.** Rating 1 hides; rating 2 stays a soft negative
  and only restores subject to eligibility; `applied` with a low rating remains
  rejected; learning keeps the `rating <= 2` negative signal.
- **P0-8 API visibility.** One structural eligibility predicate for items,
  counts, scope counts, detail and the LLM selector; the display-source lateral
  requires at least one enabled occurrence, so a disabled-only job yields no
  item and no count instead of a `source_names=NULL` failure; persisted links
  are re-validated on read.
- **P2-17 run ownership.** PostgreSQL advisory locks on a dedicated connection
  for source runs, the pipeline and the active profile's publication; owner
  tokens gate completion updates; abandoned `running` rows are reconciled only
  when the lock proves the owner is gone; profile import and `app.enrich` take
  the shared publication/pipeline locks. Tested with real concurrent sessions
  and a terminated backend.
- **P2-3 usage accounting.** Migration `20260916_0018` adds normalized
  input/output/cached tokens, latency, attempts, outcome, provider request id
  and a `usage_present` flag to `llm_evaluations` and `feedback_llm_analyses`.
- **P1-1 geographic policy.** A versioned effective-policy fingerprint (safe
  origin city, configured origin hash, commute limit, routing profile) is stored
  with travel evidence; local shortcuts are only valid while it matches, and the
  home-city shortcut requires the configured origin to agree with the home city.
- **P1-4 exclusions.** Hard negative titles are whole phrases matched with word
  boundaries against the title; explicit negative keywords remain substrings;
  compound qualification rejects are phrase-bounded.
- **P1-14 enrichment.** Candidate selection excludes already-handled
  occurrences before the caller's limit, and results publish only when the
  occurrence is still enabled and its input hash matches.
- **P1-15 URL boundary.** Shared scheme/host/path validation with private-host
  rejection, external-ID validation before URL interpolation, redirect guards
  for collection/enrichment navigation, display-only employer links, and
  hashed rejection diagnostics.
- **P1-12 profile import.** `validate_profile_document` rejects unsupported
  schema versions, privacy directives and malformed freshness before any write;
  ordinary import preserves DB-owned learned state and boosts; `base_revision`
  plus an optimistic learner write prevents import/learner overwrites.
- **P2-10 benchmark.** Metrics are named by their true denominators, read the
  real tuning constants, and only count active recommendations.
- **P2-12/P2-14/P2-15 UI.** Europe/Helsinki date formatters, independent
  recommendation pagination, and commute labels derived from
  `travel_commute_limit_minutes` with no hardcoded origin city.
- **P2-18 backups.** `make backup-db` reads `POSTGRES_USER`/`POSTGRES_DB` from
  the database container, writes a temporary file and atomically renames it
  after a checksum; `make backup-private` archives ignored profile originals
  and `.env` to `BACKUP_DIR` outside the checkout.
- **P2-19 timestamps (partial).** Adapter publication timestamps are normalized
  to aware UTC before watermark comparison.

**Implemented, not fully verified**

- **P0-9 private access.** `OPERATOR_API_TOKEN` now guards `/recommendations*`
  reads and every non-GET request at the API. The actual tunnel/reverse-proxy
  policy was not inspected, and the web route still needs to forward the token
  and validate the form origin, so external exploitability remains unverified.
- **P0-3 review backlog.** `run_review_backfill` produces a dry-run inventory
  with a frozen cohort hash and refuses paid execution without an explicit
  authorization flag and budget. No paid call was made; the resumable backfill
  executor is not implemented.
- **P1-6 discovery.** Phrase-based terms, dedupe and a bounded budget with a
  coverage warning; learned discovery queries now enter the pool and carry a
  `learned_discovery` lane. No before/after labelled comparison was produced.
- **P1-2 work-mode provenance.** Classification now separates extracted
  evidence from the display suffix and no longer treats flexibility language as
  full remote. Historical location suffixes were not reprocessed.

**Not implemented.** P1-3, P1-5, P1-7a–c, P1-8a–c, P1-9a, P1-10, P1-11,
P1-13, P2-1, P2-2, P2-4, P2-6, P2-7, the full P2-8 retry/race work, P2-11,
P2-16, P2-22 and P2-23 remain open. No production backfill, deployment,
profile import or destructive cleanup was performed for this work.

### Independent coWork review (Astra Low, Codex peer)

An independent read-only review of the working tree was run through the Codex
peer with `gpt-6-astra` at low reasoning effort (coWork skill model resolution:
Astra → Codex). Verdict: `CHANGES_REQUESTED`. All findings were verified against
source and fixed in the same working tree:

- feedback analysis now honours `llm_allowed=false` (no provider is built) and
  no longer holds a transaction across provider calls: it claims work, calls the
  provider outside the transaction, and publishes only if the feedback input hash
  still matches (a concurrent edit is discarded as `stale`);
- forbidden literal values (for example an address quoted in an allowed prose
  field) are collected and redacted from every outbound payload, including
  learned hints and embeddings;
- `/jobs/{id}` now requires the operator token and applies current eligibility to
  the recommendation it returns;
- rank refresh and the API predicate require persisted `hard_eligible`, so a
  hard-rejected approval can no longer be resurrected by ranking or a feedback
  rerating;
- the LLM selector uses the shared structural predicate (including freshness and
  hard eligibility), rating 2 is no longer excluded from review, and embeddings
  are built only for hard-eligible jobs;
- review selection over-scans and skips current cache hits before consuming paid
  slots, and fresh calls are deferred when the profile base revision changed;
- the web home and detail pages forward the operator token on private reads, and
  the feedback route rejects cross-origin posts;
- feedback restore mirrors matching's publication mode, including profile
  consent;
- collectors take the pipeline coordination lock in shared mode so collection and
  the daily pipeline cannot overlap; `ensure_source` is an atomic upsert;
- transit resolves valid cache entries before provider backoff, so a transient
  failure does not discard known routes;
- usage accounting records normalized feedback-provider usage and attempts;
- the review-backlog inventory no longer requires `is_active` and fingerprints
  expected request identities;
- enrichment candidate selection keyset-pages instead of relying on a larger scan
  cap.

The direct tests for these failures were added (consent with a provider spy,
linked-approval resurrection, inactive unreviewed inventory, cached transit during
backoff, feedback-provider usage metadata, forbidden value in prose).

### Second coWork review round (Astra Low)

A second independent Astra-Low review verified the fixes and found seven
remaining gaps plus new regression risks. All were addressed:

- feedback comments are redacted against profile-forbidden values, and numeric
  forbidden scalars (for example an integer postal code) are collected too;
- feedback publication locks the feedback row and re-checks the input hash, and
  every terminal status update is conditional on the row's `updated_at`, so a
  concurrent edit is never overwritten;
- reconciliation now re-checks every recommendation, including inactive legacy
  rows, and distinguishes hard-rule failure from the relevance threshold;
- `score_job` records `hard_eligible`/`hard_reasons` from the exclusion rules, so
  hard-rejected jobs are excluded from embeddings and the LLM selector;
- selection, evaluation, reconciliation and inventory share one sanitized request
  assembly (`evaluation_request_context`), so identities agree;
- a prompt/schema-only identity change is marked compatible and may stay visible
  awaiting refresh, while provider/model/profile/learned/job content changes are
  marked incompatible and are not published until re-reviewed;
- fresh publications re-read the job row after the provider call and defer if its
  content changed, and paid budget is consumed before the call;
- the review scan cap is wide enough to avoid cache-hit starvation;
- the inventory fingerprints the expected identity of unreviewed rows too;
- the remaining rating-2 "hidden" thresholds in the API and detail page were
  corrected to rating 1.

### Second implementation batch (2026-09-16)

Deployed to production as `b4a03cf` (migration `20260916_0019`) and `0be1fad`
(engine caching + docs); `alembic current` = `20260916_0019 (head)`, api/db
healthy, web/worker up, `/health` ok, unauthenticated `/recommendations` still
401 and the web home page 200. No paid backfill, profile import or destructive
action was run.


- **P1-3** required-language evidence (`backend/app/languages.py`): mandatory
  vs optional wording, negation, supported alternatives and level markers;
  failures are hard eligibility and cannot be recovered by semantic score.
- **P1-4** completed with configurable `exclusions.qualification_checks`
  (`backend/app/qualifications.py`): reject, adjacent-role caution,
  do-not-title-reject and required-phrase caution rules, with inflected phrase
  matching.
- **P1-7a** fetch completeness: `CollectionFetchResult.outcome`
  (`delta`/`full_snapshot`/`partial`) plus warnings; a partial fetch cannot
  advance the incremental cursor or remove listings; talentech shard/detail
  429 exhaustion is recorded as partial instead of silently continuing.
- **P1-13** canonical content: deterministic `canonical_field` precedence and a
  description guard so a short source summary cannot replace a richer enriched
  body and unchanged sources keep the canonical text.
- **P2-8** retryable feedback: migration `20260916_0019` adds
  `analysis_attempts`/`next_attempt_at`/`analysis_reason`; transient failures
  retry within a bounded budget while publication stays row-locked and
  input-hash conditional.
- **P2-4** bounded parse/schema retry and attempt-count persistence.
- **P2-11** casefolded learned exclusions and centroid provenance (model,
  dimension, contributing and missing job ids).
- **P2-16** source data-freshness `stale`/`last_success_at` diagnostics,
  separate from container liveness.

### Third implementation batch (2026-09-16)

- **P1-5** opt-in `preferences.scoring_weights`: validated relative cluster
  weights, per-cluster contribution evidence, cross-cluster term-overlap removal
  and `requires_check` kept as a caution. The legacy formula stays the default
  until a labelled comparison enables the weights. Positive `skills` and
  `strength_signals` are now part of the profile embedding input.
- **P1-6** discovery coverage counts (kept terms, learned terms, jobs attributed
  to the `learned_discovery` lane).
- **P1-8b measured, no index added:** a read-only production
  `EXPLAIN (ANALYZE, BUFFERS)` of the base active-job retrieval shows a parallel
  sequential scan + hash semi join with top-N sort at **~138 ms** and
  ~16.6k shared buffers for `limit 1000`; PostgreSQL chooses the seq scan over
  any candidate btree index at this scale. No index migration was added, and
  `pg_trgm`/full-text remains unjustified for the current Finnish token/regex
  behaviour.
- **P1-11** one engine per database URL per process with `atexit` disposal.
- **P2-16** data-freshness `stale`/`last_success_at` source diagnostics plus a
  persisted pipeline skip and one bounded catch-up run per UTC day.

### Fourth implementation batch (2026-09-16)

- **P1-8a** occurrence identity: a read-only production census found **0**
  duplicate `(source_id, external_id)` groups and **0** blank external ids in
  `job_sources`. Migration `20260916_0020` adds the partial unique index
  `uq_job_sources_source_external_id` with `CREATE UNIQUE INDEX CONCURRENTLY`
  (autocommit block, idempotent, reversible), and the occurrence insert is a
  race-safe `ON CONFLICT` upsert. Up/down migration was exercised on a
  disposable database.
- **P2-16 fix:** the source `stale` threshold now uses
  `SOURCE_STALE_AFTER_MINUTES` (default 1560) rather than the nominal poll
  interval, so a daily collector is not permanently reported stale.

### Fifth implementation batch (2026-09-16)

- **P1-8c** dedupe query: a read-only production
  `EXPLAIN (ANALYZE, BUFFERS)` of the cross-source canonical lookup showed a
  parallel sequential scan over ~100k jobs at **~42 ms** with ~12.7k shared
  buffers per listing. Migration `20260916_0021` adds
  `ix_jobs_dedupe_normalized` — a partial expression index over the three
  immutable normalized title/employer/location expressions — using
  `CREATE INDEX CONCURRENTLY` (idempotent, reversible). The date predicate is
  now explicit UTC (`published_at AT TIME ZONE 'UTC')::date`) so dedupe
  grouping no longer depends on the session timezone. Up/down migration was
  exercised on a disposable database. Post-deploy re-measurement on the same
  production lookup: **0.43 ms and 11 shared buffers**, using
  `Index Scan using ix_jobs_dedupe_normalized` (down from ~42 ms / ~12.7k
  buffers) — roughly a 100x reduction on the per-listing hot path.

### Sixth implementation batch (2026-09-16)

- **P1-7b resumable Jobly acquisition.** Migration `20260916_0022` adds
  `source_scan_members` (per source+external-id state machine:
  `pending`/`classified`/`retry`/`closed`, attempts, next-attempt, sitemap
  presence) and `source_scan_progress`. `app/collection/scan_state.py` freezes
  the merged sitemap frontier by external id (url/lastmod are mutable
  evidence), claims the oldest unresolved members first with no-date entries
  first, records outcomes after persistence, keeps a single 404 retryable until
  the configured confirmation count, never closes a parse-invalid member, closes
  sitemap-absent members only after `JOBLY_SCAN_GRACE_HOURS`, and reports backlog
  age/per-state counts. The collector runner passes the claimed batch to
  `JoblyAdapter.collect`, persists member state after the listings, and advances
  the source cursor only when the frontier is drained — so a bounded run never
  skips unprocessed older URLs and never re-fetches the same newest prefix.
  New settings: `JOBLY_SCAN_GRACE_HOURS`, `JOBLY_SCAN_RETRY_MINUTES`,
  `JOBLY_SCAN_CLOSURE_ATTEMPTS`.
- Known drain rate: with the default `JOBLY_MAX_URLS_PER_RUN=100` and one
  scheduled collection per day, draining the observed ~14k-URL Jobly sitemap
  takes many runs; the progress/backlog report makes the remaining work
  explicit and the cap is configurable.

### Seventh implementation batch (2026-09-16)

- **P1-7c (partial).** Adapters extract source deadlines through
  `extract_deadline` (`validThrough` from Jobly JSON-LD, plus
  `applicationEndDate`/`endDate`/`deadline`/`expires*`/`closingDate` at the top
  level or under `detail`) into `NormalizedListing.expires_at`. The collector
  stores the latest non-null deadline monotonically in `jobs.expires_at` on
  insert, cross-source dedupe and update, and never sets catalogue status from a
  deadline — the profile freshness policy consumes it. Tests:
  `tests/test_adapters.py`.

### Eighth implementation batch (2026-09-16)

- **P1-7c canonical closure/reopen.** Migration `20260916_0023` adds
  `job_sources.closed_at` (partial index on open occurrences).
  `apply_scan_closure` persists per-occurrence closure and hides the canonical
  job only when no other enabled, unclosed occurrence remains, so one live
  occurrence preserves the job and a partial source cannot establish absence;
  `reopen_scan_members` clears closure and restores the job when the occurrence
  is reclassified. Closed occurrences are excluded from the API list/detail
  display source, the catalogue visibility clause, the job-detail enabled-source
  gate and the recommendation structural predicate. Tested with real
  PostgreSQL.

### Ninth implementation batch (2026-09-16)

- **P1-10 retention dry-run.** `app/retention.py` reports `pg_total_relation_size`
  per table plus age-based candidate counts (raw listings not seen, old source
  events, closed occurrences, inactive recommendations, stale transit cache) and
  refuses to run in any non-dry-run mode. New settings
  `RETENTION_RAW_LISTING_DAYS` (365) and `RETENTION_ACTIVITY_DAYS` (180). No
  deletion path exists. Tests: `tests/test_retention.py`.

### Verification commands

```bash
cd backend
python -m pytest --timeout=10 -q                       # 414 passed, 37 skipped
TEST_DATABASE_URL=postgresql+psycopg://... python -m pytest --timeout=30 -q  # 451 passed
cd ../web && npm run typecheck && npm run build
```

## 2026-09-15

### Production configuration drift (inkeri.etto.fi)

The Pi host `.env` was still the 2026-06-21 first-deploy file, so several
features added later ran with their first-deploy values while the code and
migrations were current. The host `.env` was synced with the repo config:

- `LLM_PROMPT_VERSION` 7 → 8 and `LLM_EVAL_MAX_JOBS` 50 → 800 (first-deploy
  values; recommendation re-evaluation was throttled and cached prompt-version-7
  evaluations were reused).
- `DISCOVERY_SEARCH_QUERIES` expanded to the repo list (19 terms instead of 9).
- `GOOGLE_MAPS_API_KEY` and `TRANSIT_ORIGIN_ADDRESS` added: transit distance had
  been off in production, leaving 207 active recommendations in
  `transit_unavailable` and the default "Enintään 2 h" scope degraded.

`LLM_PROMPT_VERSION` must be bumped whenever `EVALUATION_INSTRUCTIONS` changes in
`backend/app/llm.py`. The instruction change in `e6c1ea7` did not bump it, so
`llm_evaluations` rows hashed with the old prompt text were reused.

### Implemented

- Migration `20260915_0016` adds `ix_job_sources_job_id`. The recommendations
  and job-detail APIs resolve each job's display source with a lateral subquery
  on `job_sources.job_id`; without the index that was a sequential scan per
  returned row (measured 13 s and ~116k buffers for the home-page
  recommendation query, ~3 ms after the index).

### Profile intake from the September 2026 applications

Three applications found in the Windows Downloads folder were copied into
`profile/inkeri/` (`raw/` originals, `extracted/` `pdftotext -layout` text):
Tietoasiantuntija / Oulun yliopiston kirjasto (2026-09-01) and Kirjastonhoitaja +
Kirjastovirkailija (musiikkiosasto) / Kokkolan kaupunginkirjasto (2026-09-10).
`preferences.application_history_signals` gained `kirjastovirkailija` and
`tietoasiantuntija` title boosts and the music-library/collection keyword boosts
`musiikkikirjasto`, `musiikkiosasto`, `kokoelmatyö`, `aineistohankinta`,
`kuvailu`, `luettelointi`, `satutuokio`. The updated `profile.yaml` was loaded
into the production database.

Measured effect (read-only deterministic probe over the production 5 184-candidate
window, old vs new profile): 3 additional jobs in the `application_history` lane,
14 jobs lifted by 5–18 points, identical pass set and top-40 order. The two new
titles did **not** become discovery search terms: `discovery_pool_terms` returned
80 terms both times because it silently truncates at `terms[:80]`
(`matching.py:652`).

### Audit

`docs/recommendation-pipeline-audit-2026-09-15.md` (findings with evidence) and
`docs/recommendation-pipeline-remediation-plan.md` (prioritised work items).
Headline results: the profile `privacy`/`freshness`/`languages`/location-radius/
cluster-weight contract is not enforced by the code; the catalogue has no
removal or expiry path (0 removed, 0 expired); the daily LLM review cap (800) is
below the deterministic pass set (2 134), so the prompt-version refresh left the
default scope empty (578 → 71 active recommendations, 0 commutable).

### Verified

- `make test-regression` passes.
- Production health: OpenAI key works (live call), 9/9 sources succeeded, daily
  pipeline completed every day since 2026-07-09, 100 668 active jobs with 1 281
  added in the preceding 24 h. The feed was updating; tokens were not exhausted.
- Prompt-version refresh verified live: 800 candidates re-evaluated at
  `prompt_version = 8` (710 skip / 68 consider / 8 apply) and 100 transit
  lookups cached; `ix_job_sources_job_id` reduced the recommendation query to
  ~3 ms.

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
