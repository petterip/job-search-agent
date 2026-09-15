import logging
import uuid
from datetime import datetime, timedelta, timezone

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
from app.collection.runner import (
    PIPELINE_RUN_LOCK_CLASS,
    SOURCE_RUN_LOCK_CLASS,
    RunOwnership,
    acquire_publication_ownership,
    acquire_run_ownership,
)
from app.config import get_settings
from app.db import get_engine
from app.enrich_locations import enrich_job_locations
from app.enrichers.runner import run_enrichment
from app.feedback_analysis import drain_pending_feedback_analyses, run_analyze_feedback
from app.feedback_learning import learn_from_feedback
from app.matching import run_matching

logger = logging.getLogger("worker.scheduler")

_SCHEDULER: AsyncIOScheduler | None = None
_LAST_CATCHUP_DATE: object | None = None

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
    settings = get_settings()
    return tuple(
        (source_name, interval_minutes)
        for source_name, interval_minutes in AVAILABLE_SCHEDULED_SOURCES
        if source_name in enabled
        and (source_name != LinkedinAdapter.source_name or settings.linkedin_enabled)
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
    """Whether any source collection is still owned by a live session.

    A legacy ``running`` row that is older than the staleness threshold and
    whose advisory lock is not held is reconciled first. A live long run keeps
    its lock, so it is never failed merely for running longer than the
    threshold.
    """
    settings = get_settings()
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                f"""
                update source_runs
                set status = 'failed',
                    finished_at = now(),
                    error_summary = coalesce(error_summary, 'abandoned: stale running run')
                where status = 'running'
                  and started_at < now() - (:stale_minutes * interval '1 minute')
                  and not exists (
                      select 1
                      from pg_locks
                      where locktype = 'advisory'
                        and classid = {SOURCE_RUN_LOCK_CLASS}
                        and objid = source_runs.source_id
                        and objsubid = 2
                        and granted
                  )
                """
            ),
            {"stale_minutes": settings.collector_stale_run_minutes},
        )
        return bool(
            connection.execute(
                sa.text("select exists (select 1 from source_runs where status = 'running')")
            ).scalar_one()
        )


def reconcile_abandoned_pipeline_runs(connection: sa.Connection) -> int:
    """Fail running pipeline rows; the caller must hold the pipeline lock."""
    result = connection.execute(
        sa.text(
            """
            update pipeline_runs
            set status = 'failed',
                finished_at = now(),
                error_summary = coalesce(error_summary, 'abandoned: owner lock was free')
            where status = 'running'
            """
        )
    )
    return int(result.rowcount or 0)


def _record_pipeline_skip(database_url: str, reason: str) -> None:
    """Persist a scheduler skip where the existing run status supports it."""
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    insert into pipeline_runs (status, finished_at, error_summary)
                    values ('skipped', now(), :reason)
                    """
                ),
                {"reason": reason},
            )
    except Exception:
        logger.warning("event=pipeline_skip_record_failed reason=%s", reason, exc_info=True)
    finally:
        engine.dispose()


# Pipeline run id -> (pipeline lock owner, profile publication lock owner, token).
# The lock sessions must outlive the short write transactions and are only
# released by finish_pipeline_run (or by the process/session ending).
_PIPELINE_OWNERS: dict[int, tuple[RunOwnership, RunOwnership, str]] = {}


def start_pipeline_run(database_url: str) -> int | None:
    engine = sa.create_engine(database_url)
    ownership = acquire_run_ownership(
        engine=engine,
        lock_class=PIPELINE_RUN_LOCK_CLASS,
        lock_object=0,
        lock_name="pipeline",
    )
    if ownership is None:
        engine.dispose()
        _record_pipeline_skip(database_url, "pipeline run ownership held elsewhere")
        logger.info("event=pipeline_run_skipped reason=run_ownership_not_acquired")
        return None
    # Matching, feedback analysis/learning and profile import all publish into
    # the active profile; the pipeline owns that lock for its whole run.
    publication = acquire_publication_ownership(engine=engine)
    if publication is None:
        ownership.release()
        engine.dispose()
        _record_pipeline_skip(database_url, "profile publication ownership held elsewhere")
        logger.info("event=pipeline_run_skipped reason=publication_ownership_not_acquired")
        return None
    owner_token = uuid.uuid4().hex
    try:
        with engine.begin() as connection:
            reconcile_abandoned_pipeline_runs(connection)
            run_id = int(
                connection.execute(
                    sa.text(
                        """
                        insert into pipeline_runs (status, owner_token)
                        values ('running', :owner_token)
                        returning id
                        """
                    ),
                    {"owner_token": owner_token},
                ).scalar_one()
            )
    except BaseException:
        publication.release()
        ownership.release()
        engine.dispose()
        raise
    _PIPELINE_OWNERS[run_id] = (ownership, publication, owner_token)
    return run_id


def finish_pipeline_run(
    database_url: str, run_id: int, status: str, error_summary: str | None = None
) -> bool:
    """Finish a running pipeline, then release its lock sessions.

    Returns ``False`` when the row was already terminal or owned by another
    token, so a stale finisher cannot overwrite a terminal status.
    """
    entry = _PIPELINE_OWNERS.pop(run_id, None)
    owner_token = entry[2] if entry is not None else None
    engine = sa.create_engine(database_url)
    finished = False
    try:
        with engine.begin() as connection:
            result = connection.execute(
                sa.text(
                    """
                    update pipeline_runs
                    set status = :status,
                        finished_at = now(),
                        error_summary = :error_summary
                    where id = :run_id
                      and status = 'running'
                      and owner_token is not distinct from cast(:owner_token as text)
                    """
                ),
                {
                    "run_id": run_id,
                    "status": status,
                    "error_summary": error_summary,
                    "owner_token": owner_token,
                },
            )
            finished = bool(result.rowcount)
    finally:
        engine.dispose()
        if entry is not None:
            entry[1].release()
            entry[0].release()
    return finished


def release_pipeline_ownership(run_id: int) -> None:
    """Unlock a pipeline run's dedicated sessions without touching its row."""
    entry = _PIPELINE_OWNERS.pop(run_id, None)
    if entry is None:
        return
    entry[1].release()
    entry[0].release()


async def scheduled_analyze_feedback() -> None:
    ownership = acquire_publication_ownership()
    if ownership is None:
        logger.info(
            "event=scheduled_feedback_analysis_skipped reason=run_ownership_not_acquired"
        )
        return
    try:
        result = run_analyze_feedback()
        if result.get("pending"):
            logger.info("event=scheduled_feedback_analysis counts=%s", result)
    except Exception:
        logger.exception("event=scheduled_feedback_analysis_failed")
    finally:
        ownership.release()


def record_pipeline_skip(*, reason: str) -> None:
    """Persist a pipeline skip so the run is visible in diagnostics."""
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                insert into pipeline_runs (status, finished_at, error_summary)
                values ('skipped', now(), :reason)
                """
            ),
            {"reason": reason},
        )


def maybe_schedule_pipeline_catchup(settings: object) -> bool:
    """Schedule at most one bounded catch-up pipeline run per UTC day.

    The daily pipeline skips while collection owns the catalogue; a single
    delayed retry keeps the day from being silently skipped without allowing an
    unbounded retry loop.
    """
    global _LAST_CATCHUP_DATE
    if _SCHEDULER is None:
        return False
    today = datetime.now(timezone.utc).date()
    if _LAST_CATCHUP_DATE == today:
        return False
    _LAST_CATCHUP_DATE = today
    delay = int(getattr(settings, "pipeline_catchup_delay_minutes", 30) or 30)
    _SCHEDULER.add_job(
        scheduled_pipeline,
        trigger="date",
        run_date=datetime.now(timezone.utc) + timedelta(minutes=delay),
        id="daily_pipeline_catchup",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("event=pipeline_catchup_scheduled delay_minutes=%s", delay)
    return True


async def scheduled_pipeline() -> None:
    settings = get_settings()
    if source_collection_running(settings.database_url):
        logger.info("event=daily_pipeline_skipped reason=source_collection_running")
        try:
            record_pipeline_skip(reason="source_collection_running")
        except Exception:
            logger.exception("event=pipeline_skip_record_failed")
        if maybe_schedule_pipeline_catchup(settings):
            logger.info("event=daily_pipeline_catchup_scheduled")
        return
    run_id = start_pipeline_run(settings.database_url)
    if run_id is None:
        logger.info("event=daily_pipeline_skipped reason=pipeline_already_running")
        return
    try:
        enrichment_result = {
            "detail_http": run_enrichment(enricher="detail_http"),
            "jobly_browser": run_enrichment(enricher="jobly_browser"),
        }
        engine = get_engine()
        with engine.begin() as connection:
            location_result = enrich_job_locations(connection)
        feedback_analysis_result = drain_pending_feedback_analyses()
        with engine.begin() as connection:
            feedback_learning_result = learn_from_feedback(connection)
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
    finally:
        # finish_pipeline_run already released the normal path; this covers a
        # cancelled or otherwise non-Exception exit without touching the row.
        release_pipeline_ownership(run_id)


def build_scheduler() -> AsyncIOScheduler:
    global _SCHEDULER
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
    _SCHEDULER = scheduler
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
