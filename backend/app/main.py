from datetime import datetime, timezone
import hmac
import json
from typing import Any, Literal

import sqlalchemy as sa
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import get_engine
from app.enrichers.repository import enrichment_queue_health, latest_enrichment_run_summary
from app.freshness import capture_as_of
from app.llm import configured_eval_model
from app.matching import load_active_profile, structural_eligibility_predicate
from app.location_evidence_service import (
    LocationEvidenceView,
    location_evidence_from_deterministic_result,
    resolve_location_evidence_for_job,
)
from app.logging import configure_logging
from app.source_links import normalize_link, source_external_apply_url
from app.travel_policy import ROUTING_PROFILE, home_city_from_profile, travel_policy_fingerprint

configure_logging()

app = FastAPI(title="Job Search Agent API", version="0.1.0")


def _private_request_needs_auth(method: str, path: str) -> bool:
    """Private reads and every mutation are a boundary.

    `/jobs` (the catalogue list) is public, but `/jobs/{id}` embeds the private
    recommendation explanation, feedback comment and analysis hypothesis, so it
    is protected like `/recommendations`.
    """
    if method not in {"GET", "HEAD", "OPTIONS"}:
        return True
    return path.startswith("/recommendations") or path.startswith("/jobs/")


@app.middleware("http")
async def enforce_operator_access(request: Any, call_next: Any) -> Any:
    """Require the operator token for private reads and all mutations.

    Active only when OPERATOR_API_TOKEN is configured, so local development and
    tests are unchanged. Loopback binding alone does not protect an externally
    exposed reverse proxy, so the same check must also be enforced at the API.
    """
    settings = get_settings()
    token = settings.operator_api_token.strip()
    if token and _private_request_needs_auth(request.method, request.url.path):
        provided = ""
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        if not provided:
            provided = request.headers.get("x-operator-token", "").strip()
        if not provided or not hmac.compare_digest(provided, token):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    checked_at: datetime
    llm_enabled: bool
    transit_distance_enabled: bool
    embedding_model: str
    embedding_dimension: int
    eval_model: str



class JobListItem(BaseModel):
    id: int
    title: str
    employer: str | None
    location: str | None
    published_at: datetime | None
    status: str
    application_url: str | None
    source_names: list[str]


class JobListResponse(BaseModel):
    items: list[JobListItem]
    limit: int
    offset: int
    total: int


class JobSourceItem(BaseModel):
    source_name: str
    application_url: str | None
    external_apply_url: str | None = None
    attribution: str | None
    last_seen_at: datetime


class SourceListResponse(BaseModel):
    sources: list[str]


class SourceStatusItem(BaseModel):
    name: str
    enabled: bool
    poll_interval_min: int
    active_jobs: int
    stored_listings: int
    last_run_status: str | None
    last_run_started_at: datetime | None
    last_run_finished_at: datetime | None
    fetched_count: int
    inserted_count: int
    updated_count: int
    unchanged_count: int
    failed_count: int
    error_summary: str | None
    recent_error_events: int
    recent_warning_events: int


class EnrichmentRunStatus(BaseModel):
    id: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    queued_count: int
    processed_count: int
    applied_count: int
    failed_count: int
    error_summary: str | None


class EnrichmentQueueHealth(BaseModel):
    queued_count: int
    running_count: int
    retry_count: int
    failed_count: int
    stale_running_count: int
    oldest_due_at: datetime | None = None


class SourceStatusResponse(BaseModel):
    sources: list[SourceStatusItem]
    enrichment_last_run: EnrichmentRunStatus | None = None
    enrichment_queue: EnrichmentQueueHealth


class RecommendationFeedbackSummary(BaseModel):
    rating: int
    applied: bool
    analysis_status: str
    comment: str | None = None
    hypothesis_fi: str | None = None


class RecommendationFeedbackBody(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    applied: bool = False
    comment: str | None = Field(default=None, max_length=1000)


RecommendationScope = Literal["commutable", "commutable_or_full_remote", "nationwide"]
RECOMMENDATION_SCOPES: frozenset[str] = frozenset(
    {"commutable", "commutable_or_full_remote", "nationwide"}
)


LOCAL_SHORTCUT_REASONS = "'exact_home_city','contains_home_city'"


def _shortcut_fingerprint_matches() -> str:
    """A stored local shortcut is valid only under the current policy identity."""
    return (
        "r.deterministic_result->'travel_assessment'->>'policy_fingerprint'"
        " = :travel_policy_fingerprint"
    )


def _origin_params_match() -> str:
    # Only routed assessments may rely on matching origin parameters; a shortcut
    # row must prove the current policy fingerprint instead.
    return (
        f"coalesce(r.travel_reason_code, '') not in ({LOCAL_SHORTCUT_REASONS})"
        " and r.travel_origin_address = :travel_origin_address"
        " and r.travel_commute_limit_minutes = :travel_commute_limit_minutes"
        " and r.travel_routing_profile = :travel_routing_profile"
    )


def recommendation_scope_sql(scope: RecommendationScope) -> tuple[str, str]:
    if scope == "commutable":
        return (
            "r.commutable = true"
            " and ("
            f"   (r.travel_reason_code in ({LOCAL_SHORTCUT_REASONS})"
            f"    and {_shortcut_fingerprint_matches()})"
            f"   or ({_origin_params_match()})"
            " )",
            "r.commutable_rank asc nulls last, r.machine_score desc, r.id desc",
        )
    if scope == "commutable_or_full_remote":
        return (
            "r.commutable_or_full_remote = true"
            " and ("
            "   r.travel_reason_code = 'full_remote'"
            f"   or (r.travel_reason_code in ({LOCAL_SHORTCUT_REASONS})"
            f"       and {_shortcut_fingerprint_matches()})"
            f"   or ({_origin_params_match()})"
            " )",
            "case when r.commutable = false and r.full_remote = true then 0 else 1 end, "
            "r.commutable_or_full_remote_rank asc nulls last, r.machine_score desc, r.id desc",
        )
    return (
        "true",
        "case when r.commutable_or_full_remote = false then 0 else 1 end, "
        "r.nationwide_rank asc nulls last, r.machine_score desc, r.id desc",
    )


def current_travel_policy_params(profile: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = get_settings()
    return {
        "travel_origin_address": settings.transit_origin_address,
        "travel_commute_limit_minutes": settings.recommendation_commute_limit_minutes,
        "travel_routing_profile": ROUTING_PROFILE,
        "travel_policy_fingerprint": travel_policy_fingerprint(
            home_city=home_city_from_profile(profile) if profile else None,
            origin_address=settings.transit_origin_address,
            commute_limit_minutes=settings.recommendation_commute_limit_minutes,
        ),
    }


def current_publication_predicate(connection: Any) -> tuple[str, dict[str, Any]]:
    """Current eligibility predicate for published recommendations.

    Jobs age out between nightly runs, so reads must apply the same hard rules
    the writer does. Merges with the current travel policy parameters because
    both contribute named bind parameters.
    """
    profile = load_active_profile(connection)
    policy_params = current_travel_policy_params(profile)
    if profile is None:
        return "true", policy_params
    predicate, params = structural_eligibility_predicate(
        profile=profile, as_of=capture_as_of()
    )
    merged = dict(policy_params)
    merged.update(params)
    return predicate, merged


def recommendation_scope_counts(
    connection: Any,
    *,
    predicate: str = "true",
    predicate_params: dict[str, Any] | None = None,
) -> "RecommendationScopeCounts":
    commutable_filter, _commutable_order = recommendation_scope_sql("commutable")
    remote_filter, _remote_order = recommendation_scope_sql("commutable_or_full_remote")
    scope_params = dict(predicate_params or current_travel_policy_params())
    row = connection.execute(
        sa.text(
            f"""
            select
                count(*) filter (where {commutable_filter})::int as commutable,
                count(*) filter (where {remote_filter})::int as commutable_or_full_remote,
                count(*)::int as nationwide
            from recommendations r
            join jobs j on j.id = r.job_id
            where r.is_active = true
              and ({predicate})
            """
        ),
        scope_params,
    ).mappings().one()
    commutable = int(row["commutable"] or 0)
    remote = int(row["commutable_or_full_remote"] or 0)
    nationwide = int(row["nationwide"] or 0)
    return RecommendationScopeCounts(
        commutable=commutable,
        commutable_or_full_remote=remote,
        nationwide=nationwide,
        remote_only=max(0, remote - commutable),
        nationwide_extra=max(0, nationwide - remote),
    )


class RecommendationListItem(BaseModel):
    id: int
    rank: int | None
    machine_score: float
    llm_score: int | None = None
    fit_tier: str | None = None
    suggested_action: str | None = None
    recommendation_category: str
    hidden_opportunity: bool
    rationale: str | None
    concerns: list[str]
    job_id: int
    title: str
    employer: str | None
    location: str | None
    location_evidence: LocationEvidenceView | None = None
    commutable: bool = False
    full_remote: bool = False
    commutable_or_full_remote: bool = False
    travel_status: str | None = None
    travel_reason_code: str | None = None
    travel_duration_seconds: int | None = None
    travel_distance_km: int | None = None
    travel_commute_limit_minutes: int | None = None
    published_at: datetime | None
    application_url: str | None
    source_names: list[str]
    is_active: bool = True
    feedback: RecommendationFeedbackSummary | None = None


class RecommendationScopeCounts(BaseModel):
    commutable: int
    commutable_or_full_remote: int
    nationwide: int
    remote_only: int
    nationwide_extra: int


class RecommendationListResponse(BaseModel):
    items: list[RecommendationListItem]
    limit: int
    offset: int
    total: int
    scope: RecommendationScope
    scope_counts: RecommendationScopeCounts


class RecommendationFeedbackResponse(BaseModel):
    id: int
    recommendation_id: int
    rating: int
    applied: bool
    analysis_status: str
    recommendation_hidden: bool
    action: Literal["good_match", "not_relevant", "applied"] | None = None


class RecommendationFeedbackAnalysisResponse(BaseModel):
    feedback_id: int
    recommendation_id: int
    provider: str
    configured_model: str
    returned_model: str | None
    prompt_version: int
    analysis: dict[str, Any]
    created_at: datetime


LEGACY_ACTION_TO_RATING: dict[str, tuple[int, bool]] = {
    "not_relevant": (1, False),
    "good_match": (4, False),
    "applied": (5, True),
}


def legacy_action_for_rating(rating: int, applied: bool) -> str | None:
    if applied:
        return "applied"
    if rating == 1:
        return "not_relevant"
    if rating == 4:
        return "good_match"
    return None


def build_scoring_snapshot(
    *,
    recommendation: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    deterministic_result = recommendation.get("deterministic_result") or {}
    if isinstance(deterministic_result, str):
        deterministic_result = json.loads(deterministic_result)
    concerns = recommendation.get("concerns") or []
    if isinstance(concerns, str):
        concerns = json.loads(concerns)
    return {
        "job_id": recommendation["job_id"],
        "recommendation_id": recommendation["id"],
        "title": recommendation["title"],
        "employer": recommendation.get("employer"),
        "location": recommendation.get("location"),
        "candidate_lanes": list(deterministic_result.get("candidate_lanes") or []),
        "hidden_opportunity": bool(deterministic_result.get("hidden_opportunity")),
        "machine_score": float(recommendation["machine_score"]),
        "vector_score": recommendation.get("vector_score"),
        "llm_score": recommendation.get("llm_score"),
        "fit_tier": recommendation.get("fit_tier"),
        "suggested_action": recommendation.get("suggested_action"),
        "rank": recommendation.get("rank"),
        "commutable": bool(recommendation.get("commutable", False)),
        "full_remote": bool(recommendation.get("full_remote", False)),
        "commutable_or_full_remote": bool(recommendation.get("commutable_or_full_remote", False)),
        "commutable_rank": recommendation.get("commutable_rank"),
        "commutable_or_full_remote_rank": recommendation.get("commutable_or_full_remote_rank"),
        "nationwide_rank": recommendation.get("nationwide_rank"),
        "travel_status": recommendation.get("travel_status"),
        "travel_reason_code": recommendation.get("travel_reason_code"),
        "travel_duration_seconds": recommendation.get("travel_duration_seconds"),
        "travel_distance_km": recommendation.get("travel_distance_km"),
        "travel_commute_limit_minutes": recommendation.get("travel_commute_limit_minutes"),
        "travel_routing_profile": recommendation.get("travel_routing_profile"),
        "travel_assessment": deterministic_result.get("travel_assessment"),
        "is_active": bool(recommendation.get("is_active", True)),
        "title_matches": list(deterministic_result.get("title_matches") or []),
        "keyword_matches": list(deterministic_result.get("keyword_matches") or []),
        "application_history_title_matches": list(
            deterministic_result.get("application_history_title_matches") or []
        ),
        "application_history_keyword_matches": list(
            deterministic_result.get("application_history_keyword_matches") or []
        ),
        "sector_matches": list(deterministic_result.get("sector_matches") or []),
        "negative_matches": list(deterministic_result.get("negative_matches") or []),
        "location_matches": list(deterministic_result.get("location_matches") or []),
        "transit_distance_km": deterministic_result.get("transit_distance_km"),
        "transit_duration_text": deterministic_result.get("transit_duration_text"),
        "rationale": recommendation.get("rationale"),
        "concerns": list(concerns),
        "llm_prompt_version": settings.llm_prompt_version,
        "eval_model": configured_eval_model(settings),
    }


def resolve_feedback_submission(
    body: RecommendationFeedbackBody | None,
    *,
    legacy_action: Literal["good_match", "not_relevant", "applied"] | None,
) -> tuple[int, bool, str | None, str | None]:
    if legacy_action is not None and body is not None and body.rating is not None:
        raise HTTPException(status_code=422, detail="use either JSON rating or legacy action, not both")
    if legacy_action is not None:
        rating, applied = LEGACY_ACTION_TO_RATING[legacy_action]
        return rating, applied, None, legacy_action
    if body is None:
        raise HTTPException(status_code=422, detail="rating is required")
    applied = body.applied
    rating = body.rating
    if applied and rating is None:
        rating = 5
    if rating is None:
        raise HTTPException(status_code=422, detail="rating is required")
    if applied and rating <= 2:
        raise HTTPException(status_code=422, detail="applied cannot be set with rating 2 or lower")
    return rating, applied, body.comment, legacy_action_for_rating(rating, applied)


def apply_recommendation_visibility_from_feedback(
    connection: sa.Connection,
    *,
    recommendation_id: int,
    rating: int,
) -> None:
    """Hide rating 1 / not_relevant; restore rating >= 2 subject to eligibility.

    Rating 2 is a soft negative: it stays visible and in the negative learning
    signals, but it never overrides expiry, a source disable or an LLM rejection.
    """
    if rating <= 1:
        connection.execute(
            sa.text(
                """
                update recommendations
                set is_active = false,
                    rank = null,
                    commutable_rank = null,
                    commutable_or_full_remote_rank = null,
                    nationwide_rank = null
                where id = :recommendation_id
                """
            ),
            {"recommendation_id": recommendation_id},
        )
        return
    profile_id = connection.execute(
        sa.text(
            """
            select profile_id
            from recommendations
            where id = :recommendation_id
            """
        ),
        {"recommendation_id": recommendation_id},
    ).scalar_one_or_none()
    if profile_id is None:
        return
    from app.matching import (
        hosted_calls_allowed_for_profile,
        refresh_active_recommendation_ranks,
    )

    settings = get_settings()
    predicate = "true"
    predicate_params: dict[str, Any] = {}
    profile = load_active_profile(connection)
    if profile is not None:
        predicate, predicate_params = structural_eligibility_predicate(
            profile=profile, as_of=capture_as_of()
        )
    # Mirror matching's mode decision, including profile consent: with hosting
    # forbidden, deterministic-only rows must not be deactivated for lacking an
    # LLM evaluation.
    hosted_allowed = hosted_calls_allowed_for_profile(profile)
    deterministic_only = (not hosted_allowed) or not str(settings.llm_provider or "").strip()
    refresh_active_recommendation_ranks(
        connection,
        profile_id=int(profile_id),
        require_llm_review=not deterministic_only,
        deterministic_only=deterministic_only,
        prompt_version=settings.llm_prompt_version,
        extra_predicate=predicate,
        extra_params=predicate_params,
    )


class JobDetailResponse(BaseModel):
    id: int
    title: str
    employer: str | None
    description: str | None
    location: str | None
    location_evidence: LocationEvidenceView | None = None
    published_at: datetime | None
    status: str
    sources: list[JobSourceItem]
    recommendation: RecommendationListItem | None = None


def build_recommendation_category(
    *,
    title: str,
    employer: str | None,
    deterministic_result: dict[str, Any] | None,
) -> tuple[str, bool]:
    result = deterministic_result or {}
    title_text = title.casefold()
    display_text = f"{title} {employer or ''}".casefold()
    display_terms = set(display_text.replace("-", " ").split())
    title_matches = set(result.get("title_matches") or [])
    keyword_matches = set(result.get("keyword_matches") or [])
    sector_matches = set(result.get("sector_matches") or [])
    candidate_lanes = set(result.get("candidate_lanes") or [])
    hidden_opportunity = bool(result.get("hidden_opportunity")) or "exploration" in candidate_lanes

    library_terms = {
        "kirjasto",
        "kirjastonhoitaja",
        "kirjastovirkailija",
        "informaatikko",
        "tiedekirjasto",
        "tietopalvelu",
        "avoin tiede",
    }
    leadership_terms = {
        "johtaja",
        "esihenkilö",
        "päällikkö",
        "koordinaattori",
        "hallinto",
        "hallintosihteeri",
        "kehittäminen",
    }
    culture_terms = {"kulttuuri", "tapahtuma", "viestintä", "sisältö", "tuotanto", "museo", "markkinointi"}
    music_literature_terms = {
        "alttoviulu",
        "artikkeli",
        "kirja",
        "kirjallisuus",
        "kirjoittaminen",
        "musiikki",
        "musiikkiaineisto",
        "musiikkitiede",
        "nuotti",
        "sisällöntuotanto",
        "viulu",
        "julkaisu",
    }
    guidance_terms = {"ohjaus", "neuvonta", "asiakaspalvelu", "nuoriso", "opastus"}
    strong_leadership_terms = leadership_terms - {"koordinaattori"}

    def has_display_term(terms: set[str]) -> bool:
        return bool(display_terms & terms)

    def has_match_term(terms: set[str]) -> bool:
        return bool((title_matches | keyword_matches | sector_matches) & terms)

    if any(term in title_text for term in library_terms) or bool(title_matches & library_terms):
        return ("Kirjasto ja tietopalvelu", hidden_opportunity)
    if any(term in title_text for term in strong_leadership_terms) or bool(title_matches & strong_leadership_terms):
        return ("Johtaminen ja hallinto", hidden_opportunity)
    if has_display_term(music_literature_terms) or has_match_term(music_literature_terms):
        return ("Musiikki, kirjallisuus ja sisällöt", hidden_opportunity)
    if has_display_term(guidance_terms) or has_match_term(guidance_terms):
        return ("Ohjaus ja asiakastyö", hidden_opportunity)
    if has_display_term(culture_terms) or has_match_term(culture_terms):
        return ("Kulttuuri, tapahtumat ja viestintä", hidden_opportunity)
    if has_display_term(leadership_terms) or has_match_term(leadership_terms):
        return ("Johtaminen ja hallinto", hidden_opportunity)
    if has_display_term(library_terms) or has_match_term(library_terms):
        return ("Kirjasto ja tietopalvelu", hidden_opportunity)
    if hidden_opportunity:
        return ("Piilo-osumat", True)
    return ("Muut vahvuusosumat", False)


def recommendation_item_from_row(row: dict) -> RecommendationListItem:
    row_data = dict(row)
    deterministic_result = row_data.pop("deterministic_result", None)
    feedback_rating = row_data.pop("feedback_rating", None)
    feedback_applied = row_data.pop("feedback_applied", None)
    feedback_analysis_status = row_data.pop("feedback_analysis_status", None)
    feedback_comment = row_data.pop("feedback_comment", None)
    feedback_hypothesis_fi = row_data.pop("feedback_hypothesis_fi", None)
    feedback = None
    if feedback_rating is not None:
        feedback = RecommendationFeedbackSummary(
            rating=int(feedback_rating),
            applied=bool(feedback_applied),
            analysis_status=str(feedback_analysis_status or "pending"),
            comment=feedback_comment,
            hypothesis_fi=feedback_hypothesis_fi,
        )
    is_active = bool(row_data.pop("is_active", True))
    recommendation_category, hidden_opportunity = build_recommendation_category(
        title=row_data["title"],
        employer=row_data.get("employer"),
        deterministic_result=deterministic_result,
    )
    stored_evidence = location_evidence_from_deterministic_result(deterministic_result)
    item = RecommendationListItem(
        **row_data,
        is_active=is_active,
        feedback=feedback,
        recommendation_category=recommendation_category,
        hidden_opportunity=hidden_opportunity,
        location_evidence=stored_evidence,
    )
    safe_url = normalize_link(item.application_url)
    if safe_url != item.application_url:
        # Re-validate persisted URLs so a stale unsafe value is never rendered.
        item = item.model_copy(update={"application_url": safe_url})
    return item


def build_jobs_where_clause(
    *,
    query: str | None,
    source: str | None,
    employer: str | None,
    location: str | None,
    published_after: datetime | None,
) -> tuple[str, dict[str, object]]:
    clauses = ["j.status = 'active'"]
    params: dict[str, object] = {}

    if query:
        clauses.append(
            """
            (
                j.title ilike :query
                or coalesce(j.employer, '') ilike :query
                or coalesce(j.description, '') ilike :query
            )
            """
        )
        params["query"] = f"%{query.strip()}%"

    if employer:
        clauses.append("coalesce(j.employer, '') ilike :employer")
        params["employer"] = f"%{employer.strip()}%"

    if location:
        clauses.append("coalesce(j.location, '') ilike :location")
        params["location"] = f"%{location.strip()}%"

    if published_after:
        clauses.append("j.published_at >= :published_after")
        params["published_after"] = published_after

    if source:
        clauses.append(
            """
            exists (
                select 1
                from job_sources filter_js
                join sources filter_s on filter_s.id = filter_js.source_id
                where filter_js.job_id = j.id
                  and filter_s.enabled = true
                  and filter_s.name = :source
            )
            """
        )
        params["source"] = source

    clauses.append(
        """
        exists (
            select 1
            from job_sources visible_js
            join sources visible_s on visible_s.id = visible_js.source_id
            where visible_js.job_id = j.id
              and visible_s.enabled = true
        )
        """
    )
    return " and ".join(f"({clause})" for clause in clauses), params


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        checked_at=datetime.now(timezone.utc),
        llm_enabled=bool(settings.llm_provider),
        transit_distance_enabled=get_settings().google_maps_configured(),
        embedding_model=settings.openai_embedding_model,
        embedding_dimension=settings.openai_embedding_dimension,
        eval_model=configured_eval_model(settings),
    )


@app.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, min_length=1, max_length=120),
    source: str | None = Query(default=None, min_length=1, max_length=64),
    employer: str | None = Query(default=None, min_length=1, max_length=120),
    location: str | None = Query(default=None, min_length=1, max_length=120),
    published_after: datetime | None = None,
) -> JobListResponse:
    where_clause, params = build_jobs_where_clause(
        query=q,
        source=source,
        employer=employer,
        location=location,
        published_after=published_after,
    )
    engine = get_engine()
    with engine.connect() as connection:
        total = int(
            connection.execute(
                sa.text(
                    f"""
                    select count(*)
                    from jobs j
                    where {where_clause}
                    """
                ),
                params,
            ).scalar_one()
        )
        rows = connection.execute(
            sa.text(
                f"""
                select
                    j.id,
                    j.title,
                    j.employer,
                    j.location,
                    j.published_at,
                    j.status,
                    display_source.application_url,
                    display_source.source_names
                from jobs j
                join lateral (
                    select
                        (array_agg(js.application_url order by js.last_seen_at desc))[1] as application_url,
                        array_agg(s.name order by s.name) as source_names
                    from job_sources js
                    join sources s on s.id = js.source_id
                    where js.job_id = j.id
                      and s.enabled = true
                    having count(s.id) > 0
                ) display_source on true
                where {where_clause}
                order by j.published_at desc nulls last, j.id desc
                limit :limit offset :offset
                """
            ),
            {**params, "limit": limit, "offset": offset},
        ).mappings()
        items = [
            JobListItem(
                **{
                    **dict(row),
                    "application_url": normalize_link(row["application_url"]),
                }
            )
            for row in rows
        ]

    return JobListResponse(items=items, limit=limit, offset=offset, total=total)


@app.get("/jobs/{job_id}", response_model=JobDetailResponse)
async def get_job(job_id: int) -> JobDetailResponse:
    engine = get_engine()
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                """
                select id, title, employer, description, location, published_at, status
                from jobs
                where id = :job_id
                  and status = 'active'
                  and exists (
                      select 1
                      from job_sources js
                      join sources s on s.id = js.source_id
                      where js.job_id = jobs.id
                        and s.enabled = true
                  )
                """
            ),
            {"job_id": job_id},
        ).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")

        source_rows = connection.execute(
            sa.text(
                """
                select
                    s.name as source_name,
                    js.application_url,
                    js.attribution,
                    js.last_seen_at,
                    rl.payload
                from job_sources js
                join sources s on s.id = js.source_id
                join raw_listings rl on rl.id = js.raw_listing_id
                where js.job_id = :job_id
                  and s.enabled = true
                order by js.last_seen_at desc, s.name
                """
            ),
            {"job_id": job_id},
        ).mappings()
        sources = []
        for source_row in source_rows:
            payload = source_row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            sources.append(
                JobSourceItem(
                    source_name=source_row["source_name"],
                    application_url=normalize_link(source_row["application_url"]),
                    external_apply_url=source_external_apply_url(
                        source_row["source_name"],
                        payload if isinstance(payload, dict) else None,
                        source_row["application_url"],
                    ),
                    attribution=source_row["attribution"],
                    last_seen_at=source_row["last_seen_at"],
                )
            )

        publication_predicate, publication_params = current_publication_predicate(connection)
        recommendation_row = connection.execute(
            sa.text(
                f"""
                select
                    r.id,
                    r.rank,
                    r.machine_score,
                    r.llm_score,
                    r.fit_tier,
                    r.suggested_action,
                    r.deterministic_result,
                    r.rationale,
                    r.concerns,
                    (r.is_active and ({publication_predicate})) as is_active,
                    r.vector_score,
                    r.commutable,
                    r.full_remote,
                    r.commutable_or_full_remote,
                    r.travel_status,
                    r.travel_reason_code,
                    r.travel_duration_seconds,
                    r.travel_distance_km,
                    r.travel_commute_limit_minutes,
                    j.id as job_id,
                    j.title,
                    j.employer,
                    j.location,
                    j.published_at,
                    display_source.application_url,
                    display_source.source_names,
                    rf.rating as feedback_rating,
                    rf.applied as feedback_applied,
                    rf.analysis_status as feedback_analysis_status,
                    rf.comment as feedback_comment,
                    case
                        when rf.analysis_status = 'completed'
                        then fa.analysis->>'hypothesis_fi'
                    end as feedback_hypothesis_fi
                from recommendations r
                join jobs j on j.id = r.job_id
                left join recommendation_feedback rf on rf.recommendation_id = r.id
                left join feedback_llm_analyses fa on fa.feedback_id = rf.id
                join lateral (
                    select
                        (array_agg(js.application_url order by js.last_seen_at desc))[1] as application_url,
                        array_agg(s.name order by s.name) as source_names
                    from job_sources js
                    join sources s on s.id = js.source_id
                    where js.job_id = j.id
                      and s.enabled = true
                    having count(s.id) > 0
                ) display_source on true
                where r.job_id = :job_id
                order by r.id desc
                limit 1
                """
            ),
            {"job_id": job_id, **publication_params},
        ).mappings().one_or_none()
        recommendation = (
            recommendation_item_from_row(recommendation_row)
            if recommendation_row is not None
            else None
        )
        job_evidence = resolve_location_evidence_for_job(
            connection,
            row["location"],
            fallback=recommendation.location_evidence if recommendation is not None else None,
            max_lookups=1,
        )
        if recommendation is not None:
            recommendation = recommendation.model_copy(update={"location_evidence": job_evidence})
        connection.commit()

    return JobDetailResponse(
        **row,
        location_evidence=job_evidence,
        sources=sources,
        recommendation=recommendation,
    )


@app.get("/sources", response_model=SourceListResponse)
async def list_sources() -> SourceListResponse:
    engine = get_engine()
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                """
                select name
                from sources
                where enabled = true
                order by name
                """
            )
        ).scalars()
        return SourceListResponse(sources=list(rows))


@app.get("/sources/status", response_model=SourceStatusResponse)
async def list_source_status() -> SourceStatusResponse:
    engine = get_engine()
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                """
                select
                    s.name,
                    s.enabled,
                    s.poll_interval_min,
                    coalesce(source_totals.active_jobs, 0) as active_jobs,
                    coalesce(source_totals.stored_listings, 0) as stored_listings,
                    latest_run.status as last_run_status,
                    latest_run.started_at as last_run_started_at,
                    latest_run.finished_at as last_run_finished_at,
                    coalesce(latest_run.fetched_count, 0) as fetched_count,
                    coalesce(latest_run.inserted_count, 0) as inserted_count,
                    coalesce(latest_run.updated_count, 0) as updated_count,
                    coalesce(latest_run.unchanged_count, 0) as unchanged_count,
                    coalesce(latest_run.failed_count, 0) as failed_count,
                    latest_run.error_summary,
                    coalesce(event_counts.recent_error_events, 0) as recent_error_events,
                    coalesce(event_counts.recent_warning_events, 0) as recent_warning_events
                from sources s
                left join lateral (
                    select
                        count(distinct js.job_id) filter (where j.status = 'active') as active_jobs,
                        count(rl.id) as stored_listings
                    from raw_listings rl
                    left join job_sources js on js.raw_listing_id = rl.id
                    left join jobs j on j.id = js.job_id
                    where rl.source_id = s.id
                ) source_totals on true
                left join lateral (
                    select
                        sr.status,
                        sr.started_at,
                        sr.finished_at,
                        sr.fetched_count,
                        sr.inserted_count,
                        sr.updated_count,
                        sr.unchanged_count,
                        sr.failed_count,
                        sr.error_summary
                    from source_runs sr
                    where sr.source_id = s.id
                    order by sr.started_at desc, sr.id desc
                    limit 1
                ) latest_run on true
                left join lateral (
                    select
                        count(*) filter (where sre.level in ('ERROR', 'CRITICAL')) as recent_error_events,
                        count(*) filter (where sre.level = 'WARNING') as recent_warning_events
                    from source_run_events sre
                    where sre.source_id = s.id
                      and sre.created_at >= now() - interval '7 days'
                ) event_counts on true
                order by s.enabled desc, s.name
                """
            )
        ).mappings()
        sources = [SourceStatusItem(**row) for row in rows]
        enrichment_row = latest_enrichment_run_summary(connection)
        enrichment_queue = enrichment_queue_health(connection)
    return SourceStatusResponse(
        sources=sources,
        enrichment_last_run=(
            EnrichmentRunStatus(**enrichment_row) if enrichment_row is not None else None
        ),
        enrichment_queue=EnrichmentQueueHealth(**enrichment_queue),
    )


@app.get("/recommendations", response_model=RecommendationListResponse)
async def list_recommendations(
    scope: RecommendationScope = Query(default="commutable"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RecommendationListResponse:
    if scope not in RECOMMENDATION_SCOPES:
        raise HTTPException(status_code=422, detail="unknown recommendation scope")
    scope_filter, scope_order = recommendation_scope_sql(scope)
    engine = get_engine()
    with engine.connect() as connection:
        publication_predicate, scope_params = current_publication_predicate(connection)
        scope_counts = recommendation_scope_counts(
            connection,
            predicate=publication_predicate,
            predicate_params=scope_params,
        )
        total = int(
            connection.execute(
                sa.text(
                    f"""
                    select count(*)
                    from recommendations r
                    join jobs j on j.id = r.job_id
                    where r.is_active = true
                      and {scope_filter}
                      and ({publication_predicate})
                    """
                ),
                scope_params,
            ).scalar_one()
        )
        rows = connection.execute(
            sa.text(
                f"""
                select
                    r.id,
                    row_number() over (order by {scope_order})::int as rank,
                    r.machine_score,
                    r.llm_score,
                    r.fit_tier,
                    r.suggested_action,
                    r.deterministic_result,
                    r.rationale,
                    r.concerns,
                    r.commutable,
                    r.full_remote,
                    r.commutable_or_full_remote,
                    r.travel_status,
                    r.travel_reason_code,
                    r.travel_duration_seconds,
                    r.travel_distance_km,
                    r.travel_commute_limit_minutes,
                    j.id as job_id,
                    j.title,
                    j.employer,
                    j.location,
                    j.published_at,
                    display_source.application_url,
                    display_source.source_names
                from recommendations r
                join jobs j on j.id = r.job_id
                join lateral (
                    select
                        (array_agg(js.application_url order by js.last_seen_at desc))[1] as application_url,
                        array_agg(s.name order by s.name) as source_names
                    from job_sources js
                    join sources s on s.id = js.source_id
                    where js.job_id = j.id
                      and s.enabled = true
                    having count(s.id) > 0
                ) display_source on true
                where r.is_active = true
                  and ({publication_predicate})
                  and {scope_filter}
                order by {scope_order}
                limit :limit offset :offset
                """
            ),
            {"scope": scope, "limit": limit, "offset": offset, **scope_params},
        ).mappings()
        items = [recommendation_item_from_row(row) for row in rows]
        connection.commit()
    return RecommendationListResponse(
        items=items,
        limit=limit,
        offset=offset,
        total=total,
        scope=scope,
        scope_counts=scope_counts,
    )


@app.get("/recommendations/{recommendation_id}/feedback", response_model=RecommendationFeedbackResponse)
async def get_recommendation_feedback(recommendation_id: int) -> RecommendationFeedbackResponse:
    engine = get_engine()
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                """
                select
                    rf.id,
                    rf.recommendation_id,
                    rf.rating,
                    rf.applied,
                    rf.analysis_status,
                    rf.action
                from recommendation_feedback rf
                where rf.recommendation_id = :recommendation_id
                """
            ),
            {"recommendation_id": recommendation_id},
        ).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="feedback not found")
    return RecommendationFeedbackResponse(
        id=int(row["id"]),
        recommendation_id=int(row["recommendation_id"]),
        rating=int(row["rating"]),
        applied=bool(row["applied"]),
        analysis_status=str(row["analysis_status"]),
        recommendation_hidden=int(row["rating"]) <= 1,
        action=row["action"],
    )


@app.post("/recommendations/{recommendation_id}/feedback", response_model=RecommendationFeedbackResponse)
async def create_recommendation_feedback(
    recommendation_id: int,
    body: RecommendationFeedbackBody | None = Body(default=None),
    action: Literal["good_match", "not_relevant", "applied"] | None = Query(default=None),
    comment: str | None = Query(default=None, max_length=1000),
) -> RecommendationFeedbackResponse:
    rating, applied, resolved_comment, legacy_action = resolve_feedback_submission(
        body,
        legacy_action=action,
    )
    if body is None and comment is not None:
        resolved_comment = comment
    settings = get_settings()
    engine = get_engine()
    with engine.begin() as connection:
        recommendation = connection.execute(
            sa.text(
                """
                select
                    r.id,
                    r.job_id,
                    r.rank,
                    r.commutable,
                    r.full_remote,
                    r.commutable_or_full_remote,
                    r.commutable_rank,
                    r.commutable_or_full_remote_rank,
                    r.nationwide_rank,
                    r.travel_status,
                    r.travel_reason_code,
                    r.travel_duration_seconds,
                    r.travel_distance_km,
                    r.travel_commute_limit_minutes,
                    r.travel_routing_profile,
                    r.machine_score,
                    r.vector_score,
                    r.llm_score,
                    r.fit_tier,
                    r.suggested_action,
                    r.deterministic_result,
                    r.rationale,
                    r.concerns,
                    r.is_active,
                    j.title,
                    j.employer,
                    j.location
                from recommendations r
                join jobs j on j.id = r.job_id
                where r.id = :recommendation_id
                """
            ),
            {"recommendation_id": recommendation_id},
        ).mappings().one_or_none()
        if recommendation is None:
            raise HTTPException(status_code=404, detail="recommendation not found")
        profile_row = connection.execute(
            sa.text(
                """
                select profile
                from job_seeker_profiles
                order by id
                limit 1
                """
            )
        ).mappings().one_or_none()
        profile = (
            dict(profile_row["profile"])
            if profile_row is not None and isinstance(profile_row["profile"], dict)
            else None
        )
        from app.feedback_analysis import sanitize_feedback_comment

        resolved_comment = sanitize_feedback_comment(resolved_comment, profile=profile)
        existing = connection.execute(
            sa.text(
                """
                select id, rating, applied, comment, scoring_snapshot, analysis_status
                from recommendation_feedback
                where recommendation_id = :recommendation_id
                """
            ),
            {"recommendation_id": recommendation_id},
        ).mappings().one_or_none()
        # Idempotency is decided from the user's input only. A rank or score
        # change between submissions must not create a new paid analysis.
        same_user_input = (
            existing is not None
            and int(existing["rating"]) == rating
            and bool(existing["applied"]) == applied
            and (existing["comment"] or None) == (resolved_comment or None)
        )
        if same_user_input:
            stored_snapshot = existing["scoring_snapshot"]
            connection.execute(
                sa.text(
                    """
                    update recommendation_feedback
                    set scoring_snapshot = CAST(:scoring_snapshot AS jsonb),
                        updated_at = now()
                    where id = :feedback_id
                    """
                ),
                {
                    "feedback_id": int(existing["id"]),
                    "scoring_snapshot": json.dumps(
                        stored_snapshot
                        if isinstance(stored_snapshot, dict)
                        else json.loads(stored_snapshot or "{}"),
                        ensure_ascii=False,
                    ),
                },
            )
            analysis_status = str(existing["analysis_status"])
            if analysis_status in {"failed", "skipped"}:
                connection.execute(
                    sa.text(
                        """
                        update recommendation_feedback
                        set analysis_status = 'pending',
                            updated_at = now()
                        where id = :feedback_id
                        """
                    ),
                    {"feedback_id": int(existing["id"])},
                )
                analysis_status = "pending"
            apply_recommendation_visibility_from_feedback(
                connection,
                recommendation_id=recommendation_id,
                rating=rating,
            )
            return RecommendationFeedbackResponse(
                id=int(existing["id"]),
                recommendation_id=recommendation_id,
                rating=rating,
                applied=applied,
                analysis_status=analysis_status,
                recommendation_hidden=rating <= 1,
                action=legacy_action,
            )
        scoring_snapshot = build_scoring_snapshot(
            recommendation=dict(recommendation),
            settings=settings,
        )
        if existing is not None:
            connection.execute(
                sa.text(
                    """
                    delete from feedback_llm_analyses
                    where feedback_id = :feedback_id
                    """
                ),
                {"feedback_id": int(existing["id"])},
            )
        feedback_id = int(
            connection.execute(
                sa.text(
                    """
                    insert into recommendation_feedback (
                        recommendation_id,
                        rating,
                        applied,
                        comment,
                        action,
                        job_id,
                        scoring_snapshot,
                        analysis_status,
                        updated_at
                    )
                    values (
                        :recommendation_id,
                        :rating,
                        :applied,
                        :comment,
                        :action,
                        :job_id,
                        CAST(:scoring_snapshot AS jsonb),
                        'pending',
                        now()
                    )
                    on conflict (recommendation_id)
                    do update set
                        rating = excluded.rating,
                        applied = excluded.applied,
                        comment = excluded.comment,
                        action = excluded.action,
                        job_id = excluded.job_id,
                        scoring_snapshot = excluded.scoring_snapshot,
                        analysis_status = 'pending',
                        updated_at = now()
                    returning id
                    """
                ),
                {
                    "recommendation_id": recommendation_id,
                    "rating": rating,
                    "applied": applied,
                    "comment": resolved_comment,
                    "action": legacy_action,
                    "job_id": recommendation["job_id"],
                    "scoring_snapshot": json.dumps(scoring_snapshot, ensure_ascii=False),
                },
            ).scalar_one()
        )
        apply_recommendation_visibility_from_feedback(
            connection,
            recommendation_id=recommendation_id,
            rating=rating,
        )
        analysis_status = "pending"
    return RecommendationFeedbackResponse(
        id=feedback_id,
        recommendation_id=recommendation_id,
        rating=rating,
        applied=applied,
        analysis_status=analysis_status,
        recommendation_hidden=rating <= 1,
        action=legacy_action,
    )


@app.get(
    "/recommendations/{recommendation_id}/feedback/analysis",
    response_model=RecommendationFeedbackAnalysisResponse,
)
async def get_recommendation_feedback_analysis(
    recommendation_id: int,
) -> RecommendationFeedbackAnalysisResponse:
    engine = get_engine()
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                """
                select
                    fa.feedback_id,
                    rf.recommendation_id,
                    fa.provider,
                    fa.configured_model,
                    fa.returned_model,
                    fa.prompt_version,
                    fa.analysis,
                    fa.created_at
                from feedback_llm_analyses fa
                join recommendation_feedback rf on rf.id = fa.feedback_id
                where rf.recommendation_id = :recommendation_id
                """
            ),
            {"recommendation_id": recommendation_id},
        ).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="feedback analysis not found")
    analysis = row["analysis"]
    if isinstance(analysis, str):
        analysis = json.loads(analysis)
    return RecommendationFeedbackAnalysisResponse(
        feedback_id=int(row["feedback_id"]),
        recommendation_id=int(row["recommendation_id"]),
        provider=str(row["provider"]),
        configured_model=str(row["configured_model"]),
        returned_model=row["returned_model"],
        prompt_version=int(row["prompt_version"]),
        analysis=dict(analysis),
        created_at=row["created_at"],
    )
