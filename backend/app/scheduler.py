import logging

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import sqlalchemy as sa

from app.adapters.duunitori import DuunitoriAdapter
from app.adapters.eures import EuresAdapter
from app.adapters.jobly import JoblyAdapter
from app.adapters.kirkkorekry import KirkkorekryAdapter
from app.adapters.kuntarekry import KuntarekryAdapter
from app.adapters.laura import LauraAdapter
from app.adapters.tmt import TmtAdapter
from app.adapters.tmt_oulu import TmtOuluAdapter
from app.adapters.varbi import OuluVarbiAdapter
from app.adapters.valtiolle import ValtiolleAdapter
from app.collection.registry import collect_source
from app.config import get_settings
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
        "match_recommendations"
    }


def prune_stale_scheduler_jobs(database_url: str, expected_job_ids: set[str]) -> int:
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        result = connection.execute(
            sa.text(
                """
                delete from apscheduler_jobs
                where (id like 'collect_%' or id = 'match_recommendations')
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


async def scheduled_match() -> None:
    settings = get_settings()
    try:
        result = run_matching(max_jobs=settings.matcher_max_jobs)
        logger.info("event=scheduled_matching result=%s", result)
    except Exception:
        logger.exception("event=scheduled_matching_failed")


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
        scheduled_match,
        trigger=CronTrigger(
            hour=settings.matcher_daily_hour,
            minute=settings.matcher_daily_minute,
            timezone="Europe/Helsinki",
        ),
        id="match_recommendations",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
