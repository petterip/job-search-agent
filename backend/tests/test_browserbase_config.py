import os

from app.config import Settings, get_settings


def test_browserbase_settings_defaults(monkeypatch):
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    monkeypatch.delenv("ENRICHMENT_ENABLED", raising=False)
    monkeypatch.delenv("ENRICHMENT_PROVIDER", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.browserbase_api_key == ""
    assert settings.browserbase_configured() is False
    assert settings.enrichment_enabled is False
    assert settings.enrichment_provider == "browserbase"


def test_browserbase_settings_from_env(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb_live_test")
    monkeypatch.setenv("ENRICHMENT_ENABLED", "true")
    monkeypatch.setenv("ENRICHMENT_PROVIDER", "browserbase")
    monkeypatch.setenv("ENRICHMENT_MAX_JOBS_PER_RUN", "25")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.browserbase_configured() is True
    assert settings.enrichment_enabled is True
    assert settings.enrichment_max_jobs_per_run == 25


def test_browserbase_configured_helper():
    assert Settings(browserbase_api_key="  ").browserbase_configured() is False
    assert Settings(browserbase_api_key="bb_live_x").browserbase_configured() is True
