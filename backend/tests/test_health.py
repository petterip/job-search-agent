from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_service_status() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "job-search-agent"
    assert body["embedding_dimension"] == 1536
    assert body["eval_model"]
    assert "transit_distance_enabled" in body


def test_health_reports_configuration_warnings(monkeypatch) -> None:
    from app import main as main_module
    from app.config import Settings

    broken = Settings(llm_provider="bogus", openai_api_key="")
    monkeypatch.setattr(main_module, "get_settings", lambda: broken)

    body = TestClient(app).get("/health").json()

    assert body["status"] == "ok"
    assert any("unsupported" in problem for problem in body["config_warnings"])


def test_health_has_no_warnings_for_a_valid_or_offline_configuration() -> None:
    from app.config import Settings

    assert Settings(llm_provider="", openai_api_key="").llm_config_problems() == []
    assert (
        Settings(
            llm_provider="openai",
            openai_api_key="key",
            openai_eval_model="gpt-5.4-nano",
            llm_eval_concurrency=4,
        ).llm_config_problems()
        == []
    )


def test_llm_config_problems_are_actionable() -> None:
    from app.config import Settings

    assert (
        "OPENAI_API_KEY"
        in Settings(llm_provider="openai", openai_api_key="").llm_config_problems()[0]
    )
    assert (
        "GEMINI_API_KEY"
        in Settings(llm_provider="gemini", gemini_api_key="").llm_config_problems()[0]
    )
    problems = Settings(
        llm_provider="openai",
        openai_api_key="key",
        llm_eval_concurrency=0,
        llm_eval_parse_retries=-1,
        llm_eval_max_jobs=-5,
    ).llm_config_problems()
    assert any("LLM_EVAL_CONCURRENCY" in problem for problem in problems)
    assert any("LLM_EVAL_PARSE_RETRIES" in problem for problem in problems)
    assert any("LLM_EVAL_MAX_JOBS" in problem for problem in problems)
