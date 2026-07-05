import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.config import get_settings
from app.embeddings import (
    build_embedding_provider,
    ensure_job_embeddings,
    ensure_profile_embedding,
    semantic_vector_scores,
)
from app.llm import (
    EvaluationProvider,
    EvaluationProviderUnavailable,
    build_evaluation_provider,
    configured_eval_model,
    evaluation_request_hash,
    job_summary,
    mark_provider_unavailable,
    minimized_profile_summary,
    normalized_evaluation_payload,
)
from app.transit_distance import (
    apply_location_evidence_to_concerns,
    build_location_evidence,
    resolve_transit_for_locations,
)

logger = logging.getLogger("matcher")

TOKEN_PATTERN = re.compile(r"[0-9a-zåäö]+")
INFLECTION_NORMALIZATIONS = {
    "asiakaspalvelun": "asiakaspalvelu",
    "asiakaspalvelussa": "asiakaspalvelu",
    "hallinnon": "hallinto",
    "hallinnossa": "hallinto",
    "hallinnollinen": "hallinto",
    "hallinnollisia": "hallinto",
    "hallinnollisten": "hallinto",
    "neuvonnan": "neuvonta",
    "neuvonnassa": "neuvonta",
    "opastuksen": "opastus",
    "opastuksessa": "opastus",
    "opastusta": "opastus",
    "julkaisuja": "julkaisu",
    "julkaisuissa": "julkaisu",
    "kirjallisuustapahtumia": "kirjallisuus",
    "kirjoitetaan": "kirjoittaminen",
    "kirjoittaa": "kirjoittaminen",
    "tapahtumien": "tapahtuma",
    "tapahtumia": "tapahtuma",
    "tapahtumissa": "tapahtuma",
    "viestinnän": "viestintä",
    "viestinnässä": "viestintä",
}
INFLECTION_SUFFIXES = (
    "issa",
    "issä",
    "sta",
    "stä",
    "lla",
    "llä",
    "lle",
    "ksi",
    "ssa",
    "ssä",
    "na",
    "nä",
    "ta",
    "tä",
    "en",
    "in",
    "n",
)
GENERIC_MATCH_TERMS = {
    "avoin",
    "haetaan",
    "johtava",
    "muu",
    "osaaja",
    "päällikkö",
    "työntekijä",
    "vastaava",
}
MISSING_QUALIFICATION_REJECT_PATTERNS = {
    "opettajan_kelpoisuus": (
        r"\bopettajan kelpoisuus\b",
        r"\bopettajakelpoisuus\b",
        r"\bkelpoinen opettaja\b",
        r"\bluokanopettaja\b",
        r"\baineenopettaja\b",
        r"\bvarhaiskasvatuksen opettaja\b",
    ),
    "c_ce_ajokortti": (
        r"\bc[\s-]*ajokortti\b",
        r"\bce[\s-]*ajokortti\b",
        r"\bc[\s-]*tai[\s-]*ce[\s-]*ajokortti\b",
        r"\bkuorma[\s-]*autokortti\b",
        r"\bkuorma[\s-]*auton kuljettaja\b",
    ),
}
COMPOUND_HARD_REJECT_TERMS = {
    "kirjastoautonkuljettaja",
}
LANE_QUOTAS = {
    "direct_title": 10,
    "application_history": 8,
    "semantic_similarity": 8,
    "transferable_duty": 6,
    "sector_context": 4,
    "exploration": 2,
}
SEMANTIC_VECTOR_THRESHOLD = 0.56


@dataclass(frozen=True)
class JobForScoring:
    id: int
    title: str
    employer: str | None
    description: str | None
    location: str | None


@dataclass(frozen=True)
class ScoreResult:
    passes: bool
    machine_score: float
    vector_score: float | None
    rationale: str
    concerns: list[str]
    deterministic_result: dict[str, Any]


def tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    return set(TOKEN_PATTERN.findall(value.casefold()))


def token_variants(token: str) -> set[str]:
    variants = {token}
    normalized = INFLECTION_NORMALIZATIONS.get(token)
    if normalized:
        variants.add(normalized)
    if len(token) >= 7:
        for suffix in INFLECTION_SUFFIXES:
            if token.endswith(suffix) and len(token) - len(suffix) >= 5:
                variants.add(token[: -len(suffix)])
    return variants


def expanded_tokens(value: str | None) -> set[str]:
    expanded: set[str] = set()
    for token in tokens(value):
        expanded.update(token_variants(token))
    return expanded


def matching_terms(profile_term_set: set[str], job_term_set: set[str]) -> list[str]:
    return sorted(term for term in profile_term_set if token_variants(term) & job_term_set)


def normalized_text(value: str | None) -> str:
    return " ".join(TOKEN_PATTERN.findall((value or "").casefold()))


def profile_terms(profile: dict[str, Any], key: str) -> set[str]:
    terms: set[str] = set()
    for cluster in profile.get("role_clusters", []):
        for value in cluster.get(key, []):
            terms.update(tokens(str(value)))
    return terms - GENERIC_MATCH_TERMS


def application_history_terms(profile: dict[str, Any], key: str) -> set[str]:
    preferences = profile.get("preferences", {})
    if not isinstance(preferences, dict):
        return set()
    signals = preferences.get("application_history_signals", {})
    if not isinstance(signals, dict):
        return set()
    terms: set[str] = set()
    for value in signals.get(key, []):
        terms.update(tokens(str(value)))
    return terms - GENERIC_MATCH_TERMS


def preferred_sector_terms(profile: dict[str, Any]) -> set[str]:
    preferences = profile.get("preferences", {})
    if not isinstance(preferences, dict):
        return set()
    terms: set[str] = set()
    for value in preferences.get("sectors_preferred", []):
        terms.update(tokens(str(value)))
    return terms - GENERIC_MATCH_TERMS


def allowed_locations(profile: dict[str, Any]) -> set[str]:
    location = profile.get("location", {})
    values = [
        location.get("home_city"),
        *location.get("region_towns", []),
        *location.get("proven_willing_locations", []),
    ]
    return {term for value in values for term in tokens(str(value))}


def exclusion_terms(profile: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    exclusions = profile.get("exclusions", {})
    if isinstance(exclusions, dict):
        for title in exclusions.get("hard_negative_titles_fi", []):
            terms.update(tokens(str(title)))
    for title in profile.get("hard_exclusions", []):
        terms.update(tokens(str(title)))
    for title in profile.get("negative_keywords", []):
        terms.update(tokens(str(title)))
    return {term for term in terms if len(term) >= 4}


def qualification_caution_terms(profile: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    exclusions = profile.get("exclusions", {})
    if not isinstance(exclusions, dict):
        return terms
    for phrase in exclusions.get("reject_if_required_qualification_missing", []):
        terms.update(token for token in tokens(str(phrase)) if len(token) >= 6)
    return terms


def missing_qualification_reject_matches(job_text: str) -> list[str]:
    matches: list[str] = []
    for label, patterns in MISSING_QUALIFICATION_REJECT_PATTERNS.items():
        if any(re.search(pattern, job_text) for pattern in patterns):
            matches.append(label)
    for term in COMPOUND_HARD_REJECT_TERMS:
        if term in job_text:
            matches.append(term)
    return sorted(set(matches))


def score_job(profile: dict[str, Any], job: JobForScoring) -> ScoreResult:
    title_terms = profile_terms(profile, "titles_fi")
    keyword_terms = profile_terms(profile, "keywords_fi")
    application_title_terms = application_history_terms(profile, "boost_titles_fi")
    application_keyword_terms = application_history_terms(profile, "boost_keywords_fi")
    application_location_terms = application_history_terms(profile, "boost_locations")
    sector_terms = preferred_sector_terms(profile)
    location_terms = allowed_locations(profile)
    negative_terms = exclusion_terms(profile)
    caution_terms = qualification_caution_terms(profile)

    job_title_terms = expanded_tokens(job.title)
    job_text = normalized_text(" ".join(part or "" for part in (job.title, job.employer, job.description)))
    job_text_terms = expanded_tokens(" ".join(part or "" for part in (job.title, job.employer, job.description)))
    job_location_terms = expanded_tokens(job.location)

    title_matches = matching_terms(title_terms, job_title_terms)
    keyword_matches = matching_terms(keyword_terms, job_text_terms)
    application_title_matches = matching_terms(application_title_terms, job_title_terms)
    application_keyword_matches = matching_terms(application_keyword_terms, job_text_terms)
    application_location_matches = matching_terms(application_location_terms, job_location_terms)
    sector_matches = matching_terms(sector_terms, job_text_terms)
    negative_matches = sorted(term for term in negative_terms if term in job_text)
    caution_matches = sorted(term for term in caution_terms if term in job_text)
    missing_qualification_matches = missing_qualification_reject_matches(job_text)
    location_matches = sorted(job_location_terms & location_terms)

    concerns: list[str] = []
    if negative_matches:
        concerns.append("Ilmoitus sisältää hakijalle poissulkevia termejä.")
    if missing_qualification_matches:
        concerns.append("Ilmoitus edellyttää kelpoisuutta, jota hakijalla ei ole.")
    if caution_matches:
        concerns.append("Kelpoisuusvaatimus pitää tarkistaa ennen hakemista.")
    if job.location and not location_matches:
        concerns.append("Sijainti ei osu hakijan ensisijaiseen alueeseen.")

    score = 0.0
    score += min(len(title_matches), 5) * 15
    score += min(len(keyword_matches), 8) * 5
    score += min(len(application_title_matches), 4) * 18
    score += min(len(application_keyword_matches), 6) * 5
    score += min(len(sector_matches), 3) * 4
    if location_matches:
        score += 15
    elif not job.location:
        score += 3
    if application_location_matches:
        score += 7
    if negative_matches or missing_qualification_matches:
        score -= 40

    candidate_lanes: list[str] = []
    if title_matches:
        candidate_lanes.append("direct_title")
    if application_title_matches or application_keyword_matches:
        candidate_lanes.append("application_history")
    if not title_matches and len(keyword_matches) >= 2:
        candidate_lanes.append("transferable_duty")
    if sector_matches and (keyword_matches or application_keyword_matches or application_title_matches):
        candidate_lanes.append("sector_context")
    hidden_opportunity = not title_matches and any(
        lane in candidate_lanes
        for lane in ("application_history", "transferable_duty", "sector_context")
    )
    if hidden_opportunity:
        candidate_lanes.append("exploration")

    score = max(0.0, min(100.0, score))
    passes = score >= 15 and not negative_matches and not missing_qualification_matches
    rationale_parts = []
    if title_matches:
        rationale_parts.append(f"Nimike sopii hakijalle: {', '.join(title_matches[:4])}.")
    if keyword_matches:
        rationale_parts.append(f"Sisältö vastaa osaamista: {', '.join(keyword_matches[:5])}.")
    if application_title_matches or application_keyword_matches:
        application_matches = [*application_title_matches[:3], *application_keyword_matches[:4]]
        rationale_parts.append(
            f"Aiemmin kiinnostaviksi valitut tehtävät tukevat osumaa: {', '.join(application_matches[:5])}."
        )
    if hidden_opportunity:
        rationale_parts.append("Tehtävä voi olla ei-ilmeinen mutta siirrettävien taitojen perusteella kiinnostava osuma.")
    if location_matches:
        rationale_parts.append(f"Sijainti sopii: {', '.join(location_matches[:3])}.")
    elif application_location_matches:
        rationale_parts.append(f"Sijainti vastaa aiempaa hakuvalintaa: {', '.join(application_location_matches[:3])}.")
    if not rationale_parts:
        rationale_parts.append("Osuma perustuu heikkoihin tekstisignaaleihin.")

    return ScoreResult(
        passes=passes,
        machine_score=round(score, 2),
        vector_score=None,
        rationale=" ".join(rationale_parts),
        concerns=concerns,
        deterministic_result={
            "passes": passes,
            "title_matches": title_matches,
            "keyword_matches": keyword_matches,
            "application_history_title_matches": application_title_matches,
            "application_history_keyword_matches": application_keyword_matches,
            "application_history_location_matches": application_location_matches,
            "sector_matches": sector_matches,
            "location_matches": location_matches,
            "negative_matches": negative_matches,
            "qualification_caution_matches": caution_matches,
            "missing_qualification_matches": missing_qualification_matches,
            "candidate_lanes": candidate_lanes,
            "hidden_opportunity": hidden_opportunity,
        },
    )


def merge_semantic_scores(
    scored: list[tuple[JobForScoring, ScoreResult]],
    vector_scores: dict[int, float],
) -> list[tuple[JobForScoring, ScoreResult]]:
    merged: list[tuple[JobForScoring, ScoreResult]] = []
    for job, result in scored:
        vector_score = vector_scores.get(job.id)
        if vector_score is None:
            merged.append((job, result))
            continue
        deterministic_result = dict(result.deterministic_result)
        candidate_lanes = list(deterministic_result.get("candidate_lanes", []))
        if vector_score >= SEMANTIC_VECTOR_THRESHOLD and "semantic_similarity" not in candidate_lanes:
            candidate_lanes.append("semantic_similarity")
        hidden_opportunity = bool(deterministic_result.get("hidden_opportunity")) or (
            vector_score >= SEMANTIC_VECTOR_THRESHOLD and not deterministic_result.get("title_matches")
        )
        if hidden_opportunity and "exploration" not in candidate_lanes:
            candidate_lanes.append("exploration")
        semantic_score = 15 + max(0.0, min(1.0, vector_score)) * 20
        machine_score = max(result.machine_score, round(semantic_score, 2))
        passes = result.passes or (
            vector_score >= SEMANTIC_VECTOR_THRESHOLD
            and not deterministic_result.get("negative_matches")
        )
        deterministic_result.update(
            {
                "vector_score": round(vector_score, 4),
                "candidate_lanes": candidate_lanes,
                "hidden_opportunity": hidden_opportunity,
                "passes": passes,
            }
        )
        rationale = result.rationale
        if "semantic_similarity" in candidate_lanes and "Semanttinen samankaltaisuus" not in rationale:
            rationale = f"{rationale} Semanttinen samankaltaisuus nostaa tämän LLM-arvioon."
        merged.append(
            (
                job,
                replace(
                    result,
                    passes=passes,
                    machine_score=machine_score,
                    vector_score=round(vector_score, 4),
                    rationale=rationale,
                    deterministic_result=deterministic_result,
                ),
            )
        )
    return merged


def score_sort_key(item: tuple[JobForScoring, ScoreResult]) -> tuple[float, int]:
    job, result = item
    return (-result.machine_score, job.id)


def rank_scored_candidates_for_review(
    scored: list[tuple[JobForScoring, ScoreResult]],
) -> list[tuple[JobForScoring, ScoreResult]]:
    ranked_by_score = sorted(scored, key=score_sort_key)
    selected: list[tuple[JobForScoring, ScoreResult]] = []
    selected_ids: set[int] = set()

    for lane, quota in LANE_QUOTAS.items():
        lane_items = [
            item
            for item in ranked_by_score
            if item[0].id not in selected_ids
            and lane in item[1].deterministic_result.get("candidate_lanes", [])
        ]
        for item in lane_items[:quota]:
            selected.append(item)
            selected_ids.add(item[0].id)

    for item in ranked_by_score:
        if item[0].id not in selected_ids:
            selected.append(item)
            selected_ids.add(item[0].id)
    return selected


def run_deterministic_recommendations(
    connection: Connection,
    *,
    max_jobs: int = 500,
) -> dict[str, int]:
    settings = get_settings()
    profile_row = connection.execute(
        sa.text(
            """
            select id, profile
            from job_seeker_profiles
            order by id
            limit 1
            """
        )
    ).mappings().one_or_none()
    if profile_row is None:
        return {"profile_id": 0, "evaluated": 0, "recommended": 0}

    profile_id = int(profile_row["id"])
    profile = dict(profile_row["profile"])
    job_rows = list(
        connection.execute(
            sa.text(
                """
                select id, title, employer, description, location
                from jobs
                where status = 'active'
                  and exists (
                      select 1
                      from job_sources js
                      join sources s on s.id = js.source_id
                      where js.job_id = jobs.id
                        and s.enabled = true
                  )
                order by published_at desc nulls last, id desc
                limit :max_jobs
                """
            ),
            {"max_jobs": max_jobs},
        ).mappings()
    )

    evaluated = 0
    all_scored: list[tuple[JobForScoring, ScoreResult]] = []
    for row in job_rows:
        evaluated += 1
        job = JobForScoring(
            id=int(row["id"]),
            title=str(row["title"]),
            employer=row["employer"],
            description=row["description"],
            location=row["location"],
        )
        all_scored.append((job, score_job(profile, job)))

    provider = build_embedding_provider(settings)
    if provider is not None and all_scored:
        try:
            ensure_profile_embedding(
                connection,
                profile_id=profile_id,
                profile=profile,
                provider=provider,
            )
            embedded_count = ensure_job_embeddings(
                connection,
                jobs=[dict(row) for row in job_rows],
                provider=provider,
            )
            semantic_scores = semantic_vector_scores(
                connection,
                profile_id=profile_id,
                job_ids=[job.id for job, _result in all_scored],
                model=provider.model,
                dimension=provider.dimension,
                limit=min(len(all_scored), max(50, settings.llm_eval_max_jobs * 3)),
            )
            all_scored = merge_semantic_scores(all_scored, semantic_scores)
            logger.info(
                "event=semantic_matching_completed embedded_jobs=%s semantic_candidates=%s",
                embedded_count,
                len(semantic_scores),
            )
        except Exception:
            logger.exception("event=semantic_matching_failed")

    scored = [
        (job, result)
        for job, result in all_scored
        if result.passes
    ]

    scored = rank_scored_candidates_for_review(scored)
    transit_by_location = resolve_transit_for_locations(
        connection,
        [job.location for job, _result in scored],
        max_lookups=min(len(scored), settings.llm_eval_max_jobs),
    )
    connection.execute(
        sa.text(
            """
            update recommendations
            set is_active = false,
                rank = null
            where profile_id = :profile_id
            """
        ),
        {"profile_id": profile_id},
    )
    for rank, (job, result) in enumerate(scored, start=1):
        transit = transit_by_location.get(job.location)
        evidence = build_location_evidence(job.location, transit)
        concerns = apply_location_evidence_to_concerns(result.concerns, evidence)
        deterministic_result = dict(result.deterministic_result)
        if transit is not None:
            deterministic_result["transit_distance_km"] = transit.distance_km
            deterministic_result["transit_duration_text"] = transit.duration_text
            deterministic_result["transit_summary_text"] = transit.summary_text
        if evidence is not None:
            deterministic_result["location_evidence"] = {
                "text": evidence.text,
                "tone": evidence.tone,
            }
        connection.execute(
            sa.text(
                """
                insert into recommendations (
                    job_id,
                    profile_id,
                    deterministic_result,
                    machine_score,
                    vector_score,
                    rank,
                    rationale,
                    concerns,
                    is_active
                )
                values (
                    :job_id,
                    :profile_id,
                    CAST(:deterministic_result AS jsonb),
                    :machine_score,
                    :vector_score,
                    :rank,
                    :rationale,
                    CAST(:concerns AS jsonb),
                    true
                )
                on conflict (profile_id, job_id)
                do update set
                    deterministic_result = excluded.deterministic_result,
                    machine_score = excluded.machine_score,
                    vector_score = excluded.vector_score,
                    rank = case
                        when exists (
                            select 1
                            from recommendation_feedback rf
                            where rf.recommendation_id = recommendations.id
                              and rf.action = 'not_relevant'
                        )
                        then null
                        else excluded.rank
                    end,
                    rationale = excluded.rationale,
                    concerns = excluded.concerns,
                    is_active = not exists (
                        select 1
                        from recommendation_feedback rf
                        where rf.recommendation_id = recommendations.id
                          and rf.action = 'not_relevant'
                    )
                """
            ),
            {
                "job_id": job.id,
                "profile_id": profile_id,
                "deterministic_result": json.dumps(deterministic_result, ensure_ascii=False),
                "machine_score": result.machine_score,
                "vector_score": result.vector_score,
                "rank": rank,
                "rationale": result.rationale,
                "concerns": json.dumps(concerns, ensure_ascii=False),
            },
        )

    return {"profile_id": profile_id, "evaluated": evaluated, "recommended": len(scored)}


def run_llm_evaluations(
    connection: Connection,
    *,
    provider: EvaluationProvider,
    max_jobs: int,
) -> dict[str, int]:
    settings = get_settings()
    profile_row = connection.execute(
        sa.text(
            """
            select id, profile
            from job_seeker_profiles
            order by id
            limit 1
            """
        )
    ).mappings().one_or_none()
    if profile_row is None:
        return {"llm_evaluated": 0, "llm_failed": 0}

    profile_id = int(profile_row["id"])
    profile_summary = minimized_profile_summary(dict(profile_row["profile"]))
    rows = connection.execute(
        sa.text(
            """
            select
                r.id as recommendation_id,
                r.job_id,
                j.title,
                j.employer,
                j.description,
                j.location
            from recommendations r
            join jobs j on j.id = r.job_id
            left join llm_evaluations current_eval on current_eval.id = r.llm_evaluation_id
            where r.profile_id = :profile_id
              and r.is_active = true
              and j.status = 'active'
            order by
                case
                    when r.llm_evaluation_id is null then 0
                    when current_eval.prompt_version is distinct from :prompt_version then 1
                    else 2
                end,
                r.rank asc nulls last,
                r.machine_score desc,
                r.id desc
            limit :max_jobs
            """
        ),
        {"profile_id": profile_id, "max_jobs": max_jobs, "prompt_version": settings.llm_prompt_version},
    ).mappings()

    evaluated = 0
    failed = 0
    eval_model = configured_eval_model(settings, provider.provider_name)
    for row in rows:
        job_summary_text = job_summary(dict(row))
        request_hash = evaluation_request_hash(
            profile_summary=profile_summary,
            job_summary_text=job_summary_text,
            model=eval_model,
            prompt_version=settings.llm_prompt_version,
        )
        existing = connection.execute(
            sa.text(
                """
                select id, response
                from llm_evaluations
                where request_hash = :request_hash
                """
            ),
            {"request_hash": request_hash},
        ).mappings().one_or_none()
        try:
            if existing is None:
                evaluation, metadata = provider.evaluate_job_fit(
                    profile_summary=profile_summary,
                    job_summary=job_summary_text,
                    model=eval_model,
                    prompt_version=settings.llm_prompt_version,
                )
                response_payload = evaluation.model_dump()
                evaluation_id = int(
                    connection.execute(
                        sa.text(
                            """
                            insert into llm_evaluations (
                                job_id,
                                profile_id,
                                provider,
                                configured_model,
                                returned_model,
                                prompt_version,
                                schema_version,
                                request_hash,
                                response
                            )
                            values (
                                :job_id,
                                :profile_id,
                                :provider,
                                :configured_model,
                                :returned_model,
                                :prompt_version,
                                1,
                                :request_hash,
                                CAST(:response AS jsonb)
                            )
                            returning id
                            """
                        ),
                        {
                            "job_id": int(row["job_id"]),
                            "profile_id": profile_id,
                            "provider": provider.provider_name,
                            "configured_model": eval_model,
                            "returned_model": metadata.get("returned_model") or eval_model,
                            "prompt_version": settings.llm_prompt_version,
                            "request_hash": request_hash,
                            "response": json.dumps(response_payload, ensure_ascii=False),
                        },
                    ).scalar_one()
                )
            else:
                evaluation_id = int(existing["id"])
                response_payload = normalized_evaluation_payload(dict(existing["response"]))
            connection.execute(
                sa.text(
                    """
                    update recommendations
                    set llm_evaluation_id = :llm_evaluation_id,
                        llm_score = :llm_score,
                        fit_tier = :fit_tier,
                        suggested_action = :suggested_action,
                        rationale = :rationale,
                        concerns = CAST(:concerns AS jsonb)
                    where id = :recommendation_id
                    """
                ),
                {
                    "recommendation_id": int(row["recommendation_id"]),
                    "llm_evaluation_id": evaluation_id,
                    "llm_score": int(response_payload["score"]),
                    "fit_tier": response_payload["fit_tier"],
                    "suggested_action": response_payload["suggested_action"],
                    "rationale": response_payload["rationale"],
                    "concerns": json.dumps(response_payload["concerns"], ensure_ascii=False),
                },
            )
            evaluated += 1
        except Exception as exc:
            failed += 1
            logger.exception("event=llm_evaluation_failed job_id=%s", row["job_id"])
            if isinstance(exc, EvaluationProviderUnavailable):
                mark_provider_unavailable(settings, str(exc))
                break
    return {"llm_evaluated": evaluated, "llm_failed": failed}


def refresh_active_recommendation_ranks(
    connection: Connection,
    *,
    profile_id: int,
    require_llm_review: bool = False,
    prompt_version: int | None = None,
) -> int:
    connection.execute(
        sa.text(
            """
            update recommendations r
            set is_active = false,
                rank = null
            where r.profile_id = :profile_id
              and (
                  r.suggested_action = 'skip'
                  or r.llm_score < 40
                  or r.fit_tier in ('generic_customer_service_only', 'not_applicable')
                  or (:require_llm_review and r.llm_evaluation_id is null)
                  or (
                      :require_llm_review
                      and :prompt_version is not null
                      and exists (
                          select 1
                          from llm_evaluations e
                          where e.id = r.llm_evaluation_id
                            and e.prompt_version is distinct from :prompt_version
                      )
                  )
              )
            """
        ),
        {
            "profile_id": profile_id,
            "require_llm_review": require_llm_review,
            "prompt_version": prompt_version,
        },
    )
    connection.execute(
        sa.text(
            """
            with ranked as (
                select
                    id,
                    row_number() over (
                        order by
                            case suggested_action
                                when 'apply' then 0
                                when 'consider' then 1
                                else 2
                            end,
                            llm_score desc nulls last,
                            machine_score desc,
                            id desc
                    ) as new_rank
                from recommendations
                where profile_id = :profile_id
                  and is_active = true
            )
            update recommendations r
            set rank = ranked.new_rank
            from ranked
            where r.id = ranked.id
            """
        ),
        {"profile_id": profile_id},
    )
    return int(
        connection.execute(
            sa.text(
                """
                select count(*)
                from recommendations
                where profile_id = :profile_id
                  and is_active = true
                """
            ),
            {"profile_id": profile_id},
        ).scalar_one()
    )


def run_matching(max_jobs: int = 500) -> dict[str, int]:
    settings = get_settings()
    engine = get_engine()
    with engine.begin() as connection:
        result = run_deterministic_recommendations(connection, max_jobs=max_jobs)
        profile_id = int(result.get("profile_id") or 0)
        provider = build_evaluation_provider(settings)
        if provider is None:
            if profile_id:
                result["recommended"] = refresh_active_recommendation_ranks(
                    connection,
                    profile_id=profile_id,
                    require_llm_review=False,
                    prompt_version=settings.llm_prompt_version,
                )
            result.update({"llm_evaluated": 0, "llm_failed": 0})
            return result
        result.update(
            run_llm_evaluations(
                connection,
                provider=provider,
                max_jobs=settings.llm_eval_max_jobs,
            )
        )
        if profile_id:
            result["recommended"] = refresh_active_recommendation_ranks(
                connection,
                profile_id=profile_id,
                require_llm_review=True,
                prompt_version=settings.llm_prompt_version,
            )
        return result
