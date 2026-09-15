from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.enrichers.base import EnrichmentInput, EnrichmentResult

logger = logging.getLogger("matcher.enrichment")

ENRICHMENT_VERSION = "1"


def compute_enrichment_input_hash(
    *,
    last_content_hash: str,
    application_url: str | None,
    canonical_source_url: str | None,
    title: str,
    employer: str | None,
    published_at: datetime | None,
    enricher: str,
    enricher_version: str = ENRICHMENT_VERSION,
) -> str:
    parts = [
        last_content_hash,
        application_url or "",
        canonical_source_url or "",
        title,
        employer or "",
        published_at.isoformat() if published_at else "",
        enricher,
        enricher_version,
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def description_is_short(description: str | None, *, threshold: int) -> bool:
    if not description or not description.strip():
        return True
    return len(description.strip()) < threshold


def load_description_state(connection: Connection, job_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        sa.text(
            """
            select
                jds.job_id,
                jds.provenance,
                jds.job_source_id,
                jds.job_enrichment_id,
                je.enricher,
                je.input_hash,
                jds.confidence
            from job_description_state jds
            left join job_enrichments je on je.id = jds.job_enrichment_id
            where jds.job_id = :job_id
            """
        ),
        {"job_id": job_id},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def record_source_provenance(
    connection: Connection,
    *,
    job_id: int,
    job_source_id: int,
) -> None:
    connection.execute(
        sa.text(
            """
            insert into job_description_state (
                job_id, provenance, job_source_id, job_enrichment_id, confidence, applied_at
            )
            values (:job_id, 'source', :job_source_id, null, null, now())
            on conflict (job_id) do update set
                provenance = 'source',
                job_source_id = excluded.job_source_id,
                job_enrichment_id = null,
                confidence = null,
                applied_at = now()
            """
        ),
        {"job_id": job_id, "job_source_id": job_source_id},
    )


def mark_source_description(
    connection: Connection,
    *,
    job_id: int,
    job_source_id: int,
    description: str,
) -> None:
    record_source_provenance(connection, job_id=job_id, job_source_id=job_source_id)
    connection.execute(
        sa.text(
            """
            update jobs
            set description = :description,
                updated_at = now()
            where id = :job_id
            """
        ),
        {"job_id": job_id, "description": description},
    )


def _load_job_description(connection: Connection, job_id: int) -> str | None:
    value = connection.execute(
        sa.text("select description from jobs where id = :job_id"),
        {"job_id": job_id},
    ).scalar_one_or_none()
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def canonical_field(existing: str | None, incoming: str | None) -> str | None:
    """Deterministic canonical precedence for a scalar job field.

    A richer value wins; ties fall back to the lexicographically smaller value
    so two sources collected in either order produce the same canonical row.
    """
    current = (existing or "").strip()
    candidate = (incoming or "").strip()
    if not current:
        return candidate or None
    if not candidate:
        return current
    if len(candidate) > len(current):
        return candidate
    if len(candidate) == len(current):
        return min(current, candidate)
    return current


def preserve_enriched_description_on_upsert(
    connection: Connection,
    *,
    job_id: int,
    source_description: str | None,
    job_source_id: int,
    content_changed: bool = True,
) -> str | None:
    """Choose the canonical description, keeping the richer text.

    A short nonempty source summary must not replace a complete enriched body,
    and an unchanged source keeps the canonical text. A genuine source edit from
    the same occurrence may replace an equally rich source body.
    """
    existing_description = _load_job_description(connection, job_id)
    incoming = (source_description or "").strip()
    if not incoming:
        return None
    if not existing_description or not existing_description.strip():
        record_source_provenance(connection, job_id=job_id, job_source_id=job_source_id)
        return incoming
    if not content_changed:
        return None
    existing_text = existing_description.strip()
    state = load_description_state(connection, job_id)
    if state and state.get("provenance") == "enriched" and len(incoming) <= len(existing_text):
        return None
    if len(incoming) < len(existing_text):
        same_occurrence = bool(state and state.get("job_source_id") == job_source_id)
        if not same_occurrence:
            return None
    if len(incoming) == len(existing_text) and incoming > existing_text:
        return None
    record_source_provenance(connection, job_id=job_id, job_source_id=job_source_id)
    return incoming


def apply_enrichment_result(
    connection: Connection,
    result: EnrichmentResult,
    *,
    short_description_threshold: int,
) -> bool:
    occurrence = connection.execute(
        sa.text(
            """
            select
                js.job_id,
                js.raw_listing_id,
                js.last_content_hash,
                js.application_url,
                rl.canonical_source_url,
                j.status,
                j.title,
                j.employer,
                j.published_at,
                s.enabled
            from job_sources js
            join jobs j on j.id = js.job_id
            join raw_listings rl on rl.id = js.raw_listing_id
            join sources s on s.id = js.source_id
            where js.id = :job_source_id
            """
        ),
        {"job_source_id": result.job_source_id},
    ).mappings().one_or_none()
    if occurrence is None or occurrence["status"] != "active" or not occurrence["enabled"]:
        return False
    # Publish only when the occurrence still carries the input that was enriched.
    current_input_hash = compute_enrichment_input_hash(
        last_content_hash=str(occurrence["last_content_hash"]),
        application_url=occurrence["application_url"],
        canonical_source_url=occurrence["canonical_source_url"],
        title=str(occurrence["title"]),
        employer=occurrence["employer"],
        published_at=occurrence["published_at"],
        enricher=result.enricher,
    )
    if current_input_hash != result.input_hash:
        logger.info(
            "event=enrichment_result_stale job_source_id=%s enricher=%s",
            result.job_source_id,
            result.enricher,
        )
        return False
    job_id = int(occurrence["job_id"])
    raw_listing_id = int(occurrence["raw_listing_id"])

    enrichment_id = connection.execute(
        sa.text(
            """
            insert into job_enrichments (
                job_id,
                job_source_id,
                raw_listing_id,
                enricher,
                input_hash,
                description,
                structured_fields,
                method,
                confidence,
                applied_to_job
            )
            values (
                :job_id,
                :job_source_id,
                :raw_listing_id,
                :enricher,
                :input_hash,
                :description,
                CAST(:structured_fields AS jsonb),
                :method,
                :confidence,
                false
            )
            on conflict (job_source_id, enricher, input_hash) do update set
                job_id = excluded.job_id,
                raw_listing_id = excluded.raw_listing_id,
                description = excluded.description,
                structured_fields = excluded.structured_fields,
                method = excluded.method,
                confidence = excluded.confidence
            returning id
            """
        ),
        {
            "job_id": job_id,
            "job_source_id": result.job_source_id,
            "raw_listing_id": raw_listing_id,
            "enricher": result.enricher,
            "input_hash": result.input_hash,
            "description": result.description,
            "structured_fields": json.dumps(result.structured_fields, ensure_ascii=False),
            "method": result.method.value,
            "confidence": result.confidence,
        },
    ).scalar_one()

    if not result.description or not result.description.strip():
        return False

    current = connection.execute(
        sa.text("select description from jobs where id = :job_id"),
        {"job_id": job_id},
    ).scalar_one_or_none()
    state = load_description_state(connection, job_id)
    current_text = (current or "").strip()
    new_text = result.description.strip()

    if state and state["provenance"] == "source" and not description_is_short(
        current,
        threshold=short_description_threshold,
    ):
        return False

    should_apply = False

    if description_is_short(current, threshold=short_description_threshold):
        should_apply = True
    elif state and state["provenance"] == "enriched":
        current_confidence = float(state.get("confidence") or 0)
        if result.confidence >= current_confidence:
            should_apply = True
    elif not state and not current_text:
        should_apply = True

    if not should_apply:
        return False

    connection.execute(
        sa.text(
            """
            update jobs
            set description = :description,
                updated_at = now()
            where id = :job_id
            """
        ),
        {"job_id": job_id, "description": new_text},
    )
    connection.execute(
        sa.text(
            """
            insert into job_description_state (
                job_id, provenance, job_source_id, job_enrichment_id, confidence, applied_at
            )
            values (:job_id, 'enriched', null, :job_enrichment_id, :confidence, now())
            on conflict (job_id) do update set
                provenance = 'enriched',
                job_source_id = null,
                job_enrichment_id = excluded.job_enrichment_id,
                confidence = excluded.confidence,
                applied_at = now()
            """
        ),
        {
            "job_id": job_id,
            "job_enrichment_id": enrichment_id,
            "confidence": result.confidence,
        },
    )
    connection.execute(
        sa.text(
            """
            update job_enrichments
            set applied_to_job = true
            where id = :job_enrichment_id
            """
        ),
        {"job_enrichment_id": enrichment_id},
    )
    return True


def list_enrichment_candidates(
    connection: Connection,
    *,
    enricher: str,
    short_description_threshold: int,
    source_names: tuple[str, ...] | None = None,
    limit: int | None = None,
) -> list[EnrichmentInput]:
    source_filter = ""
    source_priority_order = "s.name,"
    base_params: dict[str, Any] = {"threshold": short_description_threshold}
    if source_names:
        source_filter = "and s.name = any(:source_names)"
        source_priority_order = "array_position(:source_names, s.name),"
        base_params["source_names"] = list(source_names)

    # Keyset-page the scan by job id and filter handled identities in Python
    # before the caller's limit is applied, so already-enriched short
    # descriptions can never permanently conceal later eligible work.
    scan_batch = max((limit or 200) * 5, 200)
    candidates: list[EnrichmentInput] = []
    last_job_id = 0
    while True:
        rows = list(
            connection.execute(
                sa.text(
                    f"""
                    select distinct on (j.id)
                        j.id as job_id,
                        js.id as job_source_id,
                        rl.id as raw_listing_id,
                        s.id as source_id,
                        s.name as source_name,
                        js.last_content_hash,
                        js.application_url,
                        rl.canonical_source_url,
                        j.title,
                        j.employer,
                        j.published_at,
                        j.description as current_description
                    from jobs j
                    join job_sources js on js.job_id = j.id
                    join sources s on s.id = js.source_id
                    join raw_listings rl on rl.id = js.raw_listing_id
                    where j.status = 'active'
                      and s.enabled = true
                      and j.id > :last_job_id
                      {source_filter}
                      and (
                        j.description is null
                        or length(trim(j.description)) < :threshold
                      )
                    order by j.id, {source_priority_order} js.last_seen_at desc nulls last, js.id desc
                    limit :scan_batch
                    """
                ),
                {**base_params, "last_job_id": last_job_id, "scan_batch": scan_batch},
            ).mappings()
        )
        if not rows:
            break
        for row in rows:
            last_job_id = int(row["job_id"])
            input_hash = compute_enrichment_input_hash(
                last_content_hash=str(row["last_content_hash"]),
                application_url=row["application_url"],
                canonical_source_url=row["canonical_source_url"],
                title=str(row["title"]),
                employer=row["employer"],
                published_at=row["published_at"],
                enricher=enricher,
            )
            if connection.execute(
                sa.text(
                    """
                    select 1
                    from job_enrichments
                    where job_source_id = :job_source_id
                      and enricher = :enricher
                      and input_hash = :input_hash
                    """
                ),
                {
                    "job_source_id": row["job_source_id"],
                    "enricher": enricher,
                    "input_hash": input_hash,
                },
            ).scalar_one_or_none():
                continue
            if connection.execute(
                sa.text(
                    """
                    select 1
                    from enrichment_queue
                    where job_source_id = :job_source_id
                      and enricher = :enricher
                      and input_hash = :input_hash
                      and status in ('queued', 'running', 'retry')
                    """
                ),
                {
                    "job_source_id": row["job_source_id"],
                    "enricher": enricher,
                    "input_hash": input_hash,
                },
            ).scalar_one_or_none():
                continue
            candidates.append(
                EnrichmentInput(
                    job_id=int(row["job_id"]),
                    job_source_id=int(row["job_source_id"]),
                    raw_listing_id=int(row["raw_listing_id"]),
                    source_id=int(row["source_id"]),
                    source_name=str(row["source_name"]),
                    last_content_hash=str(row["last_content_hash"]),
                    application_url=row["application_url"],
                    canonical_source_url=row["canonical_source_url"],
                    title=str(row["title"]),
                    employer=row["employer"],
                    published_at=row["published_at"],
                    current_description=row["current_description"],
                    enricher=enricher,
                    enricher_version=ENRICHMENT_VERSION,
                )
            )
            if limit is not None and len(candidates) >= limit:
                return candidates
        if len(rows) < scan_batch:
            break
    return candidates


def enqueue_enrichment_candidates(
    connection: Connection,
    candidates: list[EnrichmentInput],
) -> int:
    queued = 0
    for candidate in candidates:
        input_hash = compute_enrichment_input_hash(
            last_content_hash=candidate.last_content_hash,
            application_url=candidate.application_url,
            canonical_source_url=candidate.canonical_source_url,
            title=candidate.title,
            employer=candidate.employer,
            published_at=candidate.published_at,
            enricher=candidate.enricher,
            enricher_version=candidate.enricher_version,
        )
        result = connection.execute(
            sa.text(
                """
                insert into enrichment_queue (
                    job_id,
                    job_source_id,
                    raw_listing_id,
                    enricher,
                    input_hash,
                    status,
                    priority,
                    next_attempt_at
                )
                values (
                    :job_id,
                    :job_source_id,
                    :raw_listing_id,
                    :enricher,
                    :input_hash,
                    'queued',
                    0,
                    now()
                )
                on conflict (job_source_id, enricher, input_hash)
                where status in ('queued', 'running', 'retry')
                do nothing
                """
            ),
            {
                "job_id": candidate.job_id,
                "job_source_id": candidate.job_source_id,
                "raw_listing_id": candidate.raw_listing_id,
                "enricher": candidate.enricher,
                "input_hash": input_hash,
            },
        )
        queued += int(result.rowcount or 0)
    return queued


def cancel_inactive_queue_items(connection: Connection) -> int:
    result = connection.execute(
        sa.text(
            """
            update enrichment_queue eq
            set status = 'cancelled',
                last_error = 'source occurrence no longer resolves to an active job',
                updated_at = now()
            from job_sources js
            join jobs j on j.id = js.job_id
            where eq.job_source_id = js.id
              and eq.status in ('queued', 'running', 'retry')
              and j.status <> 'active'
            """
        )
    )
    return int(result.rowcount or 0)


def requeue_stale_running_items(connection: Connection, *, stale_minutes: int = 60) -> int:
    result = connection.execute(
        sa.text(
            """
            update enrichment_queue
            set status = 'retry',
                next_attempt_at = now(),
                last_error = coalesce(last_error, 'requeued stale running enrichment item'),
                updated_at = now()
            where status = 'running'
              and updated_at < now() - (:stale_minutes * interval '1 minute')
            """
        ),
        {"stale_minutes": stale_minutes},
    )
    return int(result.rowcount or 0)


def create_enrichment_run(connection: Connection, *, metadata: dict[str, Any]) -> int:
    return int(
        connection.execute(
            sa.text(
                """
                insert into enrichment_runs (status, metadata)
                values ('running', CAST(:metadata AS jsonb))
                returning id
                """
            ),
            {"metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True)},
        ).scalar_one()
    )


def finish_enrichment_run(
    connection: Connection,
    *,
    run_id: int,
    status: str,
    queued_count: int,
    processed_count: int,
    applied_count: int,
    failed_count: int,
    error_summary: str | None = None,
) -> None:
    connection.execute(
        sa.text(
            """
            update enrichment_runs
            set status = :status,
                finished_at = now(),
                queued_count = :queued_count,
                processed_count = :processed_count,
                applied_count = :applied_count,
                failed_count = :failed_count,
                error_summary = :error_summary
            where id = :run_id
            """
        ),
        {
            "run_id": run_id,
            "status": status,
            "queued_count": queued_count,
            "processed_count": processed_count,
            "applied_count": applied_count,
            "failed_count": failed_count,
            "error_summary": error_summary,
        },
    )


def claim_due_queue_items(
    connection: Connection,
    *,
    enricher: str,
    source_names: tuple[str, ...] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    source_filter = ""
    params: dict[str, Any] = {"enricher": enricher}
    if source_names:
        source_filter = "and s.name = any(:source_names)"
        params["source_names"] = list(source_names)
    limit_sql = ""
    if limit is not None:
        limit_sql = "limit :limit"
        params["limit"] = limit
    rows = connection.execute(
        sa.text(
            f"""
            with due as (
                select eq.id
                from enrichment_queue eq
                join job_sources js on js.id = eq.job_source_id
                join sources s on s.id = js.source_id
                join jobs j on j.id = js.job_id
                where eq.enricher = :enricher
                  and eq.status in ('queued', 'retry')
                  and eq.next_attempt_at <= now()
                  and j.status = 'active'
                  {source_filter}
                order by eq.priority desc, eq.next_attempt_at, eq.id
                {limit_sql}
                for update skip locked
            )
            update enrichment_queue eq
            set status = 'running',
                attempts = eq.attempts + 1,
                updated_at = now()
            from due
            where eq.id = due.id
            returning
                eq.id as queue_id,
                eq.job_id,
                eq.job_source_id,
                eq.raw_listing_id,
                eq.enricher,
                eq.input_hash,
                eq.attempts
            """
        ),
        params,
    ).mappings()
    return [dict(row) for row in rows]


def load_queue_input(connection: Connection, *, queue_id: int) -> EnrichmentInput | None:
    row = connection.execute(
        sa.text(
            """
            select
                eq.job_id,
                eq.job_source_id,
                eq.raw_listing_id,
                s.id as source_id,
                s.name as source_name,
                js.last_content_hash,
                js.application_url,
                rl.canonical_source_url,
                j.title,
                j.employer,
                j.published_at,
                j.description as current_description,
                eq.enricher,
                :enricher_version as enricher_version
            from enrichment_queue eq
            join job_sources js on js.id = eq.job_source_id
            join sources s on s.id = js.source_id
            join raw_listings rl on rl.id = eq.raw_listing_id
            join jobs j on j.id = js.job_id
            where eq.id = :queue_id
              and j.status = 'active'
            """
        ),
        {"queue_id": queue_id, "enricher_version": ENRICHMENT_VERSION},
    ).mappings().one_or_none()
    if row is None:
        return None
    return EnrichmentInput(
        job_id=int(row["job_id"]),
        job_source_id=int(row["job_source_id"]),
        raw_listing_id=int(row["raw_listing_id"]),
        source_id=int(row["source_id"]),
        source_name=str(row["source_name"]),
        last_content_hash=str(row["last_content_hash"]),
        application_url=row["application_url"],
        canonical_source_url=row["canonical_source_url"],
        title=str(row["title"]),
        employer=row["employer"],
        published_at=row["published_at"],
        current_description=row["current_description"],
        enricher=str(row["enricher"]),
        enricher_version=str(row["enricher_version"]),
    )


def load_raw_listing_payload(connection: Connection, *, raw_listing_id: int) -> dict[str, Any]:
    payload = connection.execute(
        sa.text("select payload from raw_listings where id = :raw_listing_id"),
        {"raw_listing_id": raw_listing_id},
    ).scalar_one()
    return dict(payload)


def create_enrichment_attempt(
    connection: Connection,
    *,
    queue_id: int,
    run_id: int,
    provider: str | None,
) -> int:
    return int(
        connection.execute(
            sa.text(
                """
                insert into enrichment_attempts (queue_id, run_id, status, provider)
                values (:queue_id, :run_id, 'running', :provider)
                returning id
                """
            ),
            {"queue_id": queue_id, "run_id": run_id, "provider": provider},
        ).scalar_one()
    )


def finish_enrichment_attempt(
    connection: Connection,
    *,
    attempt_id: int,
    status: str,
    error: str | None = None,
    browser_session_id: str | None = None,
    browser_dashboard_url: str | None = None,
    artifact_paths: list[str] | None = None,
) -> None:
    connection.execute(
        sa.text(
            """
            update enrichment_attempts
            set status = :status,
                finished_at = now(),
                error = :error,
                browser_session_id = :browser_session_id,
                browser_dashboard_url = :browser_dashboard_url,
                artifact_paths = CAST(:artifact_paths AS jsonb)
            where id = :attempt_id
            """
        ),
        {
            "attempt_id": attempt_id,
            "status": status,
            "error": error,
            "browser_session_id": browser_session_id,
            "browser_dashboard_url": browser_dashboard_url,
            "artifact_paths": json.dumps(artifact_paths or [], ensure_ascii=False),
        },
    )


def complete_queue_item(connection: Connection, *, queue_id: int) -> None:
    connection.execute(
        sa.text(
            """
            update enrichment_queue
            set status = 'completed',
                updated_at = now(),
                last_error = null
            where id = :queue_id
            """
        ),
        {"queue_id": queue_id},
    )


def retry_or_fail_queue_item(
    connection: Connection,
    *,
    queue_id: int,
    error: str,
    max_attempts: int = 3,
) -> str:
    row = connection.execute(
        sa.text("select attempts from enrichment_queue where id = :queue_id"),
        {"queue_id": queue_id},
    ).mappings().one()
    next_status = "failed" if int(row["attempts"]) >= max_attempts else "retry"
    connection.execute(
        sa.text(
            """
            update enrichment_queue
            set status = :status,
                next_attempt_at = case
                    when :status = 'retry' then now() + interval '30 minutes'
                    else next_attempt_at
                end,
                last_error = :error,
                updated_at = now()
            where id = :queue_id
            """
        ),
        {"queue_id": queue_id, "status": next_status, "error": error[:2000]},
    )
    return next_status


def latest_enrichment_run_summary(connection: Connection) -> dict[str, Any] | None:
    row = connection.execute(
        sa.text(
            """
            select
                id,
                status,
                started_at,
                finished_at,
                queued_count,
                processed_count,
                applied_count,
                failed_count,
                error_summary
            from enrichment_runs
            order by started_at desc, id desc
            limit 1
            """
        )
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def enrichment_queue_health(connection: Connection) -> dict[str, Any]:
    row = connection.execute(
        sa.text(
            """
            select
                count(*) filter (where status = 'queued') as queued_count,
                count(*) filter (where status = 'running') as running_count,
                count(*) filter (where status = 'retry') as retry_count,
                count(*) filter (where status = 'failed') as failed_count,
                count(*) filter (
                    where status = 'running'
                      and updated_at < now() - interval '30 minutes'
                ) as stale_running_count,
                min(next_attempt_at) filter (where status in ('queued', 'retry')) as oldest_due_at
            from enrichment_queue
            """
        )
    ).mappings().one()
    return dict(row)
