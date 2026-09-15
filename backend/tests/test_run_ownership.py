"""Database-owned run ownership tests.

Mutual exclusion is a PostgreSQL advisory lock held on a dedicated connection
for the whole run, so these rules can only be proven against real PostgreSQL:

    TEST_DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1:55432/db \\
        python -m pytest tests/test_run_ownership.py

The database must already be migrated (``alembic upgrade head``).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from app import scheduler as scheduler_module
from app.adapters.base import CollectionFetchResult
from app.collection import runner as runner_module
from app.collection.runner import (
    SOURCE_RUN_LOCK_CLASS,
    acquire_publication_ownership,
    acquire_run_ownership,
    advisory_lock_is_held,
    finish_source_run,
    reconcile_abandoned_runs,
    run_source_collection,
)

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set; PostgreSQL integration tests skipped",
)

TABLES = (
    "source_run_events",
    "source_runs",
    "sources",
    "pipeline_runs",
    "job_seeker_profiles",
)


@pytest.fixture()
def pg_engine() -> Iterator[Any]:
    engine = create_engine(str(TEST_DATABASE_URL))
    with engine.begin() as connection:
        connection.execute(
            sa.text(f"truncate table {', '.join(TABLES)} restart identity cascade")
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seed_source(connection: Any, *, name: str = "ownershiptest") -> int:
    return int(
        connection.execute(
            sa.text(
                """
                insert into sources (name, method, poll_interval_min)
                values (:name, 'api', 60)
                returning id
                """
            ),
            {"name": name},
        ).scalar_one()
    )


def seed_profile(connection: Any, *, name: str = "test") -> int:
    return int(
        connection.execute(
            sa.text(
                """
                insert into job_seeker_profiles (name, profile)
                values (:name, '{}'::jsonb)
                returning id
                """
            ),
            {"name": name},
        ).scalar_one()
    )


def seed_running_run(connection: Any, *, source_id: int, started_at: datetime) -> int:
    return int(
        connection.execute(
            sa.text(
                """
                insert into source_runs (source_id, status, started_at, owner_token)
                values (:source_id, 'running', :started_at, 'seed-owner')
                returning id
                """
            ),
            {"source_id": source_id, "started_at": started_at},
        ).scalar_one()
    )


def source_run_status(connection: Any, run_id: int) -> str:
    return str(
        connection.execute(
            sa.text("select status from source_runs where id = :run_id"), {"run_id": run_id}
        ).scalar_one()
    )


class BlockingAdapter:
    """A source adapter that parks while the run owns the lock."""

    source_name = "ownershiptest"
    source_method = "test"
    poll_interval_min = 60
    url = "https://example.invalid/ownership"

    def __init__(self) -> None:
        self.collect_started = asyncio.Event()
        self.collect_release = asyncio.Event()

    async def collect(
        self,
        *,
        watermark: datetime | None,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_urls: int | None = None,
    ) -> CollectionFetchResult:
        self.collect_started.set()
        await self.collect_release.wait()
        return CollectionFetchResult(listings=[], pages_fetched=0, stopped_at_watermark=False)


async def test_competing_source_runs_allow_exactly_one_owner(
    pg_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "get_engine", lambda: pg_engine)
    owner_adapter = BlockingAdapter()
    owner_task = asyncio.create_task(run_source_collection(owner_adapter))
    try:
        await asyncio.wait_for(owner_adapter.collect_started.wait(), timeout=5)

        # The owner is awaiting a remote response; its lock session must be
        # idle, never sitting in an open write transaction.
        with pg_engine.connect() as probe:
            idle_in_transaction = probe.execute(
                sa.text(
                    """
                    select count(*)
                    from pg_stat_activity
                    where datname = current_database()
                      and state = 'idle in transaction'
                    """
                )
            ).scalar_one()
        assert idle_in_transaction == 0

        # A competing start records a skip instead of doing concurrent work.
        competitor_result = await run_source_collection(BlockingAdapter())
        assert competitor_result["skipped"] is True
        assert competitor_result["reason"] == "already_running"
        assert competitor_result["fetched"] == 0
        with pg_engine.connect() as probe:
            statuses = (
                probe.execute(sa.text("select status from source_runs order by id"))
                .scalars()
                .all()
            )
            skip_events = probe.execute(
                sa.text(
                    """
                    select count(*)
                    from source_run_events
                    where event_type = 'source_collection_skipped'
                    """
                )
            ).scalar_one()
        assert statuses == ["running"]
        assert skip_events == 1
    finally:
        owner_adapter.collect_release.set()
        owner_result = await asyncio.wait_for(owner_task, timeout=5)

    assert owner_result["skipped"] is False
    with pg_engine.connect() as probe:
        rows = (
            probe.execute(sa.text("select status, owner_token from source_runs order by id"))
            .mappings()
            .all()
        )
    assert [row["status"] for row in rows] == ["success"]
    assert rows[0]["owner_token"]


def test_live_long_run_is_not_declared_dead(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        source_id = seed_source(connection)
        run_id = seed_running_run(connection, source_id=source_id, started_at=_now() - timedelta(hours=3))

    ownership = acquire_run_ownership(
        engine=pg_engine,
        lock_class=SOURCE_RUN_LOCK_CLASS,
        lock_object=source_id,
        lock_name="source:ownershiptest",
    )
    assert ownership is not None
    try:
        # Far beyond the 30-minute threshold, but the owner still holds the lock.
        assert scheduler_module.source_collection_running(str(TEST_DATABASE_URL)) is True
        with pg_engine.connect() as probe:
            assert source_run_status(probe, run_id) == "running"
    finally:
        ownership.release()

    # Once the owner is gone the same row is reconciled.
    assert scheduler_module.source_collection_running(str(TEST_DATABASE_URL)) is False
    with pg_engine.connect() as probe:
        assert source_run_status(probe, run_id) == "failed"


def test_ownership_proof_reconciles_abandoned_run_regardless_of_age(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        source_id = seed_source(connection)
        run_id = seed_running_run(connection, source_id=source_id, started_at=_now())

    ownership = acquire_run_ownership(
        engine=pg_engine,
        lock_class=SOURCE_RUN_LOCK_CLASS,
        lock_object=source_id,
        lock_name="source:ownershiptest",
    )
    assert ownership is not None
    try:
        with pg_engine.begin() as connection:
            reconciled = reconcile_abandoned_runs(connection, source_id)
        assert reconciled == 1
        with pg_engine.connect() as probe:
            assert source_run_status(probe, run_id) == "failed"
    finally:
        ownership.release()


def test_terminated_owner_session_releases_ownership(pg_engine: Any) -> None:
    lock_object = 987654
    owner_engine = create_engine(str(TEST_DATABASE_URL))
    ownership = acquire_run_ownership(
        engine=owner_engine,
        lock_class=SOURCE_RUN_LOCK_CLASS,
        lock_object=lock_object,
        lock_name="source:crash",
    )
    assert ownership is not None
    try:
        with pg_engine.connect() as probe:
            assert (
                advisory_lock_is_held(
                    probe, lock_class=SOURCE_RUN_LOCK_CLASS, lock_object=lock_object
                )
                is True
            )
        # While the owner lives, nobody else can own the same lock.
        assert (
            acquire_run_ownership(
                engine=pg_engine,
                lock_class=SOURCE_RUN_LOCK_CLASS,
                lock_object=lock_object,
                lock_name="source:crash",
            )
            is None
        )

        # Simulate a crashed worker: the database session ends without
        # pg_advisory_unlock.
        owner_pid = ownership.connection.execute(sa.text("select pg_backend_pid()")).scalar_one()
        with pg_engine.connect() as probe:
            probe.execute(sa.text("select pg_terminate_backend(:pid)"), {"pid": owner_pid})
        with pg_engine.connect() as probe:
            assert (
                advisory_lock_is_held(
                    probe, lock_class=SOURCE_RUN_LOCK_CLASS, lock_object=lock_object
                )
                is False
            )

        replacement = acquire_run_ownership(
            engine=pg_engine,
            lock_class=SOURCE_RUN_LOCK_CLASS,
            lock_object=lock_object,
            lock_name="source:crash",
        )
        assert replacement is not None
        replacement.release()
    finally:
        owner_engine.dispose()


def test_stale_finisher_cannot_overwrite_terminal_status(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        source_id = seed_source(connection)
        run_id = int(
            connection.execute(
                sa.text(
                    """
                    insert into source_runs (source_id, status, owner_token)
                    values (:source_id, 'running', 'owner-a')
                    returning id
                    """
                ),
                {"source_id": source_id},
            ).scalar_one()
        )
        assert (
            finish_source_run(
                connection,
                run_id=run_id,
                owner_token="owner-a",
                status="success",
                fetched_count=3,
                inserted_count=1,
                updated_count=0,
                unchanged_count=2,
            )
            is True
        )
        # A different token must not reopen or overwrite the terminal row.
        assert (
            finish_source_run(
                connection,
                run_id=run_id,
                owner_token="owner-b",
                status="failed",
                error_summary="stale finisher",
            )
            is False
        )
        # Replaying the same owner's completion is also a no-op.
        assert (
            finish_source_run(
                connection, run_id=run_id, owner_token="owner-a", status="failed"
            )
            is False
        )
        row = connection.execute(
            sa.text(
                "select status, error_summary, fetched_count from source_runs where id = :run_id"
            ),
            {"run_id": run_id},
        ).mappings().one()
    assert row["status"] == "success"
    assert row["error_summary"] is None
    assert row["fetched_count"] == 3


def test_pipeline_run_ownership_is_exclusive(pg_engine: Any) -> None:
    first = scheduler_module.start_pipeline_run(str(TEST_DATABASE_URL))
    assert first is not None
    try:
        # A competing start records a persisted skip instead of a second run.
        assert scheduler_module.start_pipeline_run(str(TEST_DATABASE_URL)) is None
    finally:
        assert scheduler_module.finish_pipeline_run(str(TEST_DATABASE_URL), first, "completed") is True

    # A stale finisher cannot overwrite the terminal status.
    assert (
        scheduler_module.finish_pipeline_run(
            str(TEST_DATABASE_URL), first, "failed", "stale finisher"
        )
        is False
    )
    third = scheduler_module.start_pipeline_run(str(TEST_DATABASE_URL))
    assert third is not None and third != first
    assert scheduler_module.finish_pipeline_run(str(TEST_DATABASE_URL), third, "completed") is True

    with pg_engine.connect() as probe:
        rows = (
            probe.execute(sa.text("select id, status from pipeline_runs order by id"))
            .mappings()
            .all()
        )
    assert [row["status"] for row in rows] == ["completed", "skipped", "completed"]


def test_pipeline_owns_profile_publication_lock(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        seed_profile(connection)

    run_id = scheduler_module.start_pipeline_run(str(TEST_DATABASE_URL))
    assert run_id is not None
    try:
        # Manual matching/a publication cannot start while the pipeline owns
        # the active profile's publication lock.
        assert acquire_publication_ownership(engine=pg_engine) is None
    finally:
        scheduler_module.finish_pipeline_run(str(TEST_DATABASE_URL), run_id, "completed")

    ownership = acquire_publication_ownership(engine=pg_engine)
    assert ownership is not None
    ownership.release()
