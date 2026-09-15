"""PostgreSQL tests for the durable resumable sitemap scan state."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from app.collection.scan_state import (
    OUTCOME_CLASSIFIED,
    OUTCOME_INVALID,
    OUTCOME_MISSING,
    SCAN_STATE_CLASSIFIED,
    SCAN_STATE_CLOSED,
    SCAN_STATE_PENDING,
    SCAN_STATE_RETRY,
    SitemapEntry,
    claim_scan_batch,
    close_absent_members,
    frontier_drained,
    mark_scan_outcome,
    scan_backlog_report,
    sync_sitemap_entries,
)

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set; PostgreSQL integration tests skipped",
)

TABLES = (
    "source_scan_members",
    "source_scan_progress",
    "recommendation_feedback",
    "feedback_llm_analyses",
    "llm_evaluations",
    "recommendations",
    "job_sources",
    "jobs",
    "raw_listings",
    "sources",
)


@pytest.fixture()
def pg_engine() -> Iterator[Any]:
    engine = create_engine(str(TEST_DATABASE_URL))
    with engine.begin() as connection:
        connection.execute(sa.text(f"truncate table {', '.join(TABLES)} restart identity cascade"))
    try:
        yield engine
    finally:
        engine.dispose()


def _source(connection: Any, name: str = "jobly") -> int:
    return int(
        connection.execute(
            sa.text(
                """
                insert into sources (name, method, poll_interval_min, enabled)
                values (:name, 'sitemap', 60, true)
                returning id
                """
            ),
            {"name": name},
        ).scalar_one()
    )


def _entry(external_id: str, *, lastmod: datetime | None = None) -> SitemapEntry:
    return SitemapEntry(
        external_id=external_id,
        canonical_url=f"https://www.jobly.fi/tyopaikka/x-{external_id}",
        lastmod=lastmod,
    )


def test_sync_freezes_frontier_and_marks_absent(pg_engine: Any) -> None:
    base = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    with pg_engine.begin() as connection:
        source_id = _source(connection)
        report = sync_sitemap_entries(
            connection,
            source_id=source_id,
            entries=[_entry("101", lastmod=base), _entry("102"), _entry("103", lastmod=base)],
        )
        assert report["frontier_size"] == 3
        assert report["inserted"] == 3

        # A later sitemap that no longer lists 102 marks it absent, not closed.
        sync_sitemap_entries(
            connection,
            source_id=source_id,
            entries=[_entry("101", lastmod=base), _entry("103", lastmod=base)],
        )
        states = dict(
            connection.execute(
                sa.text(
                    "select external_id, state from source_scan_members where source_id = :source_id"
                ),
                {"source_id": source_id},
            ).all()
        )
        absent = connection.execute(
            sa.text(
                "select sitemap_present from source_scan_members where source_id = :source_id and external_id = '102'"
            ),
            {"source_id": source_id},
        ).scalar_one()

    assert states == {"101": SCAN_STATE_PENDING, "102": SCAN_STATE_PENDING, "103": SCAN_STATE_PENDING}
    assert absent is False


def test_claim_is_oldest_first_with_no_date_entries_first(pg_engine: Any) -> None:
    base = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    with pg_engine.begin() as connection:
        source_id = _source(connection)
        sync_sitemap_entries(
            connection,
            source_id=source_id,
            entries=[
                _entry("newest", lastmod=base + timedelta(days=5)),
                _entry("nodate"),
                _entry("oldest", lastmod=base),
                _entry("middle", lastmod=base + timedelta(days=2)),
            ],
        )

        first = claim_scan_batch(connection, source_id=source_id, limit=2)
        assert [member.external_id for member in first] == ["nodate", "oldest"]

        # Equal timestamps fall back to external_id ordering deterministically.
        sync_sitemap_entries(
            connection,
            source_id=source_id,
            entries=[_entry("b", lastmod=base), _entry("a", lastmod=base)],
        )
        # Mark the first two classified so the equal-timestamp pair is claimed.
        for external_id in ("nodate", "oldest"):
            mark_scan_outcome(
                connection,
                source_id=source_id,
                external_id=external_id,
                outcome=OUTCOME_CLASSIFIED,
            )
        # "middle"/"newest" still pending; the new a/b pair is older.
        batch = claim_scan_batch(connection, source_id=source_id, limit=10)
        assert [m.external_id for m in batch][:2] == ["a", "b"]


def test_missing_is_retryable_until_confirmed_closed(pg_engine: Any) -> None:
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    with pg_engine.begin() as connection:
        source_id = _source(connection)
        sync_sitemap_entries(connection, source_id=source_id, entries=[_entry("404")], now=now)

        first = mark_scan_outcome(
            connection, source_id=source_id, external_id="404", outcome=OUTCOME_MISSING,
            now=now, closure_attempts=3,
        )
        second = mark_scan_outcome(
            connection, source_id=source_id, external_id="404", outcome=OUTCOME_MISSING,
            now=now + timedelta(hours=2), closure_attempts=3,
        )
        third = mark_scan_outcome(
            connection, source_id=source_id, external_id="404", outcome=OUTCOME_MISSING,
            now=now + timedelta(hours=4), closure_attempts=3,
        )

    assert first == SCAN_STATE_RETRY
    assert second == SCAN_STATE_RETRY
    assert third == SCAN_STATE_CLOSED


def test_parse_invalid_never_closes(pg_engine: Any) -> None:
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    with pg_engine.begin() as connection:
        source_id = _source(connection)
        sync_sitemap_entries(connection, source_id=source_id, entries=[_entry("bad")], now=now)
        for attempt in range(6):
            state = mark_scan_outcome(
                connection,
                source_id=source_id,
                external_id="bad",
                outcome=OUTCOME_INVALID,
                now=now + timedelta(hours=attempt),
                closure_attempts=2,
            )

    assert state == SCAN_STATE_RETRY


def test_classified_member_stays_classified_on_resync(pg_engine: Any) -> None:
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    with pg_engine.begin() as connection:
        source_id = _source(connection)
        sync_sitemap_entries(connection, source_id=source_id, entries=[_entry("1")], now=now)
        mark_scan_outcome(
            connection, source_id=source_id, external_id="1", outcome=OUTCOME_CLASSIFIED, now=now
        )
        # A corrected lastmod and url refresh evidence without resetting state.
        sync_sitemap_entries(
            connection,
            source_id=source_id,
            entries=[_entry("1", lastmod=now - timedelta(days=1))],
            now=now + timedelta(hours=1),
        )
        state = connection.execute(
            sa.text("select state from source_scan_members where source_id = :source_id and external_id = '1'"),
            {"source_id": source_id},
        ).scalar_one()
        drained = frontier_drained(connection, source_id=source_id)

    assert state == SCAN_STATE_CLASSIFIED
    assert drained is True


def test_absent_members_close_only_after_grace(pg_engine: Any) -> None:
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    with pg_engine.begin() as connection:
        source_id = _source(connection)
        sync_sitemap_entries(
            connection, source_id=source_id, entries=[_entry("keep"), _entry("gone")], now=now
        )
        mark_scan_outcome(
            connection, source_id=source_id, external_id="gone", outcome=OUTCOME_CLASSIFIED, now=now
        )
        # "gone" disappears from the sitemap at T+1h.
        sync_sitemap_entries(
            connection, source_id=source_id, entries=[_entry("keep")], now=now + timedelta(hours=1)
        )
        closed_early = close_absent_members(
            connection, source_id=source_id, grace_hours=24, now=now + timedelta(hours=2)
        )
        closed_late = close_absent_members(
            connection, source_id=source_id, grace_hours=24, now=now + timedelta(hours=30)
        )

    assert closed_early == 0
    assert closed_late == 1


def test_backlog_report_reports_age_and_counts_independent_of_cap(pg_engine: Any) -> None:
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    with pg_engine.begin() as connection:
        source_id = _source(connection)
        sync_sitemap_entries(
            connection,
            source_id=source_id,
            entries=[_entry(str(i), lastmod=now - timedelta(days=10)) for i in range(150)],
            now=now,
        )
        batch = claim_scan_batch(connection, source_id=source_id, limit=100, now=now)
        for member in batch:
            mark_scan_outcome(
                connection,
                source_id=source_id,
                external_id=member.external_id,
                outcome=OUTCOME_CLASSIFIED,
                now=now,
            )
        report = scan_backlog_report(connection, source_id=source_id, now=now)

    assert len(batch) == 100
    assert report["classified"] == 100
    assert report["pending"] == 50
    assert report["oldest_open_age_hours"] is not None
    assert report["oldest_open_age_hours"] >= 0


def _seed_job_with_occurrence(connection: Any, *, source_id: int, external_id: str) -> int:
    job_id = int(
        connection.execute(
            sa.text(
                """
                insert into jobs (title, employer, description, status)
                values ('Kirjastonhoitaja', 'Kaupunki', 'Kirjasto', 'active')
                returning id
                """
            )
        ).scalar_one()
    )
    listing_id = int(
        connection.execute(
            sa.text(
                """
                insert into raw_listings (source_id, external_id, canonical_source_url, content_hash, payload)
                values (:source_id, :external_id, :url, 'h', '{}'::jsonb)
                returning id
                """
            ),
            {
                "source_id": source_id,
                "external_id": external_id,
                "url": f"https://x.invalid/{external_id}",
            },
        ).scalar_one()
    )
    connection.execute(
        sa.text(
            """
            insert into job_sources (job_id, source_id, raw_listing_id, external_id, last_content_hash)
            values (:job_id, :source_id, :listing_id, :external_id, 'h')
            """
        ),
        {"job_id": job_id, "source_id": source_id, "listing_id": listing_id, "external_id": external_id},
    )
    return job_id


def test_closure_removes_only_when_no_live_occurrence_remains(pg_engine: Any) -> None:
    from app.collection.scan_state import apply_scan_closure, reopen_scan_members

    with pg_engine.begin() as connection:
        source_a = _source(connection, name="jobly")
        source_b = _source(connection, name="other")
        job_id = _seed_job_with_occurrence(connection, source_id=source_a, external_id="101")
        # A second, live occurrence from another enabled source.
        listing_id = int(
            connection.execute(
                sa.text(
                    """
                    insert into raw_listings (source_id, external_id, canonical_source_url, content_hash, payload)
                    values (:source_id, 'b-1', 'https://y.invalid/b-1', 'h', '{}'::jsonb)
                    returning id
                    """
                ),
                {"source_id": source_b},
            ).scalar_one()
        )
        connection.execute(
            sa.text(
                """
                insert into job_sources (job_id, source_id, raw_listing_id, external_id, last_content_hash)
                values (:job_id, :source_id, :listing_id, 'b-1', 'h')
                """
            ),
            {"job_id": job_id, "source_id": source_b, "listing_id": listing_id},
        )

        removed_with_live = apply_scan_closure(
            connection, source_id=source_a, closed_external_ids=["101"]
        )
        status_with_live = connection.execute(
            sa.text("select status from jobs where id = :id"), {"id": job_id}
        ).scalar_one()

        # Close the last live occurrence as well.
        removed_all = apply_scan_closure(
            connection, source_id=source_b, closed_external_ids=["b-1"]
        )
        status_all_closed = connection.execute(
            sa.text("select status from jobs where id = :id"), {"id": job_id}
        ).scalar_one()

        reopened = reopen_scan_members(
            connection, source_id=source_a, reopened_external_ids=["101"]
        )
        status_reopened = connection.execute(
            sa.text("select status from jobs where id = :id"), {"id": job_id}
        ).scalar_one()

    assert removed_with_live == 0
    assert status_with_live == "active"
    assert removed_all == 1
    assert status_all_closed == "removed"
    assert reopened == 1
    assert status_reopened == "active"
