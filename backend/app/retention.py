"""Retention dry-run reporting.

Retention changes are irreversible and the plan requires a reviewed row/byte
inventory, a tested restore and preserved feedback/enrichment attribution before
anything is deleted. This module therefore reports only: per-table size and
age-based candidate counts. There is deliberately no automatic blanket
compaction of raw payloads and no deletion path here.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.db import get_engine


def _table_sizes(connection: Connection, tables: list[str]) -> dict[str, dict[str, int]]:
    sizes: dict[str, dict[str, int]] = {}
    for table in tables:
        row = connection.execute(
            sa.text(
                """
                select
                    pg_total_relation_size(:table) as total_bytes,
                    (select count(*) from information_schema.columns where table_name = :table) as columns
                """
            ),
            {"table": table},
        ).mappings().one()
        total_bytes = int(row["total_bytes"] or 0)
        sizes[table] = {"total_bytes": total_bytes, "total_mb": round(total_bytes / 1048576, 2)}
    return sizes


def _count(connection: Connection, sql: str, params: dict[str, Any]) -> int:
    return int(connection.execute(sa.text(sql), params).scalar_one())


def retention_report(
    connection: Connection,
    *,
    now: datetime | None = None,
    raw_listing_days: int | None = None,
    activity_days: int | None = None,
) -> dict[str, Any]:
    """Report table sizes and age-based retention candidates without deleting."""
    settings = get_settings()
    moment = now or datetime.now(timezone.utc)
    raw_days = raw_listing_days if raw_listing_days is not None else settings.retention_raw_listing_days
    act_days = activity_days if activity_days is not None else settings.retention_activity_days
    raw_cutoff = moment - timedelta(days=raw_days)
    activity_cutoff = moment - timedelta(days=act_days)

    tables = [
        "jobs",
        "job_sources",
        "raw_listings",
        "source_runs",
        "source_run_events",
        "recommendations",
        "recommendation_feedback",
        "llm_evaluations",
        "job_embeddings",
        "transit_distance_cache",
        "job_enrichments",
        "source_scan_members",
    ]
    return {
        "as_of": moment.isoformat(),
        "dry_run": True,
        "thresholds": {
            "raw_listing_days": raw_days,
            "activity_days": act_days,
        },
        "table_sizes": _table_sizes(connection, tables),
        "candidates": {
            "raw_listings_not_seen_since_cutoff": _count(
                connection,
                "select count(*) from raw_listings where last_seen_at < :cutoff",
                {"cutoff": raw_cutoff},
            ),
            "source_run_events_older_than_cutoff": _count(
                connection,
                "select count(*) from source_run_events where created_at < :cutoff",
                {"cutoff": activity_cutoff},
            ),
            "closed_job_sources_older_than_cutoff": _count(
                connection,
                "select count(*) from job_sources where closed_at is not null and closed_at < :cutoff",
                {"cutoff": activity_cutoff},
            ),
            "inactive_recommendations_older_than_cutoff": _count(
                connection,
                "select count(*) from recommendations where is_active = false and created_at < :cutoff",
                {"cutoff": activity_cutoff},
            ),
            "expired_transit_cache_entries": _count(
                connection,
                "select count(*) from transit_distance_cache where fetched_at < :cutoff",
                {"cutoff": activity_cutoff},
            ),
        },
        "notes": [
            "Report only: no rows are deleted by this command.",
            "Raw payloads are not compacted automatically.",
            "A restore drill and reviewed row inventory are required before any deletion.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Report retention candidates (dry run only).")
    parser.add_argument("--dry-run", action="store_true", help="Report only; this is the sole mode.")
    args = parser.parse_args()
    if not args.dry_run:
        raise SystemExit("retention deletion is not implemented; run with --dry-run")
    engine = get_engine()
    with engine.connect() as connection:
        report = retention_report(connection)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
