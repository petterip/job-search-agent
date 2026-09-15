import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.feedback_benchmark import latest_learned_profile_summary, run_feedback_benchmark


def scalar_int(connection: Connection, sql: str) -> int:
    return int(connection.execute(sa.text(sql)).scalar_one())


def json_safe_latest_run(row: Any) -> dict[str, Any] | None:
    """Convert the latest learning run row into JSON-serializable values."""
    if row is None:
        return None
    learned_version = row["learned_version"]
    started_at = row["started_at"]
    finished_at = row["finished_at"]
    return {
        "id": int(row["id"]),
        "status": str(row["status"]),
        "learned_version": int(learned_version) if learned_version is not None else None,
        "started_at": started_at.isoformat() if started_at is not None else None,
        "finished_at": finished_at.isoformat() if finished_at is not None else None,
    }


def run_database_audit(connection: Connection) -> dict[str, Any]:
    job_status_rows = connection.execute(
        sa.text(
            """
            select status, count(*) as count
            from jobs
            group by status
            order by status
            """
        )
    ).mappings()
    source_rows = connection.execute(
        sa.text(
            """
            select
                s.name,
                s.enabled,
                count(js.id) as linked_listings,
                count(js.id) filter (where js.application_url is null) as missing_application_urls,
                count(distinct js.job_id) filter (where j.status = 'active') as active_jobs
            from sources s
            left join job_sources js on js.source_id = s.id
            left join jobs j on j.id = js.job_id
            group by s.id, s.name, s.enabled
            order by s.enabled desc, s.name
            """
        )
    ).mappings()
    recent_event_rows = connection.execute(
        sa.text(
            """
            select
                source_name,
                count(*) filter (where level in ('ERROR', 'CRITICAL')) as errors,
                count(*) filter (where level = 'WARNING') as warnings
            from source_run_events
            where created_at >= now() - interval '7 days'
            group by source_name
            order by source_name
            """
        )
    ).mappings()
    return {
        "job_status_counts": {row["status"]: int(row["count"]) for row in job_status_rows},
        "active_jobs_without_enabled_source": scalar_int(
            connection,
            """
            select count(*)
            from jobs j
            where j.status = 'active'
              and not exists (
                  select 1
                  from job_sources js
                  join sources s on s.id = js.source_id
                  where js.job_id = j.id
                    and s.enabled = true
              )
            """,
        ),
        "active_jobs_missing_title": scalar_int(
            connection,
            """
            select count(*)
            from jobs
            where status = 'active'
              and length(trim(title)) = 0
            """,
        ),
        "active_jobs_missing_description": scalar_int(
            connection,
            """
            select count(*)
            from jobs
            where status = 'active'
              and nullif(trim(coalesce(description, '')), '') is null
            """,
        ),
        "active_jobs_missing_location": scalar_int(
            connection,
            """
            select count(*)
            from jobs
            where status = 'active'
              and nullif(trim(coalesce(location, '')), '') is null
            """,
        ),
        "active_jobs_remote_or_hybrid": scalar_int(
            connection,
            """
            select count(*)
            from jobs
            where status = 'active'
              and (location ilike '%Etä%' or location ilike '%Hybridi%')
            """,
        ),
        "source_counts": [
            {
                "name": row["name"],
                "enabled": bool(row["enabled"]),
                "linked_listings": int(row["linked_listings"]),
                "missing_application_urls": int(row["missing_application_urls"]),
                "active_jobs": int(row["active_jobs"]),
            }
            for row in source_rows
        ],
        "recent_source_events": [
            {
                "source_name": row["source_name"],
                "errors": int(row["errors"]),
                "warnings": int(row["warnings"]),
            }
            for row in recent_event_rows
        ],
        "recommendations": scalar_int(connection, "select count(*) from recommendations"),
        "active_recommendations": scalar_int(
            connection,
            "select count(*) from recommendations where is_active = true",
        ),
        "recommendations_with_llm_score": scalar_int(
            connection,
            "select count(*) from recommendations where is_active = true and llm_score is not null",
        ),
        "llm_evaluations": scalar_int(connection, "select count(*) from llm_evaluations"),
        "recommendation_feedback": scalar_int(
            connection,
            "select count(*) from recommendation_feedback",
        ),
        "feedback_by_rating": {
            int(row["rating"]): int(row["count"])
            for row in connection.execute(
                sa.text(
                    """
                    select rating, count(*) as count
                    from recommendation_feedback
                    group by rating
                    order by rating
                    """
                )
            ).mappings()
        },
        "feedback_analysis_status": {
            str(row["analysis_status"]): int(row["count"])
            for row in connection.execute(
                sa.text(
                    """
                    select analysis_status, count(*) as count
                    from recommendation_feedback
                    group by analysis_status
                    order by analysis_status
                    """
                )
            ).mappings()
        },
        "feedback_llm_analyses": scalar_int(
            connection,
            "select count(*) from feedback_llm_analyses",
        ),
        "latest_learning_run": json_safe_latest_run(
            connection.execute(
                sa.text(
                    """
                    select id, status, learned_version, started_at, finished_at
                    from learning_runs
                    order by id desc
                    limit 1
                    """
                )
            ).mappings().one_or_none()
        ),
        "learned_profile": latest_learned_profile_summary(connection),
        "feedback_benchmark": run_feedback_benchmark(connection),
        "broken_duunitori_urls": scalar_int(
            connection,
            """
            select count(*)
            from job_sources js
            join sources s on s.id = js.source_id
            where s.name = 'duunitori'
              and js.application_url like 'https://duunitori.fi/tyopaikat/%'
              and js.application_url not like 'https://duunitori.fi/tyopaikat/tyo/%'
            """,
        ),
        "tmt_urls_not_on_portal": scalar_int(
            connection,
            """
            select count(*)
            from job_sources js
            join sources s on s.id = js.source_id
            where s.name in ('tmt', 'tmt_oulu')
              and js.application_url is not null
              and js.application_url not like 'https://tyomarkkinatori.fi/%'
            """,
        ),
    }


def run_audit() -> dict[str, Any]:
    engine = get_engine()
    with engine.connect() as connection:
        return run_database_audit(connection)


def main() -> None:
    print(json.dumps(run_audit(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
