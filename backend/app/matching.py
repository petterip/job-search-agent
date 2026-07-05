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
    learned_centroid_similarity_scores,
    semantic_vector_scores,
)
from app.feedback_learning import (
    SEMANTIC_ALPHA,
    SEMANTIC_BETA,
    effective_lane_quotas,
    learned_boost_terms,
    learned_exclusion_penalty,
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
    LocationEvidence,
    apply_location_evidence_to_concerns,
    format_duration_fi,
    resolve_transit_for_queries,
)
from app.travel_policy import TravelAssessment, assess_travel, extract_destination_candidates

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
    learned_title_terms = learned_boost_terms(profile, "boost_titles_fi")
    learned_keyword_terms = learned_boost_terms(profile, "boost_keywords_fi")
    learned_location_terms = learned_boost_terms(profile, "boost_locations")
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
    learned_title_matches = matching_terms(learned_title_terms, job_title_terms)
    learned_keyword_matches = matching_terms(learned_keyword_terms, job_text_terms)
    learned_location_matches = matching_terms(learned_location_terms, job_location_terms)
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
    score += min(len(learned_title_matches), 4) * 18
    score += min(len(learned_keyword_matches), 6) * 5
    if location_matches:
        score += 15
    elif not job.location:
        score += 3
    if application_location_matches:
        score += 7
    if learned_location_matches:
        score += 7
    exclusion_penalty, learned_exclusion_matches = learned_exclusion_penalty(
        job_title=job.title,
        job_employer=job.employer,
        job_description=job.description,
        profile=profile,
    )
    if negative_matches or missing_qualification_matches:
        score -= 40

    unpenalized_machine_score = max(0.0, min(100.0, score))
    penalized_score = max(0.0, min(100.0, score - exclusion_penalty))
    score = penalized_score
    passes = (
        unpenalized_machine_score >= 15
        and not negative_matches
        and not missing_qualification_matches
    )

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
    if learned_title_matches or learned_keyword_matches:
        learned_matches = [*learned_title_matches[:3], *learned_keyword_matches[:4]]
        rationale_parts.append(
            f"Palautteen perusteella opitut signaalit tukevat osumaa: {', '.join(learned_matches[:5])}."
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
            "learned_title_matches": learned_title_matches,
            "learned_keyword_matches": learned_keyword_matches,
            "learned_location_matches": learned_location_matches,
            "learned_exclusion_matches": learned_exclusion_matches,
            "learned_exclusion_penalty": exclusion_penalty,
            "unpenalized_machine_score": round(unpenalized_machine_score, 2),
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
    *,
    preference_scores: dict[int, float] | None = None,
    anti_scores: dict[int, float] | None = None,
    alpha: float = SEMANTIC_ALPHA,
    beta: float = SEMANTIC_BETA,
) -> list[tuple[JobForScoring, ScoreResult]]:
    merged: list[tuple[JobForScoring, ScoreResult]] = []
    preference_scores = preference_scores or {}
    anti_scores = anti_scores or {}
    for job, result in scored:
        vector_score = vector_scores.get(job.id)
        if vector_score is None:
            merged.append((job, result))
            continue
        deterministic_result = dict(result.deterministic_result)
        candidate_lanes = list(deterministic_result.get("candidate_lanes", []))
        is_exploration = "exploration" in candidate_lanes
        adjusted_vector = float(vector_score)
        if job.id in preference_scores:
            adjusted_vector += alpha * preference_scores[job.id]
        if job.id in anti_scores and not is_exploration:
            adjusted_vector -= beta * anti_scores[job.id]
        adjusted_vector = max(0.0, min(1.0, adjusted_vector))
        if adjusted_vector >= SEMANTIC_VECTOR_THRESHOLD and "semantic_similarity" not in candidate_lanes:
            candidate_lanes.append("semantic_similarity")
        hidden_opportunity = bool(deterministic_result.get("hidden_opportunity")) or (
            adjusted_vector >= SEMANTIC_VECTOR_THRESHOLD and not deterministic_result.get("title_matches")
        )
        if hidden_opportunity and "exploration" not in candidate_lanes:
            candidate_lanes.append("exploration")
        semantic_score = 15 + max(0.0, min(1.0, adjusted_vector)) * 20
        machine_score = max(result.machine_score, round(semantic_score, 2))
        unpenalized = result.deterministic_result.get("unpenalized_machine_score", result.machine_score)
        unpenalized_machine_score = round(max(float(unpenalized), machine_score), 2)
        passes = result.passes or (
            adjusted_vector >= SEMANTIC_VECTOR_THRESHOLD
            and not deterministic_result.get("negative_matches")
        )
        deterministic_result.update(
            {
                "vector_score": round(adjusted_vector, 4),
                "base_vector_score": round(vector_score, 4),
                "preference_similarity": round(preference_scores.get(job.id, 0.0), 4)
                if job.id in preference_scores
                else None,
                "anti_preference_similarity": round(anti_scores[job.id], 4)
                if job.id in anti_scores
                else None,
                "unpenalized_machine_score": unpenalized_machine_score,
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
                    vector_score=round(adjusted_vector, 4),
                    rationale=rationale,
                    deterministic_result=deterministic_result,
                ),
            )
        )
    return merged


def score_sort_key(item: tuple[JobForScoring, ScoreResult], *, use_unpenalized: bool = False) -> tuple[float, int]:
    job, result = item
    if use_unpenalized:
        unpenalized = result.deterministic_result.get("unpenalized_machine_score")
        if unpenalized is not None:
            return (-float(unpenalized), job.id)
    return (-result.machine_score, job.id)


def rank_scored_candidates_for_review(
    scored: list[tuple[JobForScoring, ScoreResult]],
    *,
    profile: dict[str, Any] | None = None,
) -> list[tuple[JobForScoring, ScoreResult]]:
    ranked_by_score = sorted(scored, key=score_sort_key)
    selected: list[tuple[JobForScoring, ScoreResult]] = []
    selected_ids: set[int] = set()
    lane_quotas = effective_lane_quotas(profile or {})

    for lane, quota in lane_quotas.items():
        lane_items = [
            item
            for item in sorted(
                scored,
                key=lambda item: score_sort_key(item, use_unpenalized=lane == "exploration"),
            )
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


def apply_travel_to_scored_candidates(
    scored: list[tuple[JobForScoring, ScoreResult]],
    *,
    profile: dict[str, Any],
    connection: Connection,
) -> list[tuple[JobForScoring, ScoreResult, TravelAssessment]]:
    settings = get_settings()
    maps_available = settings.google_maps_configured()
    origin_address = settings.transit_origin_address
    commute_limit_minutes = settings.recommendation_commute_limit_minutes

    destination_queries: list[str] = []
    seen_queries: set[str] = set()
    for job, _result in scored:
        for query in extract_destination_candidates(job.location):
            if query not in seen_queries:
                seen_queries.add(query)
                destination_queries.append(query)

    transit_by_destination = resolve_transit_for_queries(
        connection,
        destination_queries,
        max_lookups=min(len(destination_queries), settings.recommendation_transit_lookup_budget),
    )

    travel_scored: list[tuple[JobForScoring, ScoreResult, TravelAssessment]] = []
    for job, result in scored:
        assessment = assess_travel(
            profile=profile,
            location=job.location,
            transit_by_destination=transit_by_destination,
            origin_address=origin_address,
            commute_limit_minutes=commute_limit_minutes,
            maps_available=maps_available,
        )
        deterministic_result = dict(result.deterministic_result)
        if "unpenalized_machine_score" not in deterministic_result:
            deterministic_result["unpenalized_machine_score"] = result.machine_score
        pre_travel_score = result.machine_score
        adjusted_score = max(0.0, min(100.0, pre_travel_score + assessment.score_adjustment))
        deterministic_result["travel_assessment"] = assessment.to_audit_dict()
        deterministic_result["location_evidence"] = {
            "text": assessment.evidence_text,
            "tone": assessment.tone,
        }
        if assessment.duration_seconds is not None:
            deterministic_result["transit_duration_text"] = format_duration_fi(
                assessment.duration_seconds
            )
        if assessment.distance_km is not None:
            deterministic_result["transit_distance_km"] = assessment.distance_km
        adjusted = replace(
            result,
            machine_score=round(adjusted_score, 2),
            deterministic_result=deterministic_result,
        )
        travel_scored.append((job, adjusted, assessment))
    return travel_scored


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
            preference_scores, anti_scores = learned_centroid_similarity_scores(
                connection,
                profile_id=profile_id,
                job_ids=[job.id for job, _result in all_scored],
                model=provider.model,
                dimension=provider.dimension,
            )
            all_scored = merge_semantic_scores(
                all_scored,
                semantic_scores,
                preference_scores=preference_scores,
                anti_scores=anti_scores,
            )
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

    travel_scored = apply_travel_to_scored_candidates(
        scored,
        profile=profile,
        connection=connection,
    )
    assessment_by_job_id = {job.id: assessment for job, _result, assessment in travel_scored}
    scored = rank_scored_candidates_for_review(
        [(job, result) for job, result, _assessment in travel_scored],
        profile=profile,
    )

    nationwide_rank_by_job_id: dict[int, int] = {}
    commutable_rank_by_job_id: dict[int, int] = {}
    commutable_or_full_remote_rank_by_job_id: dict[int, int] = {}
    commutable_order = 0
    commutable_or_full_remote_order = 0
    for nationwide_rank, (job, _result) in enumerate(scored, start=1):
        nationwide_rank_by_job_id[job.id] = nationwide_rank
        assessment = assessment_by_job_id[job.id]
        if assessment.commutable:
            commutable_order += 1
            commutable_rank_by_job_id[job.id] = commutable_order
        if assessment.commutable_or_full_remote:
            commutable_or_full_remote_order += 1
            commutable_or_full_remote_rank_by_job_id[job.id] = commutable_or_full_remote_order

    connection.execute(
        sa.text(
            """
            update recommendations
            set is_active = false,
                rank = null,
                commutable_rank = null,
                commutable_or_full_remote_rank = null,
                nationwide_rank = null
            where profile_id = :profile_id
            """
        ),
        {"profile_id": profile_id},
    )
    for job, result in scored:
        assessment = assessment_by_job_id[job.id]
        evidence = LocationEvidence(text=assessment.evidence_text, tone=assessment.tone)
        concerns = apply_location_evidence_to_concerns(result.concerns, evidence)
        deterministic_result = dict(result.deterministic_result)
        nationwide_rank = nationwide_rank_by_job_id[job.id]
        commutable_rank = commutable_rank_by_job_id.get(job.id)
        commutable_or_full_remote_rank = commutable_or_full_remote_rank_by_job_id.get(job.id)
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
                    commutable,
                    full_remote,
                    commutable_or_full_remote,
                    commutable_rank,
                    commutable_or_full_remote_rank,
                    nationwide_rank,
                    travel_status,
                    travel_reason_code,
                    travel_duration_seconds,
                    travel_distance_km,
                    travel_origin_address,
                    travel_commute_limit_minutes,
                    travel_routing_profile,
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
                    :commutable,
                    :full_remote,
                    :commutable_or_full_remote,
                    :commutable_rank,
                    :commutable_or_full_remote_rank,
                    :nationwide_rank,
                    :travel_status,
                    :travel_reason_code,
                    :travel_duration_seconds,
                    :travel_distance_km,
                    :travel_origin_address,
                    :travel_commute_limit_minutes,
                    :travel_routing_profile,
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
                              and (rf.rating = 1 or rf.action = 'not_relevant')
                        )
                        then null
                        else excluded.rank
                    end,
                    commutable = excluded.commutable,
                    full_remote = excluded.full_remote,
                    commutable_or_full_remote = excluded.commutable_or_full_remote,
                    commutable_rank = case
                        when exists (
                            select 1
                            from recommendation_feedback rf
                            where rf.recommendation_id = recommendations.id
                              and (rf.rating = 1 or rf.action = 'not_relevant')
                        )
                        then null
                        else excluded.commutable_rank
                    end,
                    commutable_or_full_remote_rank = case
                        when exists (
                            select 1
                            from recommendation_feedback rf
                            where rf.recommendation_id = recommendations.id
                              and (rf.rating = 1 or rf.action = 'not_relevant')
                        )
                        then null
                        else excluded.commutable_or_full_remote_rank
                    end,
                    nationwide_rank = case
                        when exists (
                            select 1
                            from recommendation_feedback rf
                            where rf.recommendation_id = recommendations.id
                              and (rf.rating = 1 or rf.action = 'not_relevant')
                        )
                        then null
                        else excluded.nationwide_rank
                    end,
                    travel_status = excluded.travel_status,
                    travel_reason_code = excluded.travel_reason_code,
                    travel_duration_seconds = excluded.travel_duration_seconds,
                    travel_distance_km = excluded.travel_distance_km,
                    travel_origin_address = excluded.travel_origin_address,
                    travel_commute_limit_minutes = excluded.travel_commute_limit_minutes,
                    travel_routing_profile = excluded.travel_routing_profile,
                    rationale = excluded.rationale,
                    concerns = excluded.concerns,
                    is_active = not exists (
                        select 1
                        from recommendation_feedback rf
                        where rf.recommendation_id = recommendations.id
                          and (rf.rating = 1 or rf.action = 'not_relevant')
                    )
                """
            ),
            {
                "job_id": job.id,
                "profile_id": profile_id,
                "deterministic_result": json.dumps(deterministic_result, ensure_ascii=False),
                "machine_score": result.machine_score,
                "vector_score": result.vector_score,
                "rank": nationwide_rank,
                "commutable": assessment.commutable,
                "full_remote": assessment.full_remote,
                "commutable_or_full_remote": assessment.commutable_or_full_remote,
                "commutable_rank": commutable_rank,
                "commutable_or_full_remote_rank": commutable_or_full_remote_rank,
                "nationwide_rank": nationwide_rank,
                "travel_status": assessment.status,
                "travel_reason_code": assessment.reason_code,
                "travel_duration_seconds": assessment.duration_seconds,
                "travel_distance_km": assessment.distance_km,
                "travel_origin_address": assessment.origin_address,
                "travel_commute_limit_minutes": assessment.commute_limit_minutes,
                "travel_routing_profile": assessment.routing_profile,
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
    profile = dict(profile_row["profile"])
    learned = profile.get("learned", {})
    if not isinstance(learned, dict):
        learned = {}
    learned_version = int(learned.get("version") or 0)
    profile_summary = minimized_profile_summary(profile)
    augmented_profile_summary = profile_summary
    few_shot_examples = learned.get("few_shot_examples") or []
    eval_hints = learned.get("eval_hints") or []
    if few_shot_examples:
        augmented_profile_summary += (
            "\n\nPalauteeseen perustuvat esimerkit:\n"
            + json.dumps(few_shot_examples, ensure_ascii=False)
        )
    if eval_hints:
        augmented_profile_summary += "\n\nArviointivihjeet:\n" + "\n".join(str(hint) for hint in eval_hints)
    rows = connection.execute(
        sa.text(
            """
            select
                r.id as recommendation_id,
                r.job_id,
                r.deterministic_result,
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
                case when r.commutable then 0 when r.commutable_or_full_remote then 1 else 2 end,
                r.commutable_rank asc nulls last,
                r.commutable_or_full_remote_rank asc nulls last,
                r.nationwide_rank asc nulls last,
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
    skipped = 0
    for row in rows:
        deterministic_result = row.get("deterministic_result") or {}
        if isinstance(deterministic_result, str):
            deterministic_result = json.loads(deterministic_result)
        anti_similarity = deterministic_result.get("anti_preference_similarity")
        learned_exclusion_matches = deterministic_result.get("learned_exclusion_matches") or []
        if (
            anti_similarity is not None
            and float(anti_similarity) >= 0.75
            and learned_exclusion_matches
        ):
            skipped += 1
            logger.info(
                "event=llm_evaluation_skipped reason=feedback_prefilter job_id=%s",
                row["job_id"],
            )
            continue
        job_summary_text = job_summary(dict(row))
        request_hash = evaluation_request_hash(
            profile_summary=augmented_profile_summary,
            job_summary_text=job_summary_text,
            model=eval_model,
            prompt_version=settings.llm_prompt_version,
            learned_version=learned_version,
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
                    profile_summary=augmented_profile_summary,
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
    return {"llm_evaluated": evaluated, "llm_failed": failed, "llm_skipped": skipped}


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
                rank = null,
                commutable_rank = null,
                commutable_or_full_remote_rank = null,
                nationwide_rank = null
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
                  or exists (
                      select 1
                      from recommendation_feedback rf
                      where rf.recommendation_id = r.id
                        and (rf.rating = 1 or rf.action = 'not_relevant')
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
            with nationwide as (
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
            set nationwide_rank = nationwide.new_rank,
                rank = nationwide.new_rank
            from nationwide
            where r.id = nationwide.id
            """
        ),
        {"profile_id": profile_id},
    )
    connection.execute(
        sa.text(
            """
            update recommendations
            set commutable_rank = null
            where profile_id = :profile_id
              and is_active = true
              and commutable = false
            """
        ),
        {"profile_id": profile_id},
    )
    connection.execute(
        sa.text(
            """
            update recommendations
            set commutable_or_full_remote_rank = null
            where profile_id = :profile_id
              and is_active = true
              and commutable_or_full_remote = false
            """
        ),
        {"profile_id": profile_id},
    )
    connection.execute(
        sa.text(
            """
            with commutable as (
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
                  and commutable = true
            )
            update recommendations r
            set commutable_rank = commutable.new_rank
            from commutable
            where r.id = commutable.id
            """
        ),
        {"profile_id": profile_id},
    )
    connection.execute(
        sa.text(
            """
            with commutable_or_full_remote as (
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
                  and commutable_or_full_remote = true
            )
            update recommendations r
            set commutable_or_full_remote_rank = commutable_or_full_remote.new_rank
            from commutable_or_full_remote
            where r.id = commutable_or_full_remote.id
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
