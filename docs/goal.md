# Project Goal

This document is the product north star. Stack, schema, and build phases live in [`tech-stack-plan.md`](tech-stack-plan.md). Hosted LLM and embedding choices live in [`llm-hosted.md`](llm-hosted.md). Implemented behavior is tracked in [`implementation-journal.md`](implementation-journal.md). Source endpoints and poll rules live in [`sources.yaml`](sources.yaml) and [`tyonhaku-rajapinnat.md`](tyonhaku-rajapinnat.md).

## Purpose

Build a beautiful, clean, professional web portal for Finnish open job listings, backed by a reliable and responsible collection system that uses public web sources and APIs that do not require private contracts. The project should turn the current research and endpoint regression tests into an operational product that keeps a normalized job listing dataset up to date, presents it through a polished browser-based experience, and automatically recommends the best matches for one job seeker.

The final product should run on our local server with Docker Compose. The first engineering slice should establish a trustworthy backend foundation: source adapters, incremental collection, deduplication, storage, monitoring, and repeatable quality checks. The MVP still includes a professional local web portal, but the backend should be shaped first so the portal does not require a redesign later.

## Current State

This repository currently contains research, source documentation, and repeatable probes for Finnish job listing sources. The verified source registry is maintained in [`sources.yaml`](sources.yaml), and the detailed research notes live in [`tyonhaku-rajapinnat.md`](tyonhaku-rajapinnat.md).

As of the 2026-06-20 snapshot, the best no-contract sources are:

| Source | Collection method | Role |
|---|---|---|
| Duunitori | Unofficial JSON API | Primary high-volume commercial source with full listing text |
| Työmarkkinatori | Unofficial site search API | Primary official/public-employment source |
| Laura | WordPress REST API | Primary high-volume ATS/job board source |
| Jobly | Sitemap + JSON-LD per listing | Secondary high-volume source |
| EURES | Public JSON API | Supplementary source; not reliable enough for primary incremental ordering |

The repository also has a regression gate in `scrape-test/regression_test.py`, runnable with:

```bash
make test-regression
```

## Target Outcome

The target system should continuously produce a clean, queryable dataset of Finnish open job listings with enough metadata to support search, filtering, freshness checks, source attribution, deletion of expired or removed postings, and personalized recommendations. That dataset should power a web portal that feels fast, trustworthy, and production-grade rather than experimental.

The final portal should provide:

- A clean responsive user interface for desktop and mobile browsers.
- Search and filtering by keyword, location, employer, source, publication date, and freshness.
- A recommendations view that highlights the best matches for the configured job seeker.
- A view for all new job listings, separate from personalized recommendations.
- Clear listing detail pages with source attribution and application links.
- A professional visual design suitable for repeated daily use.
- Admin or operator views for source health, collection runs, duplicate handling, and stale listings.
- Docker Compose deployment for the local server, including the web app, backend/API, database, scheduled collector, and any supporting services.
- Persistent volumes, environment-based configuration, and documented start/stop/update commands.

## Personalized Matching

The portal should include a fully automatic matching pipeline that compares newly collected job listings against one job seeker's CV, profile, location, and stated preferences.

The MVP supports exactly one job seeker and one active set of preferences. Multi-user accounts, team workflows, and separate saved profiles are intentionally out of scope until the single-profile pipeline works well.

The matching pipeline should combine deterministic filtering and LLM-based judgement:

- Deterministic filters should handle hard constraints such as location, remote/hybrid preference, language, role category, required skills, exclusion keywords, publication freshness, and application deadline.
- Machine scoring should rank likely matches using structured fields, extracted skills, title/employer signals, location distance, and preference weights.
- Semantic matching should use stored job and profile embeddings as one ranking signal alongside machine scores, not as the only ranker. See [`tech-stack-plan.md`](tech-stack-plan.md) and [`llm-hosted.md`](llm-hosted.md).
- LLM scoring should review the strongest candidates and produce a match score, concise rationale, possible concerns, and suggested next action.
- The pipeline should be 100% automatic during normal operation: new jobs are collected, normalized, filtered, scored, LLM-reviewed where appropriate, and published into the portal without manual triage.
- LLM output should be stored as structured data so the UI can show recommendation reasons without re-running the model for every page load.
- The system should keep enough intermediate scoring data to debug why a job was recommended, rejected, or ranked lower.

At minimum, each normalized listing should support:

- Stable internal ID
- Source name and source-specific ID or URL
- Title
- Employer
- Description or structured content when available
- Application URL
- Publication, modification, ingestion, and expiration timestamps when available
- Location fields where available
- Occupation/category fields where available
- Language when detectable or provided
- Raw source payload for audit/debugging
- Content hash for change detection
- Active/expired/removed state

The job seeker profile should support:

- CV text or parsed CV sections
- Target roles and preferred titles
- Skills, technologies, industries, and seniority
- Preferred locations and remote/hybrid/on-site preference
- Languages
- Salary or contract preferences when available
- Hard exclusions and negative keywords
- Free-form preference notes for LLM matching

Each recommendation should support:

- Listing ID and profile ID
- Deterministic filter result
- Machine score
- Vector similarity score when embeddings exist
- LLM score when evaluated
- Ranking position
- Match rationale
- Concerns or mismatch reasons
- Recommended action
- Evaluation timestamp and model/version metadata

## MVP Scope

The first implementation milestone should include:

1. Source adapter interface for fetching and normalizing listings.
2. Adapters for Duunitori, Työmarkkinatori, and Laura.
3. Incremental collection strategy per source:
   - Duunitori: walk pages ordered by `date_posted` until the watermark.
   - Työmarkkinatori: use `publishedAfter` with `pageSize=90`.
   - Laura: use `after` for new postings and `modified_after` for edits.
4. Persistent storage for normalized listings and raw source snapshots.
5. Deduplication across sources using URL, employer/title/location/date heuristics, and content hashes.
6. Expiration/removal handling so stale listings do not remain active indefinitely.
7. Regression checks that run locally and in CI before collector changes are accepted.
8. Basic operational logs for source health, counts, failures, and collection duration.
9. One local job seeker profile with CV, location, and preference fields.
10. Automatic deterministic filtering and scoring for the single profile.
11. LLM-based evaluation for the strongest candidate jobs.
12. Portal views for recommendations and all new listings.

## Post-MVP Scope

After the core collection pipeline is stable, add:

- Jobly sitemap and JSON-LD collection.
- EURES enrichment or comparison, without relying on `MOST_RECENT` as the primary incremental strategy.
- Recommendation tuning tools for adjusting weights, prompts, and exclusion rules.
- Feedback controls such as "good match", "not relevant", and "applied" to improve ranking later.
- Richer admin review tooling for source failures, duplicate clusters, and recommendation diagnostics.
- More city, municipality, university, and large-employer sources where collection is technically and legally acceptable.
- Official Työmarkkinatori KIPA P67 integration if credentials and IP allowlisting are obtained.
- Public internet exposure only after source terms, privacy, HTTPS, and operational monitoring have been reviewed.

## Non-Goals

The project should not initially attempt to:

- Circumvent bot protection or access controls.
- Depend on private or undocumented contracts that require credentials unless those credentials are intentionally obtained.
- Scrape sources that explicitly block automated access without a reviewed legal/operational decision.
- Treat the portal UI as ready before the collection pipeline, deduplication, and freshness guarantees are proven.
- Treat LLM output as authoritative without deterministic filters, stored rationale, and debuggable scoring data.
- Add multi-user account management before the single-job-seeker MVP is working end to end.
- Expose the portal publicly before HTTPS, source terms, and privacy handling are explicitly reviewed.
- Treat source snapshot counts as fixed requirements; they are live numbers and will drift.

## Quality Bar

The collector should be considered production-ready only when:

- Each enabled source has a documented adapter strategy.
- Regression checks pass for source availability and expected schema shape.
- Incremental runs are idempotent.
- Duplicate handling is explainable and test-covered.
- Removed or expired listings are deactivated on a predictable schedule.
- Source attribution requirements are preserved in stored data and downstream outputs.
- Failures are visible through logs or metrics rather than silent data loss.
- Recommendation runs are automatic, repeatable, and inspectable.
- LLM calls are bounded, cached/stored, and limited to candidates that pass cheaper deterministic filtering.
- The UI clearly separates personalized recommendations from the full stream of new jobs.
- Recommendation cards explain why a job is shown, what may be a mismatch, and what the next action is.
- Empty, loading, failed-source, and no-match states are handled as first-class UX states.
- The portal can be started, stopped, updated, and backed up predictably through Docker Compose on the local server.
- The web UI is responsive, visually consistent, accessible enough for normal keyboard and screen-reader use, and free of obvious layout defects.

## Responsible Use

Collection must be conservative and source-aware:

- Use clear user-agent headers.
- Respect reasonable polling intervals from [`sources.yaml`](sources.yaml).
- Prefer APIs, sitemaps, and structured data over HTML scraping.
- Keep raw payloads for debugging, but avoid exposing personal contact data unnecessarily.
- Treat the job seeker's CV, profile, preferences, and recommendation history as private data.
- Avoid sending unnecessary personal data to LLM providers; pass only the fields needed for matching.
- Make the LLM provider configurable so local or hosted models can be evaluated.
- Preserve required attribution, especially for Työmarkkinatori data.
- Re-check source terms before public redistribution or commercial use.

## Suggested Implementation Order

1. Define the normalized listing schema and storage choice.
2. Build a small collector runner that can execute one source adapter at a time.
3. Implement Duunitori and Laura first because their APIs are simple and high-volume.
4. Add Työmarkkinatori with its page-size and sorting constraints encoded in tests.
5. Add deduplication and active/expired state transitions.
6. Add the single job seeker profile and preference model.
7. Implement deterministic matching and machine scoring.
8. Add LLM evaluation for the strongest candidates and store structured recommendation output.
9. Promote the current regression tests into CI as a release gate.
10. Add Jobly once the core pipeline handles sitemap-based collection cleanly.
11. Build the web API and search/recommendation endpoints used by the portal.
12. Build the professional web portal UI with recommendations and all-new-jobs views.
13. Package the portal, API, collector, database, matching worker, and supporting services in Docker Compose for the local server.

## Success Metrics

The project is succeeding when:

- A fresh local or scheduled run can collect thousands of active listings without manual intervention.
- Re-running the collector does not create duplicates.
- New, updated, expired, and removed listings can be identified.
- Source drift is caught by regression tests before it corrupts the dataset.
- The normalized dataset is useful enough to power search or analytics without returning to raw source-specific payloads for common fields.
- New listings are automatically filtered, scored, and recommended against the single job seeker profile.
- The portal shows both personalized recommendations and the complete feed of new jobs.
- Recommendation reasons are understandable enough for the job seeker to decide whether to open or apply.
- The Docker Compose stack can run unattended on the local server and survive service restarts without data loss.
- Users can browse and search listings through a clean, professional web portal without needing to know anything about the underlying sources.
