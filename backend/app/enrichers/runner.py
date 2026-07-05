from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.db import get_engine
from app.enrichers.repository import (
    ENRICHMENT_VERSION,
    compute_enrichment_input_hash,
    latest_enrichment_run_summary,
    list_enrichment_candidates,
)

logger = logging.getLogger("enrichment.runner")


def plan_enrichment_actions(
    *,
    enricher: str = "detail_http",
    source: str | None = None,
    max_jobs: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    source_names = (source,) if source else None
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


def run_enrichment(
    *,
    dry_run: bool = False,
    enqueue_only: bool = False,
    enricher: str = "detail_http",
    source: str | None = None,
    max_jobs: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.enrichment_enabled and not dry_run:
        logger.info("event=enrichment_skipped reason=ENRICHMENT_ENABLED=false")
        return {"status": "skipped", "reason": "ENRICHMENT_ENABLED=false"}

    if dry_run:
        plan = plan_enrichment_actions(enricher=enricher, source=source, max_jobs=max_jobs)
        plan["status"] = "dry_run"
        return plan

    if enqueue_only:
        plan = plan_enrichment_actions(enricher=enricher, source=source, max_jobs=max_jobs)
        plan["status"] = "enqueue_only"
        plan["reason"] = "queue writes ship in a follow-up change"
        return plan

    plan = plan_enrichment_actions(enricher=enricher, source=source, max_jobs=max_jobs)
    plan["status"] = "not_implemented"
    plan["reason"] = "HTTP/static enricher execution ships in a follow-up change"
    return plan
