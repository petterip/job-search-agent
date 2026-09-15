"""Durable, resumable sitemap scan state for source acquisition.

A newest-first truncated scan cannot safely advance a cursor: unprocessed older
URLs would be skipped forever, while holding the cursor fixed re-fetches the
same prefix. This module keeps a frozen frontier of sitemap identities per
source and advances a durable per-member state machine instead:

- ``pending``    — seen, never successfully persisted.
- ``classified`` — successfully fetched and persisted at least once.
- ``retry``      — transient failure (404 / parse) with attempts and a due time.
- ``closed``     — confirmed gone (absent beyond grace, or 404 beyond grace).

Progress is committed only after the caller persists the listings, so the
cursor never moves ahead of durable work. A no-date entry is retained rather
than dropped, and a single 404 stays retryable until the source-specific grace
confirms closure. Parse-invalid is never treated as closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.engine import Connection

SCAN_STATE_PENDING = "pending"
SCAN_STATE_CLASSIFIED = "classified"
SCAN_STATE_RETRY = "retry"
SCAN_STATE_CLOSED = "closed"

OUTCOME_CLASSIFIED = "classified"
OUTCOME_MISSING = "missing"
OUTCOME_INVALID = "invalid"


@dataclass(frozen=True)
class ScanMember:
    external_id: str
    canonical_url: str
    lastmod: datetime | None
    state: str
    attempts: int


@dataclass(frozen=True)
class SitemapEntry:
    external_id: str
    canonical_url: str
    lastmod: datetime | None


def sync_sitemap_entries(
    connection: Connection,
    *,
    source_id: int,
    entries: Iterable[SitemapEntry],
    now: datetime | None = None,
) -> dict[str, int]:
    """Freeze/refresh the frontier from the current sitemap.

    New identities become ``pending``; known identities have their mutable
    evidence (url, lastmod) refreshed and are marked present. Identities that
    disappear from the sitemap are marked absent (a closure candidate) but keep
    their state until grace confirms it.
    """
    moment = now or datetime.now(timezone.utc)
    entry_list = list(entries)
    present_ids = [entry.external_id for entry in entry_list]
    inserted = 0
    updated = 0
    for entry in entry_list:
        existing = connection.execute(
            sa.text(
                """
                select state from source_scan_members
                where source_id = :source_id and external_id = :external_id
                """
            ),
            {"source_id": source_id, "external_id": entry.external_id},
        ).scalar_one_or_none()
        connection.execute(
            sa.text(
                """
                insert into source_scan_members (
                    source_id, external_id, canonical_url, lastmod, state,
                    sitemap_present, first_seen_at, updated_at
                )
                values (
                    :source_id, :external_id, :canonical_url, :lastmod, 'pending',
                    true, :now, :now
                )
                on conflict (source_id, external_id)
                do update set
                    canonical_url = excluded.canonical_url,
                    lastmod = excluded.lastmod,
                    sitemap_present = true,
                    state = case
                        when source_scan_members.state = 'closed' then 'pending'
                        when source_scan_members.sitemap_present = false then 'pending'
                        when excluded.lastmod is not null
                         and excluded.lastmod is distinct from source_scan_members.lastmod
                        then 'pending'
                        else source_scan_members.state
                    end,
                    next_attempt_at = case
                        when source_scan_members.state = 'closed'
                          or source_scan_members.sitemap_present = false
                          or (
                              excluded.lastmod is not null
                              and excluded.lastmod is distinct from source_scan_members.lastmod
                          )
                        then null
                        else source_scan_members.next_attempt_at
                    end,
                    updated_at = :now
                """
            ),
            {
                "source_id": source_id,
                "external_id": entry.external_id,
                "canonical_url": entry.canonical_url,
                "lastmod": entry.lastmod,
                "now": moment,
            },
        )
        if existing is None:
            inserted += 1
        else:
            updated += 1
    absent_marked = 0
    if present_ids:
        absent_marked = int(
            connection.execute(
                sa.text(
                    """
                    update source_scan_members
                    set sitemap_present = false,
                        updated_at = :now
                    where source_id = :source_id
                      and sitemap_present = true
                      and external_id <> all(:present_ids)
                    """
                ),
                {"source_id": source_id, "present_ids": present_ids, "now": moment},
            ).rowcount
            or 0
        )
    else:
        absent_marked = int(
            connection.execute(
                sa.text(
                    """
                    update source_scan_members
                    set sitemap_present = false,
                        updated_at = :now
                    where source_id = :source_id
                      and sitemap_present = true
                    """
                ),
                {"source_id": source_id, "now": moment},
            ).rowcount
            or 0
        )
    _refresh_progress(connection, source_id=source_id, now=moment)
    return {
        "frontier_size": len(entry_list),
        "inserted": inserted,
        "updated": updated,
        "absent_marked": absent_marked,
    }


def claim_scan_batch(
    connection: Connection,
    *,
    source_id: int,
    limit: int,
    now: datetime | None = None,
) -> list[ScanMember]:
    """Claim the oldest unresolved members of the frozen frontier.

    Oldest-first by ``(lastmod, external_id)`` with no-date entries first, so a
    bounded run never re-fetches the same newest prefix and no-date entries are
    not dropped.
    """
    if limit <= 0:
        return []
    moment = now or datetime.now(timezone.utc)
    rows = connection.execute(
        sa.text(
            """
            select external_id, canonical_url, lastmod, state, attempts
            from source_scan_members
            where source_id = :source_id
              and state in ('pending', 'retry')
              and (next_attempt_at is null or next_attempt_at <= :now)
            order by lastmod asc nulls first, external_id asc
            limit :limit
            """
        ),
        {"source_id": source_id, "now": moment, "limit": limit},
    ).mappings()
    return [
        ScanMember(
            external_id=str(row["external_id"]),
            canonical_url=str(row["canonical_url"]),
            lastmod=row["lastmod"],
            state=str(row["state"]),
            attempts=int(row["attempts"]),
        )
        for row in rows
    ]


def mark_scan_outcome(
    connection: Connection,
    *,
    source_id: int,
    external_id: str,
    outcome: str,
    now: datetime | None = None,
    retry_delay_minutes: int = 60,
    closure_attempts: int = 3,
) -> str:
    """Record one member outcome and return the resulting state.

    A single 404 or parse failure is retryable; a 404 becomes ``closed`` only
    after ``closure_attempts`` confirmations. Parse-invalid never closes.
    """
    moment = now or datetime.now(timezone.utc)
    row = connection.execute(
        sa.text(
            """
            select attempts, missing_attempts from source_scan_members
            where source_id = :source_id and external_id = :external_id
            """
        ),
        {"source_id": source_id, "external_id": external_id},
    ).mappings().one_or_none()
    if row is None:
        return SCAN_STATE_PENDING
    attempts = int(row["attempts"]) + 1
    missing_attempts = int(row["missing_attempts"] or 0)
    if outcome == OUTCOME_CLASSIFIED:
        state = SCAN_STATE_CLASSIFIED
        next_attempt_at = None
        error = None
        missing_attempts = 0
    elif outcome == OUTCOME_MISSING:
        # Only consecutive 404 confirmations count toward closure; parse and
        # transport failures must not shorten the grace period.
        missing_attempts += 1
        if missing_attempts >= closure_attempts:
            state = SCAN_STATE_CLOSED
            next_attempt_at = None
            error = "missing_confirmed"
        else:
            state = SCAN_STATE_RETRY
            next_attempt_at = moment + timedelta(minutes=retry_delay_minutes)
            error = "missing_retry"
    else:
        # Parse/schema failure is never closure evidence.
        state = SCAN_STATE_RETRY
        next_attempt_at = moment + timedelta(minutes=retry_delay_minutes)
        error = OUTCOME_INVALID
    connection.execute(
        sa.text(
            """
            update source_scan_members
            set state = :state,
                attempts = :attempts,
                missing_attempts = :missing_attempts,
                last_error = :error,
                next_attempt_at = :next_attempt_at,
                updated_at = :now
            where source_id = :source_id and external_id = :external_id
            """
        ),
        {
            "source_id": source_id,
            "external_id": external_id,
            "state": state,
            "attempts": attempts,
            "missing_attempts": missing_attempts,
            "error": error,
            "next_attempt_at": next_attempt_at,
            "now": moment,
        },
    )
    _refresh_progress(connection, source_id=source_id, now=moment)
    return state


def close_absent_members(
    connection: Connection,
    *,
    source_id: int,
    grace_hours: int,
    now: datetime | None = None,
) -> int:
    """Close members absent from the sitemap beyond the source grace period."""
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(hours=grace_hours)
    closed_ids = [
        str(row[0])
        for row in connection.execute(
            sa.text(
                """
                update source_scan_members
                set state = 'closed',
                    last_error = 'absent_beyond_grace',
                    next_attempt_at = null,
                    updated_at = :now
                where source_id = :source_id
                  and sitemap_present = false
                  and state <> 'closed'
                  and updated_at <= :cutoff
                returning external_id
                """
            ),
            {"source_id": source_id, "now": moment, "cutoff": cutoff},
        )
    ]
    _refresh_progress(connection, source_id=source_id, now=moment)
    return closed_ids


def _refresh_progress(connection: Connection, *, source_id: int, now: datetime) -> None:
    counts = connection.execute(
        sa.text(
            """
            select
                count(*) filter (where state <> 'closed') as frontier_size,
                count(*) filter (where state = 'classified') as classified_count,
                count(*) filter (where state in ('pending', 'retry')) as pending_count
            from source_scan_members
            where source_id = :source_id
            """
        ),
        {"source_id": source_id},
    ).mappings().one()
    frontier_size = int(counts["frontier_size"] or 0)
    pending_count = int(counts["pending_count"] or 0)
    connection.execute(
        sa.text(
            """
            insert into source_scan_progress (
                source_id, frontier_size, classified_count, pending_count,
                frontier_drained, last_scan_at, updated_at
            )
            values (:source_id, :frontier_size, :classified_count, :pending_count,
                    :drained, :now, :now)
            on conflict (source_id)
            do update set
                frontier_size = excluded.frontier_size,
                classified_count = excluded.classified_count,
                pending_count = excluded.pending_count,
                frontier_drained = excluded.frontier_drained,
                last_scan_at = excluded.last_scan_at,
                updated_at = excluded.updated_at
            """
        ),
        {
            "source_id": source_id,
            "frontier_size": frontier_size,
            "classified_count": int(counts["classified_count"] or 0),
            "pending_count": pending_count,
            "drained": pending_count == 0 and frontier_size > 0,
            "now": now,
        },
    )


def frontier_drained(connection: Connection, *, source_id: int) -> bool:
    pending = connection.execute(
        sa.text(
            """
            select count(*)
            from source_scan_members
            where source_id = :source_id
              and state in ('pending', 'retry')
            """
        ),
        {"source_id": source_id},
    ).scalar_one()
    return int(pending) == 0


def apply_scan_closure(
    connection: Connection,
    *,
    source_id: int,
    closed_external_ids: Iterable[str],
    now: datetime | None = None,
) -> int:
    """Hide a job only when every remaining enabled occurrence is closed.

    A job is removed only if it currently has an occurrence for this source with
    one of the confirmed-closed external ids and no other enabled, non-closed
    occurrence. One live occurrence therefore preserves the canonical job, and a
    partial/blocked source can never establish absence.
    """
    ids = [str(value) for value in closed_external_ids]
    if not ids:
        return 0
    moment = now or datetime.now(timezone.utc)
    # Persist occurrence-level closure evidence first; the canonical status is
    # then derived from all enabled occurrences, closed ones included.
    connection.execute(
        sa.text(
            """
            update job_sources
            set closed_at = :now
            where source_id = :source_id
              and external_id = any(:closed_ids)
              and closed_at is null
            """
        ),
        {"source_id": source_id, "closed_ids": ids, "now": moment},
    )
    return int(
        connection.execute(
            sa.text(
                """
                update jobs j
                set status = 'removed',
                    updated_at = :now
                where j.status = 'active'
                  and exists (
                      select 1
                      from job_sources js
                      where js.job_id = j.id
                        and js.source_id = :source_id
                        and js.external_id = any(:closed_ids)
                  )
                  and not exists (
                      select 1
                      from job_sources live_js
                      join sources live_s on live_s.id = live_js.source_id
                      where live_js.job_id = j.id
                        and live_s.enabled = true
                        and live_js.closed_at is null
                  )
                """
            ),
            {"source_id": source_id, "closed_ids": ids, "now": moment},
        ).rowcount
        or 0
    )


def reopen_scan_members(
    connection: Connection,
    *,
    source_id: int,
    reopened_external_ids: Iterable[str],
    now: datetime | None = None,
) -> int:
    """Restore a previously removed job when its occurrence reappears."""
    ids = [str(value) for value in reopened_external_ids]
    if not ids:
        return 0
    moment = now or datetime.now(timezone.utc)
    connection.execute(
        sa.text(
            """
            update job_sources
            set closed_at = null
            where source_id = :source_id
              and external_id = any(:reopened_ids)
              and closed_at is not null
            """
        ),
        {"source_id": source_id, "reopened_ids": ids},
    )
    return int(
        connection.execute(
            sa.text(
                """
                update jobs j
                set status = 'active',
                    updated_at = :now
                where j.status = 'removed'
                  and exists (
                      select 1
                      from job_sources js
                      where js.job_id = j.id
                        and js.source_id = :source_id
                        and js.external_id = any(:reopened_ids)
                        and js.closed_at is null
                  )
                """
            ),
            {"source_id": source_id, "reopened_ids": ids, "now": moment},
        ).rowcount
        or 0
    )


def scan_backlog_report(
    connection: Connection,
    *,
    source_id: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Backlog age and per-state counts, independent of a single run's cap."""
    moment = now or datetime.now(timezone.utc)
    row = connection.execute(
        sa.text(
            """
            select
                count(*) filter (where state = 'pending') as pending,
                count(*) filter (where state = 'retry') as retry,
                count(*) filter (where state = 'classified') as classified,
                count(*) filter (where state = 'closed') as closed,
                count(*) filter (where sitemap_present = false and state <> 'closed') as absent,
                min(first_seen_at) filter (where state in ('pending', 'retry')) as oldest_open_at
            from source_scan_members
            where source_id = :source_id
            """
        ),
        {"source_id": source_id},
    ).mappings().one()
    oldest = row["oldest_open_at"]
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return {
        "pending": int(row["pending"] or 0),
        "retry": int(row["retry"] or 0),
        "classified": int(row["classified"] or 0),
        "closed": int(row["closed"] or 0),
        "absent": int(row["absent"] or 0),
        "oldest_open_at": oldest.isoformat() if oldest is not None else None,
        "oldest_open_age_hours": (
            round((moment - oldest).total_seconds() / 3600, 1) if oldest is not None else None
        ),
    }
