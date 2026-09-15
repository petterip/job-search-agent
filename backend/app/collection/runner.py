import json
import logging
from datetime import datetime
import re
import time
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.adapters.base import NormalizedListing, SourceAdapter, ensure_aware_utc
from app.collection.scan_state import (
    OUTCOME_INVALID,
    SitemapEntry,
    claim_scan_batch,
    close_absent_members,
    frontier_drained,
    mark_scan_outcome,
    scan_backlog_report,
    sync_sitemap_entries,
)
from app.collection.events import (
    current_source_id,
    current_source_name,
    current_source_run_id,
    insert_source_run_event,
)
from app.db import get_engine
from app.enrichers.repository import (
    canonical_field,
    preserve_enriched_description_on_upsert,
    record_source_provenance,
)

logger = logging.getLogger("collector")

MATCH_TEXT_PATTERN = re.compile(r"[^0-9a-zåäö]+")
GENERIC_LOCATIONS = {"", "suomi", "finland", "sijainti ei tiedossa"}

# PostgreSQL advisory-lock namespaces. The two-integer lock form keeps the
# families apart, so a source id can never collide with a pipeline or profile
# lock object. Every run owner holds its lock on a dedicated connection for the
# whole run. Matching, feedback analysis/learning and the daily pipeline share
# the active profile's publication lock so same-profile publication cannot
# interleave; profile import is required by this contract to acquire the same
# lock (``app/profile.py`` is outside this change).
SOURCE_RUN_LOCK_CLASS = 1001
PIPELINE_RUN_LOCK_CLASS = 1002
PROFILE_PUBLICATION_LOCK_CLASS = 1003


class RunOwnership:
    """A dedicated advisory-lock connection held for the whole run.

    Session advisory locks live in the database session, so the owning
    connection must stay checked out for the entire run, including while the
    run awaits remote HTTP responses. Write transactions use other short-lived
    connections; this session stays idle and never holds a long write
    transaction. Closing it early would either release mutual exclusion or park
    the lock on a session returned to the pool, so ``release`` is the only way
    to give it up.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        connection: Connection,
        lock_class: int,
        lock_object: int,
        lock_name: str,
        dispose_engine: bool,
        shared: bool = False,
    ) -> None:
        self.engine = engine
        self.connection = connection
        self.lock_class = lock_class
        self.lock_object = lock_object
        self.lock_name = lock_name
        self.dispose_engine = dispose_engine
        self.shared = shared
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            unlock = (
                "select pg_advisory_unlock_shared(:lock_class, :lock_object)"
                if self.shared
                else "select pg_advisory_unlock(:lock_class, :lock_object)"
            )
            self.connection.execute(
                sa.text(unlock),
                {"lock_class": self.lock_class, "lock_object": self.lock_object},
            )
        except Exception:
            logger.warning(
                "event=run_ownership_unlock_failed lock=%s", self.lock_name, exc_info=True
            )
        finally:
            try:
                self.connection.close()
            finally:
                if self.dispose_engine:
                    self.engine.dispose()


def acquire_run_ownership(
    *,
    lock_class: int,
    lock_object: int,
    lock_name: str,
    engine: Engine | None = None,
    dispose_engine: bool | None = None,
    shared: bool = False,
) -> RunOwnership | None:
    """Try to own ``lock_name`` for the whole run.

    Returns ``None`` when another live session already owns the lock; the
    caller must then record a skip and do no work. ``shared=True`` takes a
    shared lock, so many collectors can hold it concurrently while the pipeline
    takes it exclusively.
    """
    owner_engine = engine if engine is not None else get_engine()
    if dispose_engine is None:
        dispose_engine = engine is None
    connection = owner_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        acquire_sql = (
            "select pg_try_advisory_lock_shared(:lock_class, :lock_object)"
            if shared
            else "select pg_try_advisory_lock(:lock_class, :lock_object)"
        )
        acquired = bool(
            connection.execute(
                sa.text(acquire_sql),
                {"lock_class": lock_class, "lock_object": lock_object},
            ).scalar_one()
        )
    except Exception:
        connection.close()
        if dispose_engine:
            owner_engine.dispose()
        raise
    if not acquired:
        connection.close()
        if dispose_engine:
            owner_engine.dispose()
        return None
    return RunOwnership(
        engine=owner_engine,
        connection=connection,
        lock_class=lock_class,
        lock_object=lock_object,
        lock_name=lock_name,
        dispose_engine=dispose_engine,
        shared=shared,
    )


def advisory_lock_is_held(connection: Connection, *, lock_class: int, lock_object: int) -> bool:
    """Whether any live session currently owns a two-integer advisory lock."""
    return bool(
        connection.execute(
            sa.text(
                """
                select exists (
                    select 1
                    from pg_locks
                    where locktype = 'advisory'
                      and classid = :lock_class
                      and objid = :lock_object
                      and objsubid = 2
                      and granted
                )
                """
            ),
            {"lock_class": lock_class, "lock_object": lock_object},
        ).scalar_one()
    )


def resolve_active_profile_id(connection: Connection) -> int | None:
    row = connection.execute(
        sa.text("select id from job_seeker_profiles order by id limit 1")
    ).scalar_one_or_none()
    return int(row) if row is not None else None


def acquire_publication_ownership(*, engine: Engine | None = None) -> RunOwnership | None:
    """Own the active profile's publication lock.

    Matching, feedback analysis/learning and profile import all publish into
    the same profile row; they must share this lock so same-profile publication
    cannot interleave. Manual commands use it exactly like the scheduler does.
    """
    owner_engine = engine if engine is not None else get_engine()
    try:
        with owner_engine.connect() as lookup:
            profile_id = resolve_active_profile_id(lookup)
    except Exception:
        if engine is None:
            owner_engine.dispose()
        raise
    lock_object = int(profile_id) if profile_id is not None else 0
    return acquire_run_ownership(
        engine=owner_engine,
        lock_class=PROFILE_PUBLICATION_LOCK_CLASS,
        lock_object=lock_object,
        lock_name=f"profile:{lock_object}",
        dispose_engine=engine is None,
    )


def ensure_source(connection: Connection, adapter: SourceAdapter) -> tuple[int, datetime | None]:
    """Return the source id and watermark, creating the row atomically.

    A concurrent first collection must not fail with a uniqueness error before
    the advisory lock is even acquired, so creation is a single upsert rather
    than SELECT-then-INSERT.
    """
    row = connection.execute(
        sa.text(
            """
            insert into sources (name, method, url, poll_interval_min)
            values (:name, :method, :url, :poll_interval_min)
            on conflict (name) do update set name = excluded.name
            returning id, incremental_watermark
            """
        ),
        {
            "name": adapter.source_name,
            "method": adapter.source_method,
            "url": adapter.url,
            "poll_interval_min": adapter.poll_interval_min,
        },
    ).one_or_none()
    if row is None:
        raise RuntimeError(f"source upsert returned no row for {adapter.source_name}")
    return int(row.id), row.incremental_watermark


def reconcile_abandoned_runs(connection: Connection, source_id: int) -> int:
    """Fail running rows for a source whose advisory lock we now hold.

    The caller must already own the source lock. PostgreSQL releases session
    advisory locks when a session ends, so holding the lock proves no live
    owner exists and any remaining ``running`` row is abandoned regardless of
    its age. The 30-minute staleness threshold is never used to declare a live
    run dead.
    """
    result = connection.execute(
        sa.text(
            """
            update source_runs
            set status = 'failed',
                finished_at = now(),
                error_summary = coalesce(error_summary, 'abandoned: owner lock was free')
            where source_id = :source_id
              and status = 'running'
            """
        ),
        {"source_id": source_id},
    )
    return int(result.rowcount or 0)


def finish_source_run(
    connection: Connection,
    *,
    run_id: int,
    owner_token: str,
    status: str,
    fetched_count: int | None = None,
    inserted_count: int | None = None,
    updated_count: int | None = None,
    unchanged_count: int | None = None,
    error_summary: str | None = None,
) -> bool:
    """Transition a run exactly once, only while this owner still holds it.

    Returns ``False`` when the row is gone, already terminal, or owned by
    somebody else, so a stale finisher cannot overwrite a terminal status.
    """
    result = connection.execute(
        sa.text(
            """
            update source_runs
            set status = :status,
                finished_at = now(),
                fetched_count = coalesce(cast(:fetched_count as integer), fetched_count),
                inserted_count = coalesce(cast(:inserted_count as integer), inserted_count),
                updated_count = coalesce(cast(:updated_count as integer), updated_count),
                unchanged_count = coalesce(cast(:unchanged_count as integer), unchanged_count),
                error_summary = :error_summary
            where id = :run_id
              and status = 'running'
              and owner_token = :owner_token
            """
        ),
        {
            "run_id": run_id,
            "owner_token": owner_token,
            "status": status,
            "fetched_count": fetched_count,
            "inserted_count": inserted_count,
            "updated_count": updated_count,
            "unchanged_count": unchanged_count,
            "error_summary": error_summary,
        },
    )
    return bool(result.rowcount)


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
    complete: bool = True,
) -> bool:
    if not complete:
        return False
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
              and (j.published_at AT TIME ZONE 'UTC')::date = :published_date
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
                on conflict (source_id, external_id) where external_id is not null
                do update set
                    raw_listing_id = excluded.raw_listing_id,
                    application_url = excluded.application_url,
                    attribution = coalesce(excluded.attribution, job_sources.attribution),
                    last_content_hash = excluded.last_content_hash,
                    last_seen_at = now()
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
                current = connection.execute(
                    sa.text("select title, employer, location from jobs where id = :job_id"),
                    {"job_id": job_id},
                ).mappings().one()
                effective_title = canonical_field(current["title"], listing.title)
                effective_employer = canonical_field(current["employer"], listing.employer)
                effective_location = canonical_field(current["location"], listing.location)
                if (
                    effective_description is not None
                    or effective_title != current["title"]
                    or effective_employer != current["employer"]
                    or effective_location != current["location"]
                ):
                    record_source_provenance(
                        connection,
                        job_id=int(job_id),
                        job_source_id=int(inserted_job_source_id),
                    )
                connection.execute(
                    sa.text(
                        """
                        update jobs
                        set title = coalesce(:title, jobs.title),
                            employer = coalesce(:employer, jobs.employer),
                            description = coalesce(:effective_description, jobs.description),
                            location = coalesce(:location, jobs.location),
                            updated_at = case
                                when jobs.title is distinct from coalesce(:title, jobs.title) then now()
                                when jobs.employer is distinct from coalesce(:employer, jobs.employer) then now()
                                when jobs.description is null and :effective_description is not null then now()
                                when jobs.location is distinct from coalesce(:location, jobs.location) then now()
                                else jobs.updated_at
                            end
                        where id = :job_id
                        """
                    ),
                    {
                        "job_id": job_id,
                        "title": effective_title,
                        "employer": effective_employer,
                        "effective_description": effective_description,
                        "location": effective_location,
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
            content_changed=False,
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
    """Collect one source under database-owned mutual exclusion.

    Ownership is a PostgreSQL advisory lock held on a dedicated connection for
    the whole run, including the remote fetch. ``skip_if_running=False`` (the
    manual ``--force`` flag) no longer bypasses exclusion: a second process
    must never write the same source concurrently. It is kept for callers that
    still pass it.
    """
    engine = get_engine()

    with engine.begin() as connection:
        source_id, watermark = ensure_source(connection, adapter)

    ownership = acquire_run_ownership(
        engine=engine,
        lock_class=SOURCE_RUN_LOCK_CLASS,
        lock_object=source_id,
        lock_name=f"source:{adapter.source_name}",
    )
    if ownership is None:
        logger.warning(
            "event=source_collection_skipped source=%s reason=run_ownership_not_acquired source_id=%s",
            adapter.source_name,
            source_id,
        )
        with engine.begin() as connection:
            insert_source_run_event(
                connection,
                source_run_id=None,
                source_id=source_id,
                source_name=adapter.source_name,
                level="warning",
                event_type="source_collection_skipped",
                message="source collection skipped: run ownership held elsewhere",
                details={"reason": "already_running"},
            )
        return {
            "source": adapter.source_name,
            "skipped": True,
            "reason": "already_running",
            "fetched": 0,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
        }

    # Collectors take the pipeline coordination lock in shared mode: many
    # collectors may run together, but the daily pipeline takes it exclusively
    # so it cannot start while collection is changing the catalogue.
    coordination = acquire_run_ownership(
        engine=engine,
        lock_class=PIPELINE_RUN_LOCK_CLASS,
        lock_object=0,
        lock_name="pipeline:shared",
        shared=True,
    )
    if coordination is None:
        ownership.release()
        logger.warning(
            "event=source_collection_skipped source=%s reason=pipeline_running source_id=%s",
            adapter.source_name,
            source_id,
        )
        with engine.begin() as connection:
            insert_source_run_event(
                connection,
                source_run_id=None,
                source_id=source_id,
                source_name=adapter.source_name,
                level="warning",
                event_type="source_collection_skipped",
                message="source collection skipped: pipeline owns the catalogue",
                details={"reason": "pipeline_running"},
            )
        return {
            "source": adapter.source_name,
            "skipped": True,
            "reason": "pipeline_running",
            "fetched": 0,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
        }

    started_monotonic = time.monotonic()
    owner_token = uuid.uuid4().hex
    run_token: Any = None
    source_id_token: Any = None
    source_name_token: Any = None
    try:
        # Holding the lock proves any earlier owner is gone, so abandoned rows
        # are reconciled here instead of by a wall-clock timeout.
        with engine.begin() as connection:
            reconcile_abandoned_runs(connection, source_id)
            run_id = int(
                connection.execute(
                    sa.text(
                        """
                        insert into source_runs (source_id, status, owner_token)
                        values (:source_id, 'running', :owner_token)
                        returning id
                        """
                    ),
                    {"source_id": source_id, "owner_token": owner_token},
                ).scalar_one()
            )
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

        run_token = current_source_run_id.set(run_id)
        source_id_token = current_source_id.set(int(source_id))
        source_name_token = current_source_name.set(adapter.source_name)
        try:
            resumable_scan = bool(getattr(adapter, "supports_resumable_scan", False))
            pending_entries: list[tuple[str, str, Any]] | None = None
            if resumable_scan:
                settings_for_scan = get_settings()
                with engine.begin() as connection:
                    members = claim_scan_batch(
                        connection,
                        source_id=source_id,
                        limit=max_urls or settings_for_scan.jobly_max_urls_per_run,
                    )
                pending_entries = [
                    (member.external_id, member.canonical_url, member.lastmod)
                    for member in members
                ]
                logger.info(
                    "event=source_scan_batch_claimed source=%s claimed=%s",
                    adapter.source_name,
                    len(pending_entries),
                )
            if resumable_scan:
                fetch_result = await adapter.collect(
                    watermark=watermark,
                    page_size=page_size,
                    max_pages=max_pages,
                    max_urls=max_urls,
                    pending_entries=pending_entries,
                )
            else:
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
                "outcome": fetch_result.outcome,
                "warnings": list(fetch_result.warnings),
            }

            with engine.begin() as connection:
                seen_external_ids: set[str] = set()
                for listing in fetch_result.listings:
                    seen_external_ids.add(listing.external_id)
                    result = upsert_listing(connection, source_id, listing)
                    counts[result] = int(counts[result]) + 1
                scan_drained = True
                if resumable_scan:
                    settings_for_scan = get_settings()
                    scan_entries = [
                        SitemapEntry(
                            external_id=str(external_id),
                            canonical_url=str(url),
                            lastmod=lastmod,
                        )
                        for external_id, url, lastmod in fetch_result.scan_entries
                    ]
                    counts["scan_sync"] = sync_sitemap_entries(
                        connection,
                        source_id=source_id,
                        entries=scan_entries,
                    )
                    for external_id, _url, _lastmod in pending_entries or []:
                        mark_scan_outcome(
                            connection,
                            source_id=source_id,
                            external_id=str(external_id),
                            outcome=fetch_result.scan_outcomes.get(
                                str(external_id), OUTCOME_INVALID
                            ),
                            retry_delay_minutes=settings_for_scan.jobly_scan_retry_minutes,
                            closure_attempts=settings_for_scan.jobly_scan_closure_attempts,
                        )
                    counts["scan_closed_absent"] = close_absent_members(
                        connection,
                        source_id=source_id,
                        grace_hours=settings_for_scan.jobly_scan_grace_hours,
                    )
                    scan_drained = frontier_drained(connection, source_id=source_id)
                    counts["scan_backlog"] = scan_backlog_report(
                        connection, source_id=source_id
                    )
                    counts["scan_frontier_drained"] = scan_drained

                if not resumable_scan and should_mark_missing_source_listings_removed(
                    watermark=watermark,
                    fetched_count=len(fetch_result.listings),
                    stopped_at_watermark=fetch_result.stopped_at_watermark,
                    max_urls=max_urls,
                    max_pages=max_pages,
                    complete=fetch_result.complete,
                ):
                    counts["removed"] = mark_missing_source_listings_removed(
                        connection,
                        source_id=source_id,
                        seen_external_ids=seen_external_ids,
                    )

                if fetch_result.complete and scan_drained:
                    next_watermark = advance_watermark(
                        connection,
                        source_id,
                        watermark,
                        fetch_result.newest_watermark,
                    )
                else:
                    # A partial fetch or an undrained scan frontier must not
                    # move the cursor: unprocessed URLs would be skipped.
                    next_watermark = watermark
                    logger.info(
                        "event=source_collection_cursor_held source=%s drained=%s complete=%s warnings=%s",
                        adapter.source_name,
                        scan_drained,
                        fetch_result.complete,
                        fetch_result.warnings,
                    )

                finished = finish_source_run(
                    connection,
                    run_id=run_id,
                    owner_token=owner_token,
                    status="success",
                    fetched_count=counts["fetched"],
                    inserted_count=counts["inserted"],
                    updated_count=counts["updated"],
                    unchanged_count=counts["unchanged"],
                )
                if not finished:
                    logger.warning(
                        "event=source_run_completion_ignored source=%s run_id=%s reason=not_running_owner",
                        adapter.source_name,
                        run_id,
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
                finish_source_run(
                    connection,
                    run_id=run_id,
                    owner_token=owner_token,
                    status="failed",
                    error_summary=str(exc)[:1000],
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
        if source_name_token is not None:
            current_source_name.reset(source_name_token)
            current_source_id.reset(source_id_token)
            current_source_run_id.reset(run_token)
        ownership.release()
        coordination.release()
