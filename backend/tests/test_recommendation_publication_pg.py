"""PostgreSQL-backed publication and freshness tests.

SQL-string mocks cannot validate these rules. Run with a disposable database:

    TEST_DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1:55432/db \\
        python -m pytest tests/test_recommendation_publication_pg.py

The database must already be migrated (`alembic upgrade head`).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from app import main as main_module
from app.freshness import capture_as_of
from app.matching import (
    reconcile_recommendation_publication,
    refresh_active_recommendation_ranks,
    structural_eligibility_predicate,
)
from support import with_privacy

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set; PostgreSQL integration tests skipped",
)

TABLES = (
    "recommendation_feedback",
    "feedback_llm_analyses",
    "llm_evaluations",
    "recommendations",
    "job_sources",
    "jobs",
    "raw_listings",
    "sources",
    "job_seeker_profiles",
    "source_run_events",
    "source_runs",
    "pipeline_runs",
)


@pytest.fixture()
def pg_engine() -> Iterator[Any]:
    engine = create_engine(str(TEST_DATABASE_URL))
    with engine.begin() as connection:
        connection.execute(
            sa.text(f"truncate table {', '.join(TABLES)} restart identity cascade")
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_profile(
    *,
    freshness: dict[str, Any] | None = None,
    exclusions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return with_privacy(
        {
            "freshness": freshness or {"max_age_days": 30, "drop_past_deadline": True},
            "exclusions": exclusions or {},
            "role_clusters": [{"titles_fi": ["kirjastonhoitaja"], "keywords_fi": ["kirjasto"]}],
            "location": {"home_city": "Testikaupunki"},
        }
    )


def seed_profile(
    connection: Any,
    *,
    name: str = "test",
    freshness: dict[str, Any] | None = None,
    exclusions: dict[str, Any] | None = None,
) -> int:
    profile = build_profile(freshness=freshness, exclusions=exclusions)
    return int(
        connection.execute(
            sa.text(
                """
                insert into job_seeker_profiles (name, profile)
                values (:name, CAST(:profile AS jsonb))
                returning id
                """
            ),
            {"name": name, "profile": __import__("json").dumps(profile)},
        ).scalar_one()
    )


def seed_source(connection: Any, *, name: str, enabled: bool) -> int:
    return int(
        connection.execute(
            sa.text(
                """
                insert into sources (name, method, poll_interval_min, enabled)
                values (:name, 'api', 60, :enabled)
                returning id
                """
            ),
            {"name": name, "enabled": enabled},
        ).scalar_one()
    )


def seed_job(
    connection: Any,
    *,
    title: str = "Kirjastonhoitaja",
    source_id: int,
    source_enabled: bool = True,
    published_at: datetime | None = None,
    expires_at: datetime | None = None,
    application_url: str | None = "https://example.invalid/apply",
) -> int:
    job_id = int(
        connection.execute(
            sa.text(
                """
                insert into jobs (title, employer, description, status, published_at, expires_at)
                values (:title, 'Testityönantaja', 'Kirjasto ja asiakaspalvelu', 'active',
                        :published_at, :expires_at)
                returning id
                """
            ),
            {
                "title": title,
                "published_at": published_at or _now() - timedelta(days=1),
                "expires_at": expires_at,
            },
        ).scalar_one()
    )
    listing_id = int(
        connection.execute(
            sa.text(
                """
                insert into raw_listings (source_id, external_id, canonical_source_url, content_hash, payload)
                values (:source_id, :external_id, :url, 'hash', '{}'::jsonb)
                returning id
                """
            ),
            {
                "source_id": source_id,
                "external_id": f"ext-{job_id}",
                "url": f"https://example.invalid/job/{job_id}",
            },
        ).scalar_one()
    )
    connection.execute(
        sa.text(
            """
            insert into job_sources (
                job_id, source_id, raw_listing_id, external_id,
                application_url, last_content_hash
            )
            values (:job_id, :source_id, :listing_id, :external_id, :application_url, 'hash')
            """
        ),
        {
            "job_id": job_id,
            "source_id": source_id,
            "listing_id": listing_id,
            "external_id": f"ext-{job_id}",
            "application_url": application_url,
        },
    )
    return job_id


def seed_recommendation(
    connection: Any,
    *,
    job_id: int,
    profile_id: int,
    is_active: bool = True,
    llm_score: int | None = 75,
    suggested_action: str | None = "consider",
    fit_tier: str | None = "transferable_weaker",
    deterministic_result: dict[str, Any] | None = None,
) -> int:
    return int(
        connection.execute(
            sa.text(
                """
                insert into recommendations (
                    job_id, profile_id, deterministic_result, machine_score,
                    rank, nationwide_rank, is_active, llm_score, suggested_action, fit_tier
                )
                values (
                    :job_id, :profile_id, CAST(:deterministic_result AS jsonb), 55.0,
                    1, 1, :is_active, :llm_score, :suggested_action, :fit_tier
                )
                returning id
                """
            ),
            {
                "job_id": job_id,
                "profile_id": profile_id,
                "deterministic_result": __import__("json").dumps(
                    deterministic_result or {"passes": True, "candidate_lanes": ["direct_title"]}
                ),
                "is_active": is_active,
                "llm_score": llm_score,
                "suggested_action": suggested_action,
                "fit_tier": fit_tier,
            },
        ).scalar_one()
    )


def _is_active(connection: Any, recommendation_id: int) -> bool:
    return bool(
        connection.execute(
            sa.text("select is_active from recommendations where id = :id"),
            {"id": recommendation_id},
        ).scalar_one()
    )


def test_disabled_only_source_is_deactivated_by_reconciliation(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        source_id = seed_source(connection, name="disabled_only", enabled=False)
        job_id = seed_job(connection, source_id=source_id)
        recommendation_id = seed_recommendation(connection, job_id=job_id, profile_id=profile_id)
        profile = build_profile()

        result = reconcile_recommendation_publication(
            connection,
            profile_id=profile_id,
            profile=profile,
            settings=_settings(),
            as_of=capture_as_of(),
            publication_mode="hosted",
        )

        assert result["reconciled_deactivated"] == 1
        assert _is_active(connection, recommendation_id) is False

        # Ranking must not resurrect a row with no enabled occurrence.
        refresh_active_recommendation_ranks(
            connection, profile_id=profile_id, require_llm_review=True
        )
        assert _is_active(connection, recommendation_id) is False


def test_enabled_occurrence_keeps_recommendation_published(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        source_id = seed_source(connection, name="enabled", enabled=True)
        job_id = seed_job(connection, source_id=source_id)
        recommendation_id = seed_recommendation(connection, job_id=job_id, profile_id=profile_id)
        profile = build_profile()

        result = reconcile_recommendation_publication(
            connection,
            profile_id=profile_id,
            profile=profile,
            settings=_settings(),
            as_of=capture_as_of(),
            publication_mode="hosted",
        )

        assert result["reconciled_deactivated"] == 0
        assert _is_active(connection, recommendation_id) is True


def test_stale_publication_is_deactivated_but_job_stays_in_catalogue(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        source_id = seed_source(connection, name="enabled", enabled=True)
        job_id = seed_job(
            connection,
            source_id=source_id,
            published_at=_now() - timedelta(days=31),
        )
        recommendation_id = seed_recommendation(connection, job_id=job_id, profile_id=profile_id)
        profile = build_profile()

        reconcile_recommendation_publication(
            connection,
            profile_id=profile_id,
            profile=profile,
            settings=_settings(),
            as_of=capture_as_of(),
            publication_mode="hosted",
        )

        assert _is_active(connection, recommendation_id) is False
        status = connection.execute(
            sa.text("select status from jobs where id = :id"), {"id": job_id}
        ).scalar_one()
        assert status == "active"


def test_past_deadline_is_deactivated(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        source_id = seed_source(connection, name="enabled", enabled=True)
        job_id = seed_job(
            connection,
            source_id=source_id,
            expires_at=_now() - timedelta(days=2),
        )
        recommendation_id = seed_recommendation(connection, job_id=job_id, profile_id=profile_id)
        profile = build_profile()

        reconcile_recommendation_publication(
            connection,
            profile_id=profile_id,
            profile=profile,
            settings=_settings(),
            as_of=capture_as_of(),
            publication_mode="hosted",
        )

        assert _is_active(connection, recommendation_id) is False


def test_drop_past_deadline_false_keeps_expired_job_published(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        source_id = seed_source(connection, name="enabled", enabled=True)
        job_id = seed_job(
            connection,
            source_id=source_id,
            expires_at=_now() - timedelta(days=2),
        )
        recommendation_id = seed_recommendation(connection, job_id=job_id, profile_id=profile_id)
        profile = build_profile(freshness={"max_age_days": 30, "drop_past_deadline": False})

        reconcile_recommendation_publication(
            connection,
            profile_id=profile_id,
            profile=profile,
            settings=_settings(),
            as_of=capture_as_of(),
            publication_mode="hosted",
        )

        assert _is_active(connection, recommendation_id) is True


def test_hard_rejection_outside_retrieval_window_is_deactivated(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(
            connection,
            exclusions={"hard_negative_titles_fi": ["myyntiedustaja"]},
        )
        source_id = seed_source(connection, name="enabled", enabled=True)
        job_id = seed_job(connection, title="Myyntiedustaja", source_id=source_id)
        recommendation_id = seed_recommendation(connection, job_id=job_id, profile_id=profile_id)
        profile = build_profile(
            exclusions={"hard_negative_titles_fi": ["myyntiedustaja"]}
        )

        result = reconcile_recommendation_publication(
            connection,
            profile_id=profile_id,
            profile=profile,
            settings=_settings(),
            as_of=capture_as_of(),
            publication_mode="hosted",
        )

        assert result["reconciled_hard_rejected"] == 1
        assert _is_active(connection, recommendation_id) is False


def test_hidden_feedback_stays_hidden_after_rank_refresh(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        source_id = seed_source(connection, name="enabled", enabled=True)
        job_id = seed_job(connection, source_id=source_id)
        recommendation_id = seed_recommendation(connection, job_id=job_id, profile_id=profile_id)
        connection.execute(
            sa.text(
                """
                insert into recommendation_feedback (
                    recommendation_id, rating, applied, job_id, scoring_snapshot, analysis_status, updated_at
                )
                values (:recommendation_id, 1, false, :job_id, '{}'::jsonb, 'pending', now())
                """
            ),
            {"recommendation_id": recommendation_id, "job_id": job_id},
        )
        profile = build_profile()

        reconcile_recommendation_publication(
            connection,
            profile_id=profile_id,
            profile=profile,
            settings=_settings(),
            as_of=capture_as_of(),
            publication_mode="hosted",
        )
        refresh_active_recommendation_ranks(
            connection, profile_id=profile_id, require_llm_review=True
        )

        assert _is_active(connection, recommendation_id) is False


def test_deterministic_only_mode_publishes_null_llm_rows_hosted_does_not(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        source_id = seed_source(connection, name="enabled", enabled=True)
        job_id = seed_job(connection, source_id=source_id)
        recommendation_id = seed_recommendation(
            connection,
            job_id=job_id,
            profile_id=profile_id,
            is_active=False,
            llm_score=None,
            suggested_action=None,
            fit_tier=None,
        )

        refresh_active_recommendation_ranks(
            connection, profile_id=profile_id, deterministic_only=True
        )
        assert _is_active(connection, recommendation_id) is True

        refresh_active_recommendation_ranks(
            connection,
            profile_id=profile_id,
            require_llm_review=True,
            deterministic_only=False,
        )
        assert _is_active(connection, recommendation_id) is False


def test_api_counts_and_items_use_the_same_eligibility_predicate(
    pg_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        enabled_source = seed_source(connection, name="enabled", enabled=True)
        disabled_source = seed_source(connection, name="disabled", enabled=False)
        fresh_job = seed_job(connection, source_id=enabled_source)
        stale_job = seed_job(
            connection, source_id=enabled_source, published_at=_now() - timedelta(days=45)
        )
        hidden_job = seed_job(connection, source_id=disabled_source)
        seed_recommendation(connection, job_id=fresh_job, profile_id=profile_id)
        seed_recommendation(connection, job_id=stale_job, profile_id=profile_id)
        seed_recommendation(connection, job_id=hidden_job, profile_id=profile_id)

    monkeypatch.setattr(main_module, "get_engine", lambda: pg_engine)
    client = TestClient(app)
    response = client.get("/recommendations?scope=nationwide&limit=50")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["scope_counts"]["nationwide"] == 1
    item_ids = [item["job_id"] for item in payload["items"]]
    assert item_ids == [fresh_job]
    # No routing origin address may appear in an API response.
    assert "travel_origin_address" not in payload["items"][0]


def test_structural_predicate_rejects_disabled_only_rows(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        disabled_source = seed_source(connection, name="disabled", enabled=False)
        job_id = seed_job(connection, source_id=disabled_source)
        seed_recommendation(connection, job_id=job_id, profile_id=profile_id)
        profile = build_profile()
        predicate, params = structural_eligibility_predicate(
            profile=profile, as_of=capture_as_of()
        )

        count = connection.execute(
            sa.text(
                f"""
                select count(*) from recommendations r
                join jobs j on j.id = r.job_id
                where r.profile_id = :profile_id and ({predicate})
                """
            ),
            {"profile_id": profile_id, **params},
        ).scalar_one()

        assert count == 0


def _settings() -> Any:
    from app.config import Settings

    return Settings(llm_provider="openai", openai_api_key="test-key", llm_prompt_version=8)


def test_review_backlog_inventory_freezes_stale_cohort(pg_engine: Any) -> None:
    from app.matching import inventory_review_backlog

    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        enabled_source = seed_source(connection, name="enabled", enabled=True)
        disabled_source = seed_source(connection, name="disabled", enabled=False)
        for _index in range(3):
            job_id = seed_job(connection, source_id=enabled_source)
            seed_recommendation(
                connection, job_id=job_id, profile_id=profile_id, is_active=True
            )
        # A disabled-only row must not count towards the eligible backlog.
        hidden_job = seed_job(connection, source_id=disabled_source)
        seed_recommendation(connection, job_id=hidden_job, profile_id=profile_id)

        first = inventory_review_backlog(
            connection, profile_id=profile_id, prompt_version=8
        )
        second = inventory_review_backlog(
            connection, profile_id=profile_id, prompt_version=8
        )

    assert first["eligible_total"] == 3
    assert first["stale_total"] == 3
    assert first["by_review_state"]["no_evaluation"] == 3
    assert first["by_scope"]["nationwide"] == 3
    assert first["cohort_hash"] == second["cohort_hash"]
    assert len(first["cohort_sample"]) == 3


def test_review_backfill_refuses_paid_execution_without_budget(pg_engine: Any) -> None:
    from app import matching as matching_module

    monkeypatch_engine = pg_engine

    original = matching_module.get_engine
    matching_module.get_engine = lambda: monkeypatch_engine  # type: ignore[assignment]
    try:
        with pg_engine.begin() as connection:
            seed_profile(connection)
        result = matching_module.run_review_backfill(dry_run=False, allow_paid=False, max_calls=0)
    finally:
        matching_module.get_engine = original  # type: ignore[assignment]

    assert result["status"] == "blocked"
    assert "authorization" in result["reason"]


def test_profile_import_preserves_learned_state_and_learner_cas(pg_engine: Any) -> None:
    from app.feedback_learning import save_profile
    from app.profile import profile_base_revision, upsert_single_profile

    imported = build_profile()
    with pg_engine.begin() as connection:
        profile_id = upsert_single_profile(connection, name="test", profile=imported)
        connection.execute(
            sa.text(
                """
                update job_seeker_profiles
                set profile = jsonb_set(profile, '{learned}', '{"version": 7}'::jsonb)
                where id = :profile_id
                """
            ),
            {"profile_id": profile_id},
        )
        upsert_single_profile(connection, name="test", profile=imported)
        row = connection.execute(
            sa.text("select profile from job_seeker_profiles where id = :profile_id"),
            {"profile_id": profile_id},
        ).scalar_one()

        assert row["learned"]["version"] == 7
        assert row["base_revision"] == profile_base_revision(imported)

        # A stale learner write must not overwrite a newer imported base.
        assert (
            save_profile(
                connection,
                profile_id=profile_id,
                profile={**imported, "learned": {"version": 8}},
                expected_base_revision="stale-revision",
            )
            is False
        )
        assert (
            save_profile(
                connection,
                profile_id=profile_id,
                profile={**imported, "learned": {"version": 8}},
                expected_base_revision=row["base_revision"],
            )
            is True
        )
        updated = connection.execute(
            sa.text("select profile from job_seeker_profiles where id = :profile_id"),
            {"profile_id": profile_id},
        ).scalar_one()
        assert updated["learned"]["version"] == 8


def test_llm_accounting_columns_exist(pg_engine: Any) -> None:
    expected = {
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "latency_ms",
        "attempts",
        "outcome",
        "provider_request_id",
        "usage_present",
    }
    with pg_engine.begin() as connection:
        for table in ("llm_evaluations", "feedback_llm_analyses"):
            columns = {
                str(row[0])
                for row in connection.execute(
                    sa.text(
                        """
                        select column_name
                        from information_schema.columns
                        where table_name = :table
                        """
                    ),
                    {"table": table},
                )
            }
            assert expected <= columns, (table, expected - columns)


def seed_evaluation(
    connection: Any,
    *,
    job_id: int,
    profile_id: int,
    prompt_version: int = 8,
    score: int = 80,
    request_hash: str | None = None,
) -> int:
    import hashlib
    import json as _json

    response = {
        "score": score,
        "fit_tier": "strong_fit",
        "rationale": "Sopii hyvin.",
        "concerns": [],
        "suggested_action": "apply",
    }
    return int(
        connection.execute(
            sa.text(
                """
                insert into llm_evaluations (
                    job_id, profile_id, provider, configured_model, returned_model,
                    prompt_version, schema_version, request_hash, response
                )
                values (
                    :job_id, :profile_id, 'openai', 'test-model', 'test-model',
                    :prompt_version, 1, :request_hash, CAST(:response AS jsonb)
                )
                returning id
                """
            ),
            {
                "job_id": job_id,
                "profile_id": profile_id,
                "prompt_version": prompt_version,
                "request_hash": request_hash
                or hashlib.sha256(f"{job_id}:{profile_id}:{prompt_version}".encode()).hexdigest(),
                "response": _json.dumps(response),
            },
        ).scalar_one()
    )


def _link_evaluation(connection: Any, recommendation_id: int, evaluation_id: int) -> None:
    connection.execute(
        sa.text(
            """
            update recommendations
            set llm_evaluation_id = :evaluation_id, llm_score = 80,
                suggested_action = 'apply', fit_tier = 'strong_fit'
            where id = :recommendation_id
            """
        ),
        {"recommendation_id": recommendation_id, "evaluation_id": evaluation_id},
    )


def test_rank_refresh_cannot_resurrect_a_hard_rejected_approval(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(
            connection, exclusions={"hard_negative_titles_fi": ["myyntiedustaja"]}
        )
        source_id = seed_source(connection, name="enabled", enabled=True)
        job_id = seed_job(connection, title="Myyntiedustaja", source_id=source_id)
        recommendation_id = seed_recommendation(connection, job_id=job_id, profile_id=profile_id)
        evaluation_id = seed_evaluation(connection, job_id=job_id, profile_id=profile_id)
        _link_evaluation(connection, recommendation_id, evaluation_id)
        profile = build_profile(exclusions={"hard_negative_titles_fi": ["myyntiedustaja"]})

        reconcile_recommendation_publication(
            connection,
            profile_id=profile_id,
            profile=profile,
            settings=_settings(),
            as_of=capture_as_of(),
            publication_mode="hosted",
            provider_name="openai",
            eval_model="test-model",
        )
        assert _is_active(connection, recommendation_id) is False

        refresh_active_recommendation_ranks(
            connection,
            profile_id=profile_id,
            require_llm_review=True,
            prompt_version=8,
        )
        assert _is_active(connection, recommendation_id) is False


def test_disabled_only_row_with_linked_approval_stays_inactive(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        source_id = seed_source(connection, name="disabled", enabled=False)
        job_id = seed_job(connection, source_id=source_id)
        recommendation_id = seed_recommendation(connection, job_id=job_id, profile_id=profile_id)
        evaluation_id = seed_evaluation(connection, job_id=job_id, profile_id=profile_id)
        _link_evaluation(connection, recommendation_id, evaluation_id)
        profile = build_profile()

        reconcile_recommendation_publication(
            connection,
            profile_id=profile_id,
            profile=profile,
            settings=_settings(),
            as_of=capture_as_of(),
            publication_mode="hosted",
            provider_name="openai",
            eval_model="test-model",
        )
        predicate, params = structural_eligibility_predicate(
            profile=profile, as_of=capture_as_of()
        )
        refresh_active_recommendation_ranks(
            connection,
            profile_id=profile_id,
            require_llm_review=True,
            prompt_version=8,
            extra_predicate=predicate,
            extra_params=params,
        )

        assert _is_active(connection, recommendation_id) is False


def test_inventory_counts_unreviewed_inactive_rows(pg_engine: Any) -> None:
    from app.matching import inventory_review_backlog

    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        source_id = seed_source(connection, name="enabled", enabled=True)
        job_id = seed_job(connection, source_id=source_id)
        seed_recommendation(
            connection, job_id=job_id, profile_id=profile_id, is_active=False, llm_score=None
        )

        inventory = inventory_review_backlog(
            connection, profile_id=profile_id, prompt_version=8
        )

    assert inventory["eligible_total"] == 1
    assert inventory["stale_total"] == 1
    assert inventory["by_publication"]["inactive"] == 1


def test_prompt_only_identity_change_is_compatible_but_model_change_is_not(
    pg_engine: Any,
) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        source_id = seed_source(connection, name="enabled", enabled=True)
        prompt_job = seed_job(connection, source_id=source_id)
        model_job = seed_job(connection, source_id=source_id)
        prompt_rec = seed_recommendation(connection, job_id=prompt_job, profile_id=profile_id)
        model_rec = seed_recommendation(connection, job_id=model_job, profile_id=profile_id)
        prompt_eval = seed_evaluation(
            connection, job_id=prompt_job, profile_id=profile_id, prompt_version=7
        )
        model_eval = seed_evaluation(
            connection, job_id=model_job, profile_id=profile_id, prompt_version=7
        )
        connection.execute(
            sa.text("update llm_evaluations set configured_model = 'old-model' where id = :id"),
            {"id": model_eval},
        )
        _link_evaluation(connection, prompt_rec, prompt_eval)
        _link_evaluation(connection, model_rec, model_eval)
        profile = build_profile()

        reconcile_recommendation_publication(
            connection,
            profile_id=profile_id,
            profile=profile,
            settings=_settings(),
            as_of=capture_as_of(),
            publication_mode="hosted",
            provider_name="openai",
            eval_model="test-model",
        )
        prompt_publication = connection.execute(
            sa.text(
                "select deterministic_result->'publication' from recommendations where id = :id"
            ),
            {"id": prompt_rec},
        ).scalar_one()
        model_publication = connection.execute(
            sa.text(
                "select deterministic_result->'publication' from recommendations where id = :id"
            ),
            {"id": model_rec},
        ).scalar_one()
        assert prompt_publication["compatible"] is True
        assert model_publication["compatible"] is False

        predicate, params = structural_eligibility_predicate(
            profile=profile, as_of=capture_as_of()
        )
        refresh_active_recommendation_ranks(
            connection,
            profile_id=profile_id,
            require_llm_review=True,
            prompt_version=8,
            extra_predicate=predicate,
            extra_params=params,
        )
        assert _is_active(connection, prompt_rec) is True
        assert _is_active(connection, model_rec) is False


def test_feedback_retry_columns_exist(pg_engine: Any) -> None:
    expected = {"analysis_attempts", "next_attempt_at", "analysis_reason"}
    with pg_engine.begin() as connection:
        columns = {
            str(row[0])
            for row in connection.execute(
                sa.text(
                    "select column_name from information_schema.columns where table_name = 'recommendation_feedback'"
                )
            )
        }
    assert expected <= columns, expected - columns


def test_job_sources_occurrence_identity_is_unique(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = seed_profile(connection)
        source_id = seed_source(connection, name="enabled", enabled=True)
        job_id = seed_job(connection, source_id=source_id)
        indexes = {
            str(row[0])
            for row in connection.execute(
                sa.text("select indexname from pg_indexes where tablename = 'job_sources'")
            )
        }
        assert "uq_job_sources_source_external_id" in indexes

        # The occurrence upsert is idempotent for the same external id.
        for _attempt in range(2):
            connection.execute(
                sa.text(
                    """
                    insert into job_sources (
                        job_id, source_id, raw_listing_id, external_id,
                        application_url, last_content_hash, last_seen_at
                    )
                    values (
                        :job_id, :source_id,
                        (select id from raw_listings where source_id = :source_id limit 1),
                        'identity-1', 'https://example.invalid/a', 'h', now()
                    )
                    on conflict (source_id, external_id) where external_id is not null
                    do update set
                        raw_listing_id = excluded.raw_listing_id,
                        application_url = excluded.application_url,
                        last_content_hash = excluded.last_content_hash,
                        last_seen_at = now()
                    """
                ),
                {"job_id": job_id, "source_id": source_id},
            )
        count = connection.execute(
            sa.text(
                "select count(*) from job_sources where source_id = :source_id and external_id = 'identity-1'"
            ),
            {"source_id": source_id},
        ).scalar_one()
        assert count == 1


def test_dedupe_expression_index_exists(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        indexes = {
            str(row[0])
            for row in connection.execute(
                sa.text("select indexname from pg_indexes where tablename = 'jobs'")
            )
        }
    assert "ix_jobs_dedupe_normalized" in indexes


def test_upsert_listing_stores_source_deadline_monotonically(pg_engine: Any) -> None:
    from datetime import datetime, timedelta, timezone

    from app.adapters.base import NormalizedListing
    from app.collection.runner import upsert_listing

    now = datetime.now(timezone.utc)

    def listing(*, expires_at: datetime | None, url: str, title: str = "Kirjastonhoitaja") -> Any:
        return NormalizedListing(
            external_id="deadline-1",
            canonical_source_url=url,
            title=title,
            employer="Kaupunki",
            description="Kirjasto.",
            location="Oulu",
            published_at=now - timedelta(days=1),
            content_hash="hash-1",
            payload={"source_url": url},
            application_url=url,
            expires_at=expires_at,
        )

    with pg_engine.begin() as connection:
        source_id = seed_source(connection, name="deadline_source", enabled=True)
        upsert_listing(connection, source_id, listing(expires_at=now + timedelta(days=10), url="https://x.invalid/a"))
        first = connection.execute(sa.text("select expires_at from jobs")).scalar_one()

        # An earlier deadline must not move the stored value backwards.
        upsert_listing(connection, source_id, listing(expires_at=now + timedelta(days=2), url="https://x.invalid/a"))
        second = connection.execute(sa.text("select expires_at from jobs")).scalar_one()

        # A later deadline replaces it.
        upsert_listing(connection, source_id, listing(expires_at=now + timedelta(days=20), url="https://x.invalid/a"))
        third = connection.execute(sa.text("select expires_at from jobs")).scalar_one()

    assert first is not None
    assert second == first
    assert third > second


def test_retention_report_runs_against_postgres(pg_engine: Any) -> None:
    from app.retention import retention_report

    with pg_engine.connect() as connection:
        report = retention_report(connection)

    assert report["dry_run"] is True
    assert report["table_sizes"]["jobs"]["total_bytes"] >= 0
    assert "raw_listings_not_seen_since_cutoff" in report["candidates"]
