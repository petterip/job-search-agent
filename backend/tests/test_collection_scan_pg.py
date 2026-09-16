"""Resumable-scan collection branch regression (P1-7b / settings lookup).

The Jobly-style scan branch reads queue settings before claiming a batch; this
test executes that branch with a fake adapter so a missing settings import can
never reach a scheduled production collection again.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from app.adapters.base import CollectionFetchResult

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set; PostgreSQL integration tests skipped",
)

TABLES = (
    "source_run_events",
    "source_runs",
    "source_scan_members",
    "source_scan_progress",
    "job_sources",
    "raw_listings",
    "jobs",
    "sources",
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


class FakeResumableAdapter:
    source_name = "fake-scan"
    source_method = "sitemap"
    poll_interval_min = 60
    url = "https://example.invalid/sitemap.xml"
    supports_resumable_scan = True

    def __init__(self) -> None:
        self.pending_entries: list[tuple[str, str, datetime | None]] | None = None

    async def collect(
        self,
        *,
        watermark: datetime | None,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_urls: int | None = None,
        pending_entries: list[tuple[str, str, datetime | None]] | None = None,
    ) -> CollectionFetchResult:
        self.pending_entries = pending_entries
        return CollectionFetchResult(
            listings=[],
            scan_entries=[("ext-1", "https://example.invalid/1", None)],
            scan_outcomes={"ext-1": "classified"},
            scan_frontier_complete=True,
        )


def test_resumable_scan_branch_runs_with_configured_settings(
    pg_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.collection import runner as runner_module

    adapter = FakeResumableAdapter()
    monkeypatch.setattr(runner_module, "get_engine", lambda: pg_engine)

    result = asyncio.run(runner_module.run_source_collection(adapter))

    assert result["skipped"] is False
    assert result["source"] == "fake-scan"
    assert adapter.pending_entries == []
    with pg_engine.connect() as connection:
        progress = connection.execute(
            sa.text("select count(*) from source_scan_progress")
        ).scalar_one()
        members = (
            connection.execute(sa.text("select external_id from source_scan_members"))
            .scalars()
            .all()
        )
    assert int(progress) == 1
    assert list(members) == ["ext-1"]
