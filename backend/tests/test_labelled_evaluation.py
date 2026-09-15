import json

import pytest

from app.labelled_evaluation import (
    Label,
    LabelledEvaluationError,
    StageState,
    evaluate_labels,
    load_labels,
    wilson_interval,
)


def _state(job_id, *, row=True, eligible=True, reviewed=True, published=True, accepted=False):
    return StageState(
        job_id=job_id,
        recommendation_row=row,
        hard_eligible=eligible,
        llm_reviewed=reviewed,
        published=published,
        accepted=accepted,
    )


def test_wilson_interval_bounds() -> None:
    assert wilson_interval(0, 0) is None
    low, high = wilson_interval(5, 10)  # type: ignore[misc]
    assert 0.0 <= low < 0.5 < high <= 1.0
    assert wilson_interval(0, 10) == (0.0, 0.278)  # type: ignore[comparison-overlap]


def test_evaluate_labels_reports_stage_denominators_and_precision() -> None:
    labels = [
        Label(1, "oulu_local", True),
        Label(2, "oulu_local", True),
        Label(3, "nationwide", True),
        Label(4, "nationwide", False),
    ]
    states = {
        1: _state(1, reviewed=True, published=True, accepted=True),
        2: _state(2, reviewed=False, published=False),
        3: _state(3, reviewed=True, published=True),
        4: _state(4, reviewed=True, published=True),
    }

    report = evaluate_labels(labels, states, sample_method="synthetic")

    assert report["overall"]["relevant"] == 3
    published = report["overall"]["stages"]["published"]
    assert published["numerator"] == 2
    assert published["denominator"] == 3
    assert published["value"] == 0.667
    assert report["overall"]["published_precision"] == {
        "numerator": 2,
        "denominator": 3,
        "value": 0.667,
        "wilson_95": report["overall"]["published_precision"]["wilson_95"],
    }
    assert set(report["by_stratum"]) == {"nationwide", "oulu_local"}
    assert report["sample_method"] == "synthetic"
    json.dumps(report)


def test_load_labels_rejects_missing_or_malformed(tmp_path) -> None:
    missing = tmp_path / "labels.json"
    with pytest.raises(LabelledEvaluationError):
        load_labels(missing)

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"labels": [{"job_id": "x", "relevant": True}]}))
    with pytest.raises(LabelledEvaluationError):
        load_labels(bad)

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"labels": []}))
    with pytest.raises(LabelledEvaluationError):
        load_labels(empty)


def test_load_labels_reads_sample_method(tmp_path) -> None:
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(
            {
                "sample_method": "stratified random",
                "labels": [{"job_id": 1, "stratum": "local", "relevant": True}],
            }
        )
    )

    labels, method = load_labels(path)

    assert labels == [Label(1, "local", True)]
    assert method == "stratified random"
