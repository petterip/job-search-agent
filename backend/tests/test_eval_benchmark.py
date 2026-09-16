"""Tests for the bounded paid benchmarks (guards and both modes, no paid calls)."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from app.eval_benchmark import (
    LabelledEvaluationError,
    run_prompt_variant_benchmark,
    run_throughput_benchmark,
)
from app.llm import JobFitEvaluation

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

LONG_DESCRIPTION = "Kirjasto ja asiakaspalvelu. " * 300 + " Vaaditaan kelpoisuus. " * 20


class FakeProvider:
    provider_name = "fake"

    def __init__(self, *, score: int = 70, action: str = "consider") -> None:
        self.score = score
        self.action = action
        self.calls = 0

    def evaluate_job_fit(self, *, profile_summary, job_summary, model, prompt_version):
        self.calls += 1
        evaluation = JobFitEvaluation(
            score=self.score,
            fit_tier="strong_fit",
            rationale="fake",
            concerns=[],
            suggested_action=self.action,
        )
        return evaluation, {
            "returned_model": model,
            "usage": {"input_tokens": 100, "output_tokens": 10},
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
                "values ('bench', CAST(:profile AS jsonb)) returning id"
            ),
            {"profile": json.dumps(with_privacy(dict(PROFILE)))},
        ).scalar_one()
    )


def _seed_job(connection: Any, *, index: int, description: str) -> int:
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
                "values ('Kirjastonhoitaja', 'Testityönantaja', :description, 'active', 'Oulu', "
                "now()) returning id"
            ),
            {"description": description},
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
    connection.execute(
        sa.text(
            "insert into recommendations (job_id, profile_id, is_active, commutable, "
            "commutable_or_full_remote, machine_score, deterministic_result) values "
            "(:job_id, :profile_id, false, true, true, 55.0, "
            "CAST('{\"passes\": true}' AS jsonb))"
        ),
        {"job_id": job_id, "profile_id": _profile_id(connection)},
    )
    return job_id


def _profile_id(connection: Any) -> int:
    return int(
        connection.execute(
            sa.text("select id from job_seeker_profiles order by id limit 1")
        ).scalar_one()
    )


def test_paid_guards_refuse_unauthorized_calls(tmp_path, monkeypatch) -> None:
    with pytest.raises(LabelledEvaluationError, match="allow-paid-calls"):
        run_throughput_benchmark(
            limit=1,
            concurrency=2,
            budget=4,
            allow_paid_calls=False,
            database_is_disposable=True,
        )
    with pytest.raises(LabelledEvaluationError, match="disposable"):
        run_throughput_benchmark(
            limit=1,
            concurrency=2,
            budget=4,
            allow_paid_calls=True,
            database_is_disposable=False,
        )
    with pytest.raises(LabelledEvaluationError, match="budget"):
        run_prompt_variant_benchmark(
            variant="excerpt", limit=4, budget=4, allow_paid_calls=True, rules=[]
        )


@pg_only
def test_throughput_mode_runs_both_arms_on_the_same_cohort(
    pg_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.eval_benchmark as benchmark

    with pg_engine.begin() as connection:
        _seed_profile(connection)
        for index in range(1, 5):
            _seed_job(
                connection, index=index, description="Kirjasto ja asiakaspalvelu."
            )
    provider = FakeProvider()
    monkeypatch.setattr(benchmark, "get_engine", lambda: pg_engine)
    monkeypatch.setattr(
        benchmark, "build_evaluation_provider", lambda settings: provider
    )

    report = run_throughput_benchmark(
        limit=2,
        concurrency=2,
        budget=8,
        allow_paid_calls=True,
        database_is_disposable=True,
    )

    assert report["sequential"]["calls"] == 2
    assert report["concurrent"]["calls"] == 2
    assert report["paid_calls"] == 4
    assert report["cohort_jobs"] == 2
    assert report["speedup"] is not None
    json.dumps(report, default=str)


@pg_only
def test_prompt_variant_mode_reports_tokens_parity_and_identity(
    pg_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.eval_benchmark as benchmark

    with pg_engine.begin() as connection:
        _seed_profile(connection)
        for index in range(1, 4):
            _seed_job(connection, index=index, description=LONG_DESCRIPTION)
    provider = FakeProvider()
    monkeypatch.setattr(benchmark, "get_engine", lambda: pg_engine)
    monkeypatch.setattr(
        benchmark, "build_evaluation_provider", lambda settings: provider
    )

    report = run_prompt_variant_benchmark(
        variant="excerpt", limit=2, budget=8, allow_paid_calls=True, rules=[]
    )

    assert report["jobs"] == 2
    assert report["paid_calls"] == 4
    assert report["decision_parity"]["same_action"] == 2
    assert report["identity_changed"] is True
    assert report["baseline_totals"]["input_tokens"] == 200
    assert report["variant_totals"]["input_tokens"] == 200
    assert all(entry["description_chars"] > 4000 for entry in report["per_job"])

    rules_report = run_prompt_variant_benchmark(
        variant="profile-rules",
        limit=1,
        budget=4,
        allow_paid_calls=True,
        rules=["Kirjastoalan kelpoisuus riittää."],
    )
    assert rules_report["identity_changed"] is True
    assert rules_report["decision_parity"]["same_action"] == 1

    with pytest.raises(LabelledEvaluationError, match="--rule is required"):
        run_prompt_variant_benchmark(
            variant="profile-rules", limit=1, budget=4, allow_paid_calls=True, rules=[]
        )


class StubProvider:
    provider_name = "stub"

    def __init__(
        self, *, model: str, score: int, action: str, in_tokens: int, usage_style: str
    ) -> None:
        self.model = model
        self.score = score
        self.action = action
        self.in_tokens = in_tokens
        self.usage_style = usage_style
        self.calls = 0

    def evaluate_job_fit(self, *, profile_summary, job_summary, model, prompt_version):
        self.calls += 1
        evaluation = JobFitEvaluation(
            score=self.score,
            fit_tier="strong_fit",
            rationale="stub",
            concerns=[],
            suggested_action=self.action,
        )
        usage = (
            {"promptTokenCount": self.in_tokens, "candidatesTokenCount": 100}
            if self.usage_style == "gemini"
            else {"input_tokens": self.in_tokens, "output_tokens": 100}
        )
        return evaluation, {"returned_model": model, "usage": usage}


@pg_only
def test_model_compare_reports_parity_tokens_and_cost(
    pg_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.eval_benchmark as benchmark

    with pg_engine.begin() as connection:
        _seed_profile(connection)
        for index in range(1, 4):
            _seed_job(connection, index=index, description=LONG_DESCRIPTION)

    baseline = StubProvider(
        model="base", score=70, action="apply", in_tokens=1000, usage_style="openai"
    )
    candidate = StubProvider(
        model="cand", score=65, action="apply", in_tokens=800, usage_style="gemini"
    )

    def fake_provider_for(provider_name: str, settings: Any) -> Any:
        return baseline if provider_name == "openai" else candidate

    monkeypatch.setattr(benchmark, "get_engine", lambda: pg_engine)
    monkeypatch.setattr(benchmark, "provider_for", fake_provider_for)
    monkeypatch.setattr(
        benchmark,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg://stub:stub@127.0.0.1:55432/stub",
            llm_provider="openai",
            openai_eval_model="base",
            gemini_eval_model="cand",
            openai_api_key="k",
            gemini_api_key="k",
            openai_eval_timeout_seconds=10,
            llm_prompt_version=1,
            llm_eval_max_jobs=800,
        ),
    )

    report = benchmark.run_model_compare_benchmark(
        limit=2,
        budget=8,
        allow_paid_calls=True,
        candidate_provider="gemini",
        candidate_model="cand",
        baseline_provider="openai",
        baseline_model="base",
        prices={
            "baseline": {"input": 0.20, "output": 1.25},
            "candidate": {"input": 0.30, "output": 2.50},
        },
    )

    assert report["jobs"] == 2
    assert report["paid_calls"] == 4
    assert report["decision_parity"]["same_action"] == 2
    assert report["decision_parity"]["same_tier"] == 2
    assert report["baseline"]["input_tokens"] == 2000
    assert report["candidate"]["input_tokens"] == 1600
    # baseline 2000/1000 in+out, candidate 1600/200
    assert report["baseline"]["cost_per_call_usd"] == round(
        (2000 / 1e6 * 0.20 + 200 / 1e6 * 1.25) / 2, 6
    )
    assert report["candidate"]["cost_per_call_usd"] == round(
        (1600 / 1e6 * 0.30 + 200 / 1e6 * 2.50) / 2, 6
    )
    assert report["baseline"]["projected_run_cost_usd"] is not None
    json.dumps(report, default=str)


def test_model_compare_requires_paid_authorization() -> None:
    import app.eval_benchmark as benchmark

    with pytest.raises(LabelledEvaluationError, match="allow-paid-calls"):
        benchmark.run_model_compare_benchmark(
            limit=2,
            budget=8,
            allow_paid_calls=False,
            candidate_provider="gemini",
            candidate_model="cand",
        )
