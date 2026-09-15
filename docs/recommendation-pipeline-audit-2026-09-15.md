# Recommendation pipeline audit — 2026-09-15

Audit of the personalised job-recommendation pipeline for the single production
profile (`profile/inkeri/`): profile intake → collection → deterministic
matching → travel/scope policy → LLM review → ranking → API → web UI, plus the
data layer and scheduling that carry it.

**Read-only audit.** No source file was changed while gathering findings. The
production actions listed in §5 were taken as part of the requested fix, not as
part of the audit.

## 1. Method and evidence base

Four independent audits were run in parallel, each required to cite exact
`file:line` plus a quoted snippet or measured production value:

| Area | Main artefacts |
|---|---|
| Deterministic matching, scoring, ranking | `backend/app/matching.py`, `travel_policy.py`, `location*.py`, `embeddings.py`, `feedback_*.py` |
| LLM integration | `backend/app/llm.py`, LLM stage of `matching.py`, `feedback_analysis.py`, `config.py`, `docs/llm-hosted.md` |
| Data layer, collection, scheduling | `scheduler.py`, `worker.py`, `collection/`, `adapters/`, `alembic/`, `compose.yaml`, `Makefile` |
| Profile contract vs implementation | `profile/inkeri/*`, `main.py`, `web/app/*`, `docs/goal.md`, `docs/architecture.md`, `docs/tech-stack-plan.md` |

Production evidence was collected read-only from the Pi stack
(`job-search-agent-{api,worker,db,web}-1`): row counts, `pg_indexes`,
`EXPLAIN (ANALYZE, BUFFERS)` on the hot queries, `pg_stat_user_tables`, and
`llm_evaluations` contents. Statements that were not independently verified
during this audit are marked **UNVERIFIED**.

Production facts at the start of the audit:

- 100 668 active jobs, 26 superseded, **0 removed, 0 expired**, `expires_at` never set.
- 108 571 `job_sources` rows; 8 137 `llm_evaluations` all stamped `prompt_version = 7`.
- 578 active recommendations; **413 of them (71 %) were older than the profile's 30-day freshness limit**.
- One feedback row in the whole system (`analysis_status = 'failed'`), 0 completed analyses.
- Daily pipeline completed every day since 2026-07-09; source collection succeeded 9/9 on the audit day.

## 2. Executive summary

The pipeline runs reliably and the scoring design is largely implemented as
documented. The defects cluster into four systemic problems:

1. **The profile contract is only partly enforced.** `profile.yaml` is presented
   as the machine-readable contract, but `privacy`, `freshness`, `languages`,
   `location.geo_radius_km`, `role_clusters[].weight`, `skills` and
   `exclusions.qualification_checks` are never read by the deterministic
   pipeline. The result is a pipeline that scores text overlap well but does
   not actually apply the rules the profile states (A1, B1, C1–C5).
2. **The catalogue has no lifecycle.** Nothing is ever removed or expired, and
   the freshness rule is not enforced, so stale postings stay recommendable
   indefinitely (B1–B3).
3. **The review stage is capped below the candidate pool.** With prompt-version
   gating, the daily LLM cap (800) is smaller than the deterministic pass set
   (2 134 on 2026-09-15), so a prompt-version change hides previously approved
   recommendations for days (D5). The same cap is also what limits recall.
4. **Hot paths were written for a small dataset.** The candidate pools are
   regex scans over `title || description` of all 100 k jobs; one matching run
   spends ≈13.6 minutes in table scans, and listing upserts do a 22 ms scan per
   listing (E1–E4).

Privacy is the single most urgent item: the profile's own
`privacy.llm_forbidden_fields` is not honoured, and a home postal code plus the
configured home street address currently leave the host on every LLM call (A1).

## 3. Profile intake: the three new applications (September 2026)

### 3.1 What was added

Three applications were found in the Windows Downloads folder and copied into
the profile in the existing `raw/` + `extracted/` convention (originals in
`raw/`, `pdftotext -layout` text in `extracted/`):

| Date | Position | Employer | Files added |
|---|---|---|---|
| 2026-09-01 | Tietoasiantuntija (ilmoitus 2026/503) | Oulun yliopiston kirjasto | `raw/Inkeri_Ponsimaa_Hakemuskirje_Tietoasiantuntija.pdf`, `extracted/…Tietoasiantuntija.txt` |
| 2026-09-10 | Kirjastonhoitaja (KOK-07-9-26) | Kokkolan kaupunginkirjasto | `raw/Inkeri_Ponsimaa_Hakemus_ja_CV_Kirjastonhoitaja_Kokkola.pdf`, `raw/Kirjastonhoitajan tehtävä … Kuntarekry hakijaportaali.pdf`, matching `extracted/*.txt` |
| 2026-09-10 | Kirjastovirkailija, pääkirjaston musiikkiosasto (KOK-07-10-26) | Kokkolan kaupunginkirjasto | `raw/Inkeri_Ponsimaa_Hakemus_ja_CV_Kirjastovirkailija_Musiikkiosasto_Kokkola.pdf`, matching `extracted/*.txt` |

`profile.yaml` was extended in `preferences.application_history_signals`:

- `boost_titles_fi += kirjastovirkailija, tietoasiantuntija`
- `boost_keywords_fi += musiikkikirjasto, musiikkiosasto, kokoelmatyö, aineistohankinta, kuvailu, luettelointi, satutuokio`
- `boost_locations` unchanged (Kokkola was already there; Oulu is the home city)

`analysis.md` and `provenance.source_files` were updated accordingly. The
children/youth focus of the Kokkola kirjastonhoitaja post was deliberately **not**
added as a generic `lapset`/`nuoret` boost, because that would also lift
non-library early-childhood roles which the qualification checks reject anyway.

### 3.2 Measured search-term effect (whole active catalogue, 100 668 jobs)

| New term | Jobs matching | Note |
|---|---|---|
| `kirjastovirkailija` (title) | 36 | already in the `library_culture` cluster; now also an application-history title boost (+18 pts) |
| `tietoasiantuntija` (title) | 14 | same — cluster title, now also an application-history boost |
| `kokoelmatyö` | 39 | new keyword |
| `luettelointi` | 20 | new keyword |
| `kuvailu` | 14 | new keyword |
| `musiikkikirjasto` / `musiikkiosasto` | 6 | new keyword |
| `satutuokio` | 5 | new keyword (already in two role clusters) |
| `aineistohankinta` | 0 | no current listing carries this exact token |
| — reference: `kirjasto` | 572 | scale of the library domain in the catalogue |

Because application-history titles are also fed into `discovery_pool_terms`
(`matching.py:629-652`), the two new titles additionally become discovery search
terms across title+description. The three applications therefore widen the
search a little, but their bigger effect is re-ranking: jobs that already
scored 15 points as cluster title matches now score up to 18 points more as
application-history matches.

### 3.3 Deterministic pool probe

A read-only probe (`/tmp/scope_probe.py`, run inside the api container) replays
`discovery_pool_terms` → `fetch_active_job_rows` → `score_job` →
`rank_scored_candidates_for_review` twice against the production database: once
with the pre-2026-09 profile and once with the updated profile. Both runs use
the same settings, the same connection and the same candidate limits, so the
profile is the only variable.

Result (probe completed after the first draft; both runs over the same 5 184-candidate
window against the production database):

| Metric | Old profile | Updated profile |
|---|---|---|
| Discovery terms | 80 | 80 (**cap reached — no new term added**) |
| Candidates fetched | 5 184 | 5 184 |
| Deterministic passes | 2 129 | 2 129 (identical set) |
| `application_history` lane | 1 317 | **1 320 (+3)** |
| `direct_title` / `transferable_duty` / `sector_context` / `exploration` lanes | 623 / 806 / 293 / 987 | unchanged |
| Jobs whose score changed | — | **14**, all positive |
| Top-40 review order | — | unchanged |

The 14 score changes are exactly the intended signals:

| Δ | Score | Job | Location |
|---|---|---|---|
| +18 | 34 → 52 | Kirjastovirkailija | Sipoo |
| +18 | 34 → 52 | Kirjastovirkailija | Suomi |
| +18 | 49 → 67 | Kirjastovirkailija | Lohja |
| +10 | 72 → 82 | Kirjastonhoitaja | Pori |
| +5 ×11 | — | Kirjastonhoitaja (Konnevesi, Hirvensalmi, Mikkeli), Informaatikko (Kajaani, Helsinki ×2, pääkaupunkiseutu), Kirjastonjohtaja (Vieremä, Suomi), Eläintieteen alan museomestari (Helsinki) | — |

**Answer to "do these applications change the scope?" — yes, but only marginally,
and the discovery stage does not see them at all.** The three applications add
three `kirjastovirkailija` jobs to the application-history lane and lift 14 jobs
by 5–18 points; they change no pass/fail decision and do not reorder the top 40.
The reason the effect is small is finding **C7**: `discovery_pool_terms` returns
`terms[:80]` (`matching.py:652`) and the list is already full from the configured
queries plus cluster titles, so the two new application titles were silently
truncated instead of becoming discovery search terms (probe: 80 terms before and
after; `discovery_terms_added: []`).

### 3.4 Scope consequence observed in production

Independent of the profile edit, the prompt-version refresh described in §5
changed production materially on 2026-09-15:

| Metric | Before refresh | After refresh |
|---|---|---|
| Active recommendations | 578 | 71 |
| Default "Enintään 2 h" scope | 87 | **0** |
| "2 h + etä" scope | 168 | 55 |
| "Koko maa" scope | 578 | 71 |
| LLM evaluations at current prompt version | 0 | 800 (cap reached) |

The v8 evaluations recorded on 2026-09-15: **710 skip (90 %), 68 consider, 8
apply**. Of the 240 Oulu-located jobs reviewed, **all 240 were skipped**
(highest skipped score 35; activation requires score ≥ 40 and a non-skip
action). The highest-scoring skipped Oulu jobs are sales, kitchen, hotel and
engineering roles — plausible rejects — but the practical outcome is that the
default commutable scope is currently empty.

Two separate causes must be distinguished:

1. **Transition artefact (fixable):** strong recommendations whose linked
   evaluation is still stamped `prompt_version = 7` are deactivated by
   `refresh_active_recommendation_ranks(..., require_llm_review=True,
   prompt_version=8)` (`matching.py:1651-1658`) even though they were never
   re-reviewed. Verified example: job 79610 "Kirjasto- ja kulttuurijohtaja",
   v7 evaluation 92 / `strong_fit` / `apply`, `is_active = false` because only
   800 of 2 134 deterministic passes could be re-evaluated in the run. This
   self-heals over ≈3 nightly runs at the current cap, or immediately with a
   one-off backfill.
2. **Calibration question (needs a product decision):** even after a full
   re-review, a 90 % skip rate and 0/240 accepts for Oulu means the local
   commutable scope stays empty. Either the local candidate pool is genuinely
   weak for this profile, or the v8 prompt's local-work rule
   (`llm.py:74-77`) is too blunt. This should be decided with a labelled sample,
   not by guessing.

## 4. Findings

Severity: **CRITICAL** = data leaves the host or the documented contract is
silently void; **HIGH** = wrong or lost data, materially wrong ranking, or a
user-visible break; **MEDIUM** = cost/robustness; **LOW** = hygiene.

### A. Privacy and data governance

**A1 — CRITICAL — The profile's privacy contract is never read; forbidden PII reaches the LLM.**
`profile.yaml:15-34` defines `llm_allowed_fields` / `llm_forbidden_fields`
(including `home_postal`, `address`). `grep -rn 'llm_forbidden_fields\|llm_allowed_fields' backend/app` → **0 hits**.
`minimized_profile_summary` (`llm.py:124-138`) whitelists the *whole* `location`
dict, and `embeddings.py:45` embeds `profile.get("location")` verbatim.
Verified in production: the stored profile contains `location.home_postal`, and
the same summary builder is used by `feedback_analysis.py:605`.
Additionally `travel_policy.to_audit_dict` (`travel_policy.py:70`) carries
`origin_address`, which `job_summary` (`llm.py:154`) embeds into every job
prompt, and `main.py:241,980` return `travel_origin_address` in the API — so the
configured home street address reaches both the provider and the browser.
*Why it matters:* the file that defines the boundary is documentation only; the
code has an independent hard-coded list. Every one of the ~800 daily calls
leaked a postal code. *Fix:* plan item P1-1.

### B. Freshness and catalogue lifecycle

**B1 — CRITICAL — The freshness contract is not enforced.**
`profile.yaml:491-493` states `max_age_days: 30` and `drop_past_deadline: true`;
`grep -w 'max_age_days\|drop_past_deadline' backend/app` → 0 hits.
`matching.py:664-787` filters only `status='active'` + enabled source, and
`passes` (`matching.py:340-344`) never consults age.
Measured before the 2026-09-15 refresh: **413 of 578 active recommendations
(71 %) were older than 30 days**. *Why it matters:* the user is shown expired
postings; the profile states an explicit rule the system ignores.

**B2 — HIGH — Removal and expiry are dead code.**
`should_mark_missing_source_listings_removed` (`collection/runner.py:134-137`)
returns `False` whenever a watermark exists, and the watermark is set after the
first successful run of every source (`runner.py:687-692`). `expires_at` has no
reader or writer anywhere in `backend/app`; `status='expired'` is never written.
Measured: `active=100668, superseded=26, removed=0, expired=0,
expires_at not null=0`. *Why it matters:* the catalogue only grows; stale jobs
remain candidate material forever; `docs/goal.md` explicitly requires
deactivation on a predictable schedule.

**B3 — HIGH — Jobly silently loses ~530 listings per day.**
`adapters/jobly.py:33-34` drops candidates with `lastmod <= watermark`,
`jobly.py:168` crawls at most `jobly_max_urls_per_run = 100` (`config.py:89`),
then the watermark advances to the newest crawled `lastmod`
(`jobly.py:180-187`, `runner.py:687-692`). Measured: `stopped_at_watermark=true`
on every daily run since at least 2026-09-10; the live sitemap had **636 URLs
newer than the stored watermark** versus a 100 cap; the DB holds 5 735 Jobly
occurrences. *Why it matters:* permanent, unrecoverable data loss, invisible to
the operator because the flag is never surfaced.

**B4 — MEDIUM — Sources that return zero rows are recorded as success.**
`adapters/talentech_regional.py:76-87` logs and `continue`s on persistent 429 or
a non-list payload, returning an empty result; `runner.py:694-714` then writes
`status='success'`. Measured: `kirkkorekry` and `oulu_varbi` return 0 rows with
status `success` and no warning event since 2026-09-11; `eures_fi` failures have
empty `error_summary`. *Why it matters:* source breakage is indistinguishable
from "nothing new".

### C. Scope and filtering contract

**C1 — HIGH — Location is a score adjustment, not the documented hard filter; the 200 km rule is dead.**
`profile.yaml:70-75` declares `geo_radius_km: 200` "authoritative",
`commute_radius_km: 200` and `work_mode_policy`; none of these keys is read by
`backend/app`. The runtime rule is `settings.recommendation_commute_limit_minutes`
(default **120** minutes, `config.py:100`) against `TRANSIT_ORIGIN_ADDRESS`.
Over-limit only adjusts the score (`travel_policy.py:382-406`) and `passes`
never checks it, although `analysis.md:36` and `architecture.md:92` describe a
hard filter. *Why it matters:* documented scope ≠ real scope; two of the three
location rules in the profile do nothing.

**C2 — HIGH — "etätyömahdollisuus" is promoted to verified full-time remote.**
`location.py:118` maps the phrase to work mode `Etä`, `location.py:138` appends
`"/ Etä"`, and `travel_policy.py:115` returns `full_remote` on that synthesized
suffix before the weak-marker guard (`:108`) can see the original text.
Measured: **25 active recommendations** were `full_remote` purely from
`etätyömahdollisuus`-style text; `docs/recommendation-distance-scope-plan.md:65`
explicitly excludes that case. The existing test covers only the raw-location
path, not this enrichment path.

**C3 — HIGH — The language hard filter is not implemented.**
`profile.yaml:141-172` defines per-language `hard_filter` rules; the
deterministic pipeline never reads `languages` (only the LLM prompt and the
embedding text do). `analysis.md:37` claims rejection above the documented
level. Jobs requiring German or Ukrainian at a high level pass the gate.

**C4 — MEDIUM-HIGH — Qualification checks are partly ignored and partly invented.**
`profile.yaml:469-485` (`qualification_checks`) and `:464-468`
(`available_qualifications`) are never read; `matching.py:110-130` hard-codes
only teacher and C/CE-licence patterns and adds an undocumented
`henkilökohtainen avustaja` rejection. `reject_if_required_qualification_missing`
is converted to *caution* terms only (`matching.py:248-255`), so
`sosiaalityöntekijän kelpoisuus` and Valvira licensing do not reject.
`exclusion_terms` reads `profile["hard_exclusions"]` / `profile["negative_keywords"]`
(`matching.py:241-244`) — keys that do not exist in the profile.

**C5 — HIGH — `role_clusters[].weight`/`eligibility` and `skills` do not affect machine scoring.**
`matching.py:194-199` flattens every cluster's titles and keywords into one set
and `:312-319` awards flat 15/5 points per match; `weight` and `eligibility` are
never read, and `skills` reaches only the LLM prompt (`llm.py:129`), not the
embeddings (`embeddings.py:43-50`). `analysis.md:40` describes the opposite
("weighted score from title/keyword overlap per cluster + transferable-skill
match") — the documentation overstates the implementation.

**C6 — MEDIUM — Learned discovery queries never reach the matcher.**
`discovery_pool_terms` (`matching.py:629-652`) uses configured queries, cluster
titles, application-history titles and learned *boost titles* — but never
`profile.learned.discovery_queries` (`feedback_learning.py:510`). The benchmark
counts `learned_discovery` lanes that no code assigns
(`feedback_benchmark.py:50-51,74-76`), so `learned_discovery_contribution` is
structurally always 0.

**C7 — MEDIUM — The discovery term list is silently truncated at 80 terms, so new application signals never become search terms.**
`discovery_pool_terms` ends with `return terms[:80]` (`matching.py:652`) and logs
nothing when it truncates. Measured with the pool probe (§3.3): 80 terms before
and after adding the two new application titles, `discovery_terms_added: []` —
the configured 19 queries plus the flattened `role_clusters[].titles_fi` already
fill the budget, so `kirjastovirkailija` and `tietoasiantuntija` were dropped
from discovery even though they are the freshest evidence of what the user
actually applied for. *Why it matters:* the profile's strongest signal (real
applications) is the first thing silently discarded, and the truncation is
invisible in logs and in the UI.

### D. Recommendation lifecycle and ranking

**D1 — HIGH — The rank refresh resurrects rows the run did not score.**
`main.py:434` calls `refresh_active_recommendation_ranks(connection,
profile_id=...)` with the defaults `require_llm_review=False,
prompt_version=None` (`matching.py:1410-1415`). Its second UPDATE
(`matching.py:1457-1492`) re-activates every profile row that is not weak/skip,
**without checking that the row was scored in this run and without joining
`jobs`** for `status='active'`. Measured at audit time: **135 active rows with
`travel_status IS NULL`** (written 2026-06-21…07-08, before the travel-aware
upsert existed) and **2 active rows whose job is `superseded`**.
`feedback-learning-plan.md:36` documents a nightly full reset that never happens
because `run_matching` passes `deactivate_existing=provider is None`
(`matching.py:1630-1631`).

**D2 — HIGH — Rating 2 hides a recommendation, contradicting the specification.**
`main.py:394` `if rating <= 2: is_active = false`; the same `rating <= 2` guard
appears at `matching.py:995,1008,1018,1028,1047,1179,1446,1482`.
`docs/feedback-learning-plan.md:395,766` and `implementation-journal.md:45` all
state that only rating 1 hides and rating 2 is a soft down-rank.
`tests/test_jobs_api.py:466` encodes the divergent behaviour, so the test suite
currently protects the bug rather than the spec.

**D3 — MEDIUM — Feedback analysis is terminally dead.**
`feedback_analysis.py:646-649` marks any exception as `failed` with no retry,
and `:717-721` marks everything `skipped` while the provider is cooling down.
`pending_feedback_rows` selects only `analysis_status = 'pending'` (`:694`) and
no requeue path exists. Production: one feedback row, `failed` since 2026-07-08,
`feedback_llm_analyses` count **0**; `collect_eval_hints`
(`feedback_learning.py:310-312`) requires `completed`, so `eval_hints` can never
populate. The documented async feedback loop has never produced output.

**D4 — MEDIUM — Provider cooldown is treated as "LLM not configured".**
`build_evaluation_provider` returns `None` while in cooldown (`llm.py:393-396`),
and `run_matching` then activates all deterministic candidates with
`require_llm_review=False` (`matching.py:1630-1641`). With the 360-minute default
cooldown (`config.py:21`) this publishes un-reviewed jobs for up to six hours —
the opposite of `llm-hosted.md:121` ("persist skipped/error states instead of
silently dropping candidates").

**D5 — MEDIUM/HIGH — The review cap is below the candidate pool, so a prompt-version change hides approved rows.**
`llm_review_bucket_limits(800)` = 320/160/320 (`matching.py:1093-1100`), while
the 2026-09-15 run had 5 184 candidates and 2 134 deterministic passes. Because
`refresh_active_recommendation_ranks(require_llm_review=True,
prompt_version=<current>)` deactivates rows whose linked evaluation is not at the
current prompt version, changing the prompt version hides strong
recommendations until their turn comes. Evidence in §3.4 (578 → 71 active; job
79610 at 92/`strong_fit`/`apply` hidden with an un-refreshed v7 evaluation).
*Why it matters:* the mechanism is correct in intent but has no bulk-backfill
path, so correctness fixes degrade the product for days.

### E. Performance and the data layer

**E1 — HIGH — One matching run spends ≈13.6 minutes in regex table scans.**
Candidate pools are regex matches over `title || description` of all jobs:
remote pool `matching.py:729-741` (8 patterns, measured **18.4 s / 598 k buffer
hits**), local pool `matching.py:686-708` (up to 52 location patterns, **162.0 s**),
discovery pool `matching.py:755-779` (up to 80 terms over title+description,
limit 3 000, **633.4 s** with the configured terms; whole
`fetch_active_job_rows` exceeded 600 s in one measurement). Only `jobs_pkey` and
`ix_jobs_location` exist; `pg_trgm` is not installed. Four such pool queries run
per matching run.

**E2 — HIGH — `job_sources` has no index or uniqueness on `(source_id, external_id)`.**
`runner.py:233-242` (and the UPDATEs at `:387-401`, `:471-491`, `:530-577`)
assume at most one row per `(source_id, external_id)`, and every listing upsert
runs that SELECT. Measured: `Seq Scan on job_sources … Rows Removed by Filter:
108571 … Execution Time: 21.977 ms`. Migration `20260620_0001` creates the
partial unique index only for `raw_listings`. No duplicates exist today, so the
unique index is safe to add. The FKs `job_sources(source_id)`,
`(raw_listing_id)` are also unindexed.

**E3 — HIGH — Cross-source dedupe full-scans `jobs` per listing.**
`runner.py:151-166` normalizes with three `regexp_replace` calls per candidate
row: measured **51.9 ms**, parallel seq scan with 33 565 rows removed per worker.
Combined with E2 that is ~74 ms of scanning per listing on a Raspberry Pi, and
it grows with the catalogue.

**E4 — MEDIUM — No index supports the portal list or the matcher's recency scan.**
`main.py:694` (and `:803`, `:999`, `matching.py:664-677`, `:765-779`) order by
`published_at desc nulls last, id desc` on `status='active'`. Measured: `/jobs`
page 1 = **403–440 ms** with `Sort Method: external merge Disk: 4992kB`;
matcher base query 150 ms, run four times per pipeline.

**E5 — MEDIUM — `enrich_job_locations` re-parses every active job's raw payload daily.**
`enrich_locations.py:67-98` joins all active jobs to `raw_listings.payload`
(hundreds of MB of JSONB) inside one transaction with no change detection, to
update typically a handful of rows; per-row errors are swallowed (`:96-97`).

**E6 — MEDIUM — No retention or housekeeping anywhere.**
Measured table sizes: `raw_listings` 108 571 rows / 568 MB (full JSONB payload
kept forever), `job_embeddings` 84 230 / 694 MB, `jobs` 100 694 / 229 MB,
`source_runs` 20 514 rows, `source_run_events` 41 348 rows (last autoanalyze
NULL; `n_live_tup` 414 vs actual 20 514).

**E7 — MEDIUM — A new SQLAlchemy `Engine` (and pool) per call.**
`db.py:9-10` `def get_engine(): return create_engine(...)` with no caching and
no dispose; also called in `scheduler.py:77,96,122,146,183` and once per
`source_run_events` write (`events.py:96,110-140`). Measured 30/100 connections
open at idle.

**E8 — LOW — Repeated per-job work in scoring.**
`score_job` recomputes ~12 profile-derived term sets for every job
(`matching.py:270-281`, called at `:834`), tokenizes the same joined text twice
(`:284-285`), the LLM stage issues one cache SELECT per candidate
(`matching.py:1295-1304`, up to 800/run), the recommendation upsert repeats four
identical feedback `exists` subqueries per row (`:995-1032`), and
`list_recommendations` runs three scans per request (`main.py:180-215`).

### F. LLM evaluation stage

**F1 — HIGH — Prompt text is not part of the cache key.**
`evaluation_request_hash` (`llm.py:159-176`) hashes profile, job summary, model,
prompt_version, schema_version and learned_version — never the instruction text.
`EVALUATION_INSTRUCTIONS` was edited in `e6c1ea7` without touching
`llm_prompt_version`, so every post-July job re-used an evaluation produced by
the older instructions and stamped with the current version. This is the root
cause of the production staleness fixed by the env sync in §5; the code defect
remains.

**F2 — HIGH — `learned.version` invalidates the whole cache every day.**
`feedback_learning.py:501` increments the version unconditionally on every
learner run (one per day: 67/68/69 on 2026-09-13/14/15), and the version is a
hash input (`matching.py:1134,1293`) while `review_priority` only inspects
`prompt_version` (`matching.py:1165-1169`). Measured: production
`learned_version = 69` with `few_shot_examples` and `eval_hints` both empty.
Direct evidence of the waste: job 9957 has **13 evaluations under
`prompt_version = 7`** (2026-06-25 … 07-29) with scores 55–78 and the tier
flipping between `strong_fit` and `transferable_weaker` — the same job, the same
prompt, paid for repeatedly, with unstable output. At the new 800 cap this is up
to 800 avoidable provider calls per day.

**F3 — MEDIUM — `LLM_EVAL_BATCH_SIZE` is dead configuration.**
`grep -rn llm_eval_batch_size backend/app` → only `config.py:19,205`. The
evaluation loop is strictly sequential, one blocking call per job
(`matching.py:1270`). `docs/llm-hosted.md:116-118,272` require bounded
parallelism (5–10) and the production `.env` sets 8.

**F4 — MEDIUM — `OPENAI_EVAL_MODEL_ESCALATED` is never used.**
Only `config.py:15,195-198` reference it; `configured_eval_model` always returns
the non-escalated model (`llm.py:386-390`). Production `returned_model` is a
single value for all 8 300+ rows. The escalation triggers documented in
`llm-hosted.md:107,201-212` are unimplemented.

**F5 — MEDIUM — Token usage and latency are computed and thrown away.**
`metadata["usage"]` is built (`llm.py:286-290`, `feedback_analysis.py:375-379`)
but the INSERT (`matching.py:1319-1354`) stores no usage/latency column, and the
stored `response` contains only score/rationale/action/tier/concerns. Spend and
model drift are therefore unmeasurable, which also blocks quantifying F2.

**F6 — MEDIUM — No retry on schema/JSON failure, no startup model validation.**
Only `insufficient_quota` is special-cased (`llm.py:277-281`); parse/validation
failures (`:282-285`) fall through to `failed += 1` (`matching.py:1393-1399`).
`build_evaluation_provider` checks only that a key exists (`llm.py:397-404`),
although `llm-hosted.md:63` requires fail-fast validation of model and key.

**F7 — LOW-MEDIUM — Cache keys omit identity fields.**
`analysis_request_hash` omits model and provider (`feedback_analysis.py:203-224`)
and `reuse_cached_analysis` ignores `configured_model` (`:571-591`); the job hash
omits provider and `returned_model` (`llm.py:168-175`), so an alias re-point by
the provider silently re-uses old-model output.

**F8 — LOW-MEDIUM — One person's CV facts are hard-coded in the shared system prompt.**
`llm.py:56-59` contains profile-specific rules (library qualification 60 op/35 ov,
Swedish B2) that belong in `profile.yaml:llm_guidance` (`llm-hosted.md:164-172`),
and the tests cement them (`test_llm.py:157-158`).

**F9 — LOW — `schema_version` is a constant and a cached-row validation failure is a dead end.**
The hash default and the INSERT both use 1 (`llm.py:165-166`,
`matching.py:1337`); a cached row that fails re-validation
(`matching.py:1356-1358`) is counted as failed without re-calling the provider,
so a future schema change would strand every cached row.

**F10 — LOW — Large prompt payload with no prompt-caching layout.**
`description[:4000]` plus the `travel_assessment` are re-sent per job
(`llm.py:144-155`); the whitelisted profile summary is ~21.5 k characters
(role_clusters 6 063, llm_guidance 3 992, skills 2 967 …), implying ~7–9 k input
tokens per call. 15 354 of 100 668 active jobs exceed the 4 000-character
excerpt, so the cap does bite. **UNVERIFIED** exact token counts (F5).

### G. API and web UI

**G1 — MEDIUM — Timestamps are rendered in the server timezone (UTC).**
`web/app/page.tsx:215-219,226-231` format with `Intl.DateTimeFormat` and no
`timeZone`; no `TZ` is set in `compose.yaml` or the web image, while the
scheduler runs `Europe/Helsinki` (`scheduler.py:254`). Verified in the web
container: `2026-01-15T22:30:00Z` renders as "15.1. klo 22.30" instead of
"16.1. klo 00.30". In production this shows the 16:00 Helsinki collection as
"klo 13.00", which is exactly the kind of mismatch that makes the feed look
stale.

**G2 — MEDIUM — Stored recommendation fields are dropped and `fit_tier` is never shown.**
`main.py:218-247` omits `vector_score`, `created_at`, `llm_evaluation_id` and
`travel_routing_profile`; the list SELECT omits `r.vector_score` (`:963-988`);
`recommendation_item_from_row` discards `deterministic_result` (`:527-558`).
`page.tsx:62` declares `fit_tier` but never renders it, and cards omit
`published_at` although `tech-stack-plan.md:486` requires source freshness.
`goal.md:90-101` requires the vector score and evaluation metadata in the UI.

**G3 — MEDIUM — Total count and shown items disagree.**
`page.tsx:98` fixes the recommendation limit at 30 with no paging
(`:294-296`), while the header prints `recommendations.total` (`:350-356`): the
scope button can read "578" above 30 cards.

**G4 — LOW — Hard-coded origin label.**
`web/app/tyopaikat/[id]/page.tsx:137` prints "Matka Oulusta" although the origin
is configurable (`config.py:99`) and returned per row (`main.py:241`).

**G5 — LOW — Documented endpoints do not exist.**
`tech-stack-plan.md:204-207` documents `GET /recommendations/{id}`,
`GET/PUT /profile` and `POST /pipeline/run`; `main.py` has nine routes and none
of them.

### H. Scheduling and operations

**H1 — MEDIUM — A day can be skipped silently.**
`scheduler.py:208-216`: if any `source_runs` row is still `running` at 16:45,
the whole daily pipeline returns — no `pipeline_runs` row, no retry, no alert.
Jobly runs have taken 228 s (and 2 700 s historically) against the 45-minute gap.

**H2 — MEDIUM — `/health` carries no operational signal and nothing alerts.**
`main.py:625-637` returns static settings with `status: "ok"`; the real audit is
only reachable via `make audit-db`; no metrics/exporter/notification code exists
(`grep -i 'prometheus|sentry|slack|alert'` → 0). `/sources/status` shows last-run
counts but never `stopped_at_watermark` or "0 rows again".

**H3 — LOW — Stale-run reconciliation races.**
`scheduler.py:120-141` marks runs older than 30 minutes as `failed`, while
`runner.py:694-714` later writes `success` for the same id, so a stuck-then-
finished run reports success and a genuinely long run is declared abandoned
while still running.

**H4 — LOW — Backup/restore targets point at the wrong database.**
`Makefile:52,57` use `-U jobsearchagent -d jobsearchagent`, while the deployed
`.env` uses `POSTGRES_USER=jobportal` / `POSTGRES_DB=jobportal`
(`compose.yaml:48-50` still has the old defaults). `make backup-db` and
`make restore-db` cannot work against production, and the deployment runbook
tells the operator to run them.

**H5 — LOW — Watermark timezone assumptions.**
`adapters/tmt.py:139` appends `Z` without converting to UTC (correct only because
the DB session timezone is `Etc/UTC`); `adapters/laura.py:98` sends a naive UTC
string to WordPress `after`/`modified_after`, which WordPress interprets in site
time — up to 3 h of over-fetch.

### I. Documentation drift

**I1 — `profile/inkeri/README.md:16` states that `profile.yaml` and
`inkeri-cv.normalized.json` "ARE tracked", but `.gitignore:13-18` ignores
`profile/**` (only `profile/README.md` is tracked).** The single most important
profile artefact is therefore unversioned, which is also why the production
`.env`/profile drift in §5 could persist unnoticed.

**I2 — `analysis.md` claims behaviour the code does not implement**: weighted
per-cluster scoring (C5), a language hard filter (C3), a hard location filter
(C1). The application-letter count was corrected to eleven during this session.

**I3 — `README.md:16` claims profile embeddings use the CV JSON and extracts;
`embeddings.py:43-50` embeds only `profile.yaml`.** `embeddings.py:44` reads
`profile["objective"]` and `llm.py:132` lists `availability`; neither key exists
in `profile.yaml` (the objective lives under `llm_guidance`).

**I4 — `tech-stack-plan.md:204-207` documents endpoints that do not exist (G5);
`goal.md` requires freshness handling that is not implemented (B1).**

**I5 — `llm-hosted.md` documents batching (`:116-118`), escalation (`:107,201-212`),
usage/error persistence (`:62,199`) and startup validation (`:63`), none of which
is implemented (F3–F6).** It also calls the privacy field lists the boundary
(`:221`), which A1 contradicts.

## 5. What this session changed

Production (Pi, `inkeri.etto.fi`), with `.env` backups kept on the host:

- `LLM_PROMPT_VERSION` 7 → 8, `LLM_EVAL_MAX_JOBS` 50 → 800, full
  `DISCOVERY_SEARCH_QUERIES`, `GOOGLE_MAPS_API_KEY` + `TRANSIT_ORIGIN_ADDRESS`
  added (transit distance had been disabled, which left 207 recommendations in
  `transit_unavailable`); api and worker recreated; `/health` now reports
  `transit_distance_enabled: true`.
- Migration `20260915_0016` adds `ix_job_sources_job_id`; the recommendations
  query dropped from ~13 s / 116 k buffers to ~3 ms.
- A full matching run then re-scored 800 candidates at prompt version 8 and
  re-ran travel assessment (100 transit lookups). Consequences are analysed in
  §3.4; the transition artefact is item P0-3 in the plan.
- The profile audit's stale application-letter count was corrected, and three
  new application letters were added to `profile/inkeri/` (§3). The updated
  `profile.yaml` was loaded into the production database
  (`python -m app.profile … --name default`, verified: `boost_titles_fi` now
  contains `kirjastovirkailija` and `tietoasiantuntija`). The next scheduled
  matching run (16:45 Europe/Helsinki) will apply it; the deterministic effect
  is already measured in §3.3.

Repo documentation updated: `docs/implementation-journal.md` (production config
drift and the index), `docs/raspberry-pi-deployment.md` (mandatory `.env` key
diff at deploy time).

## 6. Validated as correct (audit breadth)

- Recommendation caching prevents repeat paid calls within a run
  (`matching.py:1288-1358`); no cross-job hash collisions exist in production.
- Failed LLM candidates keep `llm_evaluation_id` null and are retried at
  priority 0 next run; quota failure raises `EvaluationProviderUnavailable`,
  records a cooldown and breaks the loop (`matching.py:1397-1399`).
- `store=False` on both OpenAI calls; feedback comments are redacted for
  email/phone/proper names before hashing/prompting.
- Travel duration bands (+10/+6/+3) and penalties (−15/−8/−5), home-city
  shortcuts and multi-city home-city detection match the documented policy;
  weekday-08:00 Europe/Helsinki departures and UTC-normalised decay timestamps
  are correct.
- `llm_review_bucket_limits(800)` = 320/160/320 matches the recall plan.
- Exploration-lane exemption from the anti-centroid penalty, the 0.56 pgvector
  threshold, hysteresis (−2.0 enter / −1.0 exit) and the exploration floor of 1
  are implemented as documented.
- `role_clusters` titles/keywords, `preferences.sectors_preferred`,
  `application_history_signals` boosts and `hard_negative_titles_fi` do reach
  the scorer (`matching.py:270-344`); the local pool SQL uses the profile's
  `region_towns` rather than a hard-coded list.
- Scheduler timezone handling is correct end to end, every job has
  `max_instances=1` + `coalesce=True`, and the 16:00 all-sources schedule is
  intentional per `docs/sources.yaml:3-4`.
- Embeddings are content-hash guarded, so unchanged jobs are not re-embedded.
- `make test-regression` passes; the deployed worker code is byte-identical to
  repo HEAD.

## 7. Limitations

- The deterministic pool probe (§3.3) replays the production candidate fetch
  twice and had not finished when this report was written; its numbers will be
  appended. The §3.2 SQL measurements are a proxy (raw regex over the
  catalogue), not the scorer's token logic.
- The `EXPLAIN` measurements were taken while a matching run was in flight, so
  absolute timings are upper bounds; the plans and row counts are the load-bearing
  evidence.
- `pg_stat_statements` is not installed, so the split between
  `enrich_job_locations` and the discovery query within the ~590 s pipeline is
  **UNVERIFIED**.
- The root cause of the single failed feedback analysis (2026-07-08) is
  **UNVERIFIED** (logs rotated).
- Whether `laura.fi` uses `Europe/Helsinki` for its `after` parameter (H5) is
  **UNVERIFIED**.
