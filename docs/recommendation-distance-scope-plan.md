# Recommendation Distance Scope Plan

**Updated:** 2026-07-05  
**Status:** proposed after senior review  
**Related:** [`goal.md`](goal.md), [`architecture.md`](architecture.md), [`feedback-learning-plan.md`](feedback-learning-plan.md), [`implementation-journal.md`](implementation-journal.md)

The user-facing requirement is one simple recommendations scope selector:

- `Enintään 2 h`
- `Enintään 2 h + etä`
- `Koko maa`

`Enintään 2 h` is the current default label, not a value to bake into code. The commute limit and origin must be configuration because both may change later.

The implementation must treat that selector as backend recommendation policy. A frontend-only filter would produce wrong totals, sparse pages, wrong ordering, and no protection against far-away jobs consuming LLM review capacity.

## Verified Current State

Current distance-related behavior is useful but not yet policy:

- `score_job()` only gives token-based location credit from `home_city`, `region_towns`, `proven_willing_locations`, application-history locations, and learned locations.
- `run_deterministic_recommendations()` resolves transit only after scoring and lane ordering, then stores `transit_distance_km`, `transit_duration_text`, `transit_summary_text`, and `location_evidence` in `deterministic_result`.
- `build_location_evidence()` is presentation-oriented and currently uses kilometer thresholds for tone. It cannot enforce a configurable commute cutoff.
- `GET /recommendations` has no scope parameter. It counts and orders by one active/rank view, then enriches only the returned page.
- `run_llm_evaluations()` selects candidates by `r.rank`, sends `job_summary()` with only title, employer, location, and description excerpt, and caches by request hash.
- Feedback snapshots include transit summary fields, but not a stable commute scope, local rank, nationwide rank, or travel reason code.
- Work mode is encoded into `jobs.location` text as `/ Etä` or `/ Hybridi`, not a first-class field.

## Findings To Address

| Finding | Required response |
|---|---|
| Distance is visible but late. | Move travel assessment before final candidate ordering and before LLM candidate selection. |
| Current evidence uses km thresholds. | Introduce duration-based configurable travel policy; keep evidence generation as presentation output. |
| One `rank` cannot serve two views. | Store local and nationwide rank separately. |
| API lacks scope-aware totals. | Add backend `scope` filtering and ordering. |
| LLM does not see travel assessment. | Add travel fields to job summaries and bump `LLM_PROMPT_VERSION`. |
| LLM cache could reuse old evaluations. | Include new travel-aware prompt version/request content before relying on cached evaluations. |
| Feedback hiding only clears `rank`. | Rating 1 must clear active state and both scope ranks. Re-rating must allow both ranks to be recomputed. |
| Google Maps may be unavailable or fail. | Local scope must degrade explicitly to exact-home-city only; the local + remote scope may still include verified full-time remote jobs. |
| Multi-city/vague locations are common. | Normalize and assess destination candidates, not only the raw `jobs.location` string. |
| JSONB-only scope is not enough. | Use explicit columns for filtering, counts, indexes, and rank refresh. |
| Current matching unit-test fake does not support transit cache savepoints. | Fix the test double or monkeypatch transit resolution before using matching tests as the feature baseline. |
| Remote work needs a stricter product meaning than "some remote possibility." | Track verified full-time remote separately from hybrid or occasional remote work. |

## Product Policy

Use three scopes:

| Scope | Meaning | Default |
|---|---|---|
| `commutable` | Exact home-city jobs or jobs within the configured commute limit from the configured transit origin. | Yes |
| `commutable_or_full_remote` | Everything in `commutable`, plus verified full-time remote jobs. | No |
| `nationwide` | All otherwise valid recommendations in Finland. Long distance remains a concern and ranking penalty. | No |

Definitions:

- The default commute limit is 120 minutes.
- The default transit origin is the current `TRANSIT_ORIGIN_ADDRESS` value, currently `Jalkatie 2, Oulu, Finland`.
- The threshold must come from a setting such as `RECOMMENDATION_COMMUTE_LIMIT_MINUTES`; do not encode `120`, `2h`, or `two_hour` into function names, API scope names, database column names, or reason-code names.
- The origin must come from `TRANSIT_ORIGIN_ADDRESS`; do not assume Oulu outside configuration and stored audit fields.
- The routing profile is one-way public transit using the same stable departure strategy as `routing_departure_time()` currently uses: next weekday 08:00 Europe/Helsinki.
- Unknown transit does not qualify for `commutable`, except exact home-city fallback.
- Full-time remote jobs qualify for `commutable_or_full_remote`, even when the employer office is outside the commute limit.
- Full-time remote must mean the listing can be worked remotely as the normal arrangement. Hybrid, occasional remote days, "etätyömahdollisuus", and vague flexibility are not enough for this scope unless the extracted work-mode confidence marks the job as fully remote.
- Hybrid is not automatically local or full remote. It must be within the configured commute limit to appear outside `nationwide`.

## Module Design

Add a deep backend module at the matching seam, for example `app.travel_policy`.

Small interface:

```python
def assess_travel(
    *,
    profile: dict[str, Any],
    location: str | None,
    transit_by_destination: Mapping[str, TransitDistanceResult | None],
    origin_address: str,
    commute_limit_minutes: int,
) -> TravelAssessment:
    ...
```

`TravelAssessment` should expose:

| Field | Purpose |
|---|---|
| `commutable` | True if the recommendation may appear in the default local scope under the current commute policy. |
| `full_remote` | True only when the listing is verified as full-time remote. |
| `commutable_or_full_remote` | True when either `commutable` or `full_remote` is true. |
| `status` | `full_remote`, `hybrid`, `exact_home_city`, `within_limit`, `over_limit`, `unknown`, `unrouteable`, or `vague`. |
| `duration_seconds` | Best public-transit duration when known. |
| `distance_km` | Best route distance when known. |
| `score_adjustment` | Travel fit boost/penalty applied after base scoring. |
| `evidence_text` | Finnish explanation for API/UI and feedback snapshots. |
| `tone` | `good`, `warning`, or `bad`. |
| `reason_code` | Stable machine-readable code for tests and audits. |
| `origin_address` | Origin used for the assessment. |
| `commute_limit_minutes` | Limit used for the assessment. |
| `routing_profile` | Example: `weekday_0800_public_transit`. |

This module owns travel rules. Matching, API, UI, LLM, and feedback code should consume its result instead of re-implementing distance thresholds.

## Data Model

Add explicit columns to `recommendations`:

| Column | Type | Notes |
|---|---|---|
| `commutable` | boolean not null default false | Scope filter under the current commute policy. |
| `full_remote` | boolean not null default false | Verified full-time remote filter. |
| `commutable_or_full_remote` | boolean not null default false | Convenience filter for the third scope. |
| `commutable_rank` | integer nullable | Rank inside the configured commute scope. |
| `commutable_or_full_remote_rank` | integer nullable | Rank inside the configured commute + full remote scope. |
| `nationwide_rank` | integer nullable | Rank inside `nationwide`. |
| `travel_status` | text nullable | Mirrors `TravelAssessment.status`. |
| `travel_reason_code` | text nullable | Stable audit/test reason. |
| `travel_duration_seconds` | integer nullable | Used for sorting/debugging. |
| `travel_distance_km` | integer nullable | Debug/display support. |
| `travel_origin_address` | text nullable | Origin used when computing the row. |
| `travel_commute_limit_minutes` | integer nullable | Limit used when computing the row. |
| `travel_routing_profile` | text nullable | Prevents silent interpretation drift. |

Indexes:

- `(profile_id, is_active, commutable, commutable_rank)`
- `(profile_id, is_active, commutable_or_full_remote, commutable_or_full_remote_rank)`
- `(profile_id, is_active, nationwide_rank)`

Also keep a full JSON object in `deterministic_result.travel_assessment` for audit and feedback learning:

```json
{
  "commutable": true,
  "full_remote": false,
  "commutable_or_full_remote": true,
  "status": "within_limit",
  "duration_seconds": 5400,
  "distance_km": 92,
  "score_adjustment": 12,
  "evidence_text": "Ylivieska · 1 h 30 min (julkiset)",
  "tone": "good",
  "reason_code": "transit_within_limit",
  "origin_address": "Jalkatie 2, Oulu, Finland",
  "commute_limit_minutes": 120,
  "routing_profile": "weekday_0800_public_transit"
}
```

Do not ship the feature as JSONB-only. JSONB is fine as audit data, but scope filtering and totals need columns.

## Matching Changes

Change matching in this order:

1. Score active jobs with current role/skill/sector logic.
2. Build a pre-candidate set that preserves recall: all current passing jobs plus semantic/lane-promoted jobs within `MATCHER_MAX_JOBS`.
3. Extract normalized destination candidates from `jobs.location`; handle `/ Etä`, `/ Hybridi`, comma-separated cities, `Suomi`, and empty locations.
4. Resolve transit for distinct destination candidates using `transit_distance_cache` first and Google Routes only within a configured lookup budget.
5. Assess each candidate through `travel_policy.assess_travel()`.
6. Apply travel `score_adjustment` to the machine score and store the unadjusted score in `deterministic_result`.
7. Re-run candidate lane ordering with travel-adjusted scores.
8. Reserve LLM review capacity for `commutable` first, then `commutable_or_full_remote`, then fill remaining capacity with nationwide candidates.
9. Upsert recommendations with travel columns and JSON audit payload.
10. Refresh `commutable_rank`, `commutable_or_full_remote_rank`, and `nationwide_rank` separately after LLM evaluation.

Travel weighting should make distance decisive without making it the only signal:

| Travel assessment | Effect |
|---|---|
| Full-time remote | qualifies for `commutable_or_full_remote`; positive score adjustment |
| 0-45 min | strongest local boost |
| 46-90 min | medium local boost |
| 91 min to configured limit | small local boost |
| Exact home city | local, positive, no API lookup required |
| Unknown/vague | no local eligibility; warning in nationwide |
| Over configured limit on-site | excluded from local; meaningful nationwide penalty |
| Over configured limit hybrid | excluded from local and remote scope; smaller nationwide penalty than on-site |

## Rank Refresh And Feedback

`refresh_active_recommendation_ranks()` must become scope-aware:

- Deactivate rows for existing LLM skip/weak-fit rules and rating 1 feedback as today.
- Clear `rank`, `commutable_rank`, `commutable_or_full_remote_rank`, and `nationwide_rank` when deactivating.
- Recompute `nationwide_rank` for all active recommendations.
- Recompute `commutable_rank` only for active rows with `commutable = true`.
- Recompute `commutable_or_full_remote_rank` only for active rows with `commutable_or_full_remote = true`.
- Keep legacy `rank` temporarily as an alias for `nationwide_rank` until the UI/API are migrated.

`build_scoring_snapshot()` must include:

- `commutable`
- `full_remote`
- `commutable_or_full_remote`
- `commutable_rank`
- `commutable_or_full_remote_rank`
- `nationwide_rank`
- `travel_status`
- `travel_reason_code`
- `travel_duration_seconds`
- `travel_distance_km`
- `travel_origin_address`
- `travel_commute_limit_minutes`
- `travel_routing_profile`
- `travel_assessment`

This keeps feedback learning explainable after future reranks.

## LLM Integration

LLM evaluation remains scope-independent: it evaluates whether the job is a good recommendation, not which UI toggle is selected.

Required changes:

- Include travel assessment in `job_summary()`.
- Include `travel_assessment` in `evaluation_request_hash()` through the job summary.
- Bump `LLM_PROMPT_VERSION`, because old cached evaluations did not know commute policy.
- Keep deterministic policy authoritative: the LLM may mention long travel as a concern, but it must not override the configured `commutable` cutoff.
- Order LLM candidate selection by missing/outdated evaluation first, then `commutable_rank`, then `commutable_or_full_remote_rank`, then `nationwide_rank`, then score.

## API Contract

Extend `GET /recommendations`:

```text
GET /recommendations?scope=commutable&limit=20&offset=0
GET /recommendations?scope=commutable_or_full_remote&limit=20&offset=0
GET /recommendations?scope=nationwide&limit=20&offset=0
```

Rules:

- Default `scope=commutable`.
- Reject unknown scopes with 422.
- `commutable`: filter `r.commutable = true`, order by `r.commutable_rank`.
- `commutable_or_full_remote`: filter `r.commutable_or_full_remote = true`, order by `r.commutable_or_full_remote_rank`.
- `nationwide`: no local filter, order by `r.nationwide_rank`.
- Return scope-specific `total`.
- Include item fields: `commutable`, `full_remote`, `commutable_or_full_remote`, `travel_status`, `travel_reason_code`, `travel_duration_seconds`, `travel_distance_km`, `travel_origin_address`, `travel_commute_limit_minutes`, and `location_evidence`.
- Keep `rank` in the response during transition, but set it from the selected scope rank.

If `GOOGLE_MAPS_API_KEY` is not configured, `commutable` should still return exact-home-city recommendations and `commutable_or_full_remote` should additionally return verified full-time remote recommendations. Health/status should make transit unavailability visible.

## UI Contract

Add one segmented control in the recommendations section, separate from the all-jobs filters:

- `Enintään 2 h`
- `2 h + etä`
- `Koko maa`

Implementation details:

- Read/write `scope` in the page query string.
- Preserve existing all-jobs filters and pagination when toggling scope.
- Request `/recommendations?scope=${scope}&limit=15`.
- Display scope-specific total text.
- If local scope is empty, show a clear empty state and provide the `Koko maa` option.
- Keep location evidence visible on each card.
- Ensure long travel text wraps without changing card layout unexpectedly.

## Rollout Plan

1. Add `TravelAssessment`, destination parsing, and `travel_policy.assess_travel()` with unit tests.
2. Add `RECOMMENDATION_COMMUTE_LIMIT_MINUTES` configuration with default `120`; keep origin in `TRANSIT_ORIGIN_ADDRESS`.
3. Add `RECOMMENDATION_TRANSIT_LOOKUP_BUDGET` configuration for uncached Google Routes lookups during recommendation travel assessment.
4. Add recommendation travel columns and indexes.
5. Backfill existing active recommendations from stored `deterministic_result` where possible; unknown rows should default to nationwide-only until rematched.
6. Move transit assessment earlier in `run_deterministic_recommendations()`.
7. Apply travel scoring and upsert travel columns.
8. Make rank refresh scope-aware and update feedback visibility updates.
9. Tighten remote detection so only verified full-time remote jobs set `full_remote = true`.
10. Add travel-aware `job_summary()` and bump `LLM_PROMPT_VERSION`.
11. Extend `GET /recommendations` with `scope`.
12. Add the Finnish UI selector and empty states.
13. Run backend tests, `make test-regression`, and a browser check for all scopes.
14. Update [`implementation-journal.md`](implementation-journal.md) only after behavior ships.

## Tests

Backend:

- Full-time remote jobs qualify for `commutable_or_full_remote`, not `commutable` unless they also match the commute rule.
- Hybrid and vague remote-possibility jobs do not qualify for `commutable_or_full_remote` solely because remote work is mentioned.
- Exact home-city jobs qualify without a Routes API lookup.
- With default config, `duration_seconds == 7200` qualifies and `7201` does not.
- Changing `RECOMMENDATION_COMMUTE_LIMIT_MINUTES` changes eligibility without code changes.
- Changing `TRANSIT_ORIGIN_ADDRESS` changes the stored origin and forces/requires recomputation before ranks are trusted.
- Unknown, vague, failed-route, and country-level locations do not qualify for local scope.
- Multi-location jobs qualify if any concrete destination is within the configured commute limit.
- Hybrid far-away jobs are nationwide-only.
- Missing Google Maps returns only exact-home-city local recommendations; `commutable_or_full_remote` also returns verified full-time remote recommendations.
- Matching stores travel columns and JSON audit payload.
- All three scope ranks are computed independently.
- Rating 1 clears all scope ranks; re-rating >=2 lets ranks be recomputed.
- `GET /recommendations?scope=commutable` returns scoped totals and local rank order.
- `GET /recommendations?scope=commutable_or_full_remote` returns local plus verified full-time remote recommendations.
- `GET /recommendations?scope=nationwide` includes far jobs with travel concerns.
- LLM request hashes change after travel assessment is added.
- Feedback snapshots include travel fields.
- Matching test doubles support transit cache `begin_nested()` calls or mock transit resolution deliberately.

Frontend:

- Recommendations default to the configured commute scope; with the current default this is labeled `Enintään 2 h`.
- Toggle updates `scope` in the URL and API request.
- Existing job search filters survive scope toggling.
- Empty local scope points to `Koko maa`.
- Long travel evidence wraps cleanly on desktop and mobile.

## Acceptance Criteria

The feature is complete when:

- The UI has one simple recommendation-scope selector.
- Default recommendations show exact-home-city or jobs verified within the configured commute limit.
- The `2 h + etä` option additionally includes verified full-time remote jobs.
- `Koko maa` shows the broader recommendation set.
- Counts, pagination, ranks, and feedback behavior are correct per scope.
- Nearby jobs get LLM review priority over otherwise comparable far-away jobs.
- Nationwide far-away jobs remain inspectable but are penalized and clearly explained.
- The implementation has one travel policy module instead of duplicated distance rules.
