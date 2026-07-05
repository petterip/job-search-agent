# Feedback Learning and Five-Point Rating Plan

**Updated:** 2026-07-05 (second review pass against live codebase; findings integrated in §17, research grounding in §18)
**Status:** planned — feedback is stored today, but learning from feedback is not implemented.
**Related:** [`goal.md`](goal.md), [`architecture.md`](architecture.md), [`tech-stack-plan.md`](tech-stack-plan.md), [`llm-hosted.md`](llm-hosted.md), [`implementation-journal.md`](implementation-journal.md), [`browser-enrichment-plan.md`](browser-enrichment-plan.md)

This document defines how user feedback evolves from coarse action buttons into a five-point rating scale, how each rating triggers an LLM analysis of why the user likely rated the job that way, and how feedback plus that analysis improve collection recall, deterministic scoring, semantic retrieval, and LLM evaluation on later pipeline runs — without replacing the existing staged matching architecture.

---

## 1. Problem Statement

The product already collects jobs, scores them deterministically, retrieves semantic candidates with pgvector, and sends a bounded set to an LLM classifier. Users can currently submit coarse feedback (`good_match`, `not_relevant`, `applied`), and `not_relevant` hides a recommendation from active results.

That feedback is not yet used to:

- strengthen future ranking for jobs similar to positively rated listings
- suppress repeated mistakes after negative ratings
- sharpen discovery search terms used during collection
- improve LLM review context on the next run
- explain **why** the user's rating diverged from the system's recommendation rationale

The goal is a closed learning loop: **rate a recommendation → LLM analyzes the likely reason for the rating → next daily run produces better matches**.

---

## 2. Verified Current State

| Item | State | Notes |
|---|---|---|
| `recommendation_feedback` table | Done | Stores `good_match`, `not_relevant`, `applied` (migration `20260620_0006`) |
| `POST /recommendations/{id}/feedback?action=...` | Done | Query-param action, plain insert (`backend/app/main.py`) |
| Finnish detail-page feedback buttons | Done | `web/app/tyopaikat/[id]/page.tsx` |
| `not_relevant` suppresses active recommendation | Done | POST sets `is_active=false`; matching upsert re-checks `rf.action = 'not_relevant'` |
| Append-only feedback rows | Gap | Each POST inserts a new row; no upsert or uniqueness on `recommendation_id`; any historical `not_relevant` row keeps the recommendation hidden forever |
| Hidden recommendation invisible to UI | Gap | `GET /jobs/{job_id}` selects only `r.is_active = true` recommendations — after hiding, the detail page loses the recommendation panel, so the user cannot see or change their rating |
| Nightly full reset of recommendations | Constraint | `run_deterministic_recommendations()` deactivates **all** recommendations for the profile, then re-upserts only currently scored jobs; feedback effects must survive this reset via the upsert's feedback checks |
| `refresh_active_recommendation_ranks()` feedback-unaware | Gap | Re-ranking after LLM eval never consults feedback; hide enforcement lives only in the upsert path |
| Feedback learning worker job | Missing | No pre-matching learning stage |
| Discovery query tuning from feedback | Missing | `DISCOVERY_SEARCH_QUERIES` is static; adapters read `get_settings().discovery_search_queries` only |
| LLM few-shot examples from feedback | Missing | Prompt uses profile facts only |
| LLM feedback analysis on submit | Missing | No structured "why did the user rate this?" evaluation |
| Feedback read API / job detail echo | Missing | UI cannot show current rating; only POST exists |
| Scheduler pipeline ordering | Gap | All `collect_*` jobs and `match_recommendations` fire at the same cron (default 16:00); no collect → learn → match sequence |
| `pipeline_runs` orchestration | Missing | Table exists (migration `20260620_0001`) but the worker does not use it |
| `profile_embeddings` shape | Constraint | One row per profile (`ON CONFLICT (profile_id)`); no slot for preference/anti centroids without a new table |
| Embeddings provider | OpenAI only | Semantic learning requires `OPENAI_API_KEY`; the eval provider (`LLM_PROVIDER`) is separate and may be Gemini |
| LLM cooldown scope | Risk | `provider_in_cooldown()` uses a single `llm_provider_unavailable.json`; provider-scoped, not task-scoped — feedback analysis and job-fit eval would block each other |
| Feedback on non-recommended jobs | Not supported | Detail page shows feedback only when `job.recommendation` exists |
| Portal feedback proxy | Gap | `web/app/suositukset/[id]/palaute/route.ts` forwards form `action` as query params only |
| Enrichment stage in daily run | Not shipped | See [`browser-enrichment-plan.md`](browser-enrichment-plan.md); pipeline slot reserved |

The staged matching pipeline described in [`architecture.md`](architecture.md) remains the correct foundation. Feedback learning attaches to that pipeline, not replaces it.

---

## 3. Five-Point Rating Model

### 3.1 Scale Definition

Replace the three coarse actions with one primary signal: an integer rating from **1 to 5**.

| Rating | Finnish UI label | English meaning | Learning role |
|---|---|---|---|
| **1** | Todella huono | Really bad match | Strong negative; hide recommendation; candidate for exclusion patterns |
| **2** | Huono | Bad match | Moderate negative; down-rank similar jobs |
| **3** | Neutraali | Neutral / unsure | Low learning weight; calibration only |
| **4** | Hyvä | Good match | Moderate positive; boost similar jobs |
| **5** | Erittäin hyvä | Extremely good match | Strong positive; prioritize similar jobs and discovery terms |

Rating labels are shown in Finnish in the portal. Stored values, API contracts, logs, and learning code use the numeric scale.

### 3.2 Optional Secondary Signals

Keep one optional action separate from the rating:

| Signal | Purpose |
|---|---|
| `applied` (boolean) | User actually submitted an application; strongest positive ground truth |

**Validation rule (resolved):** `rating` is required for ordinary submissions. If `applied=true` and the client omits `rating`, the API defaults `rating=5` before persisting. Reject `applied=true` with an explicit `rating` ≤ 2 (422).

Optional free-text `comment` (max 1000 chars) remains. Sanitized comment text may be included in the feedback-analysis LLM prompt; never send unsanitized PII to hosted providers (§11).

### 3.3 Migration from Current Actions

| Legacy action | Default rating |
|---|---|
| `not_relevant` | 1 |
| `good_match` | 4 |
| `applied` | 5 + `applied=true` |

After migration, deprecate `action` as the primary field. Keep it nullable during transition for backward-compatible reporting.

### 3.4 UX Contract

On recommendation detail pages:

- five clearly labeled rating controls (radio group / segmented control), not three separate verbs
- one optional "Merkitty haetuksi" toggle
- allow changing the latest rating for the same recommendation (upsert; §5.1)
- confirm visually that rating **1** hides the job from active recommendations (rating **2** down-ranks only; §6.3)
- **the detail page must keep showing the recommendation panel and current rating even after rating 1 hides it** — otherwise the user cannot revise a harsh verdict (§9.1)

Accessibility: keyboard-operable rating control with visible focus, `aria-label` per option, never rely on color alone for the selected state.

---

## 4. Target Architecture

### 4.1 High-Level Loop

```text
User rates recommendation (1–5, optional applied, optional comment)
  -> store feedback + scoring snapshot (API returns immediately)
  -> worker: analyze_feedback (async LLM)
       -> compare user rating vs system scores/rationale
       -> store structured "why likely rated this way" analysis
daily feedback learning job (worker)
  -> recompute learned state from ALL feedback (stateless; §6.2)
       -> update learned boosts / exclusions (with decay + hysteresis; §6.7)
       -> update preference / anti-preference embedding centroids
       -> tune discovery search queries
       -> prepare LLM few-shot examples
  -> collection (static + learned discovery queries)
  -> matching (deterministic + learned weights + adjusted semantic scores)
  -> LLM evaluation (few-shot + feedback pre-filter)
  -> portal recommendations
```

The portal never runs learning LLM calls on page load. Feedback persistence is synchronous; LLM feedback analysis runs asynchronously in the worker so rating submission stays fast.

### 4.2 Service Boundaries

| Component | Responsibility |
|---|---|
| `web` | Rating UI, applied toggle, comment |
| `api` | Validate and persist feedback, echo current rating, assemble snapshot |
| `worker` | `analyze_feedback` for pending rows; `learn_from_feedback` before `match_recommendations`; daily pipeline order |
| `db` | Feedback rows, snapshots, LLM analyses, learned profile fields, learning run audit |

No Redis, Celery, or separate ML service is required for MVP learning.

### 4.3 Why Not a Standalone ML Ranker

This is a single-job-seeker system with slow feedback accumulation (tens to hundreds of labels, never millions). A trained re-ranking model would be unstable at that scale, hard to debug against the product's explainability requirement, and operationally heavier than necessary. §18 grounds this in the literature: with a handful of explicit labels, centroid-style relevance feedback (Rocchio) and k-NN over labeled items are the proven approaches; learned rankers need orders of magnitude more data.

The recommended approach is an **explainable hybrid**:

1. LLM feedback analysis (interpret user intent vs system recommendation)
2. deterministic feature learning (terms, employers, sectors)
3. embedding preference vectors (similar / dissimilar)
4. discovery query tuning (recall)
5. LLM context enrichment (few-shot examples, pre-filter)

---

## 5. Data Model Changes

### 5.1 Feedback Table

Extend `recommendation_feedback`:

| Column | Type | Notes |
|---|---|---|
| `rating` | `smallint` | Required for new rows; `CHECK (rating BETWEEN 1 AND 5)` |
| `applied` | `boolean` | Default `false` |
| `comment` | `text` | Optional, unchanged |
| `scoring_snapshot` | `jsonb` | Feature snapshot at feedback time (required for new rows) |
| `job_id` | `int` FK | Denormalized from recommendation so learning joins survive inactive/reset recommendations |
| `analysis_status` | `text` | `pending`, `completed`, `failed`, `skipped` |
| `updated_at` | `timestamptz` | Set on upsert |
| `created_at` | `timestamptz` | Unchanged |

**Migration order matters.** Today every POST inserts a new row, and the matching upsert hides a recommendation if **any historical** row has `action = 'not_relevant'`. Phase A must first collapse existing rows to the latest verdict per `recommendation_id` (`created_at DESC, id DESC`), map that verdict to rating/applied, then add `UNIQUE (recommendation_id)` and switch the API to `INSERT … ON CONFLICT (recommendation_id) DO UPDATE`. Without the collapse, a later `good_match` can never unhide a recommendation once any `not_relevant` row exists.

Multiple submissions policy:

- **MVP:** upsert latest verdict per `recommendation_id` (one row).
- **Post-MVP:** optional `recommendation_feedback_history` append-only table if temporal analysis becomes important; the upsert row becomes the "current" view.

**No learning cursor column.** Because feedback volume is tiny, `learn_from_feedback` recomputes learned state from the full feedback table on every run (§6.2). This makes re-rating, decay, and conflict resolution trivially correct and removes cursor-drift bugs. `learning_runs` records what each run saw for audit, not for incremental state.

### 5.2 Scoring Snapshot Schema

Persist enough context to explain and reuse the label even after the nightly recommendation reset rewrites `deterministic_result`, `rationale`, and `concerns`:

```json
{
  "job_id": 1234,
  "recommendation_id": 88,
  "title": "Palveluassistentti",
  "employer": "Oulun kaupunki",
  "location": "Oulu",
  "candidate_lanes": ["application_history", "sector_context"],
  "hidden_opportunity": false,
  "machine_score": 62.0,
  "vector_score": 0.71,
  "llm_score": 78,
  "fit_tier": "transferable_weaker",
  "suggested_action": "consider",
  "rank": 7,
  "is_active": true,
  "title_matches": [],
  "keyword_matches": ["hallinto", "asiakaspalvelu"],
  "application_history_title_matches": [],
  "application_history_keyword_matches": ["hallinto"],
  "sector_matches": ["kunta"],
  "negative_matches": [],
  "location_matches": ["oulu"],
  "transit_distance_km": 12.4,
  "transit_duration_text": "45 min",
  "rationale": "…stored recommendation rationale…",
  "concerns": ["…"],
  "llm_prompt_version": 7,
  "eval_model": "gemini-3.1-flash-lite"
}
```

`rank` and `is_active` at feedback time are recorded deliberately: they let later analysis correct for position/presentation bias — the user only rates what the current policy chose to show (§6.7, §18). The API assembles the snapshot from the recommendation row at POST time (`backend/app/main.py`).

### 5.3 Learned Profile Fields

Store learned state in the profile JSON (single-profile system; no new table needed):

```json
{
  "preferences": {
    "learned_boosts": {
      "boost_titles_fi": ["hallintosihteeri"],
      "boost_keywords_fi": ["tietopalvelu"],
      "boost_locations": ["oulu"]
    }
  },
  "learned": {
    "version": 3,
    "exclusions": {
      "terms_fi": ["myyntitavoitteet"],
      "employers": ["example-retail-chain"],
      "sectors": ["retail"]
    },
    "term_provenance": {"myyntitavoitteet": {"net_weight": -2.4, "jobs": [1234, 1301], "first_seen": "2026-07-01"}},
    "discovery_queries": ["palveluassistentti", "hallintosihteeri"],
    "lane_quota_overrides": {"exploration": 1},
    "few_shot_examples": [
      {"rating": 5, "title": "...", "employer": "...", "verdict": "apply"}
    ],
    "updated_at": "2026-07-05T13:00:00Z",
    "source_feedback_count": 18
  }
}
```

`learned_boosts` mirrors the existing `application_history_signals` shape so `score_job()` consumes both through one code path. **`learned.exclusions` lives under `learned`, not inside `preferences`**, because `profile_embedding_text()` serializes the whole `preferences` object today (`backend/app/embeddings.py`) — exclusion terms must never enter the profile embedding payload, or semantic retrieval would seek jobs similar to the disliked ones. Positive `learned_boosts` may stay under `preferences` (they reinforce retrieval like application history does). When touching `profile_embedding_text()`, document the included keys explicitly.

`term_provenance` records, per learned term, its net weight, contributing job ids, and first-seen date — required for the audit CLI, hysteresis (§6.7), and safe rollback.

### 5.4 Learning Run Audit

Add `learning_runs`:

| Column | Purpose |
|---|---|
| `started_at`, `finished_at`, `status` | Worker audit |
| `feedback_total`, `analysis_completed` | Rows seen; rows with completed LLM analysis |
| `changes_applied` | JSONB diff summary of term/query/centroid changes vs previous run |
| `learned_version` | Profile `learned.version` after the run |
| `feedback_analysis_prompt_version` | Feedback-analysis prompt version |
| `job_fit_prompt_version` | Job-fit few-shot bundle version |

### 5.5 Feedback LLM Analysis

Add `feedback_llm_analyses` — one row per feedback row (FK to `recommendation_feedback.id`, `UNIQUE (feedback_id)`), storing provider, model, prompt version, `request_hash`, and the structured `analysis` JSONB. Do **not** store these inside `llm_evaluations` (different schema and lifecycle).

On feedback upsert (same `recommendation_id`) where rating, applied, comment, or snapshot hash changed: supersede the prior analysis row and reset `analysis_status = 'pending'`.

Structured analysis schema (`analysis` JSONB):

```json
{
  "user_rating": 1,
  "applied": false,
  "system_alignment": "over_ranked",
  "hypothesis_fi": "Käyttäjä todennäköisesti hylkäsi työn, koska rooli on myyntipainotteinen eikä vastaa hakijan julkisen palvelun painotusta, vaikka hallintoavainsanat nostivat pistettä.",
  "likely_positive_signals": ["kunta", "hallinto"],
  "likely_negative_signals": ["myynti", "myyntitavoitteet"],
  "mismatch_drivers": [
    {"driver": "role_family", "detail": "sales-heavy duties not in profile targets"},
    {"driver": "location", "detail": "Helsinki vs preferred Oulu region"}
  ],
  "suggested_actions": {
    "boost_terms_fi": [],
    "exclude_terms_fi": ["myynti", "myyntitavoitteet"],
    "boost_sectors": [],
    "exclude_sectors": ["retail"],
    "discovery_queries_add": [],
    "discovery_queries_remove": [],
    "lane_notes": "exploration lane overshot; penalize generic customer-service-only matches",
    "llm_eval_hint": "Treat sales KPI language as hard mismatch unless profile lists commercial experience"
  },
  "confidence": "high"
}
```

| Field | Purpose |
|---|---|
| `system_alignment` | `aligned`, `under_ranked`, `over_ranked`, `mixed`, `unclear` |
| `hypothesis_fi` | Short Finnish explanation for operators and future portal diagnostics |
| `likely_positive_signals` / `likely_negative_signals` | Token/sector-level evidence |
| `mismatch_drivers` | Root-cause taxonomy for repeat-failure metrics |
| `suggested_actions` | Machine-readable tuning inputs for §7.4 |

### 5.6 Learned Preference Embeddings

Add `learned_preference_embeddings`:

| Column | Notes |
|---|---|
| `profile_id` FK | |
| `kind` | `preference` or `anti_preference`; `UNIQUE (profile_id, kind, model, dimension)` |
| `model`, `dimension` | Must match `job_embeddings` rows used; centroids are invalid across model changes and must be recomputed if `OPENAI_EMBEDDING_MODEL`/dimension changes |
| `embedding` | `vector(1536)` for MVP |
| `source_job_ids` | JSONB — audit of contributing jobs |
| `updated_at` | |

Preference centroid = mean of embeddings for jobs with rating ≥ 4 or `applied=true`; anti-centroid from rating ≤ 2. **Require at least 3 qualifying rated jobs per centroid**; below that, skip and log `event=centroid_insufficient_support` (a 1–2-item "centroid" is noise). If a rated job lacks a `job_embeddings` row, skip it and log `event=centroid_job_embedding_missing`.

Centroids do **not** go into `profile_embeddings` (one row per profile, `ON CONFLICT (profile_id)`).

### 5.7 Environment Variables

Add to `.env.example` (development placeholders only):

| Variable | Default | Purpose |
|---|---|---|
| `LEARNER_DAILY_HOUR` / `LEARNER_DAILY_MINUTE` | `16` / `45` | Learn + match pipeline step (after collection) |
| `FEEDBACK_ANALYSIS_POLL_MINUTES` | `5` | Interval for `analyze_feedback` |
| `LEARNER_ANALYSIS_WAIT_MINUTES` | `30` | Max wait for pending analyses before rating-only fallback |
| `LEARNED_DISCOVERY_QUERY_CAP` | `12` | Max learned discovery terms |
| `LEARNED_EXCLUSION_CAP` | `40` | Max learned exclusion terms (§6.7) |
| `FEEDBACK_DECAY_HALF_LIFE_DAYS` | `60` | Exponential decay half-life for feedback weight (§6.7) |

Keep existing `DISCOVERY_SEARCH_QUERIES`, `MATCHER_DAILY_*`, `COLLECTOR_DAILY_*`. With approach B (single pipeline job), `MATCHER_DAILY_*` is superseded by `LEARNER_DAILY_*` for steps 3–5.

### 5.8 Data Lifecycle and Deletion

- Feedback rows FK `recommendations.id` with `ON DELETE CASCADE`.
- Profile delete/reset removes together: `recommendation_feedback`, `feedback_llm_analyses`, `learned_preference_embeddings`, and the `profile.learned` / `preferences.learned_boosts` JSON fields.
- `make audit-db` reports: feedback count by rating, pending/completed analyses, latest `learning_runs` status, current `learned.version`, learned term list with provenance.

---

## 6. Learning Algorithm by Signal Type

### 6.1 Rating Weight Table

| Rating | Base weight |
|---|---|
| 1 | −1.0 |
| 2 | −0.5 |
| 3 | 0.0 |
| 4 | +0.5 |
| 5 | +1.0 |
| `applied=true` | +1.5 (replaces the rating weight; strongest label) |

### 6.2 Deterministic Term Learning (stateless recompute)

Each daily `learn_from_feedback` run:

1. Load **all** feedback rows with snapshots (not a delta — see §5.1).
2. Prefer rows with `analysis_status = completed`; fall back to rating-only learning when analysis is missing, failed, or skipped.
3. Extract tokens, normalized employers, and sectors from each snapshot (title, keyword, sector, location matches, `candidate_lanes`).
4. Apply time decay to each row's weight (§6.7), then merge LLM `suggested_actions` with the confidence multiplier (§7.4) — subject to the grounding guard (§7.4).
5. Aggregate net weighted term frequencies (positive and negative contributions sum into one net weight per term).
6. Promote terms to `preferences.learned_boosts` when net weight ≥ +1.5 across ≥ 2 distinct jobs.
7. Promote terms/employers/sectors to `learned.exclusions` when net weight ≤ −2.0 across ≥ 2 distinct jobs; drop from exclusions only when net weight recovers above −1.0 (hysteresis, §6.7).
8. Write `term_provenance`; diff against the previous learned state into `learning_runs.changes_applied`.
9. Never override hard profile exclusions or the qualification-reject rules already encoded in `matching.py`.

Integration in `score_job()`:

- add learned title/keyword/location/sector matches using the same helpers as `application_history_signals`
- apply `learned.exclusions` as a **soft penalty and lane demotion** before candidate lane selection — except the exploration lane (§6.7)
- read lane quotas as `LANE_QUOTAS` merged with `profile.learned.lane_quota_overrides`
- store `learned_*_matches` in `deterministic_result` for portal/debug visibility

### 6.3 Visibility Rules by Rating

| Rating | Immediate API effect | Matching re-run (`ON CONFLICT` upsert) |
|---|---|---|
| 1 | `is_active=false`, `rank=null` | Stays hidden; same as legacy `not_relevant` |
| 2 | Stays visible | Must **not** hide; learned down-rank only |
| 3–5 | Stays active | Eligible; positive learning affects future runs |

Required code changes:

- `backend/app/matching.py` upsert: replace both `rf.action = 'not_relevant'` subqueries with `(rf.rating = 1 OR rf.action = 'not_relevant')`.
- `backend/app/main.py` POST: hide on rating 1 only; **re-rating from 1 to ≥ 3 must immediately set `is_active = true` again** (rank stays null until the next matching run re-ranks) — otherwise upsert-based un-hiding waits a day and looks broken.
- `refresh_active_recommendation_ranks()` should include a guard that never re-activates or ranks a recommendation whose latest feedback rating is 1 (defense in depth; today hide enforcement exists only in the deterministic upsert path).
- `GET /jobs/{job_id}`: the recommendation subquery must drop the `r.is_active = true` filter (return the latest recommendation regardless, with an `is_active` flag) so a hidden recommendation still shows its panel and current rating.

### 6.4 Embedding Preference Learning

Requires the **OpenAI** embedding provider regardless of `LLM_PROVIDER`:

1. Preference centroid from rating ≥ 4 / applied jobs; anti-centroid from rating ≤ 2 (§5.6, min-support 3).
2. Adjusted semantic score for candidates:

```text
adjusted = base_vector_score
         + α * cosine(job, preference_centroid)
         - β * cosine(job, anti_centroid)
```

Suggested MVP constants: `α = 0.15`, `β = 0.20`, tuned offline against labelled feedback. Apply in the `merge_semantic_scores()` path; **never apply the anti-centroid penalty to exploration-lane candidates** (§6.7).

**Known limitation — multimodal interests.** This profile spans several role families (library, admin, culture/events). A single mean vector blurs them. MVP accepts this (α/β are small nudges, not gates). Planned upgrade (Phase D stretch): replace the single preference centroid with **k-NN over rated jobs** — `max cosine(job, any rating≥4 job embedding)` — which handles multiple positive clusters without averaging them into mush; keep the anti-centroid as-is since negatives tend to be more thematic. See §18.

Embeddings remain **retrieval signals**, not final truth.

### 6.5 Discovery Query Tuning

1. Rank title/keyword tokens from rating 4–5 and applied jobs (plus LLM `discovery_queries_add` when confidence ≥ medium).
2. Merge top new terms into `profile.learned.discovery_queries` (cap `LEARNED_DISCOVERY_QUERY_CAP`).
3. At collection time, adapters call a shared helper `effective_discovery_queries(connection, settings)` that returns `settings.discovery_search_queries` merged with `profile.learned.discovery_queries`, deduped and capped — not env-only reads via cached `get_settings()`. Wire into `backend/app/adapters/duunitori.py`, `tmt.py`, `laura.py` (the three discovery-search sources).
4. Remove/demote learned discovery terms that repeatedly surface rating 1–2 jobs, or that the LLM lists in `discovery_queries_remove`. Never remove terms from the static env baseline.

### 6.6 LLM Context Learning

No fine-tuning in MVP.

- **Few-shot bundle:** up to 3 positive (rating 5 / applied) and 2 negative (rating 1) compact examples — title, employer, one-line reason, verdict — appended to the job-fit prompt. Prefer recent, mutually diverse examples (no two from the same employer/role family) so the bundle doesn't overfit one theme.
- **Eval hints:** include the highest-confidence `llm_eval_hint` lines (max 3) from completed analyses.
- **Feedback pre-filter:** skip LLM evaluation for candidates whose anti-centroid similarity is high and learned exclusion matches are present; prioritize LLM slots for candidates close to the preference signal. This preserves the bounded LLM budget while improving judgement quality.

### 6.7 Conflict Resolution, Temporal Dynamics, and Degeneracy Protection

This section defines how the system behaves once feedback is plentiful and partially contradictory — the long-run regime.

**Net weights, not last-write-wins.** Every term's learned status derives from the decayed net sum over all feedback (§6.2). A term rated into both buckets (e.g. "asiakaspalvelu" in a loved library job and a hated call-center job) stays neutral unless |net| clears the promotion threshold. Frequent generic tokens are additionally suppressed by the existing `GENERIC_MATCH_TERMS` set and a document-frequency cap: a term appearing in > 30 % of all snapshots cannot become a boost or exclusion (it carries no discriminative signal).

**Time decay.** Each feedback row's weight is multiplied by `0.5 ^ (age_days / FEEDBACK_DECAY_HALF_LIFE_DAYS)`. Job-search preferences drift within months (the user may broaden or narrow scope as the search progresses); decay lets the learned state follow without manual resets. `applied` rows decay at half rate (ground truth stays relevant longer).

**Hysteresis.** Enter exclusion at net ≤ −2.0, exit at net > −1.0 (§6.2). This prevents a single new positive rating from flapping a term in and out of the exclusion list every run.

**Feedback-loop degeneracy protection.** Learned exclusions reduce exposure, which prevents the very feedback that could rehabilitate a term — the classic self-reinforcing loop (§18). Mitigations, all MVP-mandatory:

1. The **exploration lane is exempt** from learned exclusions and the anti-centroid penalty. It remains the system's unbiased sample; its quota may never be overridden below 1.
2. Learned exclusions are **soft penalties**, never hard filters; only profile hard-exclusions and qualification rejects filter absolutely.
3. Exclusion **cap** (`LEARNED_EXCLUSION_CAP`, default 40): when full, keep the strongest net weights; the rest demote to zero.
4. Decay itself retires stale exclusions: with no fresh negative evidence, a term drifts back above the exit threshold and is retested.

**Conflicting signal precedence** (highest first): profile hard exclusions and qualification rejects → `applied=true` → numeric rating → LLM analysis suggestions → token co-occurrence. An LLM suggestion can never outvote the ratings it was derived from, because its weight is additive with a ≤ 1.0 multiplier (§7.4).

**Position-bias awareness.** All learning happens on shown items; the snapshot records `rank`/`is_active` (§5.2) so offline evaluation can check whether low-ranked shown items were systematically under-rated. MVP only records; no inverse-propensity correction (overkill at this scale).

**Idempotency and rollback.** Because learned state is a pure function of (feedback rows × decay date × thresholds), any run can be reproduced, and reverting a bad learned state is: fix data or thresholds, re-run. `learning_runs.changes_applied` plus `term_provenance` give the audit trail.

---

## 7. LLM Feedback Analysis

When the user submits feedback, the system runs a dedicated LLM evaluation asking: **given why we recommended this, why might the user have rated it at this level?** The output guides algorithm sharpening; it never replaces the numeric rating as ground truth.

### 7.1 Purpose

| Without analysis | With analysis |
|---|---|
| Learn only from co-occurring tokens | Learn from interpreted mismatch drivers (role family, location, qualifications, sector, work mode) |
| Hard to explain false positives | `over_ranked` cases reveal which lane or score component misfired |
| Discovery tuning by keyword frequency only | LLM proposes/removes discovery queries with context |
| Operator blind to user intent | Finnish `hypothesis_fi` documents the likely reason |

### 7.2 Inputs (Privacy-Bounded)

Same minimized boundary as job-fit evaluation ([`llm-hosted.md`](llm-hosted.md)):

| Input | Included |
|---|---|
| Minimized profile summary (`minimized_profile_summary()`) | Yes |
| Job summary (title, employer, location, description excerpt) | Yes |
| Stored recommendation rationale and concerns (from snapshot) | Yes |
| Deterministic match evidence from `scoring_snapshot` | Yes |
| System scores (`machine_score`, `vector_score`, `llm_score`, `fit_tier`, `suggested_action`) | Yes |
| User rating, `applied`, sanitized comment | Yes |
| Raw CV, application letters, contact details | No |
| Full raw source payloads | No |

The prompt must state that the model is **hypothesizing** user intent and must not invent motivations unrelated to the supplied facts.

### 7.3 Prompt Contract

New module `backend/app/feedback_analysis.py`, reusing the provider stack from `backend/app/llm.py` (OpenAI Responses / Gemini structured output), with its own `FEEDBACK_ANALYSIS_PROMPT_VERSION`. Instructions (structured JSON output):

- Compare the user's rating and optional comment against the system's rationale and scores.
- Classify `system_alignment` (`aligned` / `over_ranked` / `under_ranked` / `mixed` / `unclear`).
- Explain the most likely reason for the rating, referencing concrete job and profile facts.
- Propose machine-readable tuning actions (terms, sectors, discovery queries, lane notes, eval hints).
- If rating is 3, focus on ambiguity calibration, not strong boosts/exclusions.
- If the comment contradicts the rating, treat the rating as authoritative and note the contradiction in `mismatch_drivers`.
- Do not fabricate qualifications, preferences, or history not present in the inputs.

### 7.4 How Analysis Feeds Learning

```text
final term weight = decayed_rating_weight (§6.1, §6.7)
                  + token_frequency_weight
                  + llm_action_weight * confidence_multiplier
```

| `confidence` | Multiplier |
|---|---|
| `high` | 1.0 |
| `medium` | 0.6 |
| `low` | 0.2 |

Rules:

- **Grounding guard:** an LLM-suggested boost/exclude term is accepted only if the term (or an inflection variant per `token_variants()`) actually occurs in the snapshot's job text fields or the profile summary. Ungrounded suggestions are dropped and logged (`event=analysis_term_ungrounded`). This blocks hallucinated terms from entering learned state.
- LLM-suggested exclusions never override hard-coded qualification rejects; they add soft penalties and discovery demotions only.
- `system_alignment = over_ranked` with rating ≤ 2: parse `lane_notes` into temporary `lane_quota_overrides` (keys must match `LANE_QUOTAS` in `matching.py`: `direct_title`, `application_history`, `semantic_similarity`, `transferable_duty`, `sector_context`, `exploration`; the exploration quota may never drop below 1 — §6.7). Overrides expire when a later run's feedback no longer supports them (stateless recompute).
- `system_alignment = under_ranked` with rating ≥ 4: feed the missed signals into boost candidates and few-shot positives.

### 7.5 Execution Model

```text
POST /feedback -> 201 immediately (analysis_status = pending)

analyze_feedback (worker, every FEEDBACK_ANALYSIS_POLL_MINUTES):
  -> select feedback where analysis_status = 'pending'
  -> call LLM once per row (respect task-scoped cooldown)
  -> store feedback_llm_analyses; set analysis_status = completed | failed | skipped

learn_from_feedback (daily, before matching):
  -> wait for pending analyses up to LEARNER_ANALYSIS_WAIT_MINUTES, then proceed with rating-only fallback
  -> consume completed analyses
```

Synchronous LLM in the API request path is rejected (ties portal UX to provider latency).

**Cooldown scope:** `provider_in_cooldown()` currently uses a single `llm_provider_unavailable.json`, so a job-fit quota failure would silence feedback analysis and vice versa. Use a **separate cooldown file** (`llm_feedback_analysis_unavailable.json`) for the feedback-analysis task; when in cooldown or the provider is disabled, set `analysis_status = 'skipped'` and continue rating-only learning.

### 7.6 Caching and Cost Control

- `request_hash` = hash(profile summary + job summary + snapshot + rating + applied + sanitized comment + prompt version).
- Re-submitting an identical rating reuses the stored analysis; a changed rating/applied/comment/snapshot resets to `pending` and re-runs only if the hash differs.
- Cap: one analysis call per feedback version; no retry loops beyond the existing provider retry policy.
- Log `event=feedback_analysis_completed` with alignment and confidence; never log full prompts or profile text.

### 7.7 Operator View and Future Portal

MVP: analyses visible via `python -m app.audit`. Post-MVP: read-only portal panel, clearly labeled as a system hypothesis, never presented as a verified user statement. High-confidence analyses also feed the few-shot bundle (§6.6).

---

## 8. Pipeline Schedule and Orchestration

### 8.1 Current Scheduler Gap

`backend/app/scheduler.py` registers every `collect_*` job **and** `match_recommendations` on the same daily cron (`collector_daily_*` / `matcher_daily_*`, both default 16:00). They run concurrently; the planned collect → analyze → learn → match order does not exist yet.

### 8.2 Target Daily Order

```text
1. collect enabled sources (+ effective discovery queries)
2. enrichment (when shipped; see browser-enrichment-plan)
3. analyze_feedback (drain pending rows)
4. learn_from_feedback
5. match_recommendations (deterministic + semantic + LLM)
```

### 8.3 Recommended Orchestration

| Approach | Description |
|---|---|
| **A. Staggered cron (minimal)** | Collectors 16:00; `analyze_feedback` + `learn_from_feedback` + `match_recommendations` 16:45 via `LEARNER_DAILY_*` |
| **B. Single pipeline job (preferred)** | Replace the separate match trigger with `run_daily_pipeline` that records `pipeline_runs`, runs steps 3–5 sequentially with an overlap guard |
| **C. Continuous analysis** | `analyze_feedback` every 5 minutes; learn + match once daily |

Regardless of choice, also run `analyze_feedback` between daily batches (5-minute poll) so feedback submitted hours before the match job already has completed analysis. `learn_from_feedback` must finish before matching so the same day's run uses fresh learned state; pending analyses older than `LEARNER_ANALYSIS_WAIT_MINUTES` fall back to rating-only. Update `expected_scheduler_job_ids()` and the stale-job pruning pattern in `scheduler.py` for every new job id.

---

## 9. API and Portal Changes

### 9.1 API

Replace the action-based endpoint:

```http
POST /recommendations/{recommendation_id}/feedback
Content-Type: application/json

{ "rating": 5, "applied": false, "comment": null }
```

Validation: `rating` int 1–5 required (defaults to 5 when `applied=true` and rating omitted); reject `applied=true` with explicit rating ≤ 2 (422); `comment` max 1000 chars.

Response: `id`, `recommendation_id`, `rating`, `applied`, `analysis_status` (`pending` initially), and `recommendation_hidden` (`rating == 1`).

Read paths:

```http
GET /recommendations/{recommendation_id}/feedback   # latest verdict, 404 if none
GET /recommendations/{recommendation_id}/feedback/analysis   # optional operator endpoint
```

Also echo a `feedback` summary (rating, applied, analysis_status) inside `GET /jobs/{job_id}` — and change that endpoint's recommendation subquery to return the latest recommendation **regardless of `is_active`** with an explicit `is_active` flag, so a rating-1-hidden recommendation still renders its panel and current rating (§6.3).

**Portal proxy:** update `web/app/suositukset/[id]/palaute/route.ts` — today it forwards form `action` as query params; it must send a JSON body with `rating`/`applied` (and check the response status instead of ignoring it).

**Security:** feedback mutation remains acceptable without auth only on the loopback-default deployment ([`tech-stack-plan.md`](tech-stack-plan.md)); LAN exposure requires an operator token like `PUT /profile`.

Deprecation: accept legacy `?action=...` for one release cycle, mapped per §3.3.

### 9.2 Portal

Finnish labels (todella huono … erittäin hyvä) as a segmented control on `web/app/tyopaikat/[id]/page.tsx`, plus the applied toggle. Selecting 1 shows a visible "piilotettu suosituksista" state with the control still editable. A small "Analyysi valmistuu taustalla" hint may show while `analysis_status = pending`.

---

## 10. Observability and Quality Metrics

Every learning run must answer: rows processed, completed analyses vs rating-only fallback, terms added/removed (with provenance), discovery query changes, centroid updates or skip reasons, and the `system_alignment` distribution.

Feedback analysis quality:

| Metric | Purpose |
|---|---|
| Analysis completion rate | Share of feedback rows reaching `completed` |
| Alignment accuracy (manual spot-check) | Does `over_ranked`/`under_ranked` match operator judgement? |
| Action uptake rate | Share of LLM-suggested terms surviving the grounding guard and thresholds |
| Repeat false positive rate | Same `mismatch_drivers` recurring across rating-1 jobs |

Offline benchmark (small labelled set from real applications + feedback):

| Metric | Purpose |
|---|---|
| Recall@30 on positive labels | Rating ≥ 4 / applied jobs appear in top 30 |
| False positive rate | Rating 1–2-like jobs still reaching LLM review |
| Exploration hit rate | Positive ratings originating from the exploration lane (the canary for degeneracy — if this drops toward zero, learned exclusions are over-filtering; §6.7) |
| Discovery contribution | Recommendations traced to learned discovery terms |
| LLM call savings | Candidates skipped by the feedback pre-filter |

Store benchmark results in `learning_runs.changes_applied` or an operator note; do not block the pipeline on benchmark failure in MVP.

---

## 11. Privacy and Safety Rules

- Learning and feedback analysis use the same privacy boundary as matching: minimized profile fields and normalized job summaries only.
- Add `sanitize_feedback_comment()` (strip emails, phone numbers, personal names not already in allowed profile fields) before any comment reaches a hosted LLM; no sanitizer exists today.
- Learned terms, centroids, and hypotheses are derived system state — mark stored hypotheses clearly as system-generated estimates.
- Hard qualification rejects in `matching.py` stay authoritative over any learned or LLM-suggested exclusion.
- When `learned.version` or the few-shot bundle changes, the effective job-fit prompt inputs change: include `profile.learned.version` in `evaluation_request_hash()` (or bump `llm_prompt_version`) so cached `llm_evaluations` re-run instead of serving stale verdicts.

---

## 12. Phased Implementation Plan

### Phase A — Five-Point Feedback (user-facing)

**Goal:** rate 1–5 with optional applied, auditable snapshot, correct hide/unhide.

1. Alembic migration: add `rating`, `applied`, `job_id`, `scoring_snapshot`, `analysis_status`, `updated_at`; collapse legacy rows to latest verdict; map actions per §3.3; add `UNIQUE (recommendation_id)`.
2. `POST` → JSON body with upsert; legacy `?action` mapping for one cycle; hide on rating 1; **re-activate on re-rate ≥ 3** (§6.3).
3. `GET /recommendations/{id}/feedback`; feedback echo + inactive-recommendation visibility on `GET /jobs/{job_id}` (§9.1).
4. Assemble `scoring_snapshot` on submit (required by Phase B).
5. Five-point UI + applied toggle; fix `palaute/route.ts` to JSON.
6. `matching.py` upsert hide condition → `rating = 1 OR action = 'not_relevant'`; guard in `refresh_active_recommendation_ranks()`.
7. Tests: validation, migration mapping, upsert, hide-on-1, unhide-on-re-rate, snapshot shape, hidden-rec detail visibility.

Verification: `make test-backend`, `make test-regression`; manual: rate on `/tyopaikat/{id}`, reload shows the rating; rate 1 then re-rate 4 un-hides; duplicate submits update one row.

### Phase B — Async LLM Feedback Analysis

1. `feedback_llm_analyses` migration; `FeedbackAnalysis` schema + prompt (`FEEDBACK_ANALYSIS_PROMPT_VERSION`) in new `backend/app/feedback_analysis.py`, reusing the provider stack.
2. Task-scoped cooldown file; `skipped` fallback.
3. `analyze_feedback` worker job (5-minute poll + pre-match drain); request-hash caching; upsert invalidation.
4. `sanitize_feedback_comment()`.
5. Operator read endpoint / audit output.
6. Tests: schema validation, grounding-guard rejection, hash reuse, cooldown isolation.

### Phase C — Deterministic Learning Loop

1. `learning_runs` migration; `backend/app/feedback_learning.py` with **stateless recompute** (§6.2), decay, hysteresis, caps, provenance.
2. Merge LLM `suggested_actions` with confidence multipliers and grounding guard.
3. Wire `learned_boosts` / `learned.exclusions` / `lane_quota_overrides` into `score_job()`; exploration-lane exemption.
4. Pipeline orchestration (§8.3 B preferred); extend `expected_scheduler_job_ids()`; prune stale APScheduler rows.
5. Bump `learned.version`; invalidate stale job-fit eval cache (§11).
6. Tests: weight aggregation, decay, hysteresis flapping, conflict (same term both buckets), cap eviction, exploration exemption.

Verification: rating 5 with high-confidence analysis promotes suggested boosts; rating 1 `over_ranked` promotes exclusions and stays hidden on re-run; logs show learn-before-match ordering.

### Phase D — Semantic and Discovery Learning

1. `learned_preference_embeddings` migration (with model/dimension; §5.6).
2. Centroid computation with min-support 3 and model-change invalidation.
3. Adjusted semantic scoring in the `merge_semantic_scores()` path (exploration exempt from anti-penalty).
4. `effective_discovery_queries()` wired into Duunitori/TMT/Laura adapters; cap + dedupe; log changes in `learning_runs`.
5. (Stretch) k-NN positive similarity replacing the single preference centroid (§6.4).

Verification: learned discovery term appears in collector logs; fixture test shows the semantic lane promoting jobs near the preference signal.

### Phase E — LLM Job-Fit Context

1. Few-shot bundle (3 positive / 2 negative, diverse) + top eval hints into the job-fit prompt; include `learned.version` in the request hash.
2. Feedback-aware pre-filter for LLM slot allocation.
3. Tests: bundle assembly, hash change on version bump, pre-filter skip logging.

### Phase F — Metrics and Tuning

1. Offline benchmark harness over the labelled set (§10), including the exploration-hit-rate canary.
2. Extend `backend/app/audit.py`: feedback counts by rating, `analysis_status` breakdown, latest `learning_runs`, `learned.version`, term provenance dump.
3. Document tuned constants (`α`, `β`, thresholds, half-life) where measured.

---

## 13. Non-Goals

- Multi-user feedback or per-account preference models
- Online learning on every click (batch daily learning; analysis async but not blocking)
- Hosted LLM fine-tuning
- Replacing deterministic filters with an opaque ranker
- Redis/Celery queue infrastructure
- Synchronous LLM analysis in the feedback API request path
- Presenting LLM feedback hypotheses as verified user statements
- Feedback on jobs without a stored recommendation (all-jobs browse) in MVP
- Storing feedback analyses inside `llm_evaluations`
- Inverse-propensity / counterfactual correction of position bias (recorded, not corrected, at this scale)

---

## 14. Success Criteria

1. User can rate any recommendation 1–5 with Finnish labels; a hidden (rating 1) recommendation stays visible and re-ratable on its detail page.
2. Each rating triggers an async LLM analysis recording a Finnish hypothesis and structured, grounded tuning actions.
3. Rating 1 hides immediately and re-rating un-hides immediately; ratings 4–5 measurably boost similar jobs on the next daily run.
4. Learned state is visible in `deterministic_result` and fully auditable through `learning_runs`, `term_provenance`, and `feedback_llm_analyses`.
5. Discovery queries expand and contract from ratings + analysis, capped and never touching the env baseline.
6. Job-fit LLM evaluation uses feedback-informed few-shot examples and hints; its cache invalidates when learned state changes.
7. The daily pipeline runs collect → analyze → learn → match; the scheduler no longer fires matching concurrently with collection.
8. With 100+ partially conflicting feedback rows, learned state stays stable (no term flapping), bounded (caps respected), and recoverable (exploration hit rate stays > 0).

---

## 15. Open Decisions

| Decision | Recommendation | Alternative |
|---|---|---|
| One vs many feedback rows per recommendation | Upsert latest verdict (MVP) | Append-only history table post-MVP |
| Legacy `action` column | Keep nullable after migration | Drop in a later migration |
| Learned caps / decay half-life | Env vars per §5.7 defaults | Hard-coded constants |
| Analysis poll interval | 5 minutes | Daily-only before learn job |
| Learn waits for analysis | 30 min max, then rating-only fallback | Block until complete |
| Pipeline orchestration | Single `run_daily_pipeline` (B) | Staggered cron (A) |
| `applied` without explicit rating | Default rating 5 | Require rating always |
| Positive semantic signal | Single centroid MVP, k-NN upgrade in Phase D stretch | k-NN from day one |
| Feedback on all-jobs list | Defer post-MVP | Job-level feedback table |

Resolved: rating 1 hides immediately (and un-hides on re-rate ≥ 3); rating 2 stays visible as a down-rank signal only.

---

## 16. Related Code Touchpoints

| Area | File | Change |
|---|---|---|
| Feedback API | `backend/app/main.py` | JSON POST/GET, upsert, snapshot assembly, job-detail echo incl. inactive recommendation |
| Matching hide/upsert | `backend/app/matching.py` | Rating-based hide; rank-refresh guard; consume learned boosts/exclusions/lane overrides |
| Scoring | `backend/app/matching.py` `score_job()` | Merge learned signals via the application-history helpers |
| Embeddings | `backend/app/embeddings.py` | Centroid helpers; keep exclusions out of `profile_embedding_text()` |
| Job-fit LLM | `backend/app/llm.py` | Few-shot injection; hash includes `learned.version`; task-scoped cooldown split |
| Feedback learning | `backend/app/feedback_learning.py` (new) | `learn_from_feedback`, decay/hysteresis/caps, `effective_discovery_queries()`, comment sanitizer |
| Feedback analysis | `backend/app/feedback_analysis.py` (new) | Schema, prompt, `analyze_feedback`, grounding guard |
| Scheduler | `backend/app/scheduler.py` | New jobs, pipeline ordering, `expected_scheduler_job_ids()`, stale-job pruning pattern |
| Adapters | `backend/app/adapters/duunitori.py`, `tmt.py`, `laura.py` | Learned discovery query merge |
| Config | `backend/app/config.py` | §5.7 env vars |
| Audit | `backend/app/audit.py` | Feedback/learning reporting |
| UI | `web/app/tyopaikat/[id]/page.tsx` | Five-point control, applied toggle, hidden-state display |
| Proxy | `web/app/suositukset/[id]/palaute/route.ts` | JSON body forwarding |
| Tests | `backend/tests/test_jobs_api.py`, `test_matching.py`, new learning tests | Per-phase coverage |
| Docs | `docs/architecture.md`, `implementation-journal.md` | Keep in sync |

Run `make test-regression` before adapter changes; `make test-backend` for all backend phases. Always run tests with an explicit per-test timeout (`pytest --timeout=10`).

---

## 17. Plan Review Resolution Log

Senior review (2026-07-05, two passes against the live codebase). Findings and where each is resolved:

| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | Critical | Scheduler runs collect + match concurrently at 16:00 | §8, Phase C task 4 |
| 2 | Critical | Feedback append-only; any historical `not_relevant` row hides forever | §5.1 collapse + `UNIQUE(recommendation_id)`, Phase A |
| 3 | Critical | `profile_embeddings` allows one row per profile; centroids don't fit | §5.6 new table |
| 4 | Critical | Discovery reads env only, not profile | §6.5, Phase D |
| 5 | Critical | Snapshot needed by analysis but was deferred | Phase A task 4 |
| 6 | Critical | Hide logic keyed to legacy `action` only | §6.3, Phase A task 6 |
| 7 | High | Rating-2 UX contradicted hide rules | §3.4/§6.3 aligned |
| 8 | High | No GET/feedback echo for UI | §9.1, Phase A task 3 |
| 9 | High | Provider cooldown not task-scoped | §7.5 separate cooldown file |
| 10 | High | Eval provider ≠ embedding provider | §6.4 OpenAI note |
| 11 | High | Exclusions must not enter the profile embedding | §5.3 |
| 12 | High | Stale job-fit eval cache after learned state changes | §11 hash includes `learned.version` |
| 13–33 | Medium | First-pass findings: snapshot fields, lane overrides, comment sanitizer, env vars, module boundaries, audit CLI, section ordering, loopback security note, migration mapping, `applied`-without-rating default, `job_id` denormalization, portal proxy still action-based, enrichment pipeline slot, regression test gate | Integrated throughout §3–§16 |
| 34 | **Critical** | `GET /jobs/{job_id}` filters `r.is_active = true` — after rating 1, the recommendation panel and rating control disappear, so a hidden verdict can never be revised | §6.3, §9.1, Phase A tasks 3/7 |
| 35 | **High** | No immediate un-hide path: upsert-only re-activation would delay un-hiding until the next daily run | §6.3 re-activate on re-rate ≥ 3, Phase A task 2 |
| 36 | High | `refresh_active_recommendation_ranks()` is feedback-unaware; hide enforcement exists only in the deterministic upsert path | §6.3 guard, Phase A task 6 |
| 37 | High | No design for conflicting/aging feedback: last-write-wins, term flapping, and self-reinforcing exclusion loops were unaddressed | §6.7 (net weights, decay, hysteresis, caps, exploration exemption), §14 item 8 |
| 38 | High | LLM-suggested terms could inject hallucinated tokens into learned state | §7.4 grounding guard |
| 39 | Medium | Incremental learning cursor (`learning_processed_at`) adds drift risk for no benefit at this data scale | §5.1/§6.2 stateless recompute; cursor dropped |
| 40 | Medium | Centroids lacked model/dimension binding and min-support; single centroid blurs multimodal interests | §5.6, §6.4 k-NN upgrade path |
| 41 | Medium | Snapshot lacked `rank`/`is_active` (position bias unobservable) and `rationale`/`concerns` (overwritten nightly by the full recommendation reset) | §5.2 |
| 42 | Medium | Few-shot examples could overfit one theme | §6.6 diversity rule |
| 43 | Low | Generic high-frequency tokens could dominate learned terms | §6.7 document-frequency cap + `GENERIC_MATCH_TERMS` |

---

## 18. Research Grounding

Why this implementation path (explainable hybrid over a trained ranker) is the efficient one for a single-user, small-label system:

- **Rocchio relevance feedback (1971)** is exactly the §6.4 centroid mechanism: move the query/profile vector toward the mean of relevant items and away from non-relevant ones, with separate α/β/γ weights. It remains the standard, empirically validated baseline for learning from a handful of explicit relevance labels — see the [Stanford IR Book chapter](https://nlp.stanford.edu/IR-book/html/htmledition/the-rocchio71-algorithm-1.html) and the [relevance feedback overview](https://en.wikipedia.org/wiki/Relevance_feedback). Classic SMART weightings (α=1, β=0.75, γ=0.15) weight negatives well below positives; our α/β choice deliberately weights the anti-centroid slightly higher because our negatives are explicit 1–2 ratings, not silent skips.
- **Small-data regime:** with tens-to-hundreds of labels, content-based methods with explicit feedback (Rocchio, k-NN over rated items) outperform trained rankers, which need far more data to beat them and lose explainability. This is why §4.3 rejects a standalone ML ranker and §6.4 plans a k-NN upgrade — k-NN handles multimodal preference clusters that a single centroid averages away.
- **Profile editability beats pure feedback:** research-paper recommender studies ([Beel et al. survey](https://arxiv.org/pdf/1703.09109), citing Middleton et al.) found that letting users inspect and edit their learned model outperforms silent relevance feedback. Hence `term_provenance`, the audit CLI, and the post-MVP portal view of learned terms — learned state must stay human-legible and correctable.
- **Feedback-loop degeneracy:** the recommender-systems literature on algorithmic confounding shows that systems trained mostly on their own recommendations homogenize and degrade, because filtered-out items can never generate corrective feedback. §6.7's exploration-lane exemption, soft (never hard) learned exclusions, decay, and the exploration-hit-rate canary are the standard mitigations applied at this system's scale.
- **Explicit ratings are the strongest available signal** here (unlike large-scale systems forced onto biased implicit clicks); the plan still records `rank` at feedback time (§5.2) because presentation bias affects even explicit ratings — only shown jobs get rated.
- **LLM-as-analyst, not LLM-as-ranker:** using the LLM to interpret feedback into structured, grounded, human-auditable actions (§7) keeps the learning loop explainable and cheap — the same bounded-LLM philosophy as the existing pipeline: deterministic first, LLM where judgement matters.
