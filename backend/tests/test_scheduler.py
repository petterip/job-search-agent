import pytest

from app.config import get_settings
from app import scheduler as scheduler_module
from app.scheduler import build_scheduler, configured_scheduled_sources, expected_scheduler_job_ids


def test_build_scheduler_registers_all_default_source_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLLECTOR_ENABLED_SOURCES", raising=False)
    monkeypatch.setattr(scheduler_module, "prune_stale_scheduler_jobs", lambda *_: 0)
    monkeypatch.setattr(scheduler_module, "sync_source_enabled_flags", lambda *_: 0)
    get_settings.cache_clear()

    scheduler = build_scheduler()
    jobs = scheduler.get_jobs()
    assert len(jobs) == 10
    assert {job.id for job in jobs} == {
        "collect_duunitori",
        "collect_tmt",
        "collect_tmt_oulu",
        "collect_laura",
        "collect_jobly",
        "collect_eures_fi",
        "collect_kuntarekry",
        "collect_kirkkorekry",
        "collect_oulu_varbi",
        "match_recommendations",
    }


def test_expected_scheduler_job_ids() -> None:
    assert expected_scheduler_job_ids((("duunitori", 5), ("jobly", 60))) == {
        "collect_duunitori",
        "collect_jobly",
        "match_recommendations",
    }


def test_configured_scheduled_sources_allows_explicit_supplementals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLLECTOR_ENABLED_SOURCES", "duunitori,tmt,laura,jobly,eures_fi")
    get_settings.cache_clear()

    assert configured_scheduled_sources() == (
        ("duunitori", 5),
        ("tmt", 5),
        ("laura", 5),
        ("jobly", 60),
        ("eures_fi", 15),
    )


def test_configured_scheduled_sources_rejects_unknown_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLLECTOR_ENABLED_SOURCES", "duunitori,missing")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="missing"):
        configured_scheduled_sources()
