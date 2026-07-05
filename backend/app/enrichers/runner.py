from __future__ import annotations

import logging
import asyncio
import threading
from typing import Any

import httpx

from app.adapters.eures import EuresAdapter
from app.adapters.jobly import JoblyAdapter, extract_jobly_static_description
from app.adapters.laura import LauraAdapter
from app.adapters.talentech import (
    TALENTECH_USER_AGENT,
    extract_talentech_description,
    talentech_canonical_url,
)
from app.adapters.tmt import TmtAdapter
from app.config import get_settings
from app.db import get_engine
from app.enrichers.base import EnrichmentInput, EnrichmentMethod, EnrichmentResult
from app.enrichers.browser import browser_session, connect_playwright_over_cdp
from app.enrichers.repository import (
    ENRICHMENT_VERSION,
    apply_enrichment_result,
    cancel_inactive_queue_items,
    claim_due_queue_items,
    complete_queue_item,
    compute_enrichment_input_hash,
    create_enrichment_attempt,
    create_enrichment_run,
    enqueue_enrichment_candidates,
    finish_enrichment_attempt,
    finish_enrichment_run,
    latest_enrichment_run_summary,
    list_enrichment_candidates,
    load_queue_input,
    load_raw_listing_payload,
    requeue_stale_running_items,
    retry_or_fail_queue_item,
)

logger = logging.getLogger("enrichment.runner")

TALENTECH_BASE_URLS = {
    "kuntarekry": "https://www.kuntarekry.fi",
    "kirkkorekry": "https://kirkkorekry.fi",
    "valtiolle": "https://valtiolle.fi",
}

DETAIL_HTTP_SOURCE_NAMES = (
    "eures_fi",
    "jobly",
    "kirkkorekry",
    "kuntarekry",
    "laura",
    "tmt",
    "tmt_oulu",
    "valtiolle",
)


def default_source_names_for_enricher(enricher: str) -> tuple[str, ...] | None:
    if enricher == "detail_http":
        return DETAIL_HTTP_SOURCE_NAMES
    if enricher == "jobly_browser":
        return ("jobly",)
    return None


def _run_async_in_thread(coro):
    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - propagate from worker thread.
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def plan_enrichment_actions(
    *,
    enricher: str = "detail_http",
    source: str | None = None,
    max_jobs: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    source_names = (source,) if source else default_source_names_for_enricher(enricher)
    engine = get_engine()
    with engine.connect() as connection:
        candidates = list_enrichment_candidates(
            connection,
            enricher=enricher,
            short_description_threshold=settings.enrichment_short_description_chars,
            source_names=source_names,
            limit=max_jobs or settings.enrichment_max_jobs_per_run,
        )
        last_run = latest_enrichment_run_summary(connection)

    actions = []
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
        actions.append(
            {
                "job_id": candidate.job_id,
                "job_source_id": candidate.job_source_id,
                "raw_listing_id": candidate.raw_listing_id,
                "source_name": candidate.source_name,
                "enricher": candidate.enricher,
                "input_hash": input_hash,
                "current_description_chars": len((candidate.current_description or "").strip()),
                "action": "enqueue_and_enrich",
            }
        )

    return {
        "enrichment_enabled": settings.enrichment_enabled,
        "enricher": enricher,
        "enricher_version": ENRICHMENT_VERSION,
        "candidate_count": len(actions),
        "actions": actions,
        "last_run": last_run,
    }


def _static_payload_result(candidate: EnrichmentInput, payload: dict[str, Any]) -> EnrichmentResult | None:
    listing = None
    if candidate.source_name == "laura":
        listing = LauraAdapter().normalize(payload)
    elif candidate.source_name == "eures_fi":
        listing = EuresAdapter().normalize(payload)
    elif candidate.source_name == "jobly":
        listing = JoblyAdapter().normalize(payload, source_url=candidate.canonical_source_url or candidate.application_url or "")
    elif candidate.source_name in {"tmt", "tmt_oulu"} and payload.get("detail"):
        listing = TmtAdapter().normalize(payload)
    if listing is None or not listing.description:
        return None
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
    return EnrichmentResult(
        job_id=candidate.job_id,
        job_source_id=candidate.job_source_id,
        raw_listing_id=candidate.raw_listing_id,
        enricher=candidate.enricher,
        input_hash=input_hash,
        description=listing.description,
        method=EnrichmentMethod.HTTP_STATIC,
        confidence=0.85,
        structured_fields={},
        provider="static_payload",
    )


def _http_detail_result(candidate: EnrichmentInput, payload: dict[str, Any]) -> EnrichmentResult | None:
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
    if candidate.source_name in {"tmt", "tmt_oulu"}:
        external_id = str(payload.get("id") or "")
        if not external_id:
            return None
        with httpx.Client(timeout=30) as client:
            response = client.get(
                f"https://tyomarkkinatori.fi/api/jobposting-new/v1/public/jobpostings/{external_id}"
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            listing = TmtAdapter().normalize({**payload, "detail": response.json()})
        if not listing.description:
            return None
        return EnrichmentResult(
            job_id=candidate.job_id,
            job_source_id=candidate.job_source_id,
            raw_listing_id=candidate.raw_listing_id,
            enricher=candidate.enricher,
            input_hash=input_hash,
            description=listing.description,
            method=EnrichmentMethod.HTTP_STATIC,
            confidence=0.9,
            provider="tmt_detail_api",
        )

    if candidate.source_name in TALENTECH_BASE_URLS:
        detail_path = str(payload.get("url") or candidate.canonical_source_url or "")
        if not detail_path:
            return None
        base_url = TALENTECH_BASE_URLS[candidate.source_name]
        detail_url = talentech_canonical_url(base_url, detail_path)
        with httpx.Client(timeout=60, headers={"User-Agent": TALENTECH_USER_AGENT}, follow_redirects=True) as client:
            response = client.get(detail_url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            description = extract_talentech_description(response.text)
        if not description:
            return None
        return EnrichmentResult(
            job_id=candidate.job_id,
            job_source_id=candidate.job_source_id,
            raw_listing_id=candidate.raw_listing_id,
            enricher=candidate.enricher,
            input_hash=input_hash,
            description=description,
            method=EnrichmentMethod.HTTP_STATIC,
            confidence=0.9,
            provider="talentech_detail_html",
        )
    return None


async def _jobly_browser_result_async(candidate: EnrichmentInput) -> EnrichmentResult | None:
    url = candidate.canonical_source_url or candidate.application_url
    if candidate.source_name != "jobly" or not url:
        return None
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
    async with browser_session() as session_handle:
        if session_handle.session is None:
            return None
        async with connect_playwright_over_cdp(session_handle.session.connect_url) as cdp:
            await cdp.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            content = await cdp.page.content()
    description = extract_jobly_static_description(content)
    if not description:
        return None
    return EnrichmentResult(
        job_id=candidate.job_id,
        job_source_id=candidate.job_source_id,
        raw_listing_id=candidate.raw_listing_id,
        enricher=candidate.enricher,
        input_hash=input_hash,
        description=description,
        method=EnrichmentMethod.BROWSER_CDP,
        confidence=0.92,
        provider=session_handle.provider,
        browser_session_id=session_handle.session.session_id,
        browser_dashboard_url=session_handle.session.dashboard_url,
    )


def _jobly_browser_result(candidate: EnrichmentInput) -> EnrichmentResult | None:
    return _run_async_in_thread(_jobly_browser_result_async(candidate))


def enrich_candidate(candidate: EnrichmentInput, payload: dict[str, Any]) -> EnrichmentResult | None:
    if candidate.enricher == "jobly_browser":
        return _jobly_browser_result(candidate)
    return _static_payload_result(candidate, payload) or _http_detail_result(candidate, payload)


def run_enrichment(
    *,
    dry_run: bool = False,
    enqueue_only: bool = False,
    enricher: str = "detail_http",
    source: str | None = None,
    max_jobs: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    effective_max_jobs = max_jobs or settings.enrichment_max_jobs_per_run
    if enricher == "jobly_browser":
        effective_max_jobs = min(effective_max_jobs, settings.jobly_browser_enrich_max_per_run)
        source = source or "jobly"
    if not settings.enrichment_enabled and not dry_run:
        logger.info("event=enrichment_skipped reason=ENRICHMENT_ENABLED=false")
        return {"status": "skipped", "reason": "ENRICHMENT_ENABLED=false"}

    if dry_run:
        plan = plan_enrichment_actions(enricher=enricher, source=source, max_jobs=effective_max_jobs)
        plan["status"] = "dry_run"
        return plan

    source_names = (source,) if source else default_source_names_for_enricher(enricher)
    engine = get_engine()
    queued_count = 0
    processed_count = 0
    applied_count = 0
    failed_count = 0
    cancelled_count = 0
    requeued_count = 0
    errors: list[str] = []
    with engine.begin() as connection:
        candidates = list_enrichment_candidates(
            connection,
            enricher=enricher,
            short_description_threshold=settings.enrichment_short_description_chars,
            source_names=source_names,
            limit=effective_max_jobs,
        )
        queued_count = enqueue_enrichment_candidates(connection, candidates)
        requeued_count = requeue_stale_running_items(connection)
        cancelled_count = cancel_inactive_queue_items(connection)
        if enqueue_only:
            return {
                "status": "enqueue_only",
                "queued_count": queued_count,
                "candidate_count": len(candidates),
                "cancelled_count": cancelled_count,
                "requeued_count": requeued_count,
            }
        run_id = create_enrichment_run(
            connection,
            metadata={
                "enricher": enricher,
                "source": source,
                "max_jobs": effective_max_jobs,
                "enricher_version": ENRICHMENT_VERSION,
            },
        )
        queue_rows = claim_due_queue_items(
            connection,
            enricher=enricher,
            source_names=source_names,
            limit=effective_max_jobs,
        )
    for queue_row in queue_rows:
        queue_id = int(queue_row["queue_id"])
        with engine.begin() as connection:
            attempt_id = create_enrichment_attempt(
                connection,
                queue_id=queue_id,
                run_id=run_id,
                provider=enricher,
            )
        try:
            with engine.begin() as connection:
                candidate = load_queue_input(connection, queue_id=queue_id)
                if candidate is None:
                    raise RuntimeError("queue item no longer resolves to an active source occurrence")
                payload = load_raw_listing_payload(
                    connection,
                    raw_listing_id=candidate.raw_listing_id,
                )
            result = enrich_candidate(candidate, payload)
            with engine.begin() as connection:
                if result is None:
                    finish_enrichment_attempt(
                        connection,
                        attempt_id=attempt_id,
                        status="skipped",
                        error="no static/http detail description available",
                    )
                    complete_queue_item(connection, queue_id=queue_id)
                    processed_count += 1
                    continue
                applied = apply_enrichment_result(
                    connection,
                    result,
                    short_description_threshold=settings.enrichment_short_description_chars,
                )
                finish_enrichment_attempt(
                    connection,
                    attempt_id=attempt_id,
                    status="completed",
                    browser_session_id=result.browser_session_id,
                    browser_dashboard_url=result.browser_dashboard_url,
                )
                complete_queue_item(connection, queue_id=queue_id)
                processed_count += 1
                if applied:
                    applied_count += 1
        except Exception as exc:  # noqa: BLE001 - attempt errors must be persisted.
            error = str(exc)[:2000]
            errors.append(error)
            failed_count += 1
            with engine.begin() as connection:
                next_status = retry_or_fail_queue_item(
                    connection,
                    queue_id=queue_id,
                    error=error,
                )
                finish_enrichment_attempt(
                    connection,
                    attempt_id=attempt_id,
                    status="failed",
                    error=f"{next_status}: {error}",
                )
    with engine.begin() as connection:
        finish_enrichment_run(
            connection,
            run_id=run_id,
            status="completed" if not errors else "failed",
            queued_count=queued_count,
            processed_count=processed_count,
            applied_count=applied_count,
            failed_count=failed_count,
            error_summary="; ".join(errors[:3]) if errors else None,
        )
    return {
        "status": "completed" if not errors else "failed",
        "queued_count": queued_count,
        "processed_count": processed_count,
        "applied_count": applied_count,
        "failed_count": failed_count,
        "cancelled_count": cancelled_count,
        "requeued_count": requeued_count,
    }
