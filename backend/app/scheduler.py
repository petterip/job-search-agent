import logging

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import sqlalchemy as sa

from app.adapters.careerjet import CareerjetAdapter
from app.adapters.duunitori import DuunitoriAdapter
from app.adapters.eures import EuresAdapter
from app.adapters.jobly import JoblyAdapter
from app.adapters.kirkkorekry import KirkkorekryAdapter
from app.adapters.kuntarekry import KuntarekryAdapter
from app.adapters.laura import LauraAdapter
from app.adapters.linkedin import LinkedinAdapter
from app.adapters.tmt import TmtAdapter
from app.adapters.tmt_oulu import TmtOuluAdapter
from app.adapters.varbi import OuluVarbiAdapter
from app.adapters.valtiolle import ValtiolleAdapter
from app.collection.registry import collect_source
from app.config import get_settings
from app.db import get_engine
from app.enrich_locations import enrich_job_locations
from app.enrichers.runner import run_enrichment
from app.feedback_analysis import drain_pending_feedback_analyses, run_analyze_feedback
from app.feedback_learning import learn_from_feedback
from app.matching import run_matching

logger = logging.getLogger("worker.scheduler")

_kuntarekry_adapter = KuntarekryAdapter()
_kirkkorekry_adapter = KirkkorekryAdapter()
_valtiolle_adapter = ValtiolleAdapter()

AVAILABLE_SCHEDULED_SOURCES: tuple[tuple[str, int], ...] = (
    (DuunitoriAdapter.source_name, DuunitoriAdapter.poll_interval_min),
    (TmtAdapter.source_name, TmtAdapter.poll_interval_min),
    (TmtOuluAdapter.source_name, TmtOuluAdapter.poll_interval_min),
    (LauraAdapter.source_name, LauraAdapter.poll_interval_min),
    (JoblyAdapter.source_name, JoblyAdapter.poll_interval_min),
    (EuresAdapter.source_name, EuresAdapter.poll_interval_min),
    (_kuntarekry_adapter.source_name, _kuntarekry_adapter.poll_interval_min),
    (_kirkkorekry_adapter.source_name, _kirkkorekry_adapter.poll_interval_min),
    (OuluVarbiAdapter.source_name, OuluVarbiAdapter.poll_interval_min),
    (CareerjetAdapter.source_name, CareerjetAdapter.poll_interval_min),
    (LinkedinAdapter.source_name, LinkedinAdapter.poll_interval_min),
    (_valtiolle_adapter.source_name, _valtiolle_adapter.poll_interval_min),
)


def configured_scheduled_sources() -> tuple[tuple[str, int], ...]:
    enabled = set(get_settings().collector_enabled_sources)
    known = {source_name for source_name, _ in AVAILABLE_SCHEDULED_SOURCES}
    unknown = sorted(enabled - known)
    if unknown:
        raise ValueError(f"Unknown COLLECTOR_ENABLED_SOURCES entries: {', '.join(unknown)}")
    return tuple(
        (source_name, interval_minutes)
        for source_name, interval_minutes in AVAILABLE_SCHEDULED_SOURCES
        if source_name in enabled
    )


def expected_scheduler_job_ids(
    scheduled_sources: tuple[tuple[str, int], ...],
) -> set[str]:
    return {f"collect_{source_name}" for source_name, _ in scheduled_sources} | {
        "run_daily_pipeline",
        "analyze_feedback",
    }


def prune_stale_scheduler_jobs(database_url: str, expected_job_ids: set[str]) -> int:
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        result = connection.execute(
            sa.text(
                """
                delete from apscheduler_jobs
                where (
                    id like 'collect_%'
                    or id in ('match_recommendations', 'run_daily_pipeline', 'analyze_feedback')
                )
                  and id != all(:expected_job_ids)
                """
            ),
            {"expected_job_ids": list(expected_job_ids)},
        )
    return int(result.rowcount or 0)


def sync_source_enabled_flags(database_url: str, enabled_source_names: set[str]) -> int:
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        result = connection.execute(
            sa.text(
                """
                update sources
                set enabled = name = any(:enabled_source_names)
                """
            ),
            {"enabled_source_names": list(enabled_source_names)},
        )
    return int(result.rowcount or 0)


async def scheduled_collect(source_name: str) -> None:
    try:
        counts = await collect_source(source_name)
        if counts.get("skipped"):
            return
        logger.info("event=scheduled_collection source=%s counts=%s", source_name, counts)
    except Exception:
        logger.exception("event=scheduled_collection_failed source=%s", source_name)


def source_collection_running(database_url: str) -> bool:
    settings = get_settings()
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                update source_runs
                set status = 'failed',
                    finished_at = now(),
                    error_summary = coalesce(error_summary, 'abandoned: stale running run')
                where status = 'running'
                  and started_at < now() - (:stale_minutes * interval '1 minute')
                """
            ),
            {"stale_minutes": settings.collector_stale_run_minutes},
        )
        return bool(
            connection.execute(
                sa.text("select exists (select 1 from source_runs where status = 'running')")
            ).scalar_one()
        )


def start_pipeline_run(database_url: str) -> int | None:
    settings = get_settings()
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                update pipeline_runs
                set status = 'failed',
                    finished_at = now(),
                    error_summary = coalesce(
                        error_summary,
                        'pipeline run abandoned after stale timeout'
                    )
                where status = 'running'
                  and started_at < now() - (:stale_minutes * interval '1 minute')
                """
            ),
            {"stale_minutes": settings.collector_stale_run_minutes},
        )
        running = connection.execute(
            sa.text("select id from pipeline_runs where status = 'running' limit 1")
        ).scalar_one_or_none()
        if running is not None:
            return None
        return int(
            connection.execute(
                sa.text(
                    """
                    insert into pipeline_runs (status)
                    values ('running')
                    returning id
                    """
                )
            ).scalar_one()
        )


def finish_pipeline_run(database_url: str, run_id: int, status: str, error_summary: str | None = None) -> None:
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                update pipeline_runs
                set status = :status,
                    finished_at = now(),
                    error_summary = :error_summary
                where id = :run_id
                """
            ),
            {"run_id": run_id, "status": status, "error_summary": error_summary},
        )


async def scheduled_analyze_feedback() -> None:
    try:
        result = run_analyze_feedback()
        if result.get("pending"):
            logger.info("event=scheduled_feedback_analysis counts=%s", result)
    except Exception:
        logger.exception("event=scheduled_feedback_analysis_failed")


async def scheduled_pipeline() -> None:
    settings = get_settings()
    if source_collection_running(settings.database_url):
        logger.info("event=daily_pipeline_skipped reason=source_collection_running")
        return
    run_id = start_pipeline_run(settings.database_url)
    if run_id is None:
        logger.info("event=daily_pipeline_skipped reason=pipeline_already_running")
        return
    try:
        enrichment_result = run_enrichment()
        feedback_analysis_result = drain_pending_feedback_analyses()
        engine = get_engine()
        with engine.begin() as connection:
            feedback_learning_result = learn_from_feedback(connection)
            location_result = enrich_job_locations(connection)
        matching_result = run_matching(max_jobs=settings.matcher_max_jobs)
        finish_pipeline_run(settings.database_url, run_id, "completed")
        logger.info(
            "event=daily_pipeline_completed enrichment=%s locations=%s feedback_analysis=%s feedback_learning=%s matching=%s",
            enrichment_result,
            location_result,
            feedback_analysis_result,
            feedback_learning_result,
            matching_result,
        )
    except Exception as exc:
        finish_pipeline_run(settings.database_url, run_id, "failed", str(exc)[:1000])
        logger.exception("event=daily_pipeline_failed")


def build_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    scheduled_sources = configured_scheduled_sources()
    expected_job_ids = expected_scheduler_job_ids(scheduled_sources)
    enabled_source_names = {source_name for source_name, _ in scheduled_sources}
    pruned_count = prune_stale_scheduler_jobs(settings.database_url, expected_job_ids)
    if pruned_count:
        logger.info("event=stale_scheduler_jobs_pruned count=%s", pruned_count)
    sync_source_enabled_flags(settings.database_url, enabled_source_names)
    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=settings.database_url)},
        timezone="Europe/Helsinki",
    )
    for source_name, _interval_minutes in scheduled_sources:
        scheduler.add_job(
            scheduled_collect,
            trigger=CronTrigger(
                hour=settings.collector_daily_hour,
                minute=settings.collector_daily_minute,
                timezone="Europe/Helsinki",
            ),
            id=f"collect_{source_name}",
            args=[source_name],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        scheduled_analyze_feedback,
        trigger=IntervalTrigger(minutes=settings.feedback_analysis_poll_minutes),
        id="analyze_feedback",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        scheduled_pipeline,
        trigger=CronTrigger(
            hour=settings.learner_daily_hour,
            minute=settings.learner_daily_minute,
            timezone="Europe/Helsinki",
        ),
        id="run_daily_pipeline",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
