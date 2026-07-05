from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.enrichers.base import EnrichmentInput, EnrichmentResult

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
            select job_id, provenance, source_id, enricher, input_hash, confidence
            from job_description_state
            where job_id = :job_id
            """
        ),
        {"job_id": job_id},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def record_source_provenance(
    connection: Connection,
    *,
    job_id: int,
    source_id: int,
) -> None:
    connection.execute(
        sa.text(
            """
            insert into job_description_state (
                job_id, provenance, source_id, enricher, input_hash, confidence, applied_at
            )
            values (:job_id, 'source', :source_id, null, null, null, now())
            on conflict (job_id) do update set
                provenance = 'source',
                source_id = excluded.source_id,
                enricher = null,
                input_hash = null,
                confidence = null,
                applied_at = now()
            """
        ),
        {"job_id": job_id, "source_id": source_id},
    )


def mark_source_description(
    connection: Connection,
    *,
    job_id: int,
    source_id: int,
    description: str,
) -> None:
    record_source_provenance(connection, job_id=job_id, source_id=source_id)
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


def preserve_enriched_description_on_upsert(
    connection: Connection,
    *,
    job_id: int,
    source_description: str | None,
    source_id: int,
) -> str | None:
    existing_description = _load_job_description(connection, job_id)

    if source_description and source_description.strip():
        record_source_provenance(connection, job_id=job_id, source_id=source_id)
        return source_description.strip()

    return existing_description


def apply_enrichment_result(
    connection: Connection,
    result: EnrichmentResult,
    *,
    short_description_threshold: int,
) -> bool:
    connection.execute(
        sa.text(
            """
            insert into job_enrichments (
                job_id,
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
                :enricher,
                :input_hash,
                :description,
                CAST(:structured_fields AS jsonb),
                :method,
                :confidence,
                false
            )
            on conflict (job_id, enricher, input_hash) do update set
                description = excluded.description,
                structured_fields = excluded.structured_fields,
                method = excluded.method,
                confidence = excluded.confidence
            """
        ),
        {
            "job_id": result.job_id,
            "enricher": result.enricher,
            "input_hash": result.input_hash,
            "description": result.description,
            "structured_fields": json.dumps(result.structured_fields, ensure_ascii=False),
            "method": result.method.value,
            "confidence": result.confidence,
        },
    )

    if not result.description or not result.description.strip():
        return False

    current = connection.execute(
        sa.text("select description from jobs where id = :job_id"),
        {"job_id": result.job_id},
    ).scalar_one_or_none()
    state = load_description_state(connection, result.job_id)
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
        {"job_id": result.job_id, "description": new_text},
    )
    connection.execute(
        sa.text(
            """
            insert into job_description_state (
                job_id, provenance, source_id, enricher, input_hash, confidence, applied_at
            )
            values (:job_id, 'enriched', null, :enricher, :input_hash, :confidence, now())
            on conflict (job_id) do update set
                provenance = 'enriched',
                source_id = null,
                enricher = excluded.enricher,
                input_hash = excluded.input_hash,
                confidence = excluded.confidence,
                applied_at = now()
            """
        ),
        {
            "job_id": result.job_id,
            "enricher": result.enricher,
            "input_hash": result.input_hash,
            "confidence": result.confidence,
        },
    )
    connection.execute(
        sa.text(
            """
            update job_enrichments
            set applied_to_job = true
            where job_id = :job_id
              and enricher = :enricher
              and input_hash = :input_hash
            """
        ),
        {
            "job_id": result.job_id,
            "enricher": result.enricher,
            "input_hash": result.input_hash,
        },
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
    params: dict[str, Any] = {
        "threshold": short_description_threshold,
    }
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
            select distinct on (j.id)
                j.id as job_id,
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
              {source_filter}
              and (
                j.description is null
                or length(trim(j.description)) < :threshold
              )
            order by j.id, js.last_seen_at desc nulls last, js.id desc
            {limit_sql}
            """
        ),
        params,
    ).mappings()

    candidates: list[EnrichmentInput] = []
    for row in rows:
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
                where job_id = :job_id
                  and enricher = :enricher
                  and input_hash = :input_hash
                """
            ),
            {"job_id": row["job_id"], "enricher": enricher, "input_hash": input_hash},
        ).scalar_one_or_none():
            continue
        if connection.execute(
            sa.text(
                """
                select 1
                from enrichment_queue
                where job_id = :job_id
                  and enricher = :enricher
                  and input_hash = :input_hash
                  and status in ('queued', 'running', 'retry')
                """
            ),
            {"job_id": row["job_id"], "enricher": enricher, "input_hash": input_hash},
        ).scalar_one_or_none():
            continue
        candidates.append(
            EnrichmentInput(
                job_id=int(row["job_id"]),
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
    return candidates


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
