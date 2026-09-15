# Recommendation pipeline — verified remediation plan

**Reviewed:** 2026-09-16. **Implementation status:** planned, except the historical W0 actions below.
**Review baseline:** local HEAD `e6c1ea77dde73da83cd7e9f17d4b017b2eea9347`, including the pre-existing uncommitted audit, deployment/journal edits and migration 0016.
**Scope:** profile intake → collection/dedupe/enrichment → retrieval → deterministic/semantic scoring → travel → hosted evaluation → recommendation publication → feedback/learning → API/UI → scheduling/backup.

Companion: [original audit](recommendation-pipeline-audit-2026-09-15.md). This document supersedes its proposed remedies and the previous version of this plan. Original task IDs remain traceable; revised priorities and dependencies below govern execution.

This review changes the plan, not production or application source. No paid provider calls, production backfill, deployment, profile import, destructive cleanup or commit was performed. The original audit's production counts are historical observations, **not measurements repeated on 2026-09-16**.

## 1. Review verdict and evidence corrections

**Request changes to the original plan.** Most reported code defects are real, but several fixes would create new correctness problems. In particular:

1. **Do not implement the proposed 200 km global hard filter.** The private profile describes an inferred radius, explicitly conditional on geocoding; the later [distance-scope policy](recommendation-distance-scope-plan.md#product-policy) deliberately retains otherwise valid distant jobs in `nationwide`. Keep the configured transit duration for local membership. Reconcile the profile/docs before changing this product decision (P1-1).
2. **Do not expire catalogue jobs because one profile only wants 30-day-old results.** Profile freshness controls recommendation eligibility. Source deadlines/closure evidence control catalogue lifecycle. Incremental absence and an old `last_seen_at` do not prove a listing closed (P0-6, P1-7).
3. **Do not advance a newest-first truncated Jobly scan to its oldest fetched timestamp.** This still jumps over older unprocessed URLs. Holding the timestamp fixed while repeatedly fetching the same first 100 also never drains the backlog. Implement a resumable no-gap traversal, including equal timestamps, failed URLs and missing timestamps (P1-7b).
4. **Do not promise convergence in two nightly runs.** Selection checks prompt version, not current request identity; cached rows consume selection slots, scope quotas do not transfer unused capacity, and learned versions change daily (`matching.py:1131-1268`, `feedback_learning.py:501`). The 800 limit is **per matching invocation**, not an enforced daily token/spend limit. Freeze and count the actual stale cohort before estimating work (P0-2–P0-4).
5. **Do not replace every `rating <= 2`.** Rating 2 must remain a negative learning signal; only visibility predicates change. The separate `rating >= 3` restore branch and detail-page hidden label also need adjustment (`main.py:410`, `web/app/tyopaikat/[id]/page.tsx:172`). Preserve applied/rating validation (P0-7).
6. **Do not use `returned_model` in a pre-call cache key.** It is known only after the response. Record it as provenance; hash configured provider/model and request content. The evaluation hash already includes `schema_version`; the missing pieces are actual prompt/schema content and correct selection/invalidation (P0-2, P2-5).
7. **Do not equate zero new rows with source failure.** An empty successful incremental response is normal. Preserve explicit fetch failure/completeness evidence (P1-7a).
8. **Do not export the transit street address to fix the origin label.** P2-15 previously contradicted P0-1. Use a safe city label or neutral label. The address is necessary for the authorized server-to-Google routing call, not for evaluation, embeddings or browser display.
9. **Do not treat the audit's numbers as internally reconciled.** `710 + 68 + 8 = 786`, not 800; missing outcomes need classification. The score-change table says 14 jobs but its `+5 ×11` label implies 15 when added to the four preceding rows. Its §7 says the probe was unfinished despite §3.3 reporting completion. §3.2's discovery statement is superseded by §3.3. Preserve the original audit as historical evidence and resolve these discrepancies in the next measured report.
10. **Qualify overstatements.** D3 has no automatic retry, but resubmitting feedback explicitly requeues failed/skipped rows (`main.py:1162-1174`). F6 lacks parse/schema retries, but transport retries already exist in provider clients. F8's library/Swedish prompt rules are conditional on supplied facts, not unconditional assertions about a person. B3 demonstrates risk of missed acquisition, not proof that every lost URL is permanently unrecoverable; recover still-live gaps by sitemap reconciliation.

The original P0-1 ↔ P0-3 dependency cycle is removed. Privacy does not wait for backfill. Provider cooldown, publication safety, request identity, source cursor integrity and backup verification move ahead of throughput work.

### Verification performed

- Indexed the repository with codebase-memory-mcp, traced matching orchestration and rank refresh, checked coverage, and read exact source at the relevant seams. Private profile files are excluded from the index and were inspected directly without copying their contents into this plan.
- Independent read-only review of collection, lifecycle, dedupe, scheduler and backup; primary review of matching, providers, travel, feedback, enrichment, API and UI.
- `make test-regression`: **passed**, 2026-09-16, including all nine source probes. This establishes current source response compatibility, not catalogue completeness or production recommendation correctness.
- Initial `make test-backend` could not run because the default interpreter lacked pytest. Installed the declared backend/dev dependencies into `/tmp/recommendation-plan-review-venv`; equivalent `python -m pytest --timeout=10 -q` in `backend/`: **199 passed**, two dependency deprecation warnings. These tests mostly use doubles; passing them does not verify PostgreSQL query semantics or races.
- Web `npm run typecheck` and `npm run build`: **passed** (installed Next.js 15.5.19). This checks compilation, not browser flows.
- Synthetic, no-network probes reproduced: forbidden address sentinel in both summary/embedding output even with `privacy.llm_allowed=false`; new application title lost after 80 configured terms; learned discovery absent; multiword hard exclusion falsely rejecting a library job because its description contains `kaluston`.
- No current production SQL, provider accounting, deployment access-control test, real-browser interaction or restore drill was performed. These are explicit rollout gates, not implied successes.

## 2. Additional findings from feature review

Evidence locations refer to the review baseline. Each finding has an implementation owner/task below; none is considered fixed by documenting it.

| ID / severity | Verified defect or review gap | Evidence | Resolution |
|---|---|---|---|
| R01 / high | Old recommendations that now fail deterministic rules remain in the LLM selector and rank refresh. Neither requires current-run eligibility. Restricting refresh to touched passing rows alone would still leave untouched stale rows active. | `matching.py:885-918,1160-1182,1410-1492` | P0-5: eligibility reconciliation over existing recommendations, separate from ranking. |
| R02 / high | Deterministic upsert overwrites LLM rationale/concerns while retaining the old LLM score, action and evaluation link. Unreviewed rows can show a mixed explanation. | `matching.py:1040-1050,1359-1384` | P0-5: coherent published evaluation and separate deterministic evidence. |
| R03 / high | Hard-negative title phrases are split into tokens then searched as substrings across title, employer and description. Employer context, negated requirements and unrelated equipment text can reject valid roles. | `matching.py:234-245,257-268,297-300` | P1-4: phrase/requirement-aware gates with evidence and negative fixtures. |
| R04 / high | New freshness/language gates in `score_job` alone could be bypassed by semantic promotion, which only checks the two existing rejection lists. | `matching.py:455-460` | P0-6/P1-3/P1-4: one explicit hard-eligibility result, immutable through semantic/travel stages. |
| R05 / high | Recommendation count requires an enabled source, but item query uses an aggregate lateral join that returns a row even with zero matching sources. This can yield `source_names=NULL` and response validation failure. Detail lookup also permits a job with only disabled sources. | `main.py:711-721,941-957,990-1007` | P0-8: identical source visibility predicates for list/count/detail and review eligibility. |
| R06 / high | Feedback analysis holds a write transaction over remote calls and publishes results by feedback ID without checking whether the rating/comment changed while the call ran. Evaluation writes similarly lack a current-input guard; exception handling commits rather than rolling back a failed DB transaction. | `feedback_analysis.py:594-674,704-731`; `matching.py:1359-1399` | P1-9/P2-8: snapshot, external call, conditional short write; rollback on DB failure. |
| R07 / high | Profile import replaces the entire JSON document; learner also read-modify-writes the entire document. Import can erase learned state, and racing learner writes can restore old user configuration. Shape validation only checks that the root is a dict. | `profile.py:23-80`; `feedback_learning.py:538-549` | P1-12: validate consumed fields, preserve derived state, revision-checked writes. |
| R08 / high | Transit success cache never expires; all failures share a seven-day destination suppression. HTTP transport exceptions escape the expected error type. Failed attempts do not consume budget. | `transit_distance.py:321-389,494-552` | P1-9: distinct outcome types, TTL, bounded attempts, provider-wide transient backoff. |
| R09 / medium-high | Local shortcut trusts profile home city even when configured origin moves; API exempts `exact_home_city` rows from origin/version checks. Detail and UI fallback also use independent work-mode/distance rules. | `travel_policy.py:265-299`; `main.py:136-178`; `location_evidence_service.py:58-81`; `web/app/ui.tsx:39-70` | P1-1/P1-2/P2-15: versioned effective travel policy and common evidence. |
| R10 / high | Source/pipeline acquisition is SELECT-then-INSERT, so manual invocations or multiple processes can overlap. `max_instances=1` only protects one scheduler process. | `collection/runner.py:590-627`; `scheduler.py:144-178` | P2-17 promoted to P0: database-owned run exclusion and safe terminal transitions. |
| R11 / medium-high | Canonical title/employer/location/publication fields can oscillate with source collection order. Any nonempty source description also replaces an enriched one. | `collection/runner.py:448-481`; `enrichers/repository.py:125-140` | P1-13: deterministic field provenance and source precedence. |
| R12 / high | Enrichment applies an old result to the current occurrence/raw row without comparing input hash. Candidate LIMIT runs before cached/queued exclusion, so already-handled short descriptions can starve later work. | `enrichers/repository.py:143-208,320-397` | P1-14: compare current input at publish; exclude handled rows before limit. |
| R13 / medium | Feedback snapshot labels the evaluation with current settings rather than the actual linked evaluation; repeat submission hashes include changing ranks/state. Few-shot reasons reuse the system rationale even when feedback disagrees. | `main.py:305-361,1130-1178`; `feedback_learning.py:264-301` | P2-8/P2-10: immutable evidence identity, idempotent submission, grounded corrected examples. |
| R14 / medium | Benchmark's “false_positive_rate” is negative rated rows divided by all rated top-30 rows, not statistical FPR. It uses global stored rank rather than actual scope/API order and only labelled recommendations. A sample of skipped Oulu jobs alone cannot estimate end-to-end recall. | `feedback_benchmark.py:10-109`; `main.py:136-168` | P2-10/P2-23: define denominators, stratify across missed retrieval/gates/review/acceptance. |
| R15 / high, exposure unverified | Feedback mutation and private feedback reads have no application authentication; web POST route has no explicit origin/CSRF check. Loopback API binding does not protect an externally exposed web proxy. Actual tunnel access control was not inspected. | `main.py:1021-1280`; `web/app/suositukset/[id]/palaute/route.ts:16-59`; deployment runbook “Add authentication…” | P0-9: verify deployment boundary, protect private reads and mutation, same-origin form checks. |
| R16 / medium-high | Upstream URLs reach browser navigation and external application links without a shared scheme/host boundary. Rendering escapes text, but escaping alone does not validate URLs. | `enrichers/runner.py:256-275`; `source_links.py:6-29`; detail page application links | P1-15: source-scoped fetch/navigation validation, safe HTTP(S) display links. |

### Feature coverage and retained behavior

| Feature | Review conclusion / retained requirement |
|---|---|
| Three application additions | Private profile provenance names all three applications, and application title/keyword boosts are present. Discovery truncation is confirmed; production import and measured score deltas remain historical, not re-proven here. Keep all three application fixtures in the sanitized regression sample. |
| Collection / sources | Enabled-source predicates and configured intervals exist. Preserve blocked-source policy and keep `sources.yaml` and `tyonhaku-rajapinnat.md` synchronized. Complete snapshots, deltas and partial fetches need distinct semantics. |
| Dedupe / enrichment | Preserve source attribution, raw references and feedback identity. Fix source precedence, stale result publication and queue starvation before retention. |
| Retrieval / scoring | Local, remote, recent and discovery pools plus candidate lanes exist. Preserve exploration and application evidence while fixing gates and term budgets. Candidate limits must be reported as coverage limits. |
| Travel / scopes | Three scopes, stored ranks and explicit unknown transit are useful. Fix provenance, failure taxonomy, stale policy and cache age; preserve nationwide access. |
| Hosted evaluation | Structured response validation, `store=False`, per-call timeout, content-based reuse and quota handling exist. Fix privacy, exact request identity, backlog accounting and coherent publication before concurrency. |
| Feedback / learning | Ratings, applied flag, comment sanitization, centroids, decay, hysteresis and grounding guard exist. Rating 2 stays negative feedback. Fix retries, concurrent publication, stable hashes and benchmark interpretation. |
| API / UI | Scope totals, URL scope state, list pagination for jobs, Finnish evidence and application attribution exist. Recommendation paging, consistent eligibility, private access and configurable labels remain work. Browser acceptance is still required. |
| Operations | Scheduler timezone and configured daily source schedule exist. Fix run ownership, catch-up, truthful status, backup/restore and data retention. Do not add a queue service or monitoring stack for these repairs. |

## 3. Decisions and invariants for implementation

- **Privacy is a boundary, not arbitrary YAML key subtraction.** The current allow/deny lists contain human phrases such as “location policy summary” and “raw source files”. Map supported entries to explicit structured projections; reject unknown privacy directives on import. Forbidden nested fields win over allowed parents. Honor `llm_allowed` for all hosted profile/feedback/embedding consumers. Apply final outbound sanitization after learned examples/hints are appended. Use synthetic sensitive sentinels in committed tests, never the private production profile.
- **Separate eligibility, review progress and publication.** Hard eligibility cannot be recovered by semantic score or LLM opinion. An old approval may remain visible only while the job still passes current hard rules and the change is explicitly compatible; mark it awaiting refresh. Safety/eligibility changes invalidate affected approvals immediately. Ranking must not create eligibility.
- **One request identity at selection, cache lookup and publication.** Hash only the actual sanitized evaluation input plus provider/model and prompt/schema content. Audit timestamps, daily counters, centroids or discovery-only state do not invalidate LLM output unless they actually change that input.
- **Freshness is profile-specific.** Evaluate dates with one captured UTC `as_of` per run, persist it for audit, and define date-only deadlines in Europe/Helsinki through end of the stated day. Missing publication dates use first-seen as an explicitly tagged fallback; future/malformed dates are flagged for review, never silently treated as newest. Source last-seen is not publication time.
- **Preserve the three geographic scopes.** Global hard exclusions concern required qualifications/languages and freshness. An inferred radius must not silently remove distant nationwide recommendations. Unknown geography remains unknown; do not compare route distance with a straight-line radius.
- **Bound external work before increasing parallelism.** Explicit attempt/cost/time budgets, persisted progress and no remote calls in DB write transactions. One owner per run; fresh-input checks prevent an old call overwriting newer state.
- **Keep private data private.** Leave `profile/**` ignored. Backup/revision history belongs in a private mechanism, with sanitized fixture hashes in tracked documentation. A Git ignore exception is not a privacy solution.

## 4. Historical work, not a fresh verification

| ID | Reported 2026-09-15 change | Remaining verification |
|---|---|---|
| W0-1 | Production prompt version 8, review cap 800, discovery/transit configuration synced; host env backups made. | Read-only config fingerprint and provider/backlog counts before any new rollout; never dump `.env`. |
| W0-2 | `ix_job_sources_job_id` reportedly created concurrently; pending migration `20260915_0016`. | Compare Alembic revision, exact index definition and validity. Existing index and clean install both must migrate safely. |
| W0-3 | Three applications added to private raw/extracted/profile artifacts; production profile reportedly imported. | Compare sanitized profile revision/hash, three-source provenance and intended signals; check whether import replaced learned state (R07). |
| W0-4 | Journal and deployment docs updated. | Preserve those existing changes; reconcile claims with measured rollout evidence. |

## 5. Ordered implementation backlog

**Priority:** P0 = before the next unattended hosted/publication run; P1 = correctness/recall work next; P2 = efficiency and presentation after correctness. Some original P2 IDs are explicitly promoted. Each checkbox represents unfinished implementation, even if a current test passes.

Task owner means the code seam responsible for the change, not an assigned person. Split the named substeps into focused patches; do not combine unrelated modules into one refactor.

### Implementation status (2026-09-16, local HEAD + working tree)

Verified against a disposable PostgreSQL 18 database (`alembic upgrade head`,
then `TEST_DATABASE_URL=... python -m pytest`); the web app compiled and built.
`[x]` means the task's core behaviour is implemented and covered by tests.
Partial and blocked work is listed explicitly rather than silently completed.

| Task | Status | Evidence / remaining |
|---|---|---|
| P0-1 outbound privacy | implemented | `app/privacy.py` projects supported directives, deny-wins at depth, `llm_allowed=false` disables hosted calls, job travel/feedback snapshots sanitized, API drops `travel_origin_address`. Tests: `tests/test_privacy.py`. Stored-response inventory is proposed, not executed. |
| P0-2 exact evaluation identity | implemented | Content hash over instructions/task/schema/provider/model/job/profile/learned input; `returned_model` is provenance only; invalid cache recovers via upsert. Tests: `tests/test_llm.py`. |
| P0-4 stable learned state | implemented | Learned version bumps only on effective content change; diagnostic timestamps excluded. Tests: `tests/test_feedback_learning.py`. |
| P0-5 eligibility + coherent publication | implemented | Hard eligibility immutable through semantic/travel; structural reconciliation outside the retrieval window; LLM rationale preserved on upsert; cooldown never publishes unreviewed rows; prompt mismatch marked `awaiting_refresh`. Tests: `tests/test_recommendation_publication_pg.py`. |
| P0-6 freshness | implemented | `app/freshness.py` pure policy (30-day boundary, date-only Helsinki deadlines, future/missing flagged), applied before embeddings and on API reads. Tests: `tests/test_freshness.py`, PG publication tests. |
| P0-7 feedback visibility | implemented | Rating 1 hides, rating 2 is a soft negative and restores subject to eligibility; applied+low rating still rejected. Tests: `tests/test_jobs_api.py`. |
| P0-8 API visibility/projection | implemented | One structural predicate for items/counts/scopes/detail and the LLM selector; display-source lateral requires at least one enabled source; no raw deterministic payload or origin. Tests: PG publication tests. |
| P0-9 private access | partial | `OPERATOR_API_TOKEN` is enforced on `/recommendations*` reads and every non-GET at the API boundary, and the web feedback route forwards the token and rejects cross-origin POSTs against `PUBLIC_ORIGIN` (compose passes `.env` to the web service). The actual tunnel/reverse-proxy policy was not inspected (no access), so external exploitability remains unverified. Tests: `tests/test_private_access.py`. |
| P2-17 run ownership | implemented | PostgreSQL advisory locks on a dedicated connection for source/pipeline/profile publication, conditional owner-token completion, lock-proven reconciliation; profile import and `app.enrich` now take the shared locks. Tests: `tests/test_run_ownership.py`. |
| P0-3 review backlog | partial / blocked | Dry-run inventory is publication-independent and freezes expected request identities; `run_review_backfill` / `python -m app.match --inventory` refuses paid execution without an explicit authorization flag and budget. Production dry run (2026-09-16): 18,457 eligible stale requests (9,828 no evaluation, 8,629 stale identity; 2,822 commutable / 508 remote / 15,127 nationwide), 71 currently published. The resumable paid backfill is not executed and is blocked on explicit authorization. Tests: PG publication tests. |
| P2-3 usage accounting | implemented | Migration `20260916_0018` adds normalized token/latency/attempt/outcome/request-id columns; providers return normalized usage; evaluation and feedback writes persist it. Test: PG column assertion. Embedding/transit costs are counted as attempt counts, not yet a unified ledger. |
| P1-1 geographic policy | implemented | Effective-policy fingerprint (safe origin-city, limit, routing) invalidates stale shortcuts; home-city shortcut requires origin agreement. Tests: `tests/test_travel_policy.py`. |
| P1-2 work-mode provenance | partial | `classify_work_mode` separates evidence from the display suffix and stops treating flexibility language as full remote; historical rows are not reprocessed (needs a bounded, authorized reprocessing run). Tests: `tests/test_travel_policy.py`. |
| P1-4 phrase-aware exclusions | implemented | Hard negative titles match as word-bounded phrases against the title; negative keywords stay substring matches; compound rejects are phrase-bounded. Tests: `tests/test_matching.py`. |
| P1-6 discovery terms | implemented | Phrase-based terms, dedupe, bounded budget with a coverage warning, learned discovery queries wired into the pool with `learned_discovery` lane provenance and per-run kept/learned/attributed counts. A labelled before/after comparison still belongs to P2-23. Tests: `tests/test_matching.py`. |
| P1-14 enrichment queue | implemented | Candidate selection keyset-pages and filters handled identities before the caller limit; result publication requires the current enabled occurrence and a matching input hash. Tests: `tests/test_enrichment_repository.py`. |
| P1-15 URL validation | implemented | Shared scheme/host/path validation, private-host rejection, external-ID validation, redirect guards, display-only employer links, read-time revalidation of persisted URLs. Tests: `tests/test_url_boundaries.py`, `tests/test_source_links.py`. |
| P2-10 benchmark metrics | implemented | Metrics named by true denominators, real tuning constants, active-only rows. Tests: `tests/test_feedback_benchmark.py`. |
| P2-12/P2-14/P2-15 UI | implemented | Europe/Helsinki formatters, independent recommendation offset with pager, dynamic commute labels with no hardcoded origin. Verified by `npm run typecheck && npm run build`; browser flows remain unverified. |
| P2-18 backups | implemented | Backups use the db container's `POSTGRES_USER`/`POSTGRES_DB`, write a temporary file and atomically rename after a checksum; `backup-private` archives ignored profile originals and `.env`. Restore drill still required. |
| P2-19 timestamps | partial | Adapter publication timestamps normalize to aware UTC before watermark comparison; Laura's exact endpoint timezone semantics were not probed against the live endpoint. |
| P2-20/P2-21 docs | implemented | Private README corrected (nothing under `profile/` is tracked; embeddings come from the privacy projection; the 200 km prose is superseded); deployment runbook requires an `.env` key diff. |
| P1-3 required-language evidence | implemented | `app/languages.py` interprets mandatory/preferred/negated wording, alternatives and levels, and requires a language-context phrase so a bare country mention is not a requirement; failures are hard eligibility. Tests: `tests/test_languages.py`, `tests/test_matching.py`. |
| P1-4 qualification-aware exclusions | implemented | Phrase-aware hard titles plus configurable `exclusions.qualification_checks` (reject / adjacent-role caution / do-not-title-reject / required-phrase caution); a requirement phrase in a negated or optional sentence is a caution, and check shapes are validated on import. Tests: `tests/test_qualifications.py`, `tests/test_profile.py`. |
| P1-7a fetch completeness | partial (b implemented) | `CollectionFetchResult` carries `delta`/`full_snapshot`/`partial` plus warnings; a partial fetch cannot advance the cursor or remove listings; talentech adapters mark exhausted-429 fetches partial. Remaining adapters not audited and Jobly/`P1-7b` acquisition is not implemented. Tests: `tests/test_collection_runner.py`. |
| P1-13 stable canonical content | partial | Deterministic canonical precedence (`canonical_field`) for title/employer/location and `preserve_enriched_description_on_upsert` keeps the richer/enriched description and honours unchanged sources. Tests: `tests/test_enrichment_repository.py`. Full both-order cross-source collection test still to add. |
| P2-4 failure classification | partial | `evaluate_with_parse_retry` adds one bounded parse/schema retry, attempt counts persist, and quota/transport are not retried. Startup config validation and an explicit rollout smoke check are not implemented. Tests: `tests/test_llm.py`. |
| P2-8 retryable feedback | implemented | Migration `20260916_0019` adds attempts/`next_attempt_at`/reason; transient failures stay pending with a bounded budget, permanent failures classify, and publication is row-locked and input-hash conditional. Tests: `tests/test_feedback_analysis.py`. |
| P2-11 learned exclusion/centroid provenance | implemented | Learned exclusion matching is casefolded on both sides; centroid recomputation records model, dimension, contributing and missing job ids. Tests: `tests/test_feedback_learning.py`, `tests/test_embeddings.py`. |
| P2-16 pipeline status/catch-up | partial | Source status exposes a data-freshness `stale` flag and `last_success_at` separately from liveness; runs persist skip status. Bounded catch-up after collection is not implemented. Tests: `tests/test_private_access.py`. |
| P1-5 cluster weights | partial | Opt-in validated `preferences.scoring_weights` with per-cluster contributions, cross-cluster overlap removal and `eligibility=requires_check` kept as a caution; positive skills/strength signals added to the embedding input. The legacy additive formula remains the default until a labelled comparison enables the weights. Tests: `tests/test_matching.py`, `tests/test_embeddings.py`. |
| P1-8b recency/retrieval indexes | measured, no index added | Read-only `EXPLAIN (ANALYZE, BUFFERS)` on production: the base active-job retrieval uses a parallel seq scan + hash semi join at ~138 ms with ~16.6k buffers, so a new btree index would not be chosen; `pg_trgm`/full-text is not a drop-in for the Finnish token/regex behaviour. Recorded in the journal. |
| P1-11 engine/scoring context | implemented | One engine per database URL per process with `atexit` disposal (`app/db.py`), keyed by URL so config overrides never retain a wrong engine. Tests: `tests/test_db.py`. |
| P2-16 pipeline status/catch-up | implemented | Source status exposes data-freshness `stale`/`last_success_at` (threshold is the expected collection cadence, `SOURCE_STALE_AFTER_MINUTES`, not the nominal poll interval) separately from liveness; a skipped pipeline is persisted and one bounded catch-up run is scheduled per UTC day. Tests: `tests/test_scheduler.py`, `tests/test_private_access.py`. |
| P1-8a occurrence identity | implemented | Production census: 0 duplicate `(source_id, external_id)` groups and 0 blank external ids. Migration `20260916_0020` adds a partial unique index `CONCURRENTLY` (downgrade drops it), and the occurrence insert is now a race-safe `ON CONFLICT` upsert. Test: `tests/test_recommendation_publication_pg.py`. |
| P1-8c dedupe query | implemented | Production `EXPLAIN (ANALYZE, BUFFERS)` showed a parallel seq scan (~42 ms, 12.7k buffers) per cross-source dedupe lookup. Migration `20260916_0021` adds the matching immutable expression index; the date filter is now explicit UTC (`published_at AT TIME ZONE 'UTC'::date`). Post-deploy re-measurement: the same lookup is **0.43 ms / 11 buffers** using `Index Scan using ix_jobs_dedupe_normalized` (~100x faster, index demonstrably used). Tests: `tests/test_recommendation_publication_pg.py`. |
| P1-7b resumable Jobly acquisition | implemented | Migration `0022` adds `source_scan_members`/`source_scan_progress`; the Jobly adapter returns the merged sitemap frontier (identity = external id, url/lastmod mutable) and only fetches a claimed batch; the runner claims oldest-first `(lastmod, external_id)` with no-date entries first, persists outcomes (`classified`/`missing`/`invalid`), retries 404s until confirmed closure, never closes parse-invalid, closes sitemap-absent members after grace, and advances the cursor only when the frontier drains. Backlog age and per-state counts are reported; a reappearing or lastmod-corrected member returns to `pending` for revalidation, absence is reconciled only from a validated complete sitemap, and `missing_attempts` counts only confirmed 404s. Tests: `tests/test_scan_state_pg.py`, `tests/test_adapters.py`. Drain rate is one configured batch per scheduled collection run. |
| P1-7c deadline/closure lifecycle | implemented | Source deadlines are extracted and stored monotonically in `jobs.expires_at` (freshness consumes them; deadlines never set status). Migration `20260916_0023` adds occurrence-level `job_sources.closed_at`; a confirmed-closed occurrence hides the job only when no other enabled, unclosed occurrence remains, and reclassification clears closure and restores the job. Closed occurrences are excluded from list/detail/eligibility visibility. Tests: `tests/test_adapters.py`, `tests/test_scan_state_pg.py`, `tests/test_recommendation_publication_pg.py`. |
| P1-10 retention dry-run | partial | `app/retention.py` reports per-table size and age-based candidate counts (raw listings not seen, old source events, closed occurrences, inactive recommendations, stale transit cache) with an explicit "no rows are deleted" contract; deletion is refused. Normalizer/revision-based location reprocessing and the restore drill are not implemented. Tests: `tests/test_retention.py`. |
| P1-9a external work outside transactions | implemented | All acceptance criteria are met: instrumented tests observe no provider/HTTP call inside an open write transaction (embeddings, transit and the LLM call commit first); per-batch embedding progress is durable; a concurrent input change defers publication; and a failed candidate rolls back so later candidates proceed. `run_matching` uses a plain connection with an explicit final commit, so the commits are legal. Tests: `tests/test_embeddings.py`, `tests/test_transit_distance.py`, `tests/test_matching.py`. |
| P2-6 profile-calibrated prompt | mechanism implemented, not enabled | `llm_guidance.profile_rules` is validated (list of 1–400 char strings) and appended to the evaluation prompt as profile-specific calibration, so prompt identity changes automatically when it is used; absent field keeps the current prompt. Enabling it awaits the P2-23 labelled comparison. Tests: `tests/test_matching.py`, `tests/test_profile.py`. |
| P2-7 requirement-aware excerpt | mechanism implemented, not enabled | `requirement_aware_excerpt` keeps the description head plus decisive requirement/deadline sentences within the 4000-char budget; `LLM_REQUIREMENT_AWARE_EXCERPT` (default false) selects it so current identities are preserved until measured. Tests: `tests/test_llm.py`. |
| P2-1 bounded evaluation concurrency | not implemented | Requires measured throughput evidence from paired paid runs, which is not authorized here. |
| P2-22 remainder | partial | API shape, escalation and usage docs are reconciled; a final route/schema sweep of the plan/journal docs remains. |
| P2-23 labelled evaluation tooling | tooling implemented, labels needed | `app/labelled_evaluation.py` consumes a private labels file and reports per-stage recall/precision by stratum with denominators and Wilson intervals; it refuses to run without labels and never commits them. The actual relevance labels are human input. |


### Stage A — protect data and publication

#### [x] P0-1 — Enforce outbound privacy (A1, R15 boundary inputs)

**Owner/files:** `profile.py`, `llm.py`, `embeddings.py`, `feedback_analysis.py`; API serialization in a separate P0-8 patch. **Depends:** none. **Size/risk:** M / high impact.

Implement explicit safe projections with nested deny precedence and consent handling described in §3. Preserve the positive-only embedding design. Sanitize job travel data, feedback free text, learned hints/examples and any legacy persisted payload before outbound serialization. Keep routing address only in server-side routing/cache/audit where needed; normalize errors so it cannot escape through logs/provider exception text. Inventory previously stored responses, feedback snapshots and caches for sensitive fields; propose targeted private cleanup with backup, not automatic blanket deletion.

**Accept:** mocked OpenAI/Gemini evaluation, feedback and embedding boundaries receive no forbidden sentinels at any depth; `llm_allowed=false` causes zero hosted profile calls. Missing/invalid privacy configuration fails closed with an actionable local error. API/browser responses expose no address/postal field. A routing mock still receives its necessary server-side origin. No dependency on backfill.

#### [x] P0-2 — Exact evaluation identity (F1, F7, F9; absorbs P2-5)

**Owner/files:** `llm.py`, `feedback_analysis.py`, `matching.py`, one additive migration if identity provenance needs storage. **Depends:** P0-1. **Size/risk:** M / cache invalidation.

Fingerprint job/profile identity, instructions, task, canonical response schema, configured provider/model and sanitized request bytes. Apply the same rule to feedback analysis. Keep prompt/schema versions as readable provenance; store returned model as response metadata. Do not require a redundant version-bump test for safety once content hashing is authoritative. Reuse the existing `request_hash` and compare expected hash against linked evaluation; only add separate fields where queries/audit need them. Validate cached responses before reuse; invalid cache is recoverable miss, not a repeated terminal failure. Include job/profile ownership in lookup/link checks, even if two jobs serialize identically.

**Accept:** changing instructions/schema/provider/model/job/profile/few-shot/hint changes expected identity; metadata-only timestamps/counters do not. Identical retries reuse valid output. Invalid cached output is replaced safely without unique-key conflict. A cache row cannot attach an evaluation owned by another job/profile. `returned_model` is auditable without requiring a speculative API call to compute the key.

#### [x] P0-4 — Stable learned state (F2)

**Owner/files:** `feedback_learning.py`, request assembly in `matching.py`. **Depends:** P0-2. **Size/risk:** S–M / ranking stability.

Bump the learned version only for effective learned-state changes, excluding `updated_at` and changing diagnostic provenance. Hash actual learned prompt inputs for LLM reuse rather than the entire learner output. Time decay may legitimately cross a promotion threshold without new feedback; preserve that behavior. Deterministic/centroid changes trigger reranking, not an unrelated full LLM refresh.

**Accept:** two learner runs at the same fixed time are identical; later runs below a threshold change preserve request identity; crossing a real threshold changes the relevant score/state. Changing `eval_hints` invalidates evaluation; changing only centroid audit timestamps does not. No repeat paid call for unchanged assembled input.

#### [x] P0-5 — Reconcile eligibility and publish coherent rows (D1, D4, R01, R02; absorbs P2-9)

**Owner/files:** `matching.py`, feedback visibility caller in `main.py`. **Depends:** P0-1, P0-2 and the pure freshness/eligibility policy from P0-6. Wire publication here, then API reads in P0-8; this is a sequence of substeps, not a mutual dependency. **Size/risk:** split eligibility and publication patches / high.

Re-evaluate existing recommendations against current hard rules even if outside the bounded retrieval window; distinguish “not fetched” from “known ineligible”. Record the input/policy revision assessed. Remove eligibility and all ranks for failed rules, inactive jobs or absent enabled occurrences. Only publish a matching evaluation or explicitly compatible pending-refresh approval. Keep deterministic explanation in `deterministic_result`; preserve the linked LLM explanation until replacement succeeds. Make rank refresh operate on the valid publication set and never independently resurrect arbitrary rows.

Distinguish deliberate LLM-disabled mode, missing/misconfigured credentials, cooldown and transient failure. Only deliberate disabled mode may publish deterministic-only results, clearly identified. Do not retain prior approvals that fail current safety/freshness rules. Define NULL handling explicitly rather than relying on SQL three-valued `NOT (...)` behavior.

**Accept:** rematch a formerly accepted job after a new hard exclusion, source disable and expiry; it stays hidden after feedback rerating/rank refresh. A cached approval keeps matching score/action/rationale/evaluation ID. Cooldown creates no unreviewed approval. Fresh rows with NULL LLM fields behave correctly in both explicit disabled and hosted modes. Verify list/count/detail and all ranks in PostgreSQL.

#### [x] P0-6 — Enforce recommendation freshness (B1, R04)

**Owner/files:** retrieval/scoring in `matching.py`, publication/API policy; deadline storage belongs to P1-7c. **Depends:** none for the pure policy/tests; publication integration is owned by subsequent P0-5 and API integration by P0-8. **Size/risk:** M / recall change.

Read `max_age_days` and `drop_past_deadline`; carry publication/first-seen/deadline fields into scoring and stored evidence. Apply the same hard-eligibility result before embeddings/LLM and after semantic promotion, on publication and on API reads so jobs age out between nightly runs. Backfill source deadlines before claiming deadline completeness. Do not set global `jobs.status='expired'` solely for exceeding a profile age preference.

**Accept:** frozen-clock tests for exactly 30 days, just older, future/missing dates, expired/future/date-only deadlines and `drop_past_deadline=false`. An otherwise high semantic score cannot bypass rejection. An old but still-open job remains in the all-jobs catalogue while excluded from this profile's recommendations. Record freshness rejection counts and labelled recall impact, not “expired count becomes nonzero”.

#### [x] P0-7 — Correct feedback visibility across API and UI (D2)

**Owner/files:** `main.py`, visibility SQL in `matching.py`, detail-page hidden note and existing feedback tests. **Depends:** P0-5. **Size/risk:** S / user-visible.

Hide on rating 1 or explicit `not_relevant`; rating 2 is a soft negative. Update the restore branch to permit rating >=2 subject to eligibility, response `recommendation_hidden`, legacy mapping and detail note. Keep `rating<=2` in learning/negative-centroid logic and the restriction against `applied` with a low rating. Rerating cannot override expiry or LLM rejection.

**Accept:** 1→2 restores an otherwise eligible row with all scope ranks; 2 remains visible after nightly matching; 1 hides in every scope; expired/rejected rows remain hidden. Verify the form, POST/GET feedback contract and detail display, including legacy actions.

#### [x] P0-8 — Consistent API visibility and safe response projection (R05, A1, G2)

**Owner/files:** `main.py`, `test_jobs_api.py`. **Depends:** P0-5/P0-6 policy; P0-1 projection. **Size/risk:** S–M / contract.

Use one eligible-source predicate for recommendation items/counts/scope counts/detail and the LLM selector. Do not rely on aggregate lateral joins to filter empty sets. Filter unavailable sources before choosing a non-null application URL. Project only public-safe travel fields; do not expose raw deterministic JSON or private snapshots to solve observability. Keep operator audit fields separate.

**Accept:** PostgreSQL fixture with a disabled-only job returns no item and no count; detail returns the documented unavailable response, never `source_names=NULL` validation failure. A job with one enabled occurrence remains visible with correct attribution. All three totals equal their paginated item sets; no private origin in list/detail/feedback-analysis responses.

#### [ ] P0-9 — Verify and enforce private access (R15)

**Owner/files:** deployment configuration/runbook, web feedback route and API boundary as required. **Depends:** none. **Size/risk:** M / deployment boundary.

Inspect the actual tunnel/reverse-proxy policy read-only. Require authenticated access to private recommendations/feedback and mutations; use the existing deployment access mechanism if it is proven, otherwise a minimal operator credential/session boundary. Ensure direct API access cannot bypass it. Validate form Origin against the configured public origin and reject cross-site mutations; validate route/form IDs and preserve normal form error feedback. Do not add multi-user accounts.

**Accept:** unauthenticated external/private API access fails; legitimate authenticated same-origin read and rating succeeds; cross-origin POST fails without DB mutation. Record the tested routes and effective deployment policy. Until inspected, external exploitability remains unverified; absence of code-level checks is confirmed.

#### [x] P2-17 — Atomic run ownership (**promoted P0**, H3, R10)

**Owner/files:** `collection/runner.py`, `scheduler.py`, relevant run schema/tests. **Depends:** none. **Size/risk:** M / concurrency.

Use database-owned mutual exclusion for source and pipeline runs, including manual commands; prefer PostgreSQL advisory locking with a dedicated connection held for the entire run, including remote awaits. Keep write transactions short, and unlock/close in `finally`; never return the lock-owning session to the pool mid-run. Reconcile abandoned `running` rows only after proving the owner is gone, or use explicit heartbeat/lease ownership if the chosen execution model needs it. Completion updates must match current owner/running state. Do not declare a live run dead solely at 30 minutes. Define coordination between collection, matching, feedback and profile import; prevent concurrent same-profile publication.

**Accept:** two processes compete and exactly one starts; the other records a skip, including while the owner awaits a remote response outside a write transaction. A live long run is not failed; crash releases ownership and restart can reconcile. A stale finisher cannot overwrite terminal status. Use real PostgreSQL concurrency tests; source-run status index only if its query plan needs it.

#### [ ] P0-3 — Bounded, resumable review backlog (D5)

**Owner/files:** existing matching CLI `app.match`, `matching.py`; audit counters. **Depends:** P0-1, P0-2, P0-4, P0-5, P0-6, P2-17 and P2-3 usage accounting. **Size/risk:** M / paid work.

Add a dry-run inventory of distinct currently eligible stale requests, per scope and stale reason. Freeze profile/policy/request revision and a cohort for a backfill; checkpoint completed identity per job and resume without refetching the entire catalogue. Skip current cache hits before assigning paid-call slots. Preserve local/remote reservations but transfer unused slots to remaining stale work. Failed/pre-filtered rows need explicit retry/terminal state so they cannot occupy the first slots forever. Stop on token/cost/time/attempt budget, revision change, quota or cancellation. Reuse daily selection logic; do not add a separate divergent evaluator.

Keep the existing per-invocation cap named/documented as such. A backfill may exceed the ordinary cap only within an explicitly set aggregate budget; report attempted, paid, cached, succeeded, failed, deferred, skipped and remaining separately. Do not launch this paid run as part of plan review.

**Accept:** more than one cap of mixed-scope candidates drains across restarts without duplicate paid calls; an empty reserved scope lends capacity; a failing first row does not starve healthy rows; changed input invalidates the checkpoint. Reconcile starting cohort = completed/current + still stale + ineligible + explicitly deferred/failed. Stable active count alone is not proof of completeness. Estimate spend from measured tokens and the actual remaining cohort, not `2134/800` arithmetic.

### Stage B — source integrity and travel before recall tuning

#### [x] P1-7 — Catalogue lifecycle (B2–B4), split into three patches

**P1-7a: explicit fetch completeness.** Owner: `adapters/base.py`, affected adapters, `collection/runner.py`. Depends: P2-17. Add delta/full-snapshot/partial outcome plus structured error/warning evidence. Propagate exhausted 429/invalid payload instead of `continue` to silent success. Do not advance cursor/removal state on incomplete fetch. **Accept:** legitimate empty delta is success; partial shard failure is visible with cause; a partial run cannot establish absence. No dependency on LLM concurrency.

**P1-7b: resumable Jobly acquisition — promoted P0 for further collection.** Owner: `adapters/jobly.py`, collector cursor persistence. Depends: P1-7a. Globally merge sitemap entries by `(source_id, external_id)` using the existing Jobly external-ID extractor; canonical URL and lastmod are mutable evidence, not a second occurrence identity. Freeze a scan frontier and its member identities, then traverse oldest-first by `(lastmod, external_id)` with durable progress committed only after persistence. The progress tuple means every preceding member of that frozen frontier is durably classified; advance the source high-watermark only when the frontier drains. Persist retries/no-date entries separately using the existing PostgreSQL state pattern. A failed member can be passed only when its retry is durably retained. Reconciliation over current sitemap identities needs its own durable progress, so bounded runs cover the entire sitemap rather than the same prefix; this recovers still-live legacy gaps and late/backdated additions. A single 404 is retryable until source-specific grace/confirmation establishes closure; parse-invalid is not closed. Merely dropping the timestamp filter while fetching the same prefix is insufficient. **Accept:** interleaved sitemap pages, >100 entries, equal/null timestamps, duplicate/alias URLs sharing an external ID, corrected lastmod, transient 404→200, confirmed closure and restart eventually classify every identity with bounded attempts. Inject newer/backdated entries between batches and prove reconciliation reaches them without resetting its cursor; one occurrence retains retry/closure evidence. Report backlog age and cap reached separately from normal watermark stop. Total drained across runs, not a single capped fetch count, proves coverage.

**P1-7c: deadline/closure lifecycle.** Owner: `NormalizedListing`, normalizers, occurrence storage and collector lifecycle. Depends: P1-7a, P1-8a migration safety. Carry source deadline and closure evidence; store occurrence-level state where multiple sources disagree and derive canonical status conservatively. Remove only from a declared-complete snapshot after source-specific grace, explicit closed/detail evidence, or a deadline; unchanged incremental listings survive. Support reopenings and updated deadlines. Backfill known deadlines from retained raw payloads without HTTP where possible. **Accept:** unchanged old live listing survives; all occurrences closed hides; one live occurrence preserves; partial/blocked/failed source cannot remove; past/future deadline and reopening work. Profile freshness never globally expires a job. Keep source capability/poll policy documented in both source docs.

#### [ ] P1-8 — Measured index support (E1–E4), split by query

**P1-8a: occurrence identity.** Owner: migration + `collection/runner.py`. Depends: P2-17. Preflight duplicate `(source_id, external_id)` groups including blank IDs; do not assume yesterday's zero count. Normalize missing IDs consistently, inventory dependent FKs and unique constraints, explicitly merge/cancel conflicting queued/completed enrichment identities before relinking references without losing feedback/enrichment provenance, then add partial uniqueness for meaningful external IDs and race-safe upsert. Verify existing `job_id` index and Alembic 0016 compatibility. Concurrent index builds require a migration autocommit section, invalid-index recovery and lock timeout; test clean and already-indexed databases. **Accept:** duplicate/concurrent fixture, including colliding queued/completed enrichment rows, leaves one occurrence with preserved references and no orphan FKs or dependent uniqueness conflict; schema upgrade/downgrade/failed build are recoverable.

**P1-8b: recency and retrieval queries.** Owner: `matching.py`, list SQL and migration. Depends: P0-6. Capture exact SQL with realistic parameters and `EXPLAIN (ANALYZE, BUFFERS)` in an isolated/restored dataset. Match `DESC NULLS LAST` and actual filter/expression semantics. Try the smallest useful recency/location indexes first; add `pg_trgm` only if a matching expression index is demonstrably used. Full-text search is not a drop-in replacement for current Finnish token/regex behavior. Avoid arbitrary pre-limits that lose old-but-eligible discovery candidates. **Accept:** same labelled eligible candidates before/after; report cold/warm plans and per-pool coverage/latency. Provisional targets: candidate fetch <10 s and list DB query <50 ms on comparable Pi load; revise targets from evidence, separately from provider wall time.

**P1-8c: dedupe query.** Owner: canonical lookup in `runner.py` and migration. Depends: P1-13 canonical rules. Use an indexed normalization expression or stored deterministic key matching existing behavior, with explicit null/date/location semantics; no fuzzy merge expansion in this repair. **Accept:** idempotent same/cross-source fixtures, false-positive fixtures, both source orders, and measured query improvement without merging distinct jobs. Do not add redundant FK indexes when the chosen composite index already serves the query.

#### [ ] P1-9 — External work outside transactions; reliable transit (E1/E5, R06, R08)

**P1-9a:** split matching into read snapshot → network/compute → short conditional writes. Include embedding batches, profile embedding and feedback analysis, not just transit. Preserve per-batch progress when later calls fail. Use savepoints/rollback where a recoverable database error occurs; never `commit()` an aborted transaction. **Depends:** P0-5, P2-17. **Accept:** instrumented tests observe no network call inside an open write transaction; concurrent input change rejects stale publication; DB error does not abort unrelated later candidates.

**P1-9b:** count attempted HTTP calls, including timeout/transport/parse failures, before invoking routing. Distinguish `success`, confirmed `no_route`, transient/provider failure and `not_attempted`; only confirmed no-route becomes `unrouteable`. Apply success-cache TTL (initially seven days, documented) and include routing profile in identity; short provider-wide backoff for auth/quota/transient failure instead of seven-day per-destination poisoning. Expired cache may be shown as stale evidence but cannot certify local eligibility. Prioritize unresolved likely-local destinations; skip lookups for already-confirmed remote/home-city jobs. **Depends:** P1-1 effective policy. **Accept:** budget N makes at most N HTTP attempts under every failure; timeout does not abort matching; key repair permits retry; expired success refreshes; unknown is distinct from no route. Detail GET must use cached evidence, not trigger unbounded paid routing work under API load.

#### [x] P1-1 — Reconcile geographic policy (C1, R09)

**Owner/files:** private profile policy, `travel_policy.py`, `main.py`, distance/architecture docs. **Depends:** none for decision reconciliation; P0-5 for revision publication. **Size/risk:** M / product semantics.

Retain current three-scope policy. Mark old inferred 200 km anchor rules as superseded/deferred in the private profile/docs, keeping city preferences as scoring evidence. Do not add geocoding merely to satisfy stale prose. Persist an effective policy fingerprint including safe home-city identity, configured origin, duration and routing rules. Only take a home-city shortcut when it agrees with the effective origin city; if that cannot be established, use routing or unknown. Reassess existing shortcut rows when origin/profile changes; no blanket API bypass for `exact_home_city`.

**Accept:** far on-site/hybrid remains nationwide-only; full remote qualifies for local+remote; exact 7200/7201-second boundary follows configured limit. Changing origin/home city/limit invalidates stale local eligibility before refresh. Unknown locations remain nationwide with explanation, never silently local. Any future global radius requires an explicit revised product decision and geocoding evidence.

#### [ ] P1-2 — Work-mode provenance through every path (C2, R09)

**Owner/files:** `location.py`, `travel_policy.py`, retrieval marker predicates; detail/UI fallbacks. **Depends:** P1-1. **Size/risk:** M / membership.

Separate extracted source evidence/confidence from display `/ Etä`. A synthesized suffix cannot prove full remote. Use explicit positive full-remote evidence and handle negation, hybrid and conflicting signals conservatively. Recompute historical locations from raw source evidence; fixing future extraction alone leaves poisoned suffixes. Use the same classification in retrieval, scoring, detail evidence and UI; remove duplicated optimistic fallback behavior.

**Accept:** raw → normalize/enrich → persist → match → API/UI fixtures for `etätyömahdollisuus`, `mahdollisuus etätyöhön`, hybrid, explicit full remote, negated full remote and conflicting source signals. Ambiguous/remote-first with occasional attendance stays unverified until evidence supports full remote. Report affected historical rows; do not assert the old count of 25 is current.

### Stage C — complete the profile and feedback contract

#### [x] P1-12 — Validated, revisioned profile updates (R07)

**Owner/files:** `profile.py`, learner write in `feedback_learning.py`; private deployment runbook. **Depends:** P0-1 projection contract, P2-17 ownership. **Size/risk:** M / private data.

Validate consumed schema/version, list/object types, bounds and supported policy directives at import; report unsupported fields rather than silently claiming enforcement. Preserve DB-owned `learned` and `preferences.learned_boosts` on ordinary profile import; make reset a distinct explicit operation. Update only learner-owned subfields or use optimistic revision checks so learner/import cannot overwrite each other. Store base-profile revision/hash and source provenance without private text in logs. Import marks affected requests/policy stale.

**Accept:** malformed/unknown policy import fails before DB write; two competing learner/import writes preserve both user edits and derived state or retry cleanly. Reimporting unchanged profile is idempotent. Three application sources/signals survive import and learning. Private backup can restore the previous revision.

#### [ ] P1-3 — Required-language evidence (C3, R04)

**Owner/files:** profile validation and scoring gates. **Depends:** P1-12, P0-6 shared eligibility. **Size/risk:** M / false rejection.

Interpret supported language requirements using bounded phrase/level rules. Distinguish mandatory vs preferred, alternatives (`suomi tai ruotsi`), negation and descriptive employer text. Honor explicit per-language policies, including reject-if-required for beginner languages; do not assume merely mentioning a language is a requirement. Unknown levels or ambiguous requirements become a caution/review reason, not invented certainty.

**Accept:** explicit mandatory German/Ukrainian and above-supported levels reject; optional language, “not required”, alternative supported language and ordinary supported FI/EN/SV requirements pass. Record matched text/reason/level; semantic promotion cannot bypass a language failure. Use sanitized Finnish/English fixtures.

#### [x] P1-4 — Qualification-aware and phrase-aware exclusions (C4, R03/R04)

**Owner/files:** `matching.py`, consumed profile policy. **Depends:** P1-12, shared eligibility. **Size/risk:** M / recall.

Keep hard title exclusions as title phrases with word boundaries, not individual tokens found anywhere. For qualifications, distinguish actual required credentials from generic employer descriptions, “not required” and alternative routes; respect available credentials and caution-only ordination/youth-work checks. Drive supported phrase rules from `qualification_checks`; preserve explicitly documented hard driver exclusions. Remove undocumented assistant rejection after reviewing the labelled sample. Delete dead legacy-key reads only after profile compatibility validation.

**Accept:** the `kaluston` library fixture passes; healthcare employer text does not reject an admin role; mandatory missing licence rejects; B-only job passes; optional/negated requirement passes; known library qualification passes; uncertain ordination remains caution. Keep a regression for every supported qualification policy type and semantic bypass.

#### [ ] P1-5 — Explainable cluster weights and skill evidence (C5)

**Owner/files:** `matching.py`, positive embedding projection, benchmark. **Depends:** P1-12/P1-4, P2-23 baseline labels. **Size/risk:** M / ranking calibration.

Implement documented relative cluster weights with per-cluster contribution evidence and bounded score normalization; define overlap handling so duplicate clusters do not multiply the same evidence. Treat `eligibility=requires_check` as a qualification caution, not an arbitrary numeric penalty pretending to prove eligibility. Include positive supported skills in embedding/scoring input without negatives dominating retrieval. Correct private analysis wording to the implemented formula.

**Accept:** controlled weight changes predictably change contributions/order; duplicate terms cannot inflate score; a licensed-but-unverified role retains a caution; no weighted score overrides a hard rejection. Compare labelled top-k recall and score saturation before enabling. Reference P2-10 for benchmark constants, not the old incorrect P2-7 cross-reference.

#### [ ] P1-6 — Discovery evidence survives the budget (C6, C7)

**Owner/files:** `discovery_pool_terms`, retrieval provenance and lane assignment in `matching.py`. **Depends:** P1-12; P1-8b performance validation. **Size/risk:** S–M / recall.

Deduplicate normalized application-history title phrases first, then learned discovery queries, cluster titles and configured queries within the bounded term budget. Preserve phrase meaning rather than splitting all multiword titles into loose tokens. Record counts/source categories of dropped terms without logging private free text. Attach `learned_discovery` provenance/lane when that retrieval path actually contributes a job; adding query text alone cannot make its benchmark nonzero. Record overlap with other pools and term/pool truncation.

**Accept:** >80 competing terms retains `kirjastovirkailija` and `tietoasiantuntija`; learned-query-only fixture enters the pool and carries the lane into feedback snapshot; duplicated phrases consume one slot. Compare all three application fixtures, old/new candidate IDs, pass set and top-k changes. A full budget emits a bounded warning and explicit coverage count.

#### [ ] P2-8 — Retryable and race-safe feedback (**promoted P1**, D3, R06/R13)

**Owner/files:** `main.py`, `feedback_analysis.py`, `feedback_learning.py`; small migration for attempts/revision if needed. **Depends:** P0-2, P0-7, P1-9a, P1-12. **Size/risk:** split retry and snapshot patches / medium.

Keep transient cooldown/provider errors pending with `next_attempt_at`, attempt budget and reason; classify permanent input/config errors. Requeue historical failed/skipped feedback explicitly. Claim/snapshot work in a short transaction, call outside it, and publish only if feedback revision/content hash still matches; stale response cannot complete newer feedback. Derive snapshot evaluation/provider/version from the linked row, not current settings; preserve reviewed job/policy facts. Repeating identical rating/comment should reuse its evidence snapshot rather than create work merely because ranks changed. Do not turn an incorrect old system rationale into the justification for a user-corrected few-shot example; use grounded feedback explanation or omit the reason.

**Accept:** identical submission is idempotent; edit during provider call discards old response; cooldown later retries; permanent failures stop within budget; resubmission remains supported. Changed rank alone does not create a paid analysis. Failed DB write rolls back and next candidate can proceed. Snapshot provenance remains accurate across prompt changes.

#### [ ] P1-13 — Stable canonical content (R11)

**Owner/files:** `collection/runner.py`, `enrichers/repository.py`. **Depends:** P1-7a/P1-8a. **Size/risk:** M / dedupe identity.

Choose deterministic source/field precedence with source timestamp/quality evidence; preserve occurrence attribution and dates. Do not let a short nonempty summary replace a complete enriched description. Preserve meaningful source edits that correct obsolete content, and invalidate downstream input hashes when canonical meaning changes. Keep merges explainable and retain recommendation/feedback IDs; no new fuzzy merging algorithm.

**Accept:** collect conflicting sources in both orders and get identical canonical output; richer description survives weaker summary, genuine updated requirements invalidate evaluation/embedding, and distinct openings stay distinct. Repeated identical collection changes no canonical hash.

#### [x] P1-14 — Enrichment queue progress and stale-result guard (R12)

**Owner/files:** `enrichers/repository.py`, runner. **Depends:** P1-13/P1-9a. **Size/risk:** M / stale data.

Exclude already-enriched/current queued identities in SQL before candidate LIMIT. Apply results only if current occurrence, source-enabled status and input hash match the claimed input; otherwise retain attempt evidence and requeue current work. Preserve queue retry/backoff limits and source polling constraints. Do not rewrite source provenance using the current raw ID for an older response.

**Accept:** more than a page of already-handled short descriptions cannot starve the next eligible job; edit/relink/disable during HTTP causes old response to be ignored; retries terminate; new input becomes eligible again. Queue/status counts reconcile with attempts.

#### [x] P1-15 — Validate upstream URLs (R16)

**Owner/files:** `source_links.py`, enrichment navigation boundary and source URL normalizers. **Depends:** none. **Size/risk:** S–M / trust boundary.

Require HTTP(S) links for rendered external actions. For server/browser collection, constrain each source's scheme/host/path to its documented domains, including redirect targets; reject loopback/private destinations and unexpected external navigation. Keep legitimate employer application links displayable without permitting them as arbitrary collection targets. Validate external IDs before interpolating URL paths. Keep raw rejected value private for troubleshooting and expose a safe reason code.

**Accept:** malformed, `javascript:`, non-HTTP, private/loopback, wrong-host and redirect escape fixtures cannot navigate/fetch; legitimate source pages and employer application links work. Untrusted job descriptions remain data in prompts and escaped content in UI; no provider tools or instruction following from job text.

### Stage D — efficiency, UI and operations

The rows below retain every original P2/performance item with a concrete disposition. “Folded” means implemented and verified with the named owning task, not silently dropped.

| Task / priority | Change and scope | Dependencies | Acceptance / verification |
|---|---|---|---|
| P1-10 / P2 | Split bounded location reprocessing from retention. Track raw-content/normalizer revision so changed inputs are reparsed once. Then retention dry-run by table/status with private archive and required provenance/attribution preserved; analyze tables after actual bulk changes. No automatic blanket raw-payload compaction. | P1-7c, P1-13, P1-14, P2-18 | Unchanged location run does bounded work; changed source is reprocessed; dry-run reports rows/bytes; restore drill passes; retained feedback/enrichment source references and TMT attribution still resolve. Expire embeddings only under explicit retention policy. |
| P1-11 / P2 | Cache the engine per process and dispose at shutdown; avoid pre-fork sharing. Build one immutable scoring context per profile revision; batch cache reads and feedback visibility predicates where profiling justifies it. | P0-2, P0-5 | Repeated calls share intended engine; tests/config overrides do not retain wrong URL; shutdown disposes; identical scores and measured connection/CPU reduction. Separate engine patch from scoring optimization. |
| P2-1 / P2 | Implement documented bounded evaluation concurrency after budgets/publication work; use existing primitives, no queue service. Configure worker count explicitly; separate thread/task DB connections and stop dispatch on cooldown. | P0-3, P1-9a, P2-17 | Fake provider measures <= configured simultaneous calls and aggregate attempt cap; cancellation/retry does not duplicate publication; measured throughput gain. Concurrency never bypasses paid budget. |
| P2-2 / P2 | Defer escalation until labelled errors demonstrate benefit; document setting as unsupported and remove misleading operational promises. Add escalation only with explicit triggers, incremental cost accounting and model provenance. | P2-23, P2-3 | Config/docs/startup agree that no escalation currently occurs; no claim of support based on an unused env variable. Any later implementation has paired accuracy/cost evidence. |
| P2-3 / **P0 prerequisite to backfill** | Persist normalized input/output/cached token usage, latency, returned model, request ID where available, attempts and outcome for evaluation/feedback. Count embedding/transit costs separately. Use observed provider usage; flag missing usage. | P0-2 | Mock usage survives DB insert/API-safe audit; attempted/paid/cache/failure counts reconcile; dry-run estimate has assumptions; backfill stops at declared aggregate budget including retries. No prompts/keys in logs. |
| P2-4 / P1 | Classify configuration/auth/quota/transient/schema failures. Add one bounded parse/schema retry beyond existing transport retries, accounted in total budget. Validate config locally at startup; explicit provider smoke check at rollout, not paid probes on every healthcheck. | P2-3, P1-9a | Bad config yields one actionable failure; quota stops dispatch; retry ceiling holds across nested retry mechanisms; malformed/refused/incomplete outputs never activate. |
| P2-5 / folded | Provider/model/request/schema identity and invalid-cache recovery. | P0-2 | P0-2 tests; do not hash unknown future `returned_model`. |
| P2-6 / P2 | Move profile-specific calibration guidance into validated private profile fields while keeping generic fact-grounded instructions in code. Current library/Swedish rules are conditional; preserve their behavior during migration. | P1-12, P2-23 | Equivalent sanitized fixtures retain intended qualification interpretation; unrelated profile gets no invented degree/language. Prompt identity changes automatically. |
| P2-7 / P2 | Shorten payload with measured token evidence and requirement-aware job excerpts. Keep decisive requirements/deadlines near the end of long descriptions; stable prefix layout without dropping required facts. | P2-3, P2-23 | Paired evaluations of long/short descriptions retain qualification/remote/deadline decisions; actual token reduction measured; beyond-4000-character requirement fixture is retained. No unsupported exact token claims. |
| P2-9 / folded | Separate disabled provider from cooldown/misconfiguration and preserve only eligible compatible approvals. | P0-5 | Hosted outage never publishes deterministic-only results. |
| P2-10 / P1 | Read actual tuning constants; name metrics by true denominators (labelled negative share, labelled recall@k). Compute per-scope from visible API order/active status; distinguish raw vs adjusted vector score and actual vs estimated saved calls. | P0-5, P2-8, P2-23 | Hand-counted tiny dataset matches metrics including unrated/inactive rows; no claim of population FPR/recall from biased feedback. Remove duplicate constants only after consumer check. |
| P2-11 / P1 | Casefold learned exclusion text consistently; retain rating-2 negative support. Record centroid contributing job IDs/count/model/dimension and missing-embedding counts. | P1-12 | Mixed-case fixtures produce equal penalties; distinct-job support threshold holds; missing vectors are visible, no incompatible model vectors mixed. |
| P2-12 / P2 | Use explicit Europe/Helsinki timezone in every user-visible date formatter, including detail page; do not rely on container TZ alone for client/server parity. | none | Winter/summer UTC midnight fixtures display correct Finnish date/time; typecheck/build plus browser check. |
| P2-13 / P2 | Render fit tier/publication freshness and safe explanation/provenance fields required by `goal.md`. Add vector score with its meaning; operator evaluation ID/metadata may stay in detail/audit. Align list/detail types. | P0-8, P0-5 | API contract test and browser cards show consistent score/tier/date; absent vector handled; no private origin or raw snapshot exposed. |
| P2-14 / P1 | Implement recommendation pagination using existing backend limit/offset, independent of all-jobs offset. Preserve filters/scope in navigation and label shown range/total. Keep current extra-scope-first order unless a separately recorded product decision changes it. | P0-8 | >30 recommendations accessible in each scope; no duplicates/skips in a fixed snapshot; scope change resets recommendation offset; job filters survive. |
| P2-15 / P2 | Use safe configured origin-city label or neutral “Työmatka”; return safe policy/commute label so scope controls reflect changed duration. Remove hardcoded Oulu/2h from all fallback UI paths. | P0-1, P1-1, P1-2 | Non-Oulu origin and 90-minute setting produce correct labels in list/detail/fallback; no street address reaches HTML/JSON. |
| P2-16 / **P1** | Persist pipeline skip/degraded status and reason; bounded catch-up after source run completes. Separate liveness from data freshness/last-success diagnostics, expose safe status in existing API/source view. Do not fail container liveness solely because a source is stale. | P2-17, P1-7a | Busy scheduled slot records skip and later one catch-up; repeated restart does not duplicate it; partial collection/LLM failure is not reported as full success; stale-source readiness/status degrades while liveness remains meaningful. |
| P2-18 / **P0 before migrations** | Run backup tools with the database container's actual `POSTGRES_USER`/`POSTGRES_DB`, not undefined Make variables. Use a temporary archive then atomic rename after success. Back up DB dump, ignored profile originals/extracts and configuration/secrets as separately restricted or encrypted artifacts outside the repo; pg_dump alone does not include files. | none | Plain `make backup-db` with custom credentials yields valid `pg_restore --list`; failed dump leaves no apparently valid final archive; disposable restore verifies schema/row counts/profile plus the ignored application artifacts and usable configuration; check archive permissions and checksum after rename. Restore target is explicit; never clean production to test. |
| P2-19 / P1 | Normalize timestamps to aware UTC before TMT formatting. Verify Laura endpoint timezone semantics with a controlled read-only boundary probe/documented response rather than assuming WordPress site timezone. Use safe overlap+idempotence until proven. | P1-7a | Equivalent offset timestamps produce same UTC TMT filter; DST/boundary update fixtures lose no rows; source docs record verified semantics or remaining uncertainty. |
| P2-20 / P1 | Keep private profile tree ignored; correct private README's tracked-file claim. Define private versioned backups plus revision/hash comparison on import/deploy; recoverable derived state. | P1-12, P2-18 | Private restore works and three application artifacts are present; `git status` contains no private profile/CV; drift check reports only safe identifiers/hashes. |
| P2-21 / P1 | Reconcile private analysis and public README with actual inputs/scoring; remove nonexistent-key and radius/language claims until those gates ship. | P1-1/P1-3/P1-5 | Every claimed profile field maps to a tested consumer or explicitly deferred feature; raw CV is not described as current embedding input. |
| P2-22 / P2 | Mark absent endpoints as proposed, not shipped; reconcile hosted concurrency/escalation/usage/status docs as tasks land. Do not implement profile/pipeline endpoints merely to match stale design text. | respective owner tasks | Route/schema inventory agrees with current docs; source docs stay synchronized; journal distinguishes plan from shipped result. |
| P2-23 / **P1, before calibration changes** | Build a private labelled evaluation set across Oulu/local, nationwide, remote, three application-role families, non-obvious roles, hard rejections, not-retrieved jobs, unreviewed jobs and accepted jobs. Use skipped-local sample (~50) as one stratum, not the whole benchmark. | baseline snapshot; repeat after P0 gates | Report per-stage recall/precision on the labelled set, sample method, denominators and uncertainty; separate transition loss from relevance. Compare before/after on fixed inputs; user labels determine relevance. Do not tune for nonzero local results as a target. |

## 6. Dependency order and rollout checkpoints

`A → B` means implement A before B.

1. **Baseline and protection:** retain original dirty work; capture safe code/config/profile revision, current schema and source/backlog status. P2-18 backup/restore verification, P0-1 privacy, P0-9 access verification and P2-17 ownership can proceed independently. Do not wait for paid backfill to close privacy.
2. **Correct publication:** P0-2 identity → P0-4 stable learner → P0-5/P0-6 eligibility → P0-7/P0-8 feedback/API. Include P2-3 accounting. Test against disposable PostgreSQL, including upgrade from current schema and current mixed-version rows.
3. **Protect acquisition and evidence:** P1-7a → P1-7b; P1-8a → P1-7c; P1-1/P1-2 travel provenance; P1-9 external-call safety. Roll out source cursor repair before more truncated Jobly runs. Reconcile still-live missed URLs with bounded source-respecting work.
4. **Recover review coverage:** dry-run P0-3 on a frozen eligible cohort after prerequisite fixes. Produce exact remaining calls, estimated token range, compatible-vs-invalid approvals and stop budget. Execute paid recovery only as a separately authorized operation. Resume to terminal accounting, not to an arbitrary number of nightly runs.
5. **Improve recall:** P1-12 validated profile, P2-23 labelled baseline, P1-3/P1-4 gates, P1-8b retrieval measurement/index support, P1-6 discovery, then P1-5 weights. Apply one policy change at a time with paired fixed-input metrics; widen safe retrieval only with coverage evidence.
6. **Finish related features:** P1-13/P1-14 enrichment/dedupe correctness, P1-15 URL boundary, P2-8 feedback reliability, P2-16 catch-up, P1-8c dedupe query optimization and remaining measured tuning, then deferred efficiency/UI/doc work. P1-8a and P1-8b have already run at their dependency points. Concurrency comes after correctness, ownership and budget checks.

**Checkpoint before each rollout:** relevant unit/contract checks, real DB tests for SQL/state changes, schema compatibility and rollback plan, bounded metrics and journal entry describing only what actually shipped. `make test-regression` before source changes and after adapter changes; `make docker-config` for Compose validation (never raw interpolated config). No commits unless requested.

**Rollback:** keep additive schema compatible with old readers until migration verified. Use feature/config rollback for scoring/calibration; preserve evaluations, feedback and cursor evidence. Never roll back by resending forbidden profile fields or restoring known-invalid source cursors. Keep old approvals only under the compatibility/eligibility rule in §3. Destructive retention waits for a tested restore and reviewed row inventory.

## 7. Verification plan and definition of done

### Required integration fixtures

Reuse existing pytest and the project's PostgreSQL/pgvector stack; no SQLite substitute for PostgreSQL semantics. Add a small disposable-database test target because SQL-string mocks cannot validate these failures:

- Lifecycle: two source occurrences, complete vs delta/partial fetch, deadline, closure and reopen.
- State: current/missing/stale/invalid evaluations; current hard pass/fail; compatible pending approval; rating 1/2 transitions; inactive job; disabled-only source; NULL scores.
- Concurrency: two run starts, feedback edit during analysis, source edit during enrichment, profile edit during learning, SQL failure followed by successful next item.
- Backlog: more than two caps, empty scope reservation, all-cache-hit batch, retryable/permanent error, input revision change and restart.

Use synthetic profiles with realistic policy structure and unique forbidden sentinels. Keep private CV/application content and provider responses out of committed fixtures. Use source fixtures for deterministic edge cases; live regression probes establish availability only.

### Production verification packet for implementation

Capture UTC timestamp, code/schema/config/profile fingerprints, run ID and immutable cohort identity with every measurement. Report:

- eligible → selected → cached/attempted/succeeded/failed/deferred/skipped → published counts, per scope and stale reason;
- overdue deadline/profile-age recommendations, inactive/disabled-only linked jobs, stale identity publications and mixed explanation provenance (target zero outside explicitly compatible pending rows);
- source completeness, pending URL count/oldest age, cursor progression, duplicate identity count and lifecycle evidence;
- token usage, external attempts, latency by phase, query plans/buffers, pool connections and retention sizes;
- labelled quality before/after with selection coverage and uncertainty.

Never use “expired rows increased”, “recommendation total stabilized”, “grep now finds the key”, “0 local accepts”, or a green mocked suite as sole acceptance evidence. The desired result is valid, discoverable and explainable jobs within explicit budgets, not a forced number of recommendations.

### User-facing acceptance

Real browser checks on desktop/mobile: all three scopes, >30-item paging, changed commute duration/origin label, job search state preservation, unknown/expired/source-unavailable states, rating 1→2, feedback error/retry, keyboard focus and screen-reader labels. Verify generated HTML/network responses contain no private origin/contact fields. A web build alone does not prove these flows.

## 8. Review completion and next implementation handoff

This planning milestone covers every original task (P0-1–P0-7, P1-1–P1-11, P2-1–P2-23) plus R01–R16. Crosswalk: A1→P0-1/P0-8; B1→P0-6; B2–B4→P1-7; C1–C7→P1-1–P1-6; D1/D4→P0-5; D2→P0-7; D3→P2-8; D5→P0-3; E1–E8→P1-8–P1-11; F1–F10→P0-2/P0-4/P2-1–P2-7; G1–G5→P2-12–P2-15/P2-22; H1–H5→P2-16–P2-19; I1–I5→P2-20–P2-22. MATCH-7/9/11 retain P2-10/P2-11 ownership.

**Changed by this review:** this plan only. Existing source/migration/profile/deployment/journal work is preserved. **Validation:** live regression passed; backend 199 passed; web typecheck passed; web production build passed. **Remaining:** implement the unchecked tasks, obtain new production measurements, user relevance labels and deployment/browser/restore evidence. Begin with Stage A; do not start with a bulk paid rerun or radius filter.
