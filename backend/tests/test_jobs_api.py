from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient

from app import main as main_module
from app.main import app
from app.main import build_recommendation_category
from app.main import build_jobs_where_clause


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

    def mappings(self) -> list[dict[str, Any]]:
        return self.rows


class SourceStatusConnection:
    def __enter__(self) -> "SourceStatusConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, *args: Any, **kwargs: Any) -> MappingRows:
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


class FeedbackConnection:
    def __init__(self) -> None:
        self.calls = 0
        self.statements: list[str] = []

    def __enter__(self) -> "FeedbackConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, *args: Any, **kwargs: Any) -> ScalarResult:
        self.calls += 1
        self.statements.append(str(args[0]).lower())
        if self.calls == 1:
            return ScalarResult(1)
        return ScalarResult(42)


class FeedbackEngine:
    def __init__(self) -> None:
        self.connection = FeedbackConnection()

    def begin(self) -> FeedbackConnection:
        return self.connection


def test_get_job_returns_404_for_missing_job(monkeypatch: Any) -> None:
    monkeypatch.setattr(main_module, "get_engine", lambda: EmptyEngine())

    response = TestClient(app).get("/jobs/0")

    assert response.status_code == 404


def test_source_status_endpoint_returns_latest_run_summary(monkeypatch: Any) -> None:
    monkeypatch.setattr(main_module, "get_engine", lambda: SourceStatusEngine())

    response = TestClient(app).get("/sources/status")

    assert response.status_code == 200
    assert response.json()["sources"] == [
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
    ]


def test_recommendation_feedback_endpoint_creates_feedback(monkeypatch: Any) -> None:
    engine = FeedbackEngine()
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)

    response = TestClient(app).post("/recommendations/7/feedback?action=good_match")

    assert response.status_code == 200
    assert response.json() == {
        "id": 42,
        "recommendation_id": 7,
        "action": "good_match",
    }
    assert not any("set is_active = false" in statement for statement in engine.connection.statements)


def test_recommendation_feedback_not_relevant_hides_recommendation(monkeypatch: Any) -> None:
    engine = FeedbackEngine()
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)

    response = TestClient(app).post("/recommendations/7/feedback?action=not_relevant")

    assert response.status_code == 200
    assert any("set is_active = false" in statement for statement in engine.connection.statements)
