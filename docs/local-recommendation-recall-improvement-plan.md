# Local Recommendation Recall Improvement Plan

**Updated:** 2026-07-05  
**Status:** first implementation slice shipped locally after live algorithm review  
**Related:** [`goal.md`](goal.md), [`architecture.md`](architecture.md), [`recommendation-distance-scope-plan.md`](recommendation-distance-scope-plan.md), [`feedback-learning-plan.md`](feedback-learning-plan.md), [`implementation-journal.md`](implementation-journal.md)

The scope toggle now works mechanically, but the local scopes are still too sparse:

- `commutable`: 1 recommendation
- `commutable_or_full_remote`: 1 recommendation
- `nationwide`: 15 recommendations

This is not only a UI/API issue. The current pipeline finds local candidates, then loses them across candidate-window selection, LLM filtering, and final active-row policy. The product needs a recommendation algorithm that preserves local recall while still protecting the user from clearly bad matches.

## Evidence From Live State

Diagnostics were run against the local Compose stack on 2026-07-05.

Current stored state:

| Metric | Count |
|---|---:|
| Active jobs visible through enabled sources | 6,163 |
| Active jobs with `location = 'Oulu'` | 324 |
| Active recommendations | 15 |
| Active `commutable` recommendations | 1 |
| Active `commutable_or_full_remote` recommendations | 1 |
| Active recommendations with LLM evaluation | 15 |
| Stored LLM evaluations | 150 |
| Job embeddings | 1,041 |
| Transit cache rows | 70 |

Read-only simulation of the current deterministic matcher:

| Candidate set | Jobs examined | Deterministic passes | Local exact/home-city passes without route cache | Full remote passes |
|---|---:|---:|---:|---:|
| Latest 500 | 500 | 139 | 15 | 2 |
| Latest 1,000 (`MATCHER_MAX_JOBS`) | 1,000 | 235 | 27 | 3 |
| All active visible jobs | 6,163 | 1,400 | 258 | 18 |

With the existing transit cache applied to the latest 1,000 deterministic passes:

| Travel result | Count |
|---|---:|
| Over commute limit | 132 |
| Vague location | 39 |
| Exact home city | 27 |
| Unknown transit | 18 |
| Within commute limit | 9 |
| Over limit hybrid | 7 |
| Full remote | 3 |

The latest 1,000 therefore contain about 36 local deterministic candidates (`exact_home_city` + `transit_within_limit`) before LLM filtering, but only one local recommendation remains active.

## Current Algorithm States

### 1. Source Collection

Collection is not the immediate bottleneck. The database contains thousands of active jobs and hundreds of Oulu jobs. The local scopes are sparse even though local source data exists.

Open issue: some useful local jobs are older than the global `MATCHER_MAX_JOBS` recency window, so they never reach matching.

### 2. Candidate Window

`run_deterministic_recommendations()` reads active jobs ordered by `published_at desc, id desc` and stops at `MATCHER_MAX_JOBS`.

Effect:

- The matcher optimizes for global recency before it knows which jobs are local.
- Only 31 Oulu-location jobs appear in the latest 1,000, even though 324 active Oulu jobs exist.
- All-active deterministic scoring finds 258 local exact/home-city passes, but the matcher never sees most of them.

Root issue: the candidate window is global, not scope-aware.

### 3. Deterministic Scoring

`score_job()` produces many local deterministic passes, but it is too broad in places:

- Application-history and transferable-duty matches pull in local jobs that the LLM later judges as not applicable.
- Local low-fit roles such as restaurant, construction, technical trades, and sales training can pass deterministic scoring because they share weak terms.
- Hard negative and missing-qualification gates exist, but many unsuitable local jobs are only caught later by the LLM.

Root issue: deterministic scoring is doing both recall and quality gating with one score, then relying on the LLM to clean up.

### 4. Travel Assessment

Travel policy is useful but not yet robust enough as a local recall primitive.

Observed behavior:

- `Oulu` and `Oulu / Hybridi` become `exact_home_city`.
- `Oulu, Pohjois-Pohjanmaa` becomes `exact_home_city`.
- `Helsinki, Kuopio, Oulu` becomes local only if the route cache is available and `Oulu, Finland` is chosen as the best destination.
- `Pohjois-Pohjanmaa` is unknown unless route lookup exists.
- `Suomi` is vague.
- `Etä` is full remote.

Root issue: multi-city listings that contain the home city are too dependent on route lookup, and region-level local labels are not mapped to local towns before routing.

### 5. Ranking And Lane Quotas

`rank_scored_candidates_for_review()` reserves lane slots, then appends the rest by score. This helps hidden opportunities reach the LLM, but it does not enforce per-scope coverage.

Root issue: ranking preserves recommendation lanes, not UI scopes. A local scope can end up with too few final items.

### 6. LLM Evaluation

The LLM stage is the main final drop-off for local scope.

Current facts:

- `LLM_EVAL_MAX_JOBS` was 50 during this diagnostic run.
- The current run evaluated 50 jobs and ended with 15 active recommendations.
- Active recommendations require LLM review when a provider is configured.
- Most local candidates that reached recommendation rows were deactivated as `skip` / `not_applicable`.

This is sometimes correct. Many local deterministic candidates are genuinely poor fits. But the current policy has no fallback when local LLM-approved count is below the product minimum.

Root issue: final visibility is LLM-approved-or-hidden, with no scope-aware fallback tier or minimum coverage policy.

### 7. API Scope Filtering

The API now returns scoped rows correctly, but it can only show rows that survived final active filtering. It cannot compensate for candidate loss upstream.

Root issue: the API is a view over active rows; it is not the right layer to create recall.

## Product Policy To Add

Each recommendation scope should have a coverage target, not an absolute guarantee detached from data quality.

Proposed default targets:

| Scope | Target | Quality rule |
|---|---:|---|
| `commutable` | At least 8 items when enough local candidates exist | LLM-approved first, deterministic fallback allowed only without hard rejects |
| `commutable_or_full_remote` | At least 12 items when enough local/remote candidates exist | Same as above, full remote included |
| `nationwide` | At least 15 items | LLM-approved first |

If the target cannot be met, the API should return a structured `coverage` object explaining the limiting stage:

```json
{
  "target": 8,
  "available": 3,
  "limiting_stage": "llm_quality_gate",
  "message_fi": "Paikallisia ehdokkaita löytyi, mutta suurin osa hylättiin soveltumattomina."
}
```

Do not silently pad with bad jobs. The system should preserve recall, but still explain when local supply or quality is genuinely weak.

## Implementation Plan

### Phase 1: Add Matcher Funnel Diagnostics

**Description:** Persist or expose counts for every matching stage so sparse tabs can be explained without ad hoc SQL.

**Acceptance criteria:**
- [ ] Each matching run records counts for: active visible jobs, candidate window, deterministic passes, local passes, full-remote passes, LLM-selected, LLM-approved, LLM-skipped, active rows per scope.
- [ ] Counts include top drop reasons: below deterministic threshold, negative terms, missing qualification, over commute limit, unknown transit, LLM skip, rating hidden.
- [ ] `/sources/status` or a new internal status endpoint exposes the latest matcher funnel.

**Verification:**
- [ ] Backend test covers funnel aggregation from synthetic rows.
- [ ] Manual check shows why `commutable` has fewer than target count.

**Dependencies:** None  
**Files likely touched:** `backend/app/matching.py`, `backend/app/main.py`, `backend/tests/test_matching.py`, `backend/tests/test_jobs_api.py`  
**Estimated scope:** Medium

### Phase 2: Split Candidate Pooling By Scope

**Description:** Replace the single global latest-job window with explicit source pools that preserve local recall before scoring.

Candidate pools:

- `recent_nationwide`: current latest `MATCHER_MAX_JOBS` behavior.
- `local_exact`: all active jobs whose normalized location includes the home city or configured local towns, capped separately.
- `local_region`: active jobs in configured nearby regions/towns.
- `full_remote`: active jobs with verified full-time remote signal.
- `semantic`: vector-retrieved jobs from the embedding index.

Deduplicate by `job_id` before scoring.

**Acceptance criteria:**
- [ ] Matcher evaluates local pool candidates even when they are older than the global recency cutoff.
- [ ] Local pool cap is configurable, for example `MATCHER_LOCAL_MAX_JOBS`.
- [ ] The current local dataset produces more than the single existing local candidate before LLM filtering.

**Verification:**
- [ ] Unit test proves an older Oulu job outside the latest global window is still scored.
- [ ] Matcher funnel shows local pool input and deterministic pass counts.

**Dependencies:** Phase 1 helpful but not required  
**Files likely touched:** `backend/app/matching.py`, `backend/app/config.py`, `.env.example`, `backend/tests/test_matching.py`  
**Estimated scope:** Medium

### Phase 3: Make Travel Assessment Local-Aware Before Routing

**Description:** Normalize locations into structured travel candidates before route lookup and classify home-city membership without requiring Google Routes.

Rules:

- Any location candidate equal to `home_city` qualifies as local with status `contains_home_city`.
- Exact single-city home rows remain `exact_home_city`.
- Region labels such as `Pohjois-Pohjanmaa` expand into configured local towns before routing.
- Route lookup is used for non-home-city candidates and nearby towns, not to prove that `Oulu` is local.

**Acceptance criteria:**
- [ ] `Helsinki, Kuopio, Oulu` and `Kuopio, Oulu, Turku / Hybridi` qualify as local because they explicitly include Oulu.
- [ ] `Pohjois-Pohjanmaa` can resolve through configured local-region mappings instead of staying permanently vague.
- [ ] `travel_reason_code` distinguishes `exact_home_city`, `contains_home_city`, `local_region_candidate`, and `transit_within_limit`.

**Verification:**
- [ ] Travel policy tests cover multi-city home-city membership, region expansion, hybrid, and full remote.
- [ ] Existing route-cache behavior remains unchanged for non-home destinations.

**Dependencies:** None  
**Files likely touched:** `backend/app/travel_policy.py`, `backend/app/matching.py`, `backend/tests/test_travel_policy.py`  
**Estimated scope:** Medium

### Phase 4: Separate LLM Approval From Recommendation Visibility

**Description:** Add a fallback recommendation tier so local scopes can show deterministic candidates when LLM-approved supply is below target, without pretending the LLM approved them.

Recommended row states:

| State | Meaning | UI/API behavior |
|---|---|---|
| `llm_approved` | LLM says apply/consider and quality gates pass | Normal recommendation |
| `deterministic_fallback` | Deterministic local candidate used to meet scope coverage | Visible with lower-confidence label |
| `llm_rejected` | LLM says skip/not applicable | Hidden by default, inspectable in diagnostics |
| `feedback_hidden` | User rating 1 / not relevant | Hidden |

Fallback eligibility:

- Must be in the requested scope.
- Must not have hard negative terms or missing qualification rejects.
- Must meet a higher deterministic threshold than ordinary recall, for example `machine_score >= 35`.
- Must not have an LLM `not_applicable` result unless a later deterministic/profile version invalidates that evaluation.

**Acceptance criteria:**
- [ ] `commutable` can include deterministic fallback rows if LLM-approved rows are below target.
- [ ] API returns `recommendation_status` or equivalent so UI can label fallback rows honestly.
- [ ] LLM-rejected rows are not blindly reintroduced.

**Verification:**
- [ ] API test covers a scope with one LLM-approved row and several fallback-eligible rows.
- [ ] UI shows fallback status without implying the job is strongly recommended.

**Dependencies:** Phase 1, Phase 2  
**Files likely touched:** migration, `backend/app/matching.py`, `backend/app/main.py`, `web/app/page.tsx`, backend and web tests  
**Estimated scope:** Large, split into schema/API and UI slices

### Phase 5: Allocate LLM Review Capacity Per Scope

**Description:** Reserve LLM capacity for local and remote candidates before nationwide fill, and avoid spending local quota on obvious hard rejects.

Proposed default allocation for `LLM_EVAL_MAX_JOBS=800`:

- 320 `commutable`
- 160 `commutable_or_full_remote` not already commutable
- 320 nationwide or semantic hidden opportunities

Within each bucket, order by deterministic score, lane priority, feedback learning, and recency.

**Acceptance criteria:**
- [ ] Local candidates receive review capacity even when nationwide hidden opportunities score higher.
- [ ] Hard-reject deterministic rows do not consume LLM calls.
- [ ] Matcher funnel records selected/evaluated/approved counts by scope.

**Verification:**
- [ ] Unit test proves local LLM quota is filled before nationwide overflow.
- [ ] Current data run evaluates enough local candidates to explain whether local scarcity is quality-driven.

**Dependencies:** Phase 2, Phase 3  
**Files likely touched:** `backend/app/matching.py`, `backend/tests/test_matching.py`  
**Estimated scope:** Medium

### Phase 6: Calibrate LLM Prompt And Rejection Policy

**Description:** Review local false negatives and tune the LLM prompt/evaluation schema so it distinguishes "not ideal but locally relevant" from "not applicable".

Actions:

- Sample local deterministic candidates that the LLM skipped.
- Categorize skips into correct hard rejects, prompt over-strictness, missing profile evidence, or weak deterministic candidate.
- Add a field such as `local_transferability` or `commute_scope_note` to the LLM response only if it changes downstream behavior.
- Bump `LLM_PROMPT_VERSION` when prompt semantics change.

**Acceptance criteria:**
- [ ] Prompt tells the LLM that travel policy is deterministic and that local fallback candidates may be weaker but still useful if transferable.
- [ ] The LLM still rejects hard mismatches such as required trade credentials or unrelated physical roles.
- [ ] Regression examples cover at least five local skip cases from current data.

**Verification:**
- [ ] Unit tests cover prompt/request hash changes.
- [ ] Manual review confirms local approved/fallback balance improves without obvious junk.

**Dependencies:** Phase 1 diagnostics  
**Files likely touched:** `backend/app/llm.py`, `backend/tests/test_llm.py`, `docs/llm-hosted.md`  
**Estimated scope:** Medium

### Phase 7: Improve Remote Detection

**Description:** Full remote is currently inferred mostly from `jobs.location`. Extract work mode from richer listing text/source payloads where available.

**Acceptance criteria:**
- [ ] Work mode has explicit values: `onsite`, `hybrid`, `full_remote`, `unknown`.
- [ ] `commutable_or_full_remote` includes verified full-time remote jobs even if employer office is far away.
- [ ] Hybrid does not count as full remote.

**Verification:**
- [ ] Adapter/enrichment tests cover remote, hybrid, and vague remote language.
- [ ] Current dataset produces a measured full-remote candidate count larger than location-only detection when source text supports it.

**Dependencies:** Can run in parallel with Phase 2 after work-mode contract is agreed  
**Files likely touched:** adapters, enrichment repository/schema if persisted, `backend/app/travel_policy.py`, tests  
**Estimated scope:** Medium

## Checkpoints

### Checkpoint A: Observability And Candidate Recall

After Phases 1-3:

- [ ] Matcher funnel explains every scope count.
- [ ] Local deterministic candidate count is materially above current active count.
- [ ] Multi-city Oulu listings classify as local without relying on route lookup.
- [ ] Backend tests pass: `make test-backend`.

### Checkpoint B: User-Visible Scope Coverage

After Phases 4-5:

- [ ] `commutable` and `commutable_or_full_remote` meet configured target counts when eligible candidates exist.
- [ ] UI distinguishes LLM-approved from deterministic fallback recommendations.
- [ ] No feedback-hidden or hard-rejected jobs appear in fallback.
- [ ] Web build passes: `make test-web`.

### Checkpoint C: Quality Calibration

After Phases 6-7:

- [ ] LLM prompt version is bumped if semantics changed.
- [ ] Local false-negative examples are documented in tests or fixtures.
- [ ] Remote scope contains verified full-time remote jobs, not vague hybrid listings.
- [ ] Full regression passes: `make test-regression && make test-backend && make test-web`.

## Risks And Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Showing weak local jobs just to fill tabs | User loses trust | Use fallback tier, higher deterministic threshold, and explicit UI label. |
| LLM remains too strict for local transferability | Local tab stays sparse | Add local false-negative review set and prompt calibration. |
| Local pool grows too large | Pipeline cost and latency increase | Add separate local caps and measure funnel counts. |
| Multi-city location interpretation over-includes jobs | Local tab includes jobs where Oulu is optional or not actual workplace | Use `contains_home_city` status and evidence text; keep hybrid/remote semantics explicit. |
| Remote detection from text creates false positives | Hybrid jobs leak into remote scope | Require high-confidence full-remote phrases; keep vague remote as hybrid/unknown. |

## Open Questions

- What is the minimum acceptable count for each tab when quality is low: 5, 8, 10, or another number?
- Should deterministic fallback rows be shown in the same recommendation list, or in a separate "Paikalliset ehdokkaat" section?
- Should LLM `not_applicable` be absolute until prompt/profile version changes, or can a high local deterministic score override it into fallback?
- How old can a local job be before it should leave the local candidate pool?

## Recommended First Implementation Slice

Start with Phases 1-3. They do not change final user-visible quality policy yet, but they make the system diagnosable and ensure the matcher is actually finding all plausible local candidates before LLM and API filtering.

The first user-visible improvement should be Phase 4, but only after the fallback UX label and quality threshold are explicit.

## Implemented On 2026-07-05

- Added `MATCHER_LOCAL_MAX_JOBS` and a separate local candidate pool so older Oulu and configured nearby-location jobs are scored even when they are outside the global recency window.
- Classified multi-city listings containing the home city as local without requiring route lookup.
- Raised the LLM review budget to `LLM_EVAL_MAX_JOBS=800` and split review capacity across commutable, remote-only, and nationwide buckets.
- Added a nationwide discovery candidate pool from configured discovery/profile title terms so country-wide recall is not capped by the newest global recency window.
- Aligned active recommendation filtering with the prompt by hiding weak `consider` rows below 40 LLM points.
- Tightened prompt version 8 so local proximity cannot override weak job-content fit.
- Added a deterministic reject for `henkilökohtainen avustaja` care roles, which were incorrectly promoted by transferable customer-service signals.
- Added LLM progress logging and removed long database transactions around external LLM calls.
- Kept previously approved recommendations visible during long LLM runs while preventing unreviewed deterministic candidates from becoming active UI rows.

Final local verification after the implementation:

| Metric | Count |
|---|---:|
| Evaluated jobs | 1,309 |
| Local-pool additions | 809 |
| Deterministic passes | 712 |
| Commutable candidates before LLM | 285 |
| Commutable-or-remote candidates before LLM | 290 |
| LLM evaluated in final run | 165 |
| LLM failures | 0 |
| Active recommendations after stricter review | 13 |
| Active commutable recommendations | 9 |
| Active commutable-or-full-remote recommendations | 9 |
| Active unreviewed recommendations | 0 |
