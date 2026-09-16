# Labelled evaluation set (P2-23)

The recommendation quality metrics in `docs/implementation-journal.md` come from
a small feedback benchmark and cannot estimate population recall. A labelled
evaluation set is required before any calibration change (P2-6/P2-7) or
concurrency work (P2-1) is enabled.

The labels are **private**: keep the file under the ignored `profile/` tree (for
example `profile/inkeri/labelled-evaluation.json`) and never commit it.

## Format

```json
{
  "schema_version": 1,
  "sample_method": "how this sample was drawn (strata, date range, seed)",
  "labels": [
    {"job_id": 123, "stratum": "oulu_local", "relevant": true, "source": "user"},
    {"job_id": 456, "stratum": "nationwide", "relevant": false, "source": "user"}
  ]
}
```

`relevant` is the user's judgement of whether the job is a genuine fit. The
tooling never infers it.

## Strata

At least one stratum per bucket, with the skipped-local sample (about 50 jobs)
as one stratum rather than the whole benchmark. The sampler emits these exact
strata, each defined by an observable pipeline state:

- `oulu_local` — published commutable recommendation, using the API's own
  travel-policy-aware scope predicate (structural eligibility applied).
- `nationwide` — published non-commutable, non-remote recommendation
  (structural eligibility applied).
- `remote` — published non-commutable, full-remote recommendation, using the
  API's travel-policy-aware scope predicate (structural eligibility applied).

A stored `commutable`/`commutable_or_full_remote` flag is not enough: the scope
predicate also requires the current origin, commute limit and routing profile
(or a matching shortcut fingerprint). Rows assessed under an older policy are
excluded from the local/remote strata exactly as the API excludes them.
- `hard_rejection` — persisted hard-gate rejection, plus source-backed jobs with
  no row that the current gates already reject (language, qualification,
  negative-term or freshness).
- `unreviewed` — structurally eligible row whose linked evaluation is missing or
  has a different prompt version.
- `accepted` — recommendation the user rated 4–5 or applied to.
- `not_retrieved` — source-backed job with no row that the current deterministic
  and hard gates would pass. This is the recall-miss bucket.

The last point matters: "no recommendation row" alone does **not** mean "not
retrieved". Matching persists only deterministic passes, so the sampler replays
the pipeline's own `score_job` and hard-eligibility gates over missing rows and
sends rejects to `hard_rejection` or the diagnostic `deterministic_rejection`
bucket. Only gates-passing missing rows count toward measured recall.

`application_family` and `non_obvious` are relevance classes, not observable
states: tag them yourself with `stratum` values when labelling the rows the
sampler produced.

## Simulated review (provisional labels)

When no human labels exist yet, `app/labelled_review.py` fills the template with
a hosted LLM that role-plays the job seeker:

```bash
cd backend
python -m app.labelled_review --labels ../profile/inkeri/labelled-evaluation-sample.json \
  --out ../profile/inkeri/labelled-evaluation.json --budget 60
```

Rules the tool enforces:

- the review prompt is deliberately different from the pipeline evaluation
  prompt and never receives the stratum, so the labels are not a copy of the
  decision being measured;
- only sanitized outbound text is sent (the same privacy projection as the
  evaluation path);
- the sample is refused when it has more unique jobs than `--budget`, and an
  existing output file is refused before any paid call is made;
- the output records `labeler: simulated_llm_reviewer`, the model, and a caveat
  that the labels are not human judgements.

**These labels are provisional.** The reviewer shares a model family with the
evaluator, so its errors are correlated with the pipeline; a real human label
set supersedes them, and any enable/disable decision made from simulated labels
must be revisited once real labels exist.

Strata may overlap (a published local job can also be accepted and unreviewed).
That is intentional: per-stratum metrics keep every membership, while overall
recall and precision count each job exactly once, and a job labelled with
conflicting relevance values is rejected instead of silently double-counted.

## Running

Build an unlabelled private template first (deterministic seeded sample across
the implemented strata: `oulu_local`, `nationwide`, `remote`, `hard_rejection`,
`unreviewed`, `not_retrieved`, `accepted`):

```bash
cd backend
python -m app.labelled_evaluation --build-sample ../profile/inkeri/labelled-evaluation.json --per-stratum 50
```

This writes `relevant: null` placeholders with the job title/employer/location
as context, then you fill in `true`/`false`. Evaluation refuses to run while any
`relevant` is still `null`, so an unlabelled template cannot produce a report.

The destination must be outside the repository or inside the ignored `profile/`
tree, and an existing file is never replaced: a second run fails rather than
discarding labels you have already written. Use `--force` only to discard a
template deliberately; `--force` still rewrites the file with owner-only
permissions, publishes atomically from a temporary file, never follows a symlink
at any path component, and never truncates another name for a hard-linked file.

`--seed` selects the deterministic sample. `--scan-limit` (default 1000) bounds
how many missing-row jobs are replayed through the gates; scanning stops early
once the requested `not_retrieved` and computed `hard_rejection` samples are
full. The output records `scanned` and `scan_limit_reached`, so raise
`--scan-limit` and re-run if a bucket is short and the limit was reached.

```bash
python -m app.labelled_evaluation --labels ../profile/inkeri/labelled-evaluation.json
# or configure LABELLED_EVALUATION_PATH and run without --labels
```

`application_family` and `non_obvious` are refinements of the labelled rows: tag
them with `stratum` values from the list above when labelling, since the
sampler's strata are observable pipeline states, not relevance classes.

The report gives, per stratum and overall:

- recall at each pipeline stage (`recommendation_row`, `hard_eligible`,
  `llm_reviewed`, `published`, `accepted`) over labelled **positives**
  (deduplicated by job for the overall figure);
- published precision over published labelled rows (a different denominator);
- a Wilson 95% interval for every proportion.

Report the sample method, denominators and uncertainty together, and state the
labeller provenance. Do not treat a nonzero local result as the target, and do
not compare runs on different labelled samples.

## Measured run (2026-09-16, simulated labels)

41 unique jobs from the production sample (8 per stratum; `oulu_local` was empty
because production has no commutable recommendations): simulated relevance 8/41.
Overall stage coverage for labelled positives was 8/8 recommendation row, 8/8
hard eligible, 8/8 LLM reviewed, 7/8 published and 1/8 accepted; published
precision was 7/16 (0.438, Wilson 0.231-0.668). The informative per-stratum
signals were simulated relevance 0/8 in both `not_retrieved` and `unreviewed`
and 4/8 (`nationwide`) plus 3/8 (`remote`) in the published scopes. The sample is
stratified from pipeline states, so the overall recall figure is not a
population estimate; the `not_retrieved` denominator is 8 with a 0-0.32 interval,
which cannot rule out a substantial missed-relevant share.
