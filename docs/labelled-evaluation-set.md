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

## Suggested strata

At least one stratum per bucket, with the skipped-local sample (about 50 jobs)
as one stratum rather than the whole benchmark:

- `oulu_local` — local/commutable jobs.
- `nationwide` — otherwise valid distant jobs.
- `remote` — full-remote listings.
- `application_family` — the three application-role families
  (kirjastonhoitaja, kirjastovirkailija, tietoasiantuntija).
- `non_obvious` — transferable/non-obvious roles.
- `hard_rejection` — jobs expected to fail a hard gate.
- `not_retrieved` — jobs known to exist but outside the candidate window.
- `unreviewed` — deterministic passes with no current LLM evaluation.
- `accepted` — jobs the user applied to.

## Running

```bash
cd backend
python -m app.labelled_evaluation --labels ../profile/inkeri/labelled-evaluation.json
# or configure LABELLED_EVALUATION_PATH and run without --labels
```

The report gives, per stratum and overall:

- recall at each pipeline stage (`recommendation_row`, `hard_eligible`,
  `llm_reviewed`, `published`, `accepted`) over labelled **positives**;
- published precision over published labelled rows (a different denominator);
- a Wilson 95% interval for every proportion.

Report the sample method, denominators and uncertainty together. Do not treat a
nonzero local result as the target, and do not compare runs on different labelled
samples.
