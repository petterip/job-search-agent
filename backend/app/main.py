from datetime import datetime, timezone
from typing import Any, Literal

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.db import get_engine
from app.llm import configured_eval_model
from app.logging import configure_logging

configure_logging()

app = FastAPI(title="Job Search Agent API", version="0.1.0")


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    checked_at: datetime
    llm_enabled: bool
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


class SourceStatusResponse(BaseModel):
    sources: list[SourceStatusItem]


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
    published_at: datetime | None
    application_url: str | None
    source_names: list[str]


class RecommendationListResponse(BaseModel):
    items: list[RecommendationListItem]
    limit: int
    offset: int
    total: int


class RecommendationFeedbackResponse(BaseModel):
    id: int
    recommendation_id: int
    action: Literal["good_match", "not_relevant", "applied"]


class JobDetailResponse(BaseModel):
    id: int
    title: str
    employer: str | None
    description: str | None
    location: str | None
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
    recommendation_category, hidden_opportunity = build_recommendation_category(
        title=row_data["title"],
        employer=row_data.get("employer"),
        deterministic_result=deterministic_result,
    )
    return RecommendationListItem(
        **row_data,
        recommendation_category=recommendation_category,
        hidden_opportunity=hidden_opportunity,
    )


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
                ) display_source on true
                where {where_clause}
                order by j.published_at desc nulls last, j.id desc
                limit :limit offset :offset
                """
            ),
            {**params, "limit": limit, "offset": offset},
        ).mappings()
        items = [JobListItem(**row) for row in rows]

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
                    js.last_seen_at
                from job_sources js
                join sources s on s.id = js.source_id
                where js.job_id = :job_id
                  and s.enabled = true
                order by js.last_seen_at desc, s.name
                """
            ),
            {"job_id": job_id},
        ).mappings()
        sources = [JobSourceItem(**source_row) for source_row in source_rows]

        recommendation_row = connection.execute(
            sa.text(
                """
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
                ) display_source on true
                where r.job_id = :job_id
                  and r.is_active = true
                order by r.rank asc nulls last, r.id desc
                limit 1
                """
            ),
            {"job_id": job_id},
        ).mappings().one_or_none()
        recommendation = (
            recommendation_item_from_row(recommendation_row)
            if recommendation_row is not None
            else None
        )

    return JobDetailResponse(
        **row,
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
    return SourceStatusResponse(sources=sources)


@app.get("/recommendations", response_model=RecommendationListResponse)
async def list_recommendations(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RecommendationListResponse:
    engine = get_engine()
    with engine.connect() as connection:
        total = int(
            connection.execute(
                sa.text(
                    """
                    select count(*)
                    from recommendations r
                    join jobs j on j.id = r.job_id
                    where r.is_active = true
                      and j.status = 'active'
                      and exists (
                          select 1
                          from job_sources js
                          join sources s on s.id = js.source_id
                          where js.job_id = j.id
                            and s.enabled = true
                      )
                    """
                )
            ).scalar_one()
        )
        rows = connection.execute(
            sa.text(
                """
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
                ) display_source on true
                where r.is_active = true
                  and j.status = 'active'
                order by r.rank asc nulls last, r.machine_score desc, r.id desc
                limit :limit offset :offset
                """
            ),
            {"limit": limit, "offset": offset},
        ).mappings()
        items = [recommendation_item_from_row(row) for row in rows]
    return RecommendationListResponse(items=items, limit=limit, offset=offset, total=total)


@app.post("/recommendations/{recommendation_id}/feedback", response_model=RecommendationFeedbackResponse)
async def create_recommendation_feedback(
    recommendation_id: int,
    action: Literal["good_match", "not_relevant", "applied"],
    comment: str | None = Query(default=None, max_length=1000),
) -> RecommendationFeedbackResponse:
    engine = get_engine()
    with engine.begin() as connection:
        exists = connection.execute(
            sa.text("select 1 from recommendations where id = :recommendation_id"),
            {"recommendation_id": recommendation_id},
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=404, detail="recommendation not found")
        feedback_id = int(
            connection.execute(
                sa.text(
                    """
                    insert into recommendation_feedback (recommendation_id, action, comment)
                    values (:recommendation_id, :action, :comment)
                    returning id
                    """
                ),
                {
                    "recommendation_id": recommendation_id,
                    "action": action,
                    "comment": comment,
                },
            ).scalar_one()
        )
        if action == "not_relevant":
            connection.execute(
                sa.text(
                    """
                    update recommendations
                    set is_active = false,
                        rank = null
                    where id = :recommendation_id
                    """
                ),
                {"recommendation_id": recommendation_id},
            )
    return RecommendationFeedbackResponse(
        id=feedback_id,
        recommendation_id=recommendation_id,
        action=action,
    )
