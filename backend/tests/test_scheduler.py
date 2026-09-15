import pytest

from app.config import get_settings
from app import scheduler as scheduler_module
from app.scheduler import (
    build_scheduler,
    configured_scheduled_sources,
    expected_scheduler_job_ids,
    source_collection_running,
)


def test_build_scheduler_registers_all_default_source_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLLECTOR_ENABLED_SOURCES", raising=False)
    monkeypatch.setattr(scheduler_module, "prune_stale_scheduler_jobs", lambda *_: 0)
    monkeypatch.setattr(scheduler_module, "sync_source_enabled_flags", lambda *_: 0)
    get_settings.cache_clear()

    scheduler = build_scheduler()
    jobs = scheduler.get_jobs()
    assert len(jobs) == 11
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
        "analyze_feedback",
        "run_daily_pipeline",
    }
    cron_jobs = [job for job in jobs if job.id != "analyze_feedback"]
    assert all("cron" in str(job.trigger) for job in cron_jobs)
    analysis_jobs = [job for job in jobs if job.id == "analyze_feedback"]
    assert len(analysis_jobs) == 1
    assert "interval" in str(analysis_jobs[0].trigger).lower()
    collect_jobs = [job for job in jobs if job.id.startswith("collect_")]
    pipeline_jobs = [job for job in jobs if job.id == "run_daily_pipeline"]
    assert all("hour='16'" in str(job.trigger) for job in collect_jobs)
    assert all("minute='0'" in str(job.trigger) for job in collect_jobs)
    assert len(pipeline_jobs) == 1
    assert "hour='16'" in str(pipeline_jobs[0].trigger)
    assert "minute='45'" in str(pipeline_jobs[0].trigger)


def test_expected_scheduler_job_ids() -> None:
    assert expected_scheduler_job_ids((("duunitori", 5), ("jobly", 60))) == {
        "collect_duunitori",
        "collect_jobly",
        "analyze_feedback",
        "run_daily_pipeline",
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


def test_configured_scheduled_sources_requires_linkedin_enablement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLLECTOR_ENABLED_SOURCES", "duunitori,linkedin")
    monkeypatch.setenv("LINKEDIN_ENABLED", "false")
    get_settings.cache_clear()

    assert configured_scheduled_sources() == (("duunitori", 5),)

    monkeypatch.setenv("LINKEDIN_ENABLED", "true")
    get_settings.cache_clear()

    assert configured_scheduled_sources() == (("duunitori", 5), ("linkedin", 360))


def test_configured_scheduled_sources_rejects_unknown_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLLECTOR_ENABLED_SOURCES", "duunitori,missing")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="missing"):
        configured_scheduled_sources()


@pytest.mark.asyncio
async def test_scheduled_pipeline_runs_enrichment_locations_feedback_learning_matching_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class BeginContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args) -> None:  # noqa: ANN002
            return None

    class Engine:
        def begin(self) -> BeginContext:
            return BeginContext()

    monkeypatch.setattr(scheduler_module, "source_collection_running", lambda _database_url: False)
    monkeypatch.setattr(scheduler_module, "start_pipeline_run", lambda _database_url: 7)
    monkeypatch.setattr(scheduler_module, "finish_pipeline_run", lambda *_args, **_kwargs: calls.append("finish"))
    monkeypatch.setattr(scheduler_module, "get_engine", lambda: Engine())
    monkeypatch.setattr(
        scheduler_module,
        "run_enrichment",
        lambda *, enricher: calls.append(f"enrichment:{enricher}") or {"processed": 0},
    )
    monkeypatch.setattr(
        scheduler_module,
        "enrich_job_locations",
        lambda _connection: calls.append("locations") or {"updated": 0},
    )
    monkeypatch.setattr(
        scheduler_module,
        "drain_pending_feedback_analyses",
        lambda: calls.append("feedback_analysis") or {"processed": 0},
    )
    monkeypatch.setattr(
        scheduler_module,
        "learn_from_feedback",
        lambda _connection: calls.append("feedback_learning") or {"feedback_total": 0},
    )
    monkeypatch.setattr(
        scheduler_module,
        "run_matching",
        lambda *, max_jobs: calls.append("matching") or {"recommended": 0},
    )

    await scheduler_module.scheduled_pipeline()

    assert calls == [
        "enrichment:detail_http",
        "enrichment:jobly_browser",
        "locations",
        "feedback_analysis",
        "feedback_learning",
        "matching",
        "finish",
    ]


def test_source_collection_running_reconciles_stale_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[str] = []
    params: list[dict | None] = []

    class Result:
        def scalar_one(self) -> bool:
            return False

    class Connection:
        def execute(self, statement, parameters=None):  # noqa: ANN001
            statements.append(str(statement))
            params.append(parameters)
            return Result()

    class BeginContext:
        def __enter__(self) -> Connection:
            return Connection()

        def __exit__(self, *_args) -> None:  # noqa: ANN002
            return None

    class Engine:
        def begin(self) -> BeginContext:
            return BeginContext()

    monkeypatch.setattr(scheduler_module.sa, "create_engine", lambda _database_url: Engine())

    assert source_collection_running("postgresql://test") is False
    assert "update source_runs" in statements[0]
    assert "started_at < now()" in statements[0]
    assert params[0] == {"stale_minutes": get_settings().collector_stale_run_minutes}
    assert "select exists" in statements[1]


def test_pipeline_catchup_is_bounded_to_one_per_day(monkeypatch) -> None:
    from app import scheduler as scheduler_module
    from app.config import Settings

    class FakeScheduler:
        def __init__(self) -> None:
            self.jobs: list[dict] = []

        def add_job(self, func, **kwargs):  # noqa: ANN001
            self.jobs.append({"func": func, **kwargs})

    fake = FakeScheduler()
    monkeypatch.setattr(scheduler_module, "_SCHEDULER", fake)
    monkeypatch.setattr(scheduler_module, "_LAST_CATCHUP_DATE", None)
    settings = Settings(pipeline_catchup_delay_minutes=15)

    assert scheduler_module.maybe_schedule_pipeline_catchup(settings) is True
    assert len(fake.jobs) == 1
    assert fake.jobs[0]["id"] == "daily_pipeline_catchup"
    # A second attempt the same day is refused.
    assert scheduler_module.maybe_schedule_pipeline_catchup(settings) is False
    assert len(fake.jobs) == 1
    monkeypatch.setattr(scheduler_module, "_LAST_CATCHUP_DATE", None)


def test_catchup_is_not_scheduled_without_a_scheduler(monkeypatch) -> None:
    from app import scheduler as scheduler_module
    from app.config import Settings

    monkeypatch.setattr(scheduler_module, "_SCHEDULER", None)
    monkeypatch.setattr(scheduler_module, "_LAST_CATCHUP_DATE", None)

    assert scheduler_module.maybe_schedule_pipeline_catchup(Settings()) is False
