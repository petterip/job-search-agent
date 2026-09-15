"""Labelled evaluation tooling for the recommendation pipeline (P2-23).

The relevance labels themselves are private human input. This module consumes a
private labels file (never committed) and reports per-stage coverage by stratum
with explicit denominators and Wilson uncertainty, so quality claims are not
made from a biased feedback sample.

Labels file format (JSON):

```json
{
  "schema_version": 1,
  "sample_method": "how the sample was drawn",
  "labels": [
    {"job_id": 123, "stratum": "oulu_local", "relevant": true, "source": "user"}
  ]
}
```

Stages are observable pipeline states: a recommendation row exists, the job is
hard-eligible, it has been LLM-reviewed, and it is published.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.db import get_engine

STAGES = ("recommendation_row", "hard_eligible", "llm_reviewed", "published")
Z_95 = 1.959963984540054


class LabelledEvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Label:
    job_id: int
    stratum: str
    relevant: bool


@dataclass(frozen=True)
class StageState:
    job_id: int
    recommendation_row: bool
    hard_eligible: bool
    llm_reviewed: bool
    published: bool
    accepted: bool = False


def wilson_interval(successes: int, total: int, *, z: float = Z_95) -> tuple[float, float] | None:
    """95% Wilson score interval for a binomial proportion."""
    if total <= 0:
        return None
    phat = successes / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
        / denominator
    )
    return (round(max(0.0, centre - margin), 3), round(min(1.0, centre + margin), 3))


def load_labels(path: Path) -> tuple[list[Label], str]:
    if not path.exists():
        raise LabelledEvaluationError(f"labelled evaluation file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise LabelledEvaluationError("labelled evaluation file must be a JSON object")
    raw_labels = payload.get("labels")
    if not isinstance(raw_labels, list) or not raw_labels:
        raise LabelledEvaluationError("labelled evaluation file contains no labels")
    labels: list[Label] = []
    for index, item in enumerate(raw_labels):
        if not isinstance(item, dict):
            raise LabelledEvaluationError(f"labels[{index}] must be an object")
        job_id = item.get("job_id")
        relevant = item.get("relevant")
        stratum = item.get("stratum") or "unspecified"
        if not isinstance(job_id, int) or isinstance(job_id, bool):
            raise LabelledEvaluationError(f"labels[{index}].job_id must be an integer")
        if not isinstance(relevant, bool):
            raise LabelledEvaluationError(f"labels[{index}].relevant must be a boolean")
        labels.append(Label(job_id=job_id, stratum=str(stratum), relevant=relevant))
    sample_method = str(payload.get("sample_method") or "").strip()
    return labels, sample_method


def load_stage_states(
    connection: Connection,
    *,
    profile_id: int,
    job_ids: list[int],
) -> dict[int, StageState]:
    if not job_ids:
        return {}
    rows = connection.execute(
        sa.text(
            """
            select
                j.id as job_id,
                (r.id is not null) as recommendation_row,
                coalesce(r.deterministic_result->>'hard_eligible', 'true') <> 'false'
                    as hard_eligible,
                (r.llm_evaluation_id is not null) as llm_reviewed,
                coalesce(r.is_active, false) as published,
                coalesce(rf.rating >= 4 or rf.applied, false) as accepted
            from jobs j
            left join recommendations r
              on r.job_id = j.id and r.profile_id = :profile_id
            left join recommendation_feedback rf on rf.recommendation_id = r.id
            where j.id = any(:job_ids)
            """
        ),
        {"profile_id": profile_id, "job_ids": job_ids},
    ).mappings()
    return {
        int(row["job_id"]): StageState(
            job_id=int(row["job_id"]),
            recommendation_row=bool(row["recommendation_row"]),
            hard_eligible=bool(row["hard_eligible"]),
            llm_reviewed=bool(row["llm_reviewed"]),
            published=bool(row["published"]),
            accepted=bool(row["accepted"]),
        )
        for row in rows
    }


def _stage_summary(labels: list[Label], states: dict[int, StageState]) -> dict[str, Any]:
    relevant = [label for label in labels if label.relevant]
    published = [
        label
        for label in labels
        if (state := states.get(label.job_id)) is not None and state.published
    ]
    relevant_published = sum(1 for label in published if label.relevant)
    stages: dict[str, Any] = {}
    for stage in STAGES:
        reached = sum(
            1
            for label in relevant
            if (state := states.get(label.job_id)) is not None and getattr(state, stage)
        )
        stages[stage] = {
            "numerator": reached,
            "denominator": len(relevant),
            "value": round(reached / len(relevant), 3) if relevant else None,
            "wilson_95": wilson_interval(reached, len(relevant)),
        }
    accepted = sum(
        1
        for label in relevant
        if (state := states.get(label.job_id)) is not None and state.accepted
    )
    stages["accepted"] = {
        "numerator": accepted,
        "denominator": len(relevant),
        "value": round(accepted / len(relevant), 3) if relevant else None,
        "wilson_95": wilson_interval(accepted, len(relevant)),
    }
    return {
        "labelled": len(labels),
        "relevant": len(relevant),
        "not_relevant": len(labels) - len(relevant),
        "published_total": len(published),
        "relevant_published": relevant_published,
        # Precision has a different denominator from recall; name it explicitly.
        "published_precision": {
            "numerator": relevant_published,
            "denominator": len(published),
            "value": round(relevant_published / len(published), 3) if published else None,
            "wilson_95": wilson_interval(relevant_published, len(published)),
        },
        "stages": stages,
    }


def evaluate_labels(
    labels: list[Label],
    states: dict[int, StageState],
    *,
    sample_method: str = "",
) -> dict[str, Any]:
    strata: dict[str, list[Label]] = {}
    for label in labels:
        strata.setdefault(label.stratum, []).append(label)
    return {
        "sample_method": sample_method,
        "labels": len(labels),
        "overall": _stage_summary(labels, states),
        "by_stratum": {
            stratum: _stage_summary(stratum_labels, states)
            for stratum, stratum_labels in sorted(strata.items())
        },
        "notes": [
            "Recall denominators are labelled positives only; precision denominators are published labelled rows.",
            "Strata are reported separately because a single biased sample cannot estimate population recall.",
            "Wilson 95% intervals are reported for every proportion.",
        ],
    }


def run_labelled_evaluation(path: Path) -> dict[str, Any]:
    labels, sample_method = load_labels(path)
    engine = get_engine()
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("select id from job_seeker_profiles order by id limit 1")
        ).scalar_one_or_none()
        if row is None:
            raise LabelledEvaluationError("no profile found")
        states = load_stage_states(
            connection,
            profile_id=int(row),
            job_ids=sorted({label.job_id for label in labels}),
        )
    return evaluate_labels(labels, states, sample_method=sample_method)


def resolve_labels_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    settings = get_settings()
    configured = getattr(settings, "labelled_evaluation_path", "")
    if configured:
        return Path(configured)
    raise LabelledEvaluationError(
        "no labelled evaluation file configured; pass --labels PATH or set LABELLED_EVALUATION_PATH"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report per-stage recall/precision from a private labelled sample."
    )
    parser.add_argument("--labels", default=None, help="Path to the private labels JSON.")
    args = parser.parse_args()
    report = run_labelled_evaluation(resolve_labels_path(args.labels))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
