from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient

from app import main as main_module
from app.main import app
from app.main import build_recommendation_category
from app.main import build_jobs_where_clause
from app.main import build_scoring_snapshot
from app.main import resolve_feedback_submission


def test_jobs_where_clause_filters_keyword_and_enabled_source_visibility() -> None:
    where_clause, params = build_jobs_where_clause(
        query="data",
        source=None,
        employer=None,
        location=None,
        published_after=None,
    )

    assert "j.title ilike :query" in where_clause
    assert "visible_s.enabled = true" in where_clause
    assert params == {"query": "%data%"}


def test_jobs_where_clause_filters_source_employer_and_publication_date() -> None:
    published_after = datetime(2026, 6, 1, tzinfo=timezone.utc)

    where_clause, params = build_jobs_where_clause(
        query=None,
        source="duunitori",
        employer="Oulun yliopisto",
        location="Oulu",
        published_after=published_after,
    )

    assert "filter_s.name = :source" in where_clause
    assert "coalesce(j.employer, '') ilike :employer" in where_clause
    assert "coalesce(j.location, '') ilike :location" in where_clause
    assert "j.published_at >= :published_after" in where_clause
    assert params == {
        "source": "duunitori",
        "employer": "%Oulun yliopisto%",
        "location": "%Oulu%",
        "published_after": published_after,
    }


def test_recommendation_category_prioritizes_library_work() -> None:
    category, hidden = build_recommendation_category(
        title="Kirjastonhoitaja",
        employer="Kaupunki",
        deterministic_result={"title_matches": ["kirjastonhoitaja"], "hidden_opportunity": False},
    )

    assert category == "Kirjasto ja tietopalvelu"
    assert hidden is False


def test_recommendation_category_detects_leadership_and_admin_strengths() -> None:
    category, hidden = build_recommendation_category(
        title="Hallintosihteeri",
        employer="Seurakuntayhtymä",
        deterministic_result={"keyword_matches": ["hallinto", "tapahtuma"], "candidate_lanes": ["application_history"]},
    )

    assert category == "Johtaminen ja hallinto"
    assert hidden is False


def test_recommendation_category_keeps_hidden_opportunity_visible() -> None:
    category, hidden = build_recommendation_category(
        title="Projektisuunnittelija",
        employer="Säätiö",
        deterministic_result={"candidate_lanes": ["exploration"], "hidden_opportunity": True},
    )

    assert category == "Piilo-osumat"
    assert hidden is True


def test_recommendation_category_does_not_hide_communications_role_under_library_keywords() -> None:
    category, hidden = build_recommendation_category(
        title="Viestintäasiantuntija viestintäyksikköön",
        employer="Tiedekirjasto",
        deterministic_result={
            "keyword_matches": ["kirjasto", "viestintä"],
            "candidate_lanes": ["exploration"],
            "hidden_opportunity": True,
        },
    )

    assert category == "Kulttuuri, tapahtumat ja viestintä"
    assert hidden is True


def test_recommendation_category_highlights_music_literature_and_publication_strengths() -> None:
    category, hidden = build_recommendation_category(
        title="Kulttuurisisältöjen koordinaattori",
        employer="Kulttuurikeskus",
        deterministic_result={
            "keyword_matches": ["musiikki", "viulu", "julkaisu", "kirjoittaminen"],
            "candidate_lanes": ["exploration"],
            "hidden_opportunity": True,
        },
    )

    assert category == "Musiikki, kirjallisuus ja sisällöt"
    assert hidden is True


class EmptyResult:
    def mappings(self) -> "EmptyResult":
        return self

    def one_or_none(self) -> None:
        return None


class EmptyConnection:
    def __enter__(self) -> "EmptyConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, *args: Any, **kwargs: Any) -> EmptyResult:
        return EmptyResult()


class EmptyEngine:
    def connect(self) -> EmptyConnection:
        return EmptyConnection()


class MappingRows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> "MappingRows":
        return self

    def __iter__(self):
        return iter(self.rows)

    def one_or_none(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def one(self) -> dict[str, Any]:
        assert self.rows
        return self.rows[0]


class SourceStatusConnection:
    def __enter__(self) -> "SourceStatusConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, statement: object, *args: Any, **kwargs: Any) -> MappingRows:
        sql = str(statement).lower()
        if "from enrichment_runs" in sql:
            return MappingRows(
                [
                    {
                        "id": 7,
                        "status": "completed",
                        "started_at": datetime(2026, 6, 20, 11, 0, tzinfo=timezone.utc),
                        "finished_at": datetime(2026, 6, 20, 11, 1, tzinfo=timezone.utc),
                        "queued_count": 3,
                        "processed_count": 3,
                        "applied_count": 2,
                        "failed_count": 0,
                        "error_summary": None,
                    }
                ]
            )
        if "from enrichment_queue" in sql:
            return MappingRows(
                [
                    {
                        "queued_count": 2,
                        "running_count": 1,
                        "retry_count": 1,
                        "failed_count": 0,
                        "stale_running_count": 1,
                        "oldest_due_at": datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),
                    }
                ]
            )
        return MappingRows(
            [
                {
                    "name": "duunitori",
                    "enabled": True,
                    "poll_interval_min": 5,
                    "active_jobs": 32,
                    "stored_listings": 32,
                    "last_run_status": "succeeded",
                    "last_run_started_at": datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc),
                    "last_run_finished_at": datetime(2026, 6, 20, 10, 1, tzinfo=timezone.utc),
                    "fetched_count": 10,
                    "inserted_count": 2,
                    "updated_count": 1,
                    "unchanged_count": 7,
                    "failed_count": 0,
                    "error_summary": None,
                    "recent_error_events": 0,
                    "recent_warning_events": 1,
                }
            ]
        )


class SourceStatusEngine:
    def connect(self) -> SourceStatusConnection:
        return SourceStatusConnection()


class ScalarResult:
    def __init__(self, value: int | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> int | None:
        return self.value

    def scalar_one(self) -> int:
        assert self.value is not None
        return self.value


class MappingOneRow:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    def mappings(self) -> "MappingOneRow":
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self.row


class FeedbackConnection:
    def __init__(self, *, recommendation: dict[str, Any] | None = None, feedback_row: dict[str, Any] | None = None) -> None:
        self.calls = 0
        self.statements: list[str] = []
        self.last_params: dict[str, Any] | None = None
        self.recommendation = recommendation or {
            "id": 7,
            "job_id": 99,
            "rank": 3,
            "machine_score": 62.0,
            "vector_score": 0.71,
            "llm_score": 78,
            "fit_tier": "transferable_weaker",
            "suggested_action": "consider",
            "deterministic_result": {
                "candidate_lanes": ["application_history"],
                "keyword_matches": ["hallinto"],
                "location_matches": ["oulu"],
            },
            "rationale": "Sopii hallintokokemukseen",
            "concerns": ["etäisyys"],
            "is_active": True,
            "title": "Palveluassistentti",
            "employer": "Oulun kaupunki",
            "location": "Oulu",
        }
        self.feedback_row = feedback_row

    def __enter__(self) -> "FeedbackConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        self.statements.append(str(args[0]).lower())
        if len(args) > 1 and isinstance(args[1], dict):
            self.last_params = args[1]
        elif kwargs.get("parameters"):
            self.last_params = kwargs["parameters"]
        sql = str(args[0]).lower()
        if "from recommendations r" in sql and "join jobs j" in sql and "where r.id" in sql:
            return MappingOneRow(self.recommendation)
        if "from job_seeker_profiles" in sql:
            return MappingOneRow({"profile": {"objective": "Kirjastonhoitaja"}})
        if "from recommendation_feedback" in sql and (
            "where recommendation_id" in sql or "where rf.recommendation_id" in sql
        ):
            return MappingOneRow(self.feedback_row)
        if "returning id" in sql:
            return ScalarResult(42)
        return ScalarResult(1)


class FeedbackEngine:
    def __init__(self, *, recommendation: dict[str, Any] | None = None, feedback_row: dict[str, Any] | None = None) -> None:
        self.connection = FeedbackConnection(recommendation=recommendation, feedback_row=feedback_row)

    def begin(self) -> FeedbackConnection:
        return self.connection

    def connect(self) -> FeedbackConnection:
        return self.connection


class RecommendationListConnection:
    def __init__(self) -> None:
        self.params: list[dict[str, Any] | None] = []
        self.statements: list[str] = []

    def __enter__(self) -> "RecommendationListConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, statement: object, parameters: dict[str, Any] | None = None) -> Any:
        sql = str(statement).lower()
        self.statements.append(sql)
        self.params.append(parameters)
        if "count(*) filter" in sql:
            return MappingRows(
                [
                    {
                        "commutable": 3,
                        "commutable_or_full_remote": 5,
                        "nationwide": 8,
                    }
                ]
            )
        if "select count(*)" in sql:
            return ScalarResult(0)
        return MappingRows([])

    def commit(self) -> None:
        return None


class RecommendationListEngine:
    def __init__(self) -> None:
        self.connection = RecommendationListConnection()

    def connect(self) -> RecommendationListConnection:
        return self.connection


def test_get_job_returns_404_for_missing_job(monkeypatch: Any) -> None:
    monkeypatch.setattr(main_module, "get_engine", lambda: EmptyEngine())

    response = TestClient(app).get("/jobs/0")

    assert response.status_code == 404


def test_source_status_endpoint_returns_latest_run_summary(monkeypatch: Any) -> None:
    monkeypatch.setattr(main_module, "get_engine", lambda: SourceStatusEngine())

    response = TestClient(app).get("/sources/status")

    assert response.status_code == 200
    assert response.json() == {
        "sources": [
            {
                "name": "duunitori",
                "enabled": True,
                "poll_interval_min": 5,
                "active_jobs": 32,
                "stored_listings": 32,
                "last_run_status": "succeeded",
                "last_run_started_at": "2026-06-20T10:00:00Z",
                "last_run_finished_at": "2026-06-20T10:01:00Z",
                "fetched_count": 10,
                "inserted_count": 2,
                "updated_count": 1,
                "unchanged_count": 7,
                "failed_count": 0,
                "error_summary": None,
                "recent_error_events": 0,
                "recent_warning_events": 1,
            }
        ],
        "enrichment_last_run": {
            "id": 7,
            "status": "completed",
            "started_at": "2026-06-20T11:00:00Z",
            "finished_at": "2026-06-20T11:01:00Z",
            "queued_count": 3,
            "processed_count": 3,
            "applied_count": 2,
            "failed_count": 0,
            "error_summary": None,
        },
        "enrichment_queue": {
            "queued_count": 2,
            "running_count": 1,
            "retry_count": 1,
            "failed_count": 0,
            "stale_running_count": 1,
            "oldest_due_at": "2026-06-20T12:00:00Z",
        },
    }


def test_recommendation_feedback_sanitizes_comment_before_storage(monkeypatch: Any) -> None:
    engine = FeedbackEngine()
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)

    response = TestClient(app).post(
        "/recommendations/7/feedback",
        json={"rating": 3, "applied": False, "comment": "Soitto Matti Meikäläiselle test@example.com"},
    )

    assert response.status_code == 200
    insert_sql = next(
        statement for statement in engine.connection.statements if "on conflict (recommendation_id)" in statement
    )
    params = engine.connection.last_params
    assert params is not None
    assert "Matti" not in str(params.get("comment") or "")
    assert "test@example.com" not in str(params.get("comment") or "")


def test_recommendation_feedback_endpoint_creates_feedback(monkeypatch: Any) -> None:
    engine = FeedbackEngine()
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)

    response = TestClient(app).post(
        "/recommendations/7/feedback",
        json={"rating": 4, "applied": False},
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": 42,
        "recommendation_id": 7,
        "rating": 4,
        "applied": False,
        "analysis_status": "pending",
        "recommendation_hidden": False,
        "action": "good_match",
    }
    assert any("on conflict (recommendation_id)" in statement for statement in engine.connection.statements)
    assert any("set is_active = true" in statement for statement in engine.connection.statements)


def test_recommendation_feedback_rating_one_hides_recommendation(monkeypatch: Any) -> None:
    engine = FeedbackEngine()
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)

    response = TestClient(app).post(
        "/recommendations/7/feedback",
        json={"rating": 1, "applied": False},
    )

    assert response.status_code == 200
    assert response.json()["recommendation_hidden"] is True
    assert any("set is_active = false" in statement for statement in engine.connection.statements)


def test_recommendation_feedback_rating_two_hides_recommendation(monkeypatch: Any) -> None:
    engine = FeedbackEngine()
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)

    response = TestClient(app).post(
        "/recommendations/7/feedback",
        json={"rating": 2, "applied": False},
    )

    assert response.status_code == 200
    assert response.json()["recommendation_hidden"] is True
    assert any("set is_active = false" in statement for statement in engine.connection.statements)


def test_recommendation_feedback_rerate_unhides_recommendation(monkeypatch: Any) -> None:
    engine = FeedbackEngine()
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)

    response = TestClient(app).post(
        "/recommendations/7/feedback",
        json={"rating": 4, "applied": False},
    )

    assert response.status_code == 200
    assert any("set is_active = true" in statement for statement in engine.connection.statements)
    assert any("set nationwide_rank" in statement for statement in engine.connection.statements)


def test_recommendation_feedback_legacy_action_mapping(monkeypatch: Any) -> None:
    engine = FeedbackEngine()
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)

    response = TestClient(app).post("/recommendations/7/feedback?action=applied")

    assert response.status_code == 200
    assert response.json()["rating"] == 5
    assert response.json()["applied"] is True


def test_recommendation_feedback_rejects_applied_with_low_rating(monkeypatch: Any) -> None:
    engine = FeedbackEngine()
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)

    response = TestClient(app).post(
        "/recommendations/7/feedback",
        json={"rating": 2, "applied": True},
    )

    assert response.status_code == 422


def test_get_recommendation_feedback_returns_latest_verdict(monkeypatch: Any) -> None:
    engine = FeedbackEngine(
        feedback_row={
            "id": 42,
            "recommendation_id": 7,
            "rating": 5,
            "applied": True,
            "analysis_status": "pending",
            "action": "applied",
        }
    )
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)

    response = TestClient(app).get("/recommendations/7/feedback")

    assert response.status_code == 200
    assert response.json()["rating"] == 5
    assert response.json()["applied"] is True


def test_build_scoring_snapshot_includes_travel_scope_fields(monkeypatch: Any) -> None:
    settings = main_module.get_settings()
    snapshot = build_scoring_snapshot(
        recommendation={
            "id": 88,
            "job_id": 1234,
            "rank": 7,
            "commutable": True,
            "full_remote": False,
            "commutable_or_full_remote": True,
            "commutable_rank": 2,
            "commutable_or_full_remote_rank": 2,
            "nationwide_rank": 5,
            "travel_status": "within_limit",
            "travel_reason_code": "transit_within_limit",
            "travel_duration_seconds": 3600,
            "travel_distance_km": 50,
            "travel_origin_address": "Jalkatie 2, Oulu, Finland",
            "travel_commute_limit_minutes": 120,
            "travel_routing_profile": "weekday_0800_public_transit",
            "machine_score": 62.0,
            "vector_score": 0.71,
            "llm_score": 78,
            "fit_tier": "transferable_weaker",
            "suggested_action": "consider",
            "deterministic_result": {
                "candidate_lanes": ["application_history"],
                "keyword_matches": ["hallinto"],
                "location_matches": ["oulu"],
                "hidden_opportunity": False,
                "travel_assessment": {"commutable": True, "full_remote": False},
            },
            "rationale": "Sopii hallintokokemukseen",
            "concerns": ["etäisyys"],
            "is_active": True,
            "title": "Palveluassistentti",
            "employer": "Oulun kaupunki",
            "location": "Oulu",
        },
        settings=settings,
    )

    assert snapshot["commutable"] is True
    assert snapshot["full_remote"] is False
    assert snapshot["commutable_or_full_remote"] is True
    assert snapshot["commutable_rank"] == 2
    assert snapshot["travel_assessment"]["commutable"] is True


def test_recommendation_scope_sql_maps_filters_and_order() -> None:
    from app.main import recommendation_scope_sql

    commutable_filter, commutable_order = recommendation_scope_sql("commutable")
    remote_filter, remote_order = recommendation_scope_sql("commutable_or_full_remote")
    nationwide_filter, nationwide_order = recommendation_scope_sql("nationwide")

    assert "r.commutable = true" in commutable_filter
    assert "r.travel_reason_code = 'exact_home_city'" in commutable_filter
    assert "r.travel_origin_address = :travel_origin_address" in commutable_filter
    assert "r.commutable_rank" in commutable_order
    assert "r.commutable_or_full_remote = true" in remote_filter
    assert "r.travel_reason_code in ('exact_home_city', 'full_remote')" in remote_filter
    assert "r.travel_commute_limit_minutes = :travel_commute_limit_minutes" in remote_filter
    assert "r.commutable = false and r.full_remote = true" in remote_order
    assert "r.commutable_or_full_remote_rank" in remote_order
    assert nationwide_filter == "true"
    assert "r.commutable_or_full_remote = false" in nationwide_order
    assert "r.nationwide_rank" in nationwide_order


def test_recommendations_scope_filters_use_current_travel_policy(monkeypatch: Any) -> None:
    engine = RecommendationListEngine()
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)
    monkeypatch.setenv("TRANSIT_ORIGIN_ADDRESS", "Testikatu 1, Oulu, Finland")
    monkeypatch.setenv("RECOMMENDATION_COMMUTE_LIMIT_MINUTES", "90")
    main_module.get_settings.cache_clear()

    response = TestClient(app).get("/recommendations?scope=commutable_or_full_remote")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 0
    assert payload["scope_counts"] == {
        "commutable": 3,
        "commutable_or_full_remote": 5,
        "nationwide": 8,
        "remote_only": 2,
        "nationwide_extra": 3,
    }
    assert any("travel_origin_address" in statement for statement in engine.connection.statements)
    count_params = engine.connection.params[0]
    assert count_params is not None
    assert count_params["travel_origin_address"] == "Testikatu 1, Oulu, Finland"
    assert count_params["travel_commute_limit_minutes"] == 90
    main_module.get_settings.cache_clear()

def test_build_scoring_snapshot_includes_rank_and_rationale(monkeypatch: Any) -> None:
    settings = main_module.get_settings()
    snapshot = build_scoring_snapshot(
        recommendation={
            "id": 88,
            "job_id": 1234,
            "rank": 7,
            "machine_score": 62.0,
            "vector_score": 0.71,
            "llm_score": 78,
            "fit_tier": "transferable_weaker",
            "suggested_action": "consider",
            "deterministic_result": {
                "candidate_lanes": ["application_history"],
                "keyword_matches": ["hallinto"],
                "location_matches": ["oulu"],
                "hidden_opportunity": False,
            },
            "rationale": "Sopii hallintokokemukseen",
            "concerns": ["etäisyys"],
            "is_active": True,
            "title": "Palveluassistentti",
            "employer": "Oulun kaupunki",
            "location": "Oulu",
        },
        settings=settings,
    )

    assert snapshot["rank"] == 7
    assert snapshot["rationale"] == "Sopii hallintokokemukseen"
    assert snapshot["keyword_matches"] == ["hallinto"]
    assert snapshot["llm_prompt_version"] == settings.llm_prompt_version


def test_resolve_feedback_submission_defaults_applied_without_rating() -> None:
    rating, applied, comment, action = resolve_feedback_submission(
        main_module.RecommendationFeedbackBody(applied=True),
        legacy_action=None,
    )

    assert rating == 5
    assert applied is True
    assert action == "applied"
    assert comment is None
