# Feedback Learning and Five-Point Rating Plan

**Updated:** 2026-07-05  
**Status:** planned — feedback is stored today, but learning from feedback is not implemented.  
**Related:** [`goal.md`](goal.md), [`architecture.md`](architecture.md), [`tech-stack-plan.md`](tech-stack-plan.md), [`llm-hosted.md`](llm-hosted.md), [`implementation-journal.md`](implementation-journal.md)

This document defines how user feedback should evolve from coarse action buttons into a five-point rating scale, how each rating triggers an LLM analysis of why the user likely rated the job that way, and how feedback plus that analysis should improve collection recall, deterministic scoring, semantic retrieval, and LLM evaluation on later pipeline runs — without replacing the existing staged matching architecture.

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
| `recommendation_feedback` table | Done | Stores `good_match`, `not_relevant`, `applied` |
| `POST /recommendations/{id}/feedback?action=...` | Done | `backend/app/main.py` |
| Finnish detail-page feedback buttons | Done | `web/app/tyopaikat/[id]/page.tsx` |
| `not_relevant` suppresses active recommendation | Done | Upsert logic in `backend/app/matching.py` |
| Positive feedback affects next matching run | Missing | No learned weights or terms |
| Feedback scoring snapshot | Missing | Cannot audit why a job was rated |
| Feedback learning worker job | Missing | No pre-matching learning stage |
| Discovery query tuning from feedback | Missing | `DISCOVERY_SEARCH_QUERIES` is static |
| LLM few-shot examples from feedback | Missing | Prompt uses profile facts only |
| LLM feedback analysis on submit | Missing | No structured “why did the user rate this?” evaluation |

The staged matching pipeline described in [`architecture.md`](architecture.md) remains the correct foundation. Feedback learning should attach to that pipeline, not replace it.

---

## 3. Five-Point Rating Model

### 3.1 Scale Definition

Replace the three coarse actions with one primary signal: an integer rating from **1 to 5**.

| Rating | Finnish UI label | English meaning | Learning role |
|---|---|---|---|
| **1** | Todella huono | Really bad match | Strong negative; hide recommendation; add exclusion patterns |
| **2** | Huono | Bad match | Moderate negative; down-rank similar jobs |
| **3** | Neutraali | Neutral / unsure | Low learning weight; useful for calibration only |
| **4** | Hyvä | Good match | Moderate positive; boost similar jobs |
| **5** | Erittäin hyvä | Extremely good match | Strong positive; prioritize similar jobs and discovery terms |

Rating labels are shown in Finnish in the portal. Stored values, API contracts, logs, and learning code use the numeric scale.

### 3.2 Optional Secondary Signals

Keep one optional action separate from the rating:

| Signal | Purpose |
|---|---|
| `applied` (boolean) | User actually submitted an application; strongest positive ground truth |

A job can be rated **5** without being marked applied, and applied jobs should usually also receive a high rating. Applied status is a stronger label than rating alone and should receive the highest learning weight.

Optional free-text `comment` (max 1000 chars) remains available for operator review. Sanitized comment text may be included in the feedback-analysis LLM prompt when the user explicitly provided it; never send unsanitized PII to hosted providers.

### 3.3 Migration from Current Actions

Map existing feedback rows when introducing the rating column:

| Legacy action | Default rating |
|---|---|
| `not_relevant` | 1 |
| `good_match` | 4 |
| `applied` | 5 + `applied=true` |

After migration, deprecate `action` as the primary feedback field. Keep it nullable during transition or derive it from rating for backward-compatible reporting.

### 3.4 UX Contract

On recommendation detail pages:

- show five clearly labeled rating controls (radio group or segmented control), not three separate verbs
- show one optional “Marked as applied” toggle or button
- allow changing the latest rating for the same recommendation (upsert latest user verdict, append history if audit requires it)
- confirm visually when rating **1** or **2** hides the job from active recommendations

Accessibility requirements:

- keyboard-operable rating control with visible focus
- `aria-label` per rating option
- do not rely on color alone to distinguish selected rating

---

## 4. Target Architecture

### 4.1 High-Level Loop

```text
User rates recommendation (1–5, optional applied, optional comment)
  -> store feedback + scoring snapshot (API returns immediately)
  -> worker: analyze_feedback (async LLM)
       -> compare user rating vs system scores/rationale
       -> store structured “why likely rated this way” analysis
  -> daily feedback learning job (worker)
       -> merge rating weights + LLM analysis actions
       -> update learned boosts / exclusions
       -> update preference / anti-preference embedding centroids
       -> tune discovery search queries
       -> prepare LLM few-shot examples
  -> collection (static + learned discovery queries)
  -> matching (deterministic + learned weights + adjusted semantic scores)
  -> LLM evaluation (few-shot + feedback pre-filter)
  -> portal recommendations
```

The portal never runs learning or LLM calls on page load. Feedback persistence is synchronous; LLM feedback analysis runs asynchronously in the worker so rating submission stays fast.

### 4.2 Service Boundaries

| Component | Responsibility |
|---|---|
| `web` | Rating UI, optional applied toggle, comment |
| `api` | Validate and persist feedback + snapshot; never block on LLM analysis |
| `worker` | Run `analyze_feedback` for pending rows; run `learn_from_feedback` before `match_recommendations` |
| `db` | Feedback rows, scoring snapshots, LLM feedback analyses, learned profile fields, learning run audit |

No Redis, Celery, or separate ML service is required for MVP learning.

### 4.3 Why Not a Standalone ML Ranker

This is a single-job-seeker system with slow feedback accumulation (tens to hundreds of labels, not millions). A full re-ranking model would be:

- hard to debug against product requirements for explainability
- unstable with very small datasets
- operationally heavier than necessary

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
| `rating` | `smallint` | Required for new rows; check `rating between 1 and 5` |
| `applied` | `boolean` | Default `false` |
| `comment` | `text` | Optional, unchanged |
| `scoring_snapshot` | `jsonb` | Feature snapshot at feedback time |
| `created_at` | `timestamptz` | Unchanged |

Deprecate or nullable:

| Column | Notes |
|---|---|
| `action` | Legacy enum; migrate then stop writing |

Recommended index: `(recommendation_id, created_at desc)` for latest verdict lookup.

Policy for multiple submissions:

- **MVP:** one latest active verdict per recommendation (upsert by `recommendation_id`)
- **Post-MVP:** append-only history table if temporal analysis becomes important

### 5.2 Scoring Snapshot Schema

Persist enough context to explain and reuse the label:

```json
{
  "job_id": 1234,
  "title": "Palveluassistentti",
  "employer": "Oulun kaupunki",
  "location": "Oulu",
  "candidate_lanes": ["application_history", "sector_context"],
  "machine_score": 62.0,
  "vector_score": 0.71,
  "llm_score": 78,
  "fit_tier": "transferable_weaker",
  "suggested_action": "consider",
  "title_matches": [],
  "keyword_matches": ["hallinto", "asiakaspalvelu"],
  "sector_matches": ["kunta"],
  "negative_matches": [],
  "prompt_version": 7,
  "model": "gemini-3.1-flash-lite"
}
```

The API assembles this snapshot from the current `recommendations` row and linked `llm_evaluations` metadata when feedback is submitted.

### 5.3 Learned Profile Fields

Store derived preferences inside the single job seeker profile JSON (no multi-user complexity):

```json
{
  "preferences": {
    "learned_boosts": {
      "boost_titles_fi": ["palveluassistentti"],
      "boost_keywords_fi": ["hallinto", "tapahtumat"],
      "boost_locations": ["oulu"],
      "boost_sectors": ["kunta"]
    },
    "learned_exclusions": {
      "terms_fi": ["myynti", "commission"],
      "employers": [],
      "sectors": ["retail"]
    }
  },
  "learned": {
    "discovery_queries": ["palveluassistentti", "hallintosihteeri"],
    "preference_centroid_embedding_id": 42,
    "anti_centroid_embedding_id": 43,
    "few_shot_examples": [
      {"rating": 5, "title": "...", "employer": "...", "verdict": "apply"},
      {"rating": 1, "title": "...", "employer": "...", "verdict": "skip"}
    ],
    "updated_at": "2026-07-05T13:00:00Z",
    "source_feedback_count": 18
  }
}
```

`learned_boosts` mirrors the existing `application_history_signals` shape so `score_job()` can consume both sources with one code path.

### 5.4 Learning Run Audit

Add `learning_runs`:

| Column | Purpose |
|---|---|
| `started_at`, `finished_at`, `status` | Worker audit |
| `feedback_processed` | Count of new/changed feedback rows |
| `changes_applied` | JSONB summary of term/query/centroid updates |
| `prompt_version` | LLM few-shot bundle version |

### 5.5 Feedback LLM Analysis

Add `feedback_llm_analyses` (one row per feedback submission; upsert when user re-rates):

| Column | Type | Notes |
|---|---|---|
| `feedback_id` | `int` FK | Unique; one analysis per feedback row |
| `status` | `text` | `pending`, `completed`, `failed`, `skipped` |
| `analysis` | `jsonb` | Structured LLM output (schema below) |
| `request_hash` | `text` | Dedup unchanged re-runs |
| `model` | `text` | Provider model id |
| `prompt_version` | `int` | Feedback-analysis prompt version |
| `llm_evaluation_id` | `int` FK nullable | Reuse `llm_evaluations` audit pattern if desired |
| `created_at`, `completed_at` | `timestamptz` | Audit |

Extend `recommendation_feedback`:

| Column | Type | Notes |
|---|---|---|
| `analysis_status` | `text` | Denormalized mirror of latest analysis status for quick API reads |

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

Field definitions:

| Field | Purpose |
|---|---|
| `system_alignment` | `aligned`, `under_ranked`, `over_ranked`, `mixed`, `unclear` |
| `hypothesis_fi` | Short Finnish explanation for operators and future portal diagnostics |
| `likely_positive_signals` / `likely_negative_signals` | Token/sector candidates for learning |
| `mismatch_drivers` | Structured root-cause tags for tuning |
| `suggested_actions` | Machine-readable tuning hints consumed by `learn_from_feedback` |
| `confidence` | `low`, `medium`, `high` — low confidence reduces learning weight |

---

## 6. Learning Algorithm by Rating

### 6.1 Weight Table

Use explicit weights so behavior stays auditable:

| Signal | Learning weight |
|---|---|
| Rating 1 | −1.0 negative |
| Rating 2 | −0.5 negative |
| Rating 3 | 0.0 (ignored by default) |
| Rating 4 | +0.5 positive |
| Rating 5 | +1.0 positive |
| `applied=true` | +1.5 positive (stacks with rating) |

Repeated patterns increase confidence; single ratings produce small nudges only.

### 6.2 Deterministic Term Learning

Worker job `learn_from_feedback`:

1. Load feedback rows not yet processed by the latest successful learning run (or all rows on first run).
2. Prefer rows with `analysis_status = completed`; fall back to rating-only learning when LLM provider is disabled or analysis failed.
3. Extract tokens from title, employer, description keywords, location, and `candidate_lanes` in each snapshot.
4. Merge LLM `suggested_actions` with weight table outputs (LLM suggestions receive an explicit multiplier; see section 7.4).
5. Aggregate weighted term frequencies separately for positive and negative buckets.
6. Promote terms to `learned_boosts` when positive weight exceeds threshold (example: ≥ 1.5 cumulative).
7. Promote terms to `learned_exclusions` when negative weight exceeds threshold (example: ≤ −1.0 cumulative).
8. Never override hard profile exclusions or qualification reject rules already encoded in matching.

Integration in `score_job()`:

- add learned title/keyword/location/sector matches using the same helpers as `application_history_signals`
- apply learned exclusion terms before candidate lane selection
- store `learned_*_matches` in `deterministic_result` for portal/debug visibility

### 6.3 Visibility Rules from Rating

| Rating | Portal behavior on next API read |
|---|---|
| 1 | `is_active=false`, `rank=null` (same as legacy `not_relevant`) |
| 2 | remain visible but strongly down-ranked unless user re-rates |
| 3–5 | remain eligible; positive ratings increase future rank indirectly through learning |

Immediate hide only for rating **1** keeps the UI honest: “really bad” means “stop recommending this”.

### 6.4 Embedding Preference Learning

When embeddings are enabled (`OPENAI_API_KEY` or future local provider):

1. Collect embeddings for jobs with rating ≥ 4 or `applied=true` → compute **preference centroid** (mean vector).
2. Collect embeddings for jobs with rating ≤ 2 → compute **anti-centroid**.
3. Store centroids in `profile_embeddings` with types `preference_learned` and `anti_preference_learned`.

Adjusted semantic score for candidate jobs:

```text
adjusted = base_vector_score
         + α * cosine(job, preference_centroid)
         - β * cosine(job, anti_centroid)
```

Suggested MVP constants: `α = 0.15`, `β = 0.20`, tuned offline against labelled feedback.

Embeddings remain **retrieval signals**, not final truth. Deterministic rules and LLM review still decide suitability.

### 6.5 Discovery Query Tuning

Positive feedback should improve **recall** upstream:

1. Rank title and keyword tokens from rating 4–5 and applied jobs.
2. Merge top new terms into `profile.learned.discovery_queries` (cap at 12 terms).
3. Worker collection merges `DISCOVERY_SEARCH_QUERIES` + learned queries for Duunitori, TMT, and Laura discovery paths already described in [`architecture.md`](architecture.md).
4. Remove or demote discovery terms that repeatedly appear in rating 1–2 jobs.

This closes the loop between user taste and source harvesting without crawling the entire market differently for every user.

### 6.6 LLM Context Learning

Do not fine-tune the hosted model for MVP. Instead:

**Few-shot examples**

- Select up to 3 positive (rating 5 or applied) and up to 2 negative (rating 1) examples.
- Add a compact “prior user verdicts” section to the evaluation prompt (`prompt_version` bump required).
- Strip employer/job text to the minimum needed; never include comments or raw profile files.

**Feedback pre-filter before LLM calls**

- Skip LLM evaluation when a candidate is semantically close to the anti-centroid and deterministic score is below a mid threshold.
- Prioritize LLM slots for candidates close to the preference centroid or with strong learned boost matches.

This preserves the bounded LLM budget while improving judgement quality.

---

## 7. LLM Feedback Analysis

When a user submits feedback, the system runs a dedicated LLM evaluation that asks: **given what we recommended and why, why might the user have rated this job at this level?** The output guides algorithm sharpening; it does not replace the numeric rating as ground truth.

### 7.1 Purpose

| Without analysis | With analysis |
|---|---|
| Learn only from co-occurring tokens | Learn from interpreted mismatch drivers (role family, location, qualifications, sector, work mode) |
| Hard to explain false positives | `over_ranked` cases reveal which lane or score component misfired |
| Discovery tuning is keyword frequency only | LLM can propose/add/remove discovery queries with context |
| Operator blind to user intent | Finnish `hypothesis_fi` documents likely reason for each rating |

### 7.2 Inputs (Privacy-Bounded)

Send the same minimized boundary as job-fit evaluation ([`llm-hosted.md`](llm-hosted.md)):

| Input | Included |
|---|---|
| Minimized profile summary | Yes — `minimized_profile_summary()` allowed keys only |
| Job summary (title, employer, location, description excerpt) | Yes |
| Stored recommendation rationale and concerns | Yes |
| Deterministic match evidence from `scoring_snapshot` | Yes |
| System scores (`machine_score`, `vector_score`, `llm_score`, `fit_tier`, `suggested_action`) | Yes |
| User rating, `applied`, sanitized comment | Yes |
| Raw CV, application letters, contact details | No |
| Full raw source payloads | No |

The prompt must state explicitly that the model is **hypothesizing** user intent; it must not invent user motivations unrelated to supplied facts.

### 7.3 Prompt Contract

Add a new prompt family in `backend/app/llm.py` (or `feedback_analysis.py`) with its own `FEEDBACK_ANALYSIS_PROMPT_VERSION`.

Instructions (Finnish output for `hypothesis_fi`; structured fields in JSON):

- Compare the user's rating and optional comment against the system's recommendation rationale and scores.
- Identify whether the system was aligned, too optimistic (`over_ranked`), or too pessimistic (`under_ranked`).
- Explain the most likely reason the user rated the job at this level, referencing concrete job and profile facts.
- Propose machine-readable tuning actions: terms to boost/exclude, sectors, discovery queries, lane notes, and LLM-eval hints for future runs.
- If rating is 3 (neutral), focus on ambiguity and calibration — what was acceptable vs missing — rather than strong boosts or exclusions.
- If user comment contradicts the rating, treat the rating as authoritative and mention the contradiction in `mismatch_drivers`.
- Do not fabricate qualifications, user preferences, or application history not present in the profile summary.

Use structured outputs (same provider stack as job-fit evaluation: OpenAI or Gemini).

### 7.4 How Analysis Feeds Learning

`learn_from_feedback` merges three signal sources:

```text
final_boost/exclude weight =
    rating_weight (section 6.1)
  + token_frequency_weight
  + llm_action_weight * confidence_multiplier
```

Suggested confidence multipliers:

| `confidence` | Multiplier |
|---|---|
| `high` | 1.0 |
| `medium` | 0.6 |
| `low` | 0.2 |

Rules:

- LLM-suggested exclusions never override hard-coded qualification rejects; they only add soft penalties or discovery demotions.
- When `system_alignment = over_ranked` and rating ≤ 2, increase weight on `lane_notes` and demote the responsible candidate lane quota temporarily (example: reduce `exploration` by 1 for the next run).
- When `system_alignment = under_ranked` and rating ≥ 4, promote `likely_positive_signals` even if token frequency is low (single strong signal allowed).
- Deduplicate suggested terms already in profile hard exclusions or explicit target titles.

### 7.5 Execution Model

**Do not block the feedback API on LLM latency.**

```text
POST /feedback
  -> insert feedback row (analysis_status = pending)
  -> return 201 immediately

Worker (short interval or piggyback on daily pipeline):
  analyze_feedback
    -> select feedback where analysis_status = pending
    -> call LLM once per row (respect provider cooldown)
    -> store feedback_llm_analyses
    -> set analysis_status = completed | failed | skipped

learn_from_feedback (daily, before matching):
    -> wait for pending analyses older than N minutes OR proceed with rating-only fallback
    -> consume completed analyses
```

Scheduling options:

| Option | Recommendation |
|---|---|
| Poll pending rows every 5 minutes | Good for responsive learning on a local server |
| Run `analyze_feedback` once before `learn_from_feedback` | Minimum viable; acceptable if daily batch is enough |
| Synchronous LLM in API request | Rejected — poor UX and ties portal to provider latency |

When the LLM provider is disabled or in cooldown, set `analysis_status = skipped` and continue with rating-only learning.

### 7.6 Caching and Cost Control

- Hash inputs: profile summary + job summary + scoring snapshot + rating + applied + sanitized comment + prompt version → `request_hash`.
- Re-submitting the same rating without material changes reuses the stored analysis.
- Re-rating invalidates prior analysis (new feedback row or upsert with new hash → re-analyze).
- Cap: one analysis call per feedback submission; no retry loops beyond existing provider retry policy.
- Log `event=feedback_analysis_completed` with alignment and confidence; never log full prompts or profile text.

### 7.7 Operator and Future Portal Use

MVP stores analysis for operator audit (`python -m app.audit` or future admin view). Post-MVP may show a collapsible “Why you might have rated this” diagnostic on the job detail page — **read-only**, clearly labeled as system hypothesis, not user-facing accusation.

Analysis rows also enrich few-shot bundles for job-fit evaluation: include one compact `hypothesis_fi` + `suggested_actions.llm_eval_hint` pair where `confidence = high`.

---

## 8. Pipeline Schedule

Recommended worker order on the daily cron (Europe/Helsinki, currently 16:00):

```text
1. collect enabled sources (+ learned discovery queries)
2. enrichment (when shipped)
3. analyze_feedback (drain pending rows)
4. learn_from_feedback
5. match_recommendations (deterministic + semantic + LLM)
```

Additionally, run `analyze_feedback` on a short interval (example: every 5 minutes) so same-day learning can use fresh analyses before the daily match job when feedback is submitted early.

`learn_from_feedback` must complete before matching so the same day's run uses fresh learned state. Pending analyses older than a configurable threshold (example: 30 minutes) may be skipped by learning rather than blocking the pipeline.

If collection and matching remain on the same schedule slot, serialize with explicit pipeline state — matching must not read stale pre-learning profile data.

---

## 9. API and Portal Changes

### 9.1 API

Replace action-based feedback with:

```http
POST /recommendations/{recommendation_id}/feedback
Content-Type: application/json

{
  "rating": 5,
  "applied": false,
  "comment": null
}
```

Validation:

- `rating` required, integer 1–5
- `applied` optional boolean, default false
- `comment` optional string, max 1000

Response includes `rating`, `applied`, `id`, `recommendation_id`, `analysis_status` (`pending` initially), and whether the recommendation was hidden (`rating == 1`).

Optional read endpoint for operators:

```http
GET /recommendations/{recommendation_id}/feedback/analysis
```

Returns latest structured analysis when `analysis_status = completed`; 404 or `pending` otherwise. Not required for MVP portal UI.

Deprecation path:

- accept legacy `?action=good_match|not_relevant|applied` for one release cycle
- map legacy calls to rating values from section 3.3

### 9.2 Portal

Finnish labels on the job detail recommendation panel:

| Control | Label |
|---|---|
| Rating 1 | Todella huono |
| Rating 2 | Huono |
| Rating 3 | Neutraali |
| Rating 4 | Hyvä |
| Rating 5 | Erittäin hyvä |
| Applied toggle | Merkitse haetuksi |

Show the current rating if feedback already exists. Submitting rating 1 should immediately reflect hidden state on reload. Do not wait for LLM analysis before showing success — analysis runs in the background.

---

## 10. Observability and Quality Metrics

Every learning run should answer:

- how many feedback rows were processed
- how many had completed LLM analysis vs rating-only fallback
- which terms were added or removed from boosts/exclusions
- which discovery queries changed
- whether centroids were updated or skipped (missing embeddings)
- distribution of `system_alignment` values (`over_ranked`, `under_ranked`, etc.)

Feedback analysis quality metrics:

| Metric | Purpose |
|---|---|
| Analysis completion rate | Share of feedback rows reaching `completed` |
| Alignment accuracy (manual spot-check) | Does `over_ranked` / `under_ranked` match operator judgement? |
| Action uptake rate | Share of LLM-suggested terms that learning job applied |
| Repeat false positive rate | Same `mismatch_drivers` appearing on multiple rating-1 jobs |

Offline benchmark (small labelled set from real applications + feedback):

| Metric | Purpose |
|---|---|
| Recall@30 on positive labels | Rating ≥ 4 and applied jobs appear in top 30 |
| False positive rate | Rating 1–2 jobs that still reach LLM review |
| Exploration hit rate | Positive ratings originating from `exploration` lane |
| Discovery contribution | Recommendations traced to learned discovery terms |
| LLM call savings | Candidates skipped by feedback pre-filter |

Store benchmark results in `learning_runs.changes_applied` or a separate operator note; do not block the pipeline on benchmark failure in MVP.

---

## 11. Privacy and Safety Rules

- Learning and feedback analysis use the same privacy boundary as matching: minimized profile fields and normalized job summaries only.
- Sanitize user comments before inclusion in LLM prompts (strip emails, phone numbers, personal names not already in allowed profile fields).
- Learned terms, centroids, and analyses must be deletable together with profile data.
- Feedback analysis must not infer protected attributes or fabricate user motivations.
- Hard qualification reject rules in `matching.py` remain authoritative over learned exclusions and LLM-suggested exclusions.
- Mark stored hypotheses clearly as system-generated estimates, not verified user statements.

---

## 12. Phased Implementation Plan

### Phase A — Five-Point Feedback (user-facing)

**Goal:** Users can rate recommendations on a 1–5 scale and optionally mark applied.

Tasks:

1. Alembic migration: add `rating`, `applied`, `scoring_snapshot`; migrate legacy `action` rows.
2. Update `POST /recommendations/{id}/feedback` request/response models.
3. Replace three-button UI with five-point control + applied toggle.
4. Preserve hide behavior for rating 1 in matching upsert logic.
5. Unit tests for validation, migration mapping, and hide-on-rating-1.

Verification:

- `make test-backend` passes
- manual rating submission on `/tyopaikat/{id}` persists and reloads correctly

### Phase B — Feedback Analysis (LLM)

**Goal:** Each feedback submission receives an async structured LLM analysis.

Tasks:

1. Alembic migration: `feedback_llm_analyses`, `recommendation_feedback.analysis_status`.
2. Implement `FeedbackAnalysis` structured schema and prompt (`FEEDBACK_ANALYSIS_PROMPT_VERSION`).
3. Worker job `analyze_feedback` with request-hash dedup and provider cooldown handling.
4. Wire `analysis_status = pending` on feedback create; poll or pre-match drain.
5. Unit tests with fixture LLM provider; verify privacy-bounded inputs.

Verification:

- submitting rating creates `pending` row; worker stores `completed` analysis with `hypothesis_fi` and `suggested_actions`
- provider disabled → `skipped`, feedback API still succeeds

### Phase C — Snapshot and Learning Foundation

**Goal:** Persist auditable features and run learning that consumes ratings + analyses.

Tasks:

1. Assemble `scoring_snapshot` on feedback submit.
2. Add `learning_runs` table and `learn_from_feedback` worker job.
3. Implement term aggregation with weight table; merge LLM `suggested_actions`.
4. Wire learned terms into `score_job()` and store match evidence in `deterministic_result`.
5. Unit tests for weight aggregation, LLM action merge, and score changes.

Verification:

- rating 5 with high-confidence analysis promotes LLM-suggested boost terms
- rating 1 with `over_ranked` analysis promotes exclusion terms and hides recommendation on re-run

### Phase D — Semantic and Discovery Learning

**Goal:** Improve recall and semantic ranking from feedback.

Tasks:

1. Compute and store preference / anti-preference centroids in `profile_embeddings`.
2. Apply adjusted semantic scoring in matching.
3. Merge learned discovery queries into collection discovery paths.
4. Cap and dedupe discovery terms; log changes in `learning_runs`.

Verification:

- learned discovery term appears in collector logs
- semantic lane promotes jobs closer to preference centroid in fixture tests

### Phase E — LLM Feedback Context

**Goal:** Improve LLM judgement without fine-tuning.

Tasks:

1. Build few-shot example bundle from highest-confidence feedback rows and analyses.
2. Bump job-fit `prompt_version`; include examples and `llm_eval_hint` values from analyses.
3. Add feedback-aware pre-filter before `run_llm_evaluations`.
4. Record `feedback_informed` metadata on `llm_evaluations`.

Verification:

- prompt fixture tests include examples
- LLM candidate count drops when anti-centroid filter applies

### Phase F — Benchmark and Operator Visibility

**Goal:** Measure whether learning helps.

Tasks:

1. Add offline benchmark script over stored feedback labels.
2. Expose learning run summary and feedback analysis counts via audit CLI or operator endpoint.
3. Document tuning constants (`α`, `β`, thresholds) in this file when measured.

---

## 13. Non-Goals

- Multi-user feedback or per-account preference models
- Online learning on every click (batch daily learning only; analysis async but not blocking)
- Hosted LLM fine-tuning
- Replacing deterministic filters with an opaque ranker
- Redis/Celery queue infrastructure for learning
- Synchronous LLM analysis in the feedback API request path
- Treating LLM feedback hypotheses as verified user statements in the portal UI

---

## 14. Success Criteria

The feedback learning system is working when:

1. A user can rate any active recommendation on a 1–5 scale with Finnish labels from “todella huono” to “erittäin hyvä”.
2. Each rating triggers an async LLM analysis that records a Finnish hypothesis and structured tuning actions.
3. Rating 1 hides the recommendation; ratings 4–5 measurably boost similar jobs on the next daily run.
4. Learned state is visible in `deterministic_result` and auditable through `learning_runs` and `feedback_llm_analyses`.
5. Discovery queries expand or contract based on ratings and analysis suggestions, not keyword frequency alone.
6. Job-fit LLM evaluation uses feedback-informed examples and hints from high-confidence analyses.
7. The staged pipeline order remains: collect → analyze → learn → match → portal.

---

## 15. Open Decisions

| Decision | Recommendation | Alternative |
|---|---|---|
| One vs many feedback rows per recommendation | Upsert latest verdict for MVP | Append-only history for analytics |
| Rating 2 visibility | Down-rank only | Hide like rating 1 |
| Neutral rating 3 | Ignore in learning | Weak negative if LLM score was high |
| Legacy `action` column | Keep nullable after migration | Drop in later migration |
| Learned query cap | 12 terms | Configurable env var |
| Analysis poll interval | 5 minutes | Daily-only before learn job |
| Learn waits for analysis | 30 minute max wait, then fallback | Block learn until all complete |

Resolve rating 2 visibility during Phase A UX review; default recommendation is down-rank only so users can correct a harsh but not absolute rejection.
