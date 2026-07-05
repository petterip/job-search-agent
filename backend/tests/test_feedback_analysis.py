import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings, get_settings
from app import feedback_analysis as feedback_analysis_module
from app.feedback_analysis import (
    FeedbackAnalysis,
    SuggestedActions,
    analysis_request_hash,
    apply_grounding_guard,
    feedback_analysis_in_cooldown,
    feedback_content_hash,
    mark_feedback_analysis_unavailable,
    reuse_cached_analysis,
    run_analyze_feedback,
    sanitize_feedback_comment,
)


def test_feedback_analysis_schema_validation() -> None:
    analysis = FeedbackAnalysis.model_validate(
        {
            "user_rating": 1,
            "applied": False,
            "system_alignment": "over_ranked",
            "hypothesis_fi": "Myyntipainotteinen rooli ei vastaa profiilia.",
            "likely_positive_signals": ["hallinto"],
            "likely_negative_signals": ["myynti"],
            "mismatch_drivers": [
                {"driver": "role_family", "detail": "sales-heavy duties"},
            ],
            "suggested_actions": {
                "boost_terms_fi": [],
                "exclude_terms_fi": ["myynti"],
                "boost_sectors": [],
                "exclude_sectors": [],
                "discovery_queries_add": [],
                "discovery_queries_remove": [],
                "lane_notes": "",
                "llm_eval_hint": "",
            },
            "confidence": "high",
        }
    )

    assert analysis.system_alignment == "over_ranked"
    assert analysis.suggested_actions.exclude_terms_fi == ["myynti"]


def test_sanitize_feedback_comment_strips_email_and_phone() -> None:
    sanitized = sanitize_feedback_comment(
        "Ota yhteyttä osoitteeseen test@example.com tai 040 123 4567",
    )

    assert sanitized is not None
    assert "test@example.com" not in sanitized
    assert "040 123 4567" not in sanitized


def test_sanitize_feedback_comment_strips_unlisted_personal_names() -> None:
    sanitized = sanitize_feedback_comment(
        "Tapasin Matti Meikäläisen ja hän sanoi että rooli ei sovi.",
        profile={"objective": "Kirjastonhoitaja Oulussa"},
    )

    assert sanitized is not None
    assert "Matti" not in sanitized
    assert "Meikäläisen" not in sanitized
    assert "[nimi poistettu]" in sanitized


def test_grounding_guard_rejects_ungrounded_terms() -> None:
    analysis = FeedbackAnalysis(
        user_rating=1,
        applied=False,
        system_alignment="over_ranked",
        hypothesis_fi="Testi",
        likely_positive_signals=[],
        likely_negative_signals=[],
        mismatch_drivers=[],
        suggested_actions=SuggestedActions(
            boost_terms_fi=["hallinto"],
            exclude_terms_fi=["myynti", "keksittysana"],
        ),
        confidence="medium",
    )
    snapshot = {
        "title": "Hallintosihteeri",
        "keyword_matches": ["hallinto"],
        "negative_matches": ["myynti"],
    }

    grounded = apply_grounding_guard(
        analysis,
        snapshot=snapshot,
        profile_summary='{"objective":"hallinto"}',
    )

    assert grounded.suggested_actions.boost_terms_fi == ["hallinto"]
    assert grounded.suggested_actions.exclude_terms_fi == ["myynti"]


def test_analysis_request_hash_changes_when_rating_changes() -> None:
    snapshot = {"title": "Kirjastonhoitaja", "keyword_matches": ["kirjasto"]}
    base_kwargs = {
        "profile_summary": "{}",
        "job_summary_text": '{"title":"Kirjastonhoitaja"}',
        "scoring_snapshot": snapshot,
        "applied": False,
        "sanitized_comment": None,
        "prompt_version": 1,
    }
    first = analysis_request_hash(rating=4, **base_kwargs)
    second = analysis_request_hash(rating=5, **base_kwargs)

    assert first != second


def test_feedback_content_hash_is_stable_for_identical_payload() -> None:
    snapshot = {"title": "Kirjastonhoitaja"}
    first = feedback_content_hash(rating=4, applied=False, comment=None, scoring_snapshot=snapshot)
    second = feedback_content_hash(rating=4, applied=False, comment=None, scoring_snapshot=snapshot)

    assert first == second


class ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, Any]] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.statements.append(str(statement).lower())
        self.params.append(params or {})
        return self


def test_reuse_cached_analysis_marks_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = RecordingConnection()
    monkeypatch.setattr(
        feedback_analysis_module,
        "mark_feedback_analysis_status",
        lambda *_args, **_kwargs: None,
    )

    connection.execute = lambda *_args, **_kwargs: ScalarResult(9)  # type: ignore[method-assign]

    assert reuse_cached_analysis(connection, feedback_id=3, request_hash="abc") is True  # type: ignore[arg-type]


def test_feedback_analysis_cooldown_is_isolated_from_job_fit_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(storage_dir=str(tmp_path), llm_provider="openai")
    mark_feedback_analysis_unavailable(settings, "quota")

    assert feedback_analysis_in_cooldown(settings) is True
    assert not (tmp_path / "llm_provider_unavailable.json").exists()


class FakeProvider:
    provider_name = "openai"

    def __init__(self) -> None:
        self.calls = 0

    def analyze_feedback(self, **kwargs: Any) -> tuple[FeedbackAnalysis, dict[str, Any]]:
        self.calls += 1
        return (
            FeedbackAnalysis(
                user_rating=4,
                applied=False,
                system_alignment="aligned",
                hypothesis_fi="Sopii profiiliin.",
                likely_positive_signals=["hallinto"],
                likely_negative_signals=[],
                mismatch_drivers=[],
                suggested_actions=SuggestedActions(),
                confidence="high",
            ),
            {"returned_model": "test-model", "prompt_version": 1},
        )


class AnalyzeEngine:
    def __init__(self, connection: "AnalyzeConnection") -> None:
        self.connection = connection

    def begin(self) -> "AnalyzeConnection":
        return self.connection


class MappingRowsResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> "MappingRowsResult":
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Any:
        return iter(self.rows)


class AnalyzeConnection:
    def __init__(self) -> None:
        self.rows = [
            {
                "id": 1,
                "recommendation_id": 7,
                "job_id": 99,
                "rating": 4,
                "applied": False,
                "comment": None,
                "scoring_snapshot": {
                    "title": "Hallintosihteeri",
                    "keyword_matches": ["hallinto"],
                    "machine_score": 60,
                },
                "title": "Hallintosihteeri",
                "employer": "Kaupunki",
                "location": "Oulu",
                "description": "Hallintoa",
            }
        ]
        self.profile = {"objective": "hallinto", "role_clusters": []}
        self.committed = False

    def __enter__(self) -> "AnalyzeConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        self.committed = True
        return None

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement).lower()
        if "from job_seeker_profiles" in sql:
            return MappingRowsResult([{"profile": self.profile}])
        if "analysis_status = 'pending'" in sql:
            return MappingRowsResult(self.rows)
        if "from feedback_llm_analyses" in sql and "request_hash" in sql:
            return ScalarResult(None)
        return MappingRowsResult([])


def test_run_analyze_feedback_calls_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    connection = AnalyzeConnection()
    engine = AnalyzeEngine(connection)
    monkeypatch.setattr(feedback_analysis_module, "get_engine", lambda: engine)
    monkeypatch.setattr(feedback_analysis_module, "build_feedback_analysis_provider", lambda _settings: provider)
    monkeypatch.setattr(feedback_analysis_module, "configured_eval_model", lambda *_args, **_kwargs: "test-model")
    monkeypatch.setattr(
        feedback_analysis_module,
        "store_feedback_analysis",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        feedback_analysis_module,
        "mark_feedback_analysis_status",
        lambda *_args, **_kwargs: None,
    )

    result = run_analyze_feedback(limit=5)

    assert result["completed"] == 1
    assert provider.calls == 1


def test_run_analyze_feedback_skips_when_provider_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = AnalyzeConnection()
    engine = AnalyzeEngine(connection)
    monkeypatch.setattr(feedback_analysis_module, "get_engine", lambda: engine)
    monkeypatch.setattr(feedback_analysis_module, "build_feedback_analysis_provider", lambda _settings: None)
    monkeypatch.setattr(
        feedback_analysis_module,
        "mark_feedback_analysis_status",
        lambda *_args, **_kwargs: None,
    )

    result = run_analyze_feedback(limit=5)

    assert result["skipped"] == 1
