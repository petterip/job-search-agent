"""Tests for the simulated-human labeller and the bounded paid benchmarks.

No test here makes a real provider call: the labeller and the benchmark accept
injected providers, and the paid-call guards are asserted directly.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from app.labelled_review import (
    LABELER_CAVEAT,
    LABELER_NAME,
    LabelledEvaluationError,
    _parse_label,
    run_simulated_review,
)

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pg_only = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set; PostgreSQL integration tests skipped",
)

PROFILE = {
    "role_clusters": [{"titles_fi": ["kirjastonhoitaja"], "keywords_fi": ["kirjasto"]}],
    "location": {"home_city": "Oulu"},
    "freshness": {"max_age_days": 30, "drop_past_deadline": True},
}


@pytest.fixture()
def pg_engine() -> Iterator[Any]:
    engine = create_engine(str(TEST_DATABASE_URL))
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "truncate table recommendation_feedback, llm_evaluations, recommendations, "
                "job_sources, raw_listings, jobs, sources, job_seeker_profiles "
                "restart identity cascade"
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_profile(connection: Any) -> int:
    from support import with_privacy

    return int(
        connection.execute(
            sa.text(
                "insert into job_seeker_profiles (name, profile) "
                "values ('sample', CAST(:profile AS jsonb)) returning id"
            ),
            {"profile": json.dumps(with_privacy(dict(PROFILE)))},
        ).scalar_one()
    )


def _seed_job(connection: Any, *, index: int, title: str = "Kirjastonhoitaja") -> int:
    source_id = int(
        connection.execute(
            sa.text(
                "insert into sources (name, method, poll_interval_min, enabled) "
                "values (:name, 'api', 60, true) returning id"
            ),
            {"name": f"bench-{index}"},
        ).scalar_one()
    )
    job_id = int(
        connection.execute(
            sa.text(
                "insert into jobs (title, employer, description, status, location, published_at) "
                "values (:title, 'Testityönantaja', :description, 'active', 'Oulu', now()) "
                "returning id"
            ),
            {"title": title, "description": "Kirjasto ja asiakaspalvelu. " * 5},
        ).scalar_one()
    )
    listing_id = int(
        connection.execute(
            sa.text(
                "insert into raw_listings (source_id, external_id, canonical_source_url, "
                "content_hash, payload) values (:source_id, :external_id, :url, 'hash', "
                "'{}'::jsonb) returning id"
            ),
            {
                "source_id": source_id,
                "external_id": f"ext-{index}",
                "url": f"https://example.invalid/job/{index}",
            },
        ).scalar_one()
    )
    connection.execute(
        sa.text(
            "insert into job_sources (job_id, source_id, raw_listing_id, external_id, "
            "last_content_hash) values (:job_id, :source_id, :listing_id, :external_id, 'hash')"
        ),
        {
            "job_id": job_id,
            "source_id": source_id,
            "listing_id": listing_id,
            "external_id": f"ext-{index}",
        },
    )
    return job_id


def _seed_recommendation(connection: Any, *, job_id: int, profile_id: int) -> int:
    return int(
        connection.execute(
            sa.text(
                "insert into recommendations (job_id, profile_id, is_active, commutable, "
                "commutable_or_full_remote, machine_score, deterministic_result) values "
                "(:job_id, :profile_id, false, true, true, 55.0, "
                "CAST('{\"passes\": true}' AS jsonb)) returning id"
            ),
            {"job_id": job_id, "profile_id": profile_id},
        ).scalar_one()
    )


def _template(tmp_path, entries: list[dict[str, Any]]):
    path = tmp_path / "template.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sample_method": "synthetic",
                "unlabelled": True,
                "labels": entries,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_parse_label_validates_the_response() -> None:
    assert _parse_label({"relevant": True, "confidence": 80, "reason": "ok"}) == (
        True,
        80,
        "ok",
    )
    assert _parse_label(
        {"relevant": False, "confidence": 500, "reason": "x" * 400}
    ) == (
        False,
        100,
        "x" * 240,
    )
    with pytest.raises(ValueError):
        _parse_label({"relevant": "yes"})
    with pytest.raises(ValueError):
        _parse_label(["not", "an", "object"])


@pg_only
def test_run_simulated_review_labels_every_stratum_membership(
    pg_engine: Any, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.labelled_review as review

    with pg_engine.begin() as connection:
        profile_id = _seed_profile(connection)
        first = _seed_job(connection, index=1)
        second = _seed_job(connection, index=2, title="Myyntipäällikkö")
        _seed_recommendation(connection, job_id=first, profile_id=profile_id)
    monkeypatch.setattr(review, "get_engine", lambda: pg_engine)
    monkeypatch.setattr(
        review,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="test-key",
            gemini_api_key="",
            llm_provider="openai",
            openai_eval_model="fake",
            gemini_eval_model="fake",
        ),
    )

    calls: list[int] = []

    def fake_label_job(*, provider_name, api_key, model, prompt):
        calls.append(1)
        relevant = "Kirjastonhoitaja" in prompt
        return (
            relevant,
            70,
            "simulated",
            {"returned_model": model, "request_id": "req", "usage": {}},
        )

    monkeypatch.setattr(review, "label_job", fake_label_job)
    template = _template(
        tmp_path,
        [
            {"job_id": first, "stratum": "oulu_local", "relevant": None},
            {"job_id": first, "stratum": "unreviewed", "relevant": None},
            {"job_id": second, "stratum": "not_retrieved", "relevant": None},
        ],
    )
    output = tmp_path / "labels.json"

    summary = run_simulated_review(
        template, output_path=output, budget=5, provider_name="openai", model="fake"
    )

    assert summary["unique_jobs"] == 2
    assert summary["calls"] == 2
    payload = json.loads(output.read_text())
    assert payload["labeler"] == LABELER_NAME
    assert payload["labeler_caveat"] == LABELER_CAVEAT
    assert payload["unlabelled"] is False
    by_stratum = {entry["stratum"]: entry for entry in payload["labels"]}
    assert by_stratum["oulu_local"]["relevant"] is True
    assert by_stratum["unreviewed"]["relevant"] is True
    assert by_stratum["not_retrieved"]["relevant"] is False
    assert all(entry["source"] == LABELER_NAME for entry in payload["labels"])
    assert output.stat().st_mode & 0o777 == 0o600


@pg_only
def test_run_simulated_review_refuses_over_budget_and_existing_output(
    pg_engine: Any, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.labelled_review as review

    with pg_engine.begin() as connection:
        _seed_profile(connection)
        job_id = _seed_job(connection, index=1)
    monkeypatch.setattr(review, "get_engine", lambda: pg_engine)
    monkeypatch.setattr(
        review,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="test-key",
            gemini_api_key="",
            llm_provider="openai",
            openai_eval_model="fake",
            gemini_eval_model="fake",
        ),
    )
    template = _template(tmp_path, [{"job_id": job_id, "stratum": "unreviewed"}])

    with pytest.raises(LabelledEvaluationError, match="budget allows"):
        run_simulated_review(template, output_path=tmp_path / "out.json", budget=0)

    output = tmp_path / "out.json"
    output.write_text("{}")
    with pytest.raises(LabelledEvaluationError, match="already exists"):
        run_simulated_review(
            template, output_path=output, budget=1, provider_name="openai", model="fake"
        )
