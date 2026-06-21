# System Architecture

**Updated:** 2026-06-21  
**Scope:** current local-server architecture and the target recommendation pipeline for finding direct matches, transferable matches, and hidden opportunities for one job seeker.

This document complements [`goal.md`](goal.md), [`tech-stack-plan.md`](tech-stack-plan.md), [`llm-hosted.md`](llm-hosted.md), [`sources.yaml`](sources.yaml), and [`implementation-journal.md`](implementation-journal.md). The north star remains: collect Finnish open jobs reliably, normalize them into one dataset, and automatically surface the jobs that are genuinely worth the job seeker's attention.

## High-Level Shape

The system is a Docker Compose stack:

| Service | Responsibility |
|---|---|
| `web` | Finnish Next.js portal for job browsing, job detail pages, source health, and recommendations. |
| `api` | FastAPI service exposing jobs, sources, recommendations, feedback, and health endpoints. |
| `worker` | Python background process for scheduled collection, enrichment, matching, and LLM evaluation. |
| `db` | PostgreSQL + pgvector-compatible database for normalized jobs, raw payloads, run logs, profile data, recommendations, and LLM evaluations. |

The default deployment is local and private: web on `127.0.0.1:3080`, API on `127.0.0.1:8008`. The stack stores a private single-job-seeker profile and recommendation history, so public exposure requires a separate HTTPS, authentication, source-terms, and privacy review.

## Data Flow

```text
Sources
  -> source adapters
  -> raw_listings + source_runs + source_run_events
  -> normalization
  -> jobs + job_sources
  -> enrichment: description, location, work mode, deadline
  -> dedupe and active/removed/expired state
  -> deterministic matching
  -> machine scoring and silent-signal boosts
  -> top-candidate LLM evaluation
  -> stored recommendations
  -> Finnish portal UI
```

The portal never calls an LLM on page load. It reads stored recommendation rows with stored rationale, concerns, score, suggested action, and model metadata.

## Collection Layer

Each source is represented by a source adapter with source-specific fetching and normalization logic. The current source strategy is source-aware:

| Source | Role |
|---|---|
| Duunitori | High-volume commercial source with strong full-text listing coverage. |
| Työmarkkinatori / TMT Oulu | Official/public-employment source; detail enrichment is needed for descriptions. |
| Laura | High-volume ATS source with WordPress REST access. |
| Jobly, EURES, Kuntarekry, Kirkkorekry, Oulu Varbi | Supplementary sources integrated through their safest available public mechanisms. |

Broad incremental polling is not sufficient for all high-value matches. A posting can remain open and relevant while being older than the current source watermark. To protect recall, Duunitori, TMT, and Laura also merge a small configured set of targeted discovery searches from `DISCOVERY_SEARCH_QUERIES` into every run. The default terms cover library and information-service roles such as `kirjastonhoitaja`, `kirjastovirkailija`, `kirjasto`, `informaatikko`, and `kirjastopedagogi`, plus selected music/content/culture terms such as `musiikkikirjasto`, `musiikkitapahtumat`, `sisällöntuottaja`, and `kulttuurituottaja`. These searches expand collection recall only; downstream scoring and LLM review still decide whether the jobs are suitable.

Every collection run writes durable run metadata. The important operational records are:

- `source_runs`: started/finished timestamps, status, fetched/inserted/updated/unchanged/failed counts, and error summary.
- `source_run_events`: structured per-run warnings and errors for later analysis.
- `raw_listings`: source payload snapshots and content hashes.
- `job_sources`: source-specific occurrences, attribution, application URL, first/last seen timestamps.
- `jobs`: canonical job records shown by the portal.

This matters because source failures should be visible as data, not only as console logs. Later tuning should be based on actual failed events, missing fields, stale counts, and per-source quality.

## Normalization And Enrichment

The canonical `jobs` table should hold fields that all downstream stages can trust:

- title
- employer
- description
- location
- publication and freshness timestamps
- status: active, expired, removed, or superseded
- application URL through `job_sources`
- source attribution

Enrichment fills gaps left by source APIs. The current examples are TMT detail descriptions and location cleanup. The target architecture should also extract:

- work mode: remote, hybrid, on-site, unknown
- application deadline
- required qualifications and licences
- language requirements
- sector and employer type
- role family and seniority
- schedule and contract type

These fields should be extracted before matching so cheap deterministic logic can reject obvious mismatches and prioritize promising hidden matches.

## Matching Pipeline

The system should not send every collected job to an LLM. The architecture is intentionally staged:

1. **Hard filters:** remove inactive, expired, removed, disabled-source, impossible-location, and clearly excluded roles.
2. **Deterministic feature extraction:** tokenize titles, descriptions, employers, locations, qualifications, language requirements, and work-mode signals.
3. **Machine scoring:** score title, skill, sector, location, work-mode, freshness, and preference matches.
4. **Silent-signal boosts:** add positive weight for patterns found in the job seeker's real application history and other derived profile signals.
5. **Targeted source discovery:** recover source-side keyword matches for high-value role families that would be missed by watermarks alone.
6. **Semantic retrieval:** use embeddings to find jobs whose wording differs from the profile but whose duties are semantically related.
7. **Candidate set building:** merge the strongest deterministic, machine-score, silent-signal, discovery, and semantic candidates.
8. **LLM classification:** send only the top bounded set to the configured LLM provider.
9. **Stored ranking:** store LLM score, fit tier, rationale, concerns, suggested action, prompt version, model metadata, and request hash.

The current implementation follows this core idea: deterministic scoring first, multi-lane candidate ordering next, then bounded LLM evaluation for top active candidates. The multi-lane version keeps separate review capacity for direct title matches, real application-history matches, semantic similarity, transferable-duty matches, sector-context matches, and a small exploration lane.

## What Are Silent Signals?

Silent signals are useful evidence that the job seeker may not explicitly name as a target title, but that the system can derive from trusted data:

- roles she actually chose to apply for
- sectors repeatedly appearing in application letters
- locations she has already considered acceptable
- duties recurring across CV and applications
- transferable patterns such as public-service administration, library pedagogy, events, cultural work, parish work, group guidance, documentation, customer guidance, and small-unit responsibility

These signals are now represented in the profile as `preferences.application_history_signals`: PII-stripped title, keyword, and location boosts derived from real application material. They improve recall and ranking, but they are not hard requirements and do not create missing qualifications.

Examples of silent-signal boosts:

| Signal | Why it matters |
|---|---|
| `palvelusihteeri`, `palveluassistentti`, `hallintosihteeri` | The job seeker has shown real interest in office/admin service roles, even when the title is not library-specific. |
| `museo`, `opastus`, `yleisötyö`, `tapahtumat` | Cultural and audience-facing roles may be suitable hidden matches through library, events, and public-service experience. |
| `seurakunta`, `kunta`, `julkinen palvelu` | Employer/sector fit can matter as much as exact title fit. |
| Oulu, Haukipudas, Rovaniemi, Ranua, Ylivieska, Pelkosenniemi, Kokkola | These locations are proven willingness signals and should rank above unknown edge locations. |

## LLM Role

The LLM is a classifier and reviewer, not the primary crawler, database, or first-pass search engine.

It should answer one question for one candidate job:

> Is this job genuinely applicable for this job seeker, using only the allowed profile facts and the job listing facts?

The LLM should:

- verify whether stated requirements are actually met
- avoid inventing degrees, licences, or experience
- identify transferable fit that keyword matching misses
- classify `strong_fit`, `transferable_weaker`, `generic_customer_service_only`, or `not_applicable`
- return `apply`, `consider`, or `skip`
- write Finnish rationale and concerns for the portal

The LLM should not receive raw application letters, contact details, full private source files, or every job in the database. It receives a minimized allowed profile summary and one job summary for bounded top candidates only.

When an LLM provider is enabled, active portal recommendations should be LLM-reviewed candidates that pass quality thresholds. Deterministic-only recommendations are a fallback for runs where no LLM provider is configured.

## Finding Hidden Opportunities

The system should deliberately look for jobs the job seeker may not search for herself. These "hidden opportunities" are not random weak matches; they are jobs where the duties, context, and requirements fit even if the title is unexpected.

The recommended architecture is a **multi-lane candidate generator**:

| Lane | Purpose | Example |
|---|---|---|
| Direct-title lane | Catch obvious title matches. | kirjastonhoitaja, palvelusihteeri |
| Application-history lane | Boost jobs similar to real past applications. | palveluassistentti, museo-opas |
| Transferable-duty lane | Find jobs by duties rather than title. | neuvonta, tapahtumat, hallinto, ryhmänohjaus |
| Sector lane | Reward familiar public-service contexts. | kunta, seurakunta, museo, oppilaitos |
| Semantic lane | Use embeddings for wording mismatch. | "asiakasohjaus" matching "neuvonta ja palveluohjaus" |
| Exploration lane | Sample a small number of plausible low-score jobs for LLM review. | unusual titles with strong duty overlap |

Each lane should contribute candidates before final ranking. This avoids a common failure mode: a single blended score can bury hidden opportunities because their title match is weak even though the duties are strong.

## More Efficient Architecture For Better Fit

The efficient target design is not "LLM everything." It is "extract better signals cheaply, then spend LLM calls where judgement matters."

Recommended improvements:

1. **Add structured extraction before scoring.** Extract qualifications, work mode, language requirements, deadlines, employer sector, role family, and duty tags from each job. This reduces false positives before LLM review.
2. **Keep improving Finnish-aware text normalization.** The current implementation handles a curated set of common job-listing inflections such as `hallinto` vs. `hallinnon`, `tapahtuma` vs. `tapahtumien`, and `opastus` vs. `opastusta`. This should later expand into a tested synonym/lemmatization layer.
3. **Continue expanding multi-lane candidate selection.** Direct, application-history, semantic similarity, transferable-duty, sector-context, and exploration lanes exist; stronger structured duty extraction should become an additional lane.
4. **Use embeddings as retrieval, not final truth.** Embeddings should recover semantically similar jobs that keywords miss, then deterministic and LLM stages decide whether they are actually suitable.
5. **Add feedback learning.** `good_match`, `not_relevant`, and `applied` feedback should update future ranking features. `not_relevant` should suppress repeated recommendations; `applied` and `good_match` should strengthen similar future patterns.
6. **Escalate only ambiguous high-value cases.** Routine jobs go to the cheap evaluator. Borderline high-potential hidden opportunities can go to a stronger model or second-pass prompt.
7. **Track per-stage rejection reasons.** Store why each candidate was filtered or demoted so tuning can target real failure modes.
8. **Benchmark with known-interest examples.** Use real past applications as positive examples and clearly unsuitable listings as negatives. Measure Recall@30, precision of top recommendations, and hidden-opportunity discovery rate.

## Candidate Selection Policy

A practical candidate policy for each matching run:

1. Score all active jobs deterministically.
2. Keep all jobs above a hard deterministic threshold.
3. Add top N direct-title matches.
4. Add top N application-history matches.
5. Add top N semantic embedding matches.
6. Add top N sector/duty-transfer matches.
7. Add a small exploration sample from plausible-but-not-obvious jobs.
8. Remove hard exclusions and expired jobs.
9. Deduplicate the merged candidate set.
10. Send only the final bounded set to LLM evaluation.

This keeps cost bounded while still letting the system discover jobs outside obvious search terms.

## Privacy Boundary

The private profile material has three layers:

| Layer | Use |
|---|---|
| Raw files | Local-only reference. Never send to hosted LLM providers. |
| Extracted text | Local analysis only unless PII-stripped and explicitly allowed. |
| Minimized profile summary | Allowed input for matching and hosted LLM evaluation. |

The system should prefer derived, minimized, auditable fields over raw text. The profile's `privacy.llm_allowed_fields` and `privacy.llm_forbidden_fields` are the boundary.

## Observability And Review

Every automatic run should leave enough evidence to answer:

- Which sources ran, when, and with what counts?
- Which errors happened, with enough detail to improve harvesters later?
- Which jobs were inserted, updated, unchanged, expired, or removed?
- Which fields were missing after enrichment?
- Which jobs passed deterministic scoring?
- Which jobs were sent to the LLM?
- Which model and prompt version produced each recommendation?
- Why was a recommendation shown, skipped, hidden, or re-ranked?

The architecture should treat logs, run events, and stored intermediate scoring data as product features. Recommendation quality cannot be improved if failures and rejection reasons are invisible.

## Current Gaps

The current system is aligned with the staged architecture, but these gaps remain:

- Direct, application-history, semantic similarity, transferable-duty, sector-context, and exploration candidate lanes are implemented before LLM review.
- Embeddings are stored in PostgreSQL/pgvector and used as retrieval, not final truth. The profile embedding uses positive matching signals rather than raw profile files or exclusion text, so semantic search looks for attractive opportunities instead of jobs similar to disqualifying terms.
- Finnish inflection handling exists for common job-listing terms, but synonym and broader lemmatization coverage is still limited.
- Work-mode extraction should become a first-class field so remote/hybrid/on-site fit is reliable.
- Feedback is stored and can hide irrelevant recommendations, but it does not yet train future ranking weights.
- Candidate generation should add richer structured duty extraction lanes.
- Recommendation quality should be measured with a small labelled benchmark built from real known-interest applications and unsuitable negatives.

## Target End State

The desired system should be able to say:

1. "This job is an obvious fit."
2. "This job is a transferable fit even though the title is not obvious."
3. "This job looked promising by keywords but fails because of a missing requirement."
4. "This hidden opportunity is worth opening because the duties match prior applications and transferable experience."
5. "This source or harvester is degrading because location, description, or detail extraction is missing."

That is the architecture that can find not only jobs the job seeker already knows to search for, but also credible hidden opportunities she might otherwise miss.
