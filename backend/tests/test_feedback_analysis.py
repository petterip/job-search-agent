import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings, get_settings
from app import feedback_analysis as feedback_analysis_module
from support import with_privacy
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

    connection.execute = lambda *_args, **_kwargs: MappingRowsResult(  # type: ignore[method-assign]
        [
            {
                "analysis": {
                    "user_rating": 4,
                    "applied": False,
                    "system_alignment": "aligned",
                    "hypothesis_fi": "sopii",
                    "likely_positive_signals": [],
                    "likely_negative_signals": [],
                    "mismatch_drivers": [],
                    "suggested_actions": {},
                    "confidence": "high",
                }
            }
        ]
    )

    assert reuse_cached_analysis(connection, feedback_id=3, request_hash="abc") is True  # type: ignore[arg-type]
    assert reuse_cached_analysis(connection, feedback_id=3, request_hash="abc") is True  # type: ignore[arg-type]


def test_reuse_cached_analysis_rejects_unusable_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = RecordingConnection()
    monkeypatch.setattr(
        feedback_analysis_module,
        "mark_feedback_analysis_status",
        lambda *_args, **_kwargs: None,
    )
    connection.execute = lambda *_args, **_kwargs: MappingRowsResult(  # type: ignore[method-assign]
        [{"analysis": {"not": "a valid analysis"}}]
    )

    assert reuse_cached_analysis(connection, feedback_id=3, request_hash="abc") is False  # type: ignore[arg-type]


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

    def connect(self) -> "AnalyzeConnection":
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
        self.profile = with_privacy({"objective": "hallinto", "role_clusters": []})
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
        if "from recommendation_feedback rf" in sql and "join jobs" in sql:
            return MappingRowsResult(self.rows)
        if "from feedback_llm_analyses" in sql and "request_hash" in sql:
            return MappingRowsResult([])
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


def test_run_analyze_feedback_respects_privacy_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = AnalyzeConnection()
    connection.profile = with_privacy({"objective": "hallinto"}, llm_allowed=False)
    engine = AnalyzeEngine(connection)
    monkeypatch.setattr(feedback_analysis_module, "get_engine", lambda: engine)
    monkeypatch.setattr(
        feedback_analysis_module,
        "mark_feedback_analysis_status",
        lambda *_args, **_kwargs: None,
    )
    built: list[bool] = []

    def fake_build(_settings: Any) -> Any:
        built.append(True)
        return FakeProvider()

    monkeypatch.setattr(feedback_analysis_module, "build_feedback_analysis_provider", fake_build)

    result = run_analyze_feedback(limit=5)

    assert built == []
    assert result["skipped"] == 1
    assert result["completed"] == 0


def test_openai_feedback_provider_returns_normalized_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.feedback_analysis import OpenAIFeedbackAnalysisProvider

    class FakeUsage:
        def model_dump(self) -> dict[str, Any]:
            return {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 40}}

    class FakeResponse:
        output_text = json.dumps(
            {
                "user_rating": 4,
                "applied": False,
                "system_alignment": "aligned",
                "hypothesis_fi": "sopii",
                "likely_positive_signals": [],
                "likely_negative_signals": [],
                "mismatch_drivers": [],
                "suggested_actions": {},
                "confidence": "high",
            }
        )
        model = "openai-returned"
        usage = FakeUsage()
        id = "req-1"

    class FakeResponses:
        def create(self, **_kwargs: Any) -> Any:
            return FakeResponse()

    class FakeOpenAI:
        def __init__(self, api_key: str, max_retries: int) -> None:  # noqa: ARG002
            self.responses = FakeResponses()

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    provider = OpenAIFeedbackAnalysisProvider(api_key="test-key")

    _analysis, metadata = provider.analyze_feedback(
        profile_summary="{}",
        job_summary_text="{}",
        feedback_context="{}",
        model="test-model",
        prompt_version=1,
    )

    assert metadata["usage_normalized"]["input_tokens"] == 100
    assert metadata["usage_normalized"]["output_tokens"] == 20
    assert metadata["usage_normalized"]["cached_tokens"] == 40
    assert metadata["usage_normalized"]["usage_present"] is True
    assert metadata["request_id"] == "req-1"
    assert metadata["attempts"] == 1


def test_transient_provider_failure_schedules_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.feedback_analysis import EvaluationProviderUnavailable, analyze_feedback_row
    from app.config import get_settings

    connection = AnalyzeConnection()
    engine = AnalyzeEngine(connection)
    captured: dict[str, Any] = {}

    def fake_mark(*_args: Any, **kwargs: Any) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setattr(feedback_analysis_module, "_mark_status_if_current", fake_mark)
    monkeypatch.setattr(
        feedback_analysis_module,
        "mark_feedback_analysis_unavailable",
        lambda *_args, **_kwargs: None,
    )

    class FailingProvider:
        provider_name = "openai"

        def analyze_feedback(self, **_kwargs: Any) -> Any:
            raise EvaluationProviderUnavailable("quota")

    row = {
        **connection.rows[0],
        "analysis_attempts": 1,
        "updated_at": datetime.now(timezone.utc),
    }
    outcome = analyze_feedback_row(
        row,
        provider=FailingProvider(),
        settings=get_settings(),
        profile=connection.profile,
        engine=engine,
    )

    assert outcome == "deferred"
    assert captured["status"] == "pending"
    assert captured["reason"] == "provider_unavailable"
    assert captured["schedule_retry_at"] is not None
    assert captured["count_attempt"] is True


def test_attempt_budget_exhaustion_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.feedback_analysis import EvaluationProviderUnavailable, analyze_feedback_row
    from app.config import Settings

    connection = AnalyzeConnection()
    engine = AnalyzeEngine(connection)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        feedback_analysis_module,
        "_mark_status_if_current",
        lambda *_a, **kw: (captured.update(kw), True)[1],
    )
    monkeypatch.setattr(
        feedback_analysis_module,
        "mark_feedback_analysis_unavailable",
        lambda *_args, **_kwargs: None,
    )

    class FailingProvider:
        provider_name = "openai"

        def analyze_feedback(self, **_kwargs: Any) -> Any:
            raise EvaluationProviderUnavailable("quota")

    row = {
        **connection.rows[0],
        "analysis_attempts": 99,
        "updated_at": datetime.now(timezone.utc),
    }
    outcome = analyze_feedback_row(
        row,
        provider=FailingProvider(),
        settings=Settings(
            llm_provider="openai",
            openai_api_key="k",
            feedback_analysis_max_attempts=5,
        ),
        profile=connection.profile,
        engine=engine,
    )

    assert outcome == "skipped"
    assert captured["status"] == "skipped"
    assert captured["reason"] == "attempt_budget_exhausted"
    assert captured["schedule_retry_at"] is None
