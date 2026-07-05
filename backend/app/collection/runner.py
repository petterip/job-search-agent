import json
import logging
from datetime import datetime
import re
import time
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.adapters.base import NormalizedListing, SourceAdapter, ensure_aware_utc
from app.collection.events import (
    current_source_id,
    current_source_name,
    current_source_run_id,
    insert_source_run_event,
)
from app.config import get_settings
from app.db import get_engine
from app.enrichers.repository import preserve_enriched_description_on_upsert, record_source_provenance

logger = logging.getLogger("collector")

MATCH_TEXT_PATTERN = re.compile(r"[^0-9a-zåäö]+")
GENERIC_LOCATIONS = {"", "suomi", "finland", "sijainti ei tiedossa"}


def ensure_source(connection: Connection, adapter: SourceAdapter) -> tuple[int, datetime | None]:
    row = connection.execute(
        sa.text(
            """
            select id, incremental_watermark
            from sources
            where name = :name
            """
        ),
        {"name": adapter.source_name},
    ).one_or_none()
    if row is not None:
        return int(row.id), row.incremental_watermark

    source_id = int(
        connection.execute(
            sa.text(
                """
                insert into sources (name, method, url, poll_interval_min)
                values (:name, :method, :url, :poll_interval_min)
                returning id
                """
            ),
            {
                "name": adapter.source_name,
                "method": adapter.source_method,
                "url": adapter.url,
                "poll_interval_min": adapter.poll_interval_min,
            },
        ).scalar_one()
    )
    return source_id, None


def reconcile_stale_runs(connection: Connection, source_id: int) -> None:
    settings = get_settings()
    connection.execute(
        sa.text(
            """
            update source_runs
            set status = 'failed',
                finished_at = now(),
                error_summary = 'abandoned: stale running run'
            where source_id = :source_id
              and status = 'running'
              and started_at < now() - make_interval(mins => :stale_minutes)
            """
        ),
        {"source_id": source_id, "stale_minutes": settings.collector_stale_run_minutes},
    )


def has_active_run(connection: Connection, source_id: int) -> bool:
    return (
        connection.execute(
            sa.text(
                """
                select exists (
                    select 1
                    from source_runs
                    where source_id = :source_id and status = 'running'
                )
                """
            ),
            {"source_id": source_id},
        ).scalar_one()
        is True
    )


def normalize_match_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = MATCH_TEXT_PATTERN.sub(" ", value.casefold())
    return " ".join(normalized.split())


def is_generic_location(value: str | None) -> bool:
    return normalize_match_text(value) in GENERIC_LOCATIONS


def dedupe_key(listing: NormalizedListing) -> str | None:
    if not listing.employer or not listing.published_at:
        return None
    published_at = ensure_aware_utc(listing.published_at)
    if published_at is None:
        return None
    parts = [
        normalize_match_text(listing.title),
        normalize_match_text(listing.employer),
        normalize_match_text(listing.location),
        published_at.date().isoformat(),
    ]
    if not parts[0] or not parts[1]:
        return None
    return "|".join(parts)


def should_mark_missing_source_listings_removed(
    *,
    watermark: datetime | None,
    fetched_count: int,
    stopped_at_watermark: bool,
    max_urls: int | None,
    max_pages: int | None,
) -> bool:
    if watermark is not None:
        return False
    if stopped_at_watermark:
        return False
    if fetched_count == 0:
        return False
    if max_urls is not None and fetched_count >= max_urls:
        return False
    if max_pages is not None:
        return False
    return True


def find_cross_source_job(connection: Connection, source_id: int, listing: NormalizedListing) -> int | None:
    key = dedupe_key(listing)
    if key is None:
        return None
    row = connection.execute(
        sa.text(
            """
            select j.id
            from jobs j
            join job_sources js on js.job_id = j.id
            where js.source_id <> :source_id
              and j.status = 'active'
              and trim(regexp_replace(lower(coalesce(j.title, '')), '[^0-9a-zåäö]+', ' ', 'g')) = :title
              and trim(regexp_replace(lower(coalesce(j.employer, '')), '[^0-9a-zåäö]+', ' ', 'g')) = :employer
              and trim(regexp_replace(lower(coalesce(j.location, '')), '[^0-9a-zåäö]+', ' ', 'g')) = :location
              and j.published_at::date = :published_date
            order by j.id
            limit 1
            """
        ),
        {
            "source_id": source_id,
            "title": normalize_match_text(listing.title),
            "employer": normalize_match_text(listing.employer),
            "location": normalize_match_text(listing.location),
            "published_date": ensure_aware_utc(listing.published_at).date(),  # type: ignore[union-attr]
        },
    ).scalar_one_or_none()
    return int(row) if row is not None else None


def upsert_listing(
    connection: Connection,
    source_id: int,
    listing: NormalizedListing,
) -> str:
    existing_hash = connection.execute(
        sa.text(
            """
            select rl.content_hash
            from raw_listings rl
            where rl.source_id = :source_id and rl.external_id = :external_id
            """
        ),
        {"source_id": source_id, "external_id": listing.external_id},
    ).scalar_one_or_none()

    raw_id = connection.execute(
        sa.text(
            """
            insert into raw_listings (
                source_id,
                external_id,
                canonical_source_url,
                content_hash,
                payload,
                last_seen_at
            )
            values (
                :source_id,
                :external_id,
                :canonical_source_url,
                :content_hash,
                CAST(:payload AS jsonb),
                now()
            )
            on conflict (source_id, external_id) where external_id is not null
            do update set
                content_hash = excluded.content_hash,
                canonical_source_url = excluded.canonical_source_url,
                payload = excluded.payload,
                last_seen_at = now()
            returning id
            """
        ),
        {
            "source_id": source_id,
            "external_id": listing.external_id,
            "canonical_source_url": listing.canonical_source_url,
            "content_hash": listing.content_hash,
            "payload": json.dumps(listing.payload, ensure_ascii=False),
        },
    ).scalar_one()

    application_url = listing.application_url or listing.canonical_source_url

    existing_source_row = connection.execute(
        sa.text(
            """
            select js.id as job_source_id, js.job_id
            from job_sources js
            where js.source_id = :source_id and js.external_id = :external_id
            """
        ),
        {"source_id": source_id, "external_id": listing.external_id},
    ).mappings().one_or_none()
    existing_job = existing_source_row["job_id"] if existing_source_row is not None else None
    existing_job_source_id = (
        int(existing_source_row["job_source_id"]) if existing_source_row is not None else None
    )

    relinked_to_cross_source = False
    if existing_job is not None:
        cross_source_job = find_cross_source_job(connection, source_id, listing)
        if cross_source_job is not None and int(cross_source_job) != int(existing_job):
            previous_job = int(existing_job)
            connection.execute(
                sa.text(
                    """
                    update job_sources
                    set job_id = :job_id
                    where source_id = :source_id and external_id = :external_id
                    """
                ),
                {
                    "job_id": cross_source_job,
                    "source_id": source_id,
                    "external_id": listing.external_id,
                },
            )
            connection.execute(
                sa.text(
                    """
                    update jobs j
                    set status = 'superseded',
                        updated_at = now()
                    where j.id = :job_id
                      and not exists (
                          select 1
                          from job_sources js
                          where js.job_id = j.id
                      )
                    """
                ),
                {"job_id": previous_job},
            )
            existing_job = cross_source_job
            relinked_to_cross_source = True

    if existing_job is None:
        job_id = find_cross_source_job(connection, source_id, listing)
        result = "deduplicated" if job_id is not None else "inserted"
        if job_id is None:
            job_id = connection.execute(
                sa.text(
                    """
                    insert into jobs (title, employer, description, location, published_at, status)
                    values (:title, :employer, :description, :location, :published_at, 'active')
                    returning id
                    """
                ),
                {
                    "title": listing.title,
                    "employer": listing.employer,
                    "description": listing.description,
                    "location": listing.location,
                    "published_at": listing.published_at,
                },
            ).scalar_one()
        inserted_job_source_id = connection.execute(
            sa.text(
                """
                insert into job_sources (
                    job_id,
                    source_id,
                    raw_listing_id,
                    external_id,
                    application_url,
                    attribution,
                    last_content_hash,
                    last_seen_at
                )
                values (
                    :job_id,
                    :source_id,
                    :raw_listing_id,
                    :external_id,
                    :application_url,
                    :attribution,
                    :last_content_hash,
                    now()
                )
                returning id
                """
            ),
            {
                "job_id": job_id,
                "source_id": source_id,
                "raw_listing_id": raw_id,
                "external_id": listing.external_id,
                "application_url": application_url,
                "attribution": listing.attribution,
                "last_content_hash": listing.content_hash,
            },
        ).scalar_one()
        if listing.description and listing.description.strip():
            if result == "inserted":
                record_source_provenance(
                    connection,
                    job_id=int(job_id),
                    job_source_id=int(inserted_job_source_id),
                )
            elif job_id is not None:
                effective_description = preserve_enriched_description_on_upsert(
                    connection,
                    job_id=int(job_id),
                    source_description=listing.description,
                    job_source_id=int(inserted_job_source_id),
                )
                connection.execute(
                    sa.text(
                        """
                        update jobs
                        set description = coalesce(:effective_description, jobs.description),
                            employer = coalesce(jobs.employer, :employer),
                            updated_at = case
                                when jobs.description is null and :effective_description is not null then now()
                                when jobs.employer is null and :employer is not null then now()
                                else jobs.updated_at
                            end
                        where id = :job_id
                        """
                    ),
                    {
                        "job_id": job_id,
                        "effective_description": effective_description,
                        "employer": listing.employer,
                    },
                )
        return result

    if existing_hash == listing.content_hash:
        if existing_job_source_id is None:
            raise RuntimeError("existing job_sources row is missing id")
        effective_description = preserve_enriched_description_on_upsert(
            connection,
            job_id=int(existing_job),
            source_description=listing.description,
            job_source_id=existing_job_source_id,
        )
        connection.execute(
            sa.text(
                """
                update job_sources
                set last_seen_at = now(),
                    application_url = :application_url
                where source_id = :source_id and external_id = :external_id
                """
            ),
            {
                "source_id": source_id,
                "external_id": listing.external_id,
                "application_url": application_url,
            },
        )
        connection.execute(
            sa.text(
                """
                update jobs
                set employer = coalesce(jobs.employer, :employer),
                    description = coalesce(:effective_description, jobs.description),
                    location = case
                        when jobs.location is null then :location
                        when trim(regexp_replace(lower(coalesce(jobs.location, '')), '[^0-9a-zåäö]+', ' ', 'g')) in ('', 'suomi', 'finland', 'sijainti ei tiedossa')
                             and cast(:location as text) is not null
                             and trim(regexp_replace(lower(cast(:location as text)), '[^0-9a-zåäö]+', ' ', 'g')) not in ('', 'suomi', 'finland', 'sijainti ei tiedossa')
                        then :location
                        else jobs.location
                    end,
                    status = 'active',
                    updated_at = case
                        when jobs.employer is null and :employer is not null then now()
                        when jobs.description is null and :effective_description is not null then now()
                        when jobs.location is null and cast(:location as text) is not null then now()
                        when trim(regexp_replace(lower(coalesce(jobs.location, '')), '[^0-9a-zåäö]+', ' ', 'g')) in ('', 'suomi', 'finland', 'sijainti ei tiedossa')
                             and cast(:location as text) is not null
                             and trim(regexp_replace(lower(cast(:location as text)), '[^0-9a-zåäö]+', ' ', 'g')) not in ('', 'suomi', 'finland', 'sijainti ei tiedossa')
                        then now()
                        when jobs.status <> 'active' then now()
                        else jobs.updated_at
                    end
                where id = :job_id
                """
            ),
            {
                "job_id": existing_job,
                "employer": listing.employer,
                "effective_description": effective_description,
                "location": listing.location,
            },
        )
        return "deduplicated" if relinked_to_cross_source else "unchanged"

    if existing_job_source_id is None:
        raise RuntimeError("existing job_sources row is missing id")
    effective_description = preserve_enriched_description_on_upsert(
        connection,
        job_id=int(existing_job),
        source_description=listing.description,
        job_source_id=existing_job_source_id,
    )
    connection.execute(
        sa.text(
            """
            update jobs
            set title = :title,
                employer = :employer,
                description = coalesce(:effective_description, jobs.description),
                location = :location,
                published_at = :published_at,
                status = 'active',
                updated_at = now()
            where id = :job_id
            """
        ),
        {
            "job_id": existing_job,
            "title": listing.title,
            "employer": listing.employer,
            "effective_description": effective_description,
            "location": listing.location,
            "published_at": listing.published_at,
        },
    )
    connection.execute(
        sa.text(
            """
            update job_sources
            set raw_listing_id = :raw_listing_id,
                application_url = :application_url,
                attribution = coalesce(:attribution, job_sources.attribution),
                last_content_hash = :last_content_hash,
                last_seen_at = now()
            where source_id = :source_id and external_id = :external_id
            """
        ),
        {
            "raw_listing_id": raw_id,
            "application_url": application_url,
            "attribution": listing.attribution,
            "last_content_hash": listing.content_hash,
            "source_id": source_id,
            "external_id": listing.external_id,
        },
    )
    return "updated"


def advance_watermark(
    connection: Connection,
    source_id: int,
    current_watermark: datetime | None,
    newest_published_at: datetime | None,
) -> datetime | None:
    current_watermark = ensure_aware_utc(current_watermark)
    newest_published_at = ensure_aware_utc(newest_published_at)
    if newest_published_at is None:
        return current_watermark
    next_watermark = (
        newest_published_at
        if current_watermark is None or newest_published_at > current_watermark
        else current_watermark
    )
    connection.execute(
        sa.text(
            """
            update sources
            set incremental_watermark = :watermark
            where id = :source_id
            """
        ),
        {"source_id": source_id, "watermark": next_watermark},
    )
    return next_watermark


def mark_missing_source_listings_removed(
    connection: Connection,
    *,
    source_id: int,
    seen_external_ids: set[str],
) -> int:
    if not seen_external_ids:
        result = connection.execute(
            sa.text(
                """
                update jobs j
                set status = 'removed',
                    updated_at = now()
                from job_sources js
                where js.job_id = j.id
                  and js.source_id = :source_id
                  and j.status = 'active'
                  and not exists (
                      select 1
                      from job_sources other_js
                      join sources other_s on other_s.id = other_js.source_id
                      where other_js.job_id = j.id
                        and other_js.source_id <> :source_id
                        and other_s.enabled = true
                  )
                """
            ),
            {"source_id": source_id},
        )
        return int(result.rowcount or 0)

    result = connection.execute(
        sa.text(
            """
            update jobs j
            set status = 'removed',
                updated_at = now()
            from job_sources js
            where js.job_id = j.id
              and js.source_id = :source_id
              and js.external_id <> all(:seen_external_ids)
              and j.status = 'active'
              and not exists (
                  select 1
                  from job_sources other_js
                  join sources other_s on other_s.id = other_js.source_id
                  where other_js.job_id = j.id
                    and other_js.source_id <> :source_id
                    and other_s.enabled = true
              )
            """
        ),
        {"source_id": source_id, "seen_external_ids": list(seen_external_ids)},
    )
    return int(result.rowcount or 0)


async def run_source_collection(
    adapter: SourceAdapter,
    *,
    page_size: int | None = None,
    max_pages: int | None = None,
    max_urls: int | None = None,
    skip_if_running: bool = True,
) -> dict[str, Any]:
    engine = get_engine()

    with engine.begin() as connection:
        source_id, watermark = ensure_source(connection, adapter)
        reconcile_stale_runs(connection, source_id)
        if skip_if_running and has_active_run(connection, source_id):
            logger.warning(
                "event=source_collection_skipped source=%s reason=already_running source_id=%s",
                adapter.source_name,
                source_id,
            )
            insert_source_run_event(
                connection,
                source_run_id=None,
                source_id=source_id,
                source_name=adapter.source_name,
                level="warning",
                event_type="source_collection_skipped",
                message="source collection skipped: already running",
                details={"reason": "already_running"},
            )
            return {
                "source": adapter.source_name,
                "skipped": True,
                "fetched": 0,
                "inserted": 0,
                "updated": 0,
                "unchanged": 0,
            }

        run_id = connection.execute(
            sa.text(
                """
                insert into source_runs (source_id, status)
                values (:source_id, 'running')
                returning id
                """
            ),
            {"source_id": source_id},
        ).scalar_one()
        insert_source_run_event(
            connection,
            source_run_id=run_id,
            source_id=source_id,
            source_name=adapter.source_name,
            level="info",
            event_type="source_collection_started",
            message="source collection started",
            details={
                "watermark": watermark.isoformat() if watermark else None,
                "page_size": page_size,
                "max_pages": max_pages,
                "max_urls": max_urls,
            },
        )

    started_monotonic = time.monotonic()
    run_token = current_source_run_id.set(int(run_id))
    source_id_token = current_source_id.set(int(source_id))
    source_name_token = current_source_name.set(adapter.source_name)
    try:
        fetch_result = await adapter.collect(
            watermark=watermark,
            page_size=page_size,
            max_pages=max_pages,
            max_urls=max_urls,
        )
        counts: dict[str, Any] = {
            "source": adapter.source_name,
            "skipped": False,
            "fetched": len(fetch_result.listings),
            "inserted": 0,
            "deduplicated": 0,
            "updated": 0,
            "unchanged": 0,
            "removed": 0,
            "pages_fetched": fetch_result.pages_fetched,
            "stopped_at_watermark": fetch_result.stopped_at_watermark,
        }

        with engine.begin() as connection:
            seen_external_ids: set[str] = set()
            for listing in fetch_result.listings:
                seen_external_ids.add(listing.external_id)
                result = upsert_listing(connection, source_id, listing)
                counts[result] = int(counts[result]) + 1
            if should_mark_missing_source_listings_removed(
                watermark=watermark,
                fetched_count=len(fetch_result.listings),
                stopped_at_watermark=fetch_result.stopped_at_watermark,
                max_urls=max_urls,
                max_pages=max_pages,
            ):
                counts["removed"] = mark_missing_source_listings_removed(
                    connection,
                    source_id=source_id,
                    seen_external_ids=seen_external_ids,
                )

            next_watermark = advance_watermark(
                connection,
                source_id,
                watermark,
                fetch_result.newest_watermark,
            )

            connection.execute(
                sa.text(
                    """
                    update source_runs
                    set status = 'success',
                        finished_at = now(),
                        fetched_count = :fetched_count,
                        inserted_count = :inserted_count,
                        updated_count = :updated_count,
                        unchanged_count = :unchanged_count
                    where id = :run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "fetched_count": counts["fetched"],
                    "inserted_count": counts["inserted"],
                    "updated_count": counts["updated"],
                    "unchanged_count": counts["unchanged"],
                },
            )
            insert_source_run_event(
                connection,
                source_run_id=run_id,
                source_id=source_id,
                source_name=adapter.source_name,
                level="info",
                event_type="source_collection_succeeded",
                message="source collection succeeded",
                details={
                    **counts,
                    "watermark": watermark.isoformat() if watermark else None,
                    "next_watermark": next_watermark.isoformat() if next_watermark else None,
                    "duration_seconds": round(time.monotonic() - started_monotonic, 3),
                },
            )

        logger.info(
            "event=source_collected source=%s watermark=%s next_watermark=%s counts=%s",
            adapter.source_name,
            watermark,
            next_watermark,
            counts,
        )
        return counts
    except Exception as exc:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    update source_runs
                    set status = 'failed',
                        finished_at = now(),
                        error_summary = :error_summary
                    where id = :run_id
                    """
                ),
                {"run_id": run_id, "error_summary": str(exc)[:1000]},
            )
            insert_source_run_event(
                connection,
                source_run_id=run_id,
                source_id=source_id,
                source_name=adapter.source_name,
                level="error",
                event_type="source_collection_failed",
                message=str(exc),
                details={
                    "exception_type": type(exc).__name__,
                    "duration_seconds": round(time.monotonic() - started_monotonic, 3),
                },
            )
        raise
    finally:
        current_source_name.reset(source_name_token)
        current_source_id.reset(source_id_token)
        current_source_run_id.reset(run_token)
