# Hosted LLM Plan

**Updated:** 2026-06-20 (v3 -- hosted-provider research refresh)  
**Scope:** hosted API providers only. Local-model experiments are covered by the broader stack plan, not selected here.  
**References:** [`goal.md`](goal.md), [`tech-stack-plan.md`](tech-stack-plan.md).

This document chooses the hosted LLM path for the job-matching pipeline and defines what each model call should accomplish. The outcome is intentionally implementation-oriented: pick one cheap, reliable default; keep provider swaps possible; benchmark Finnish retrieval before expanding the model stack.

---

## Research outcome for this project

Use **OpenAI first** for the MVP:

| Pipeline stage | MVP choice | Why |
|---|---|---|
| Job/profile embeddings | `text-embedding-3-small`, 1536 dimensions | Lowest-friction default, cheap enough to embed all collected listings, same provider as evaluator. |
| Routine job-fit evaluation | `gpt-5.4-nano` | Current low-cost OpenAI model positioned for classification, extraction, and ranking; good task fit for one-job structured scoring. |
| Escalation evaluation | `gpt-5.4-mini` | Use only for ambiguous candidates or operator-triggered re-review. |
| Provider fallback | `gemini-3.1-flash-lite` | Current GA Gemini low-cost model; supports structured output and gives provider diversity. |
| Optional retrieval upgrade | `voyage-4-lite` or `voyage-4` | Strong hosted multilingual retrieval option if Finnish recall is weak. |

Do **not** build the MVP around Anthropic Claude, Gemini 3.5 Flash, GPT-5.4 full, or GPT-5.5 for routine scoring. They are valid current model families, but the project workload is high-volume structured classification/ranking over short Finnish job summaries. Bigger models should be reserved for disputes, evaluations, or manual review.

Do **not** invent dated model snapshots. Use stable provider aliases in `.env` unless an official model page explicitly lists a snapshot and the project intentionally wants pinned behavior. In both cases, validate the configured ID at worker startup and record the exact resolved model/version returned by the provider in `llm_evaluations`.

---

## Corrections to the previous draft

The v2 draft improved the first pass, but still overfit to brittle provider details:

| Issue | Correction |
|---|---|
| Treating dated snapshots as safe from memory | Use stable aliases such as `gpt-5.4-nano` by default; use dated snapshots only if copied from the official model page and deliberately pinned. |
| Treating ChatGPT retirement as API retirement | ChatGPT model removal and API deprecation are separate. Only the provider API deprecation page should drive API lifecycle decisions. |
| Ranking an older Gemini Flash model as current fallback | The current low-cost GA fallback is `gemini-3.1-flash-lite`; `gemini-3.5-flash` is stronger but much more expensive. |
| Understating Claude structured-output support | Claude has structured outputs, but Anthropic is not the MVP default because OpenAI/Gemini fit this workload at lower integration cost. |
| Choosing embeddings without a benchmark | The first pgvector dimension is a schema commitment. Start cheap, then run a Finnish retrieval benchmark before changing dimensions/provider. |

---

## Role in the pipeline

LLMs are the final expensive stage:

1. Collect and normalize listings.
2. Deterministic hard filters: location, exclusions, deadlines, language, source availability.
3. Machine scores from structured fields and profile weights.
4. Embedding similarity for job text versus profile summary.
5. LLM evaluation on top candidates only.
6. Store recommendation and evaluation records; the portal reads stored data.

Hosted calls are **bounded**, **stored**, and **opt-in**. If `LLM_PROVIDER` or its API key is missing, the worker must skip hosted scoring and keep deterministic, machine, and vector ranking operational.

---

## Model selection rules

1. **Match the model to the task.** This is structured classification/ranking, not open-ended reasoning.
2. **Use current GA/stable models only.** Avoid preview, legacy, or already-deprecated IDs for default config.
3. **Configure aliases, record exact metadata.** `.env` may say `gpt-5.4-nano`; `llm_evaluations` stores provider, configured model, returned model/version, prompt version, timestamps, token usage, and schema version.
4. **Validate at startup.** The worker should fail fast or disable LLM scoring with a clear health warning if the configured model is unavailable.
5. **Keep one embedding dimension per deployment.** Changing embedding model/dimension means Alembic migration plus full re-embedding.
6. **Benchmark Finnish retrieval.** Public multilingual claims are not enough for Finnish job-title compounds, location phrases, and Työmarkkinatori vocabulary.

---

## Embeddings

### Recommendation

Start with **OpenAI `text-embedding-3-small` at 1536 dimensions**.

This is not a claim that it is the best Finnish embedder. It is the pragmatic MVP choice because it is cheap, easy to operate, and avoids introducing a second provider before the collection and recommendation pipeline is proven.

### Upgrade candidates

| Model | Provider | Dimensions | Use when |
|---|---|---:|---|
| `text-embedding-3-small` | OpenAI | 1536 | MVP default. |
| `text-embedding-3-large` | OpenAI | 3072, or lower via dimension shortening where supported | OpenAI-only quality upgrade after benchmark failure. |
| `voyage-4-lite` | Voyage AI | 1024 default; also 256/512/2048 | Lower-latency/cost multilingual retrieval upgrade. |
| `voyage-4` | Voyage AI | 1024 default; also 256/512/2048 | Higher-quality hosted multilingual retrieval upgrade. |
| `gemini-embedding-2` | Google | 3072-class | Consider only if standardizing on Google or using Gemini File Search/multimodal retrieval. Overkill for plain text MVP. |

### Finnish retrieval benchmark

Before switching providers or dimensions, create a small local benchmark from real collected listings:

- 30-50 profile-derived queries in Finnish and English, e.g. "kirjasto ja kulttuuri asiakaspalvelu Oulu", "hallinnon assistentti etätyö", "tapahtumakoordinaattori Pohjois-Suomi".
- Label at least 5 relevant or acceptable listings per query from Duunitori, Työmarkkinatori, and Laura where possible.
- Measure Recall@10, Recall@30, MRR, and the share of strong human matches missing from top 30.
- Upgrade embeddings only if the default misses clearly relevant Finnish matches often enough to affect recommendations.

Embed canonical job text and the minimized profile summary. Re-embed on content-hash/profile-summary change only.

---

## Evaluation models

### Recommendation

| Tier | Model | Provider | Use |
|---|---|---|---|
| Primary | `gpt-5.4-nano` | OpenAI | Routine one-job structured evaluations. |
| Escalation | `gpt-5.4-mini` | OpenAI | Ambiguous or high-machine-score candidates where nano is uncertain. |
| Fallback | `gemini-3.1-flash-lite` | Google | Provider diversity and OpenAI outage fallback. |
| Quality audit | `gemini-3.5-flash`, `gpt-5.4`, or `gpt-5.5` | Google/OpenAI | Offline evaluation, prompt tuning, disputed cases only. |
| Not default | Anthropic Claude | Anthropic | Re-check the latest Claude model and pricing before adding; not needed for MVP routine scoring. |

Use OpenAI Structured Outputs or Gemini structured output with a Pydantic schema. Validate every response locally even when provider-side schema enforcement is enabled.

### Concurrency and batching

Use **one job per request**, with bounded parallelism:

- `LLM_EVAL_BATCH_SIZE=5..10`
- `LLM_EVAL_MAX_JOBS=800` per run by default; lower it in constrained environments
- retry once on transient provider/schema failures
- persist skipped/error states instead of silently dropping candidates

Do not put many jobs into one prompt for ranking. Multi-job prompts are cheaper per call but harder to debug, easier to bias by ordering, and produce weaker per-card explanations.

---

## Prompt goals

### 1. Job embedding

**When:** after normalization and on content change.  
**Provider:** embedding API.

**Goal:** produce a vector that captures role type, sector, skills, location, seniority, schedule, and employer context.

**Input shape:**

```text
{title} | {employer} | {location} | {occupation_category}
{description_excerpt}
```

**Output:** fixed-dimension float vector.

**Non-goals:** ranking, rationale, or profile comparison.

### 2. Profile embedding

**When:** profile create/update and when allowed summary fields change.

**Goal:** encode target roles, transferable skills, sector preferences, location policy, language ability, and exclusions as one semantic document.

**Input:** minimized summary from `llm_allowed_fields` in `profile/*/profile.yaml`. Never send `llm_forbidden_fields`.

**Output:** same embedding dimension as jobs.

### 3. Job-profile evaluation

**When:** after deterministic + machine + vector stages; top candidates only.  
**Default provider/model:** `gpt-5.4-nano`.

**Goal:** act as a Finnish job-fit reviewer for one job seeker and one listing. Reward credible transferable fit over title overlap. Check hard requirements before giving high scores. Produce stored UI-ready rationale and concerns that are terse enough to read in a recommendation card.

Use profile `llm_guidance`:

| Field | Goal |
|---|---|
| `objective` | Score applicability and prevent generic matches from crowding out stronger transferable matches. |
| `fit_tiers` | Exactly one of `strong_fit`, `transferable_weaker`, `generic_customer_service_only`, `not_applicable`. |
| `eligibility_checks` | Verify stated qualification, licence, language, location, and schedule requirements. |
| `reward_signals` | Public sector, advisory/admin, library/culture, events, North Finland, remote/hybrid, and other profile-specific positives. |
| `caution_signals` | Missing licences, bad hybrid/location fit, excluded titles, language gaps, and generic service-only roles. |

**System message goal:** impartial Finnish job-match reviewer; use only provided facts; do not invent qualifications; return valid schema; keep `rationale` and `concerns` in Finnish.

**User message goal:** assess this one job against this one profile; cite transferable evidence; flag gaps; suggest action.

**Output schema:**

```json
{
  "score": 0,
  "fit_tier": "strong_fit",
  "rationale": "Suomenkielinen perustelu, joka viittaa siirrettävään näyttöön.",
  "concerns": ["Mahdollinen pätevyys-, sijainti-, kieli- tai aikatauluriski."],
  "suggested_action": "apply"
}
```

Recommended constraints:

- `score`: integer 0-100.
- `fit_tier`: enum.
- `suggested_action`: enum `apply | consider | skip`.
- `rationale`: concise Finnish text, at most two sentences / 360 characters.
- `concerns`: at most three Finnish strings, max 120 characters each; empty list allowed only when there is no meaningful concern.
- Avoid repeating the same missing-requirement phrase across concerns. Prefer direct labels such as `SAP-osaaminen puuttuu`.

Persist to `llm_evaluations`: listing ID, profile ID, provider, configured model, returned model/version, prompt version, schema version, request hash, response JSON, token usage, latency, timestamp, and error/retry metadata.

### 4. Escalation evaluation

**When:**

- nano returns `transferable_weaker` with high machine/vector score,
- nano returns `not_applicable` but deterministic/machine signals are strong,
- schema retries fail,
- operator requests re-review.

**Provider/model:** `gpt-5.4-mini` by default.

**Goal:** resolve ambiguity around qualification wording, transferable fit, location/hybrid requirements, and generic customer-service matches. Store as a separate evaluation row and mark which evaluation is active for recommendation ranking.

---

## Privacy and payload rules

| Rule | Detail |
|---|---|
| Opt-in | Hosted calls require `LLM_PROVIDER` plus provider API key. |
| Allowed profile data | `llm_allowed_fields` only. |
| Forbidden profile data | Raw CV, letters with PII, address, phone, email, birth date, SSN, photo. |
| Job text | Normalized fields plus stable excerpt; no raw source blobs. |
| Retention | Store local outputs and metadata; do not depend on provider conversation history. |
| Logging | Never log profile text or full prompt at info level. |
| Deletion | Profile deletion must also delete profile embeddings and recommendation/evaluation payloads containing profile-derived text. |

Set OpenAI `store: false` or the equivalent provider option where available for stateless evaluation calls.

---

## Provider abstraction

```python
class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EvaluationProvider(Protocol):
    async def evaluate_job_fit(
        self,
        profile_summary: str,
        job_summary: str,
        schema: type[JobFitEvaluation],
        *,
        model_tier: Literal["default", "escalated"] = "default",
    ) -> JobFitEvaluation: ...
```

MVP implementations:

- `OpenAIEmbeddingProvider`
- `OpenAIEvaluationProvider`
- `GeminiEvaluationProvider` only after the OpenAI path is working or provider fallback is needed

Keep the Pydantic model provider-neutral. Provider-specific adapters should translate the same schema into OpenAI/Gemini request formats.

---

## Environment variables

| Variable | Example | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `openai` | Primary hosted provider; empty disables hosted scoring. |
| `OPENAI_API_KEY` | `...` | OpenAI embeddings/evaluation. |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model. |
| `OPENAI_EMBEDDING_DIMENSION` | `1536` | Must match pgvector column. |
| `OPENAI_EVAL_MODEL` | `gpt-5.4-nano` | Default evaluator. |
| `OPENAI_EVAL_MODEL_ESCALATED` | `gpt-5.4-mini` | Escalation evaluator. |
| `GEMINI_API_KEY` | `...` | Optional fallback provider. |
| `GEMINI_EVAL_MODEL` | `gemini-3.1-flash-lite` | Gemini fallback evaluator. |
| `LLM_EVAL_BATCH_SIZE` | `8` | Max parallel evaluation requests. |
| `LLM_EVAL_MAX_JOBS` | `800` | Max jobs sent to LLM per run. |
| `LLM_PROMPT_VERSION` | `5` | Prompt/schema audit version. |

---

## Source notes checked 2026-06-20

- OpenAI model docs list `gpt-5.4-nano` as the cheap GPT-5.4-class model for classification, data extraction, and ranking, with pricing shown as `$0.20 / $1.25` per 1M input/output tokens.
- OpenAI pricing lists `gpt-5.4-mini` at `$0.75 / $4.50` and `gpt-5.4` at `$2.50 / $15.00` per 1M input/output tokens.
- OpenAI's model list marks `gpt-5.5` as latest/flagship and lists `gpt-5.4-mini`, `gpt-5.4-nano`, `text-embedding-3-large`, and `text-embedding-3-small` as current models.
- OpenAI Structured Outputs are designed to make model responses adhere to supplied JSON Schema.
- OpenAI API deprecations are separate from ChatGPT UI retirements; use the API deprecations page for implementation lifecycle decisions.
- Gemini model docs list `gemini-3.1-flash-lite` and `gemini-3.5-flash` as stable models, and `gemini-embedding-2` as the stable Gemini embedding model.
- Gemini structured outputs support JSON Schema/Pydantic-style schemas.
- Anthropic structured-output docs list current Claude models with structured-output support; re-check the latest Claude low-cost model before adding Anthropic as a provider.
- Voyage embeddings docs list `voyage-4-lite`, `voyage-4`, and `voyage-4-large` as current text embedding models with 32K context and configurable dimensions.

Reference URLs:

- https://developers.openai.com/api/docs/models/gpt-5.4-nano
- https://developers.openai.com/api/docs/models
- https://openai.com/api/pricing/
- https://developers.openai.com/api/docs/guides/structured-outputs
- https://developers.openai.com/api/docs/deprecations
- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite
- https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash
- https://ai.google.dev/gemini-api/docs/models/gemini-embedding-2
- https://ai.google.dev/gemini-api/docs/structured-output
- https://ai.google.dev/gemini-api/docs/pricing
- https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- https://docs.voyageai.com/docs/embeddings
