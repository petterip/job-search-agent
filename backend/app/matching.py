import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.config import get_settings
from app.freshness import (
    capture_as_of,
    evaluate_freshness,
    freshness_sql_clause,
    load_freshness_policy,
    policy_revision,
)
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
    learned_boosts,
    learned_exclusion_penalty,
)
from app.llm import (
    EvaluationProvider,
    EvaluationProviderUnavailable,
    build_evaluation_provider,
    configured_eval_model,
    evaluate_with_parse_retry,
    evaluation_request_hash,
    job_summary,
    mark_provider_unavailable,
    minimized_profile_summary,
    normalized_evaluation_payload,
    validated_evaluation_payload,
)
from app.languages import evaluate_language_requirements
from app.privacy import (
    PrivacyConfigurationError,
    collect_forbidden_values,
    outbound_llm_allowed,
    sanitize_learned_payload,
    sanitize_outbound,
)
from app.qualifications import evaluate_qualification_checks
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
DISCOVERY_POOL_EXCLUDED_TERMS = GENERIC_MATCH_TERMS | {
    "asiakaspalvelu",
    "hallinto",
    "johtaminen",
    "kunta",
    "kaupunki",
    "palvelu",
    "työ",
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
    "henkilökohtainen avustaja",
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
    published_at: Any = None
    first_seen_at: Any = None
    expires_at: Any = None


@dataclass(frozen=True)
class ScoreResult:
    passes: bool
    machine_score: float
    vector_score: float | None
    rationale: str
    concerns: list[str]
    deterministic_result: dict[str, Any]
    hard_eligible: bool = True
    hard_reasons: tuple[str, ...] = ()


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


def exclusion_phrases(profile: dict[str, Any]) -> list[str]:
    """Hard negative titles as whole phrases, not loose tokens.

    Splitting `"henkilökohtainen avustaja"` into tokens made any listing that
    merely mentions `avustaja` a hard rejection. Phrases are matched with word
    boundaries against the job title only.
    """
    phrases: list[str] = []
    exclusions = profile.get("exclusions", {})
    if isinstance(exclusions, dict):
        phrases.extend(
            str(title).strip()
            for title in exclusions.get("hard_negative_titles_fi", [])
            if str(title).strip()
        )
    phrases.extend(
        str(title).strip()
        for title in profile.get("hard_exclusions", [])
        if str(title).strip()
    )
    return phrases


def exclusion_terms(profile: dict[str, Any]) -> set[str]:
    """Normalized hard negative title phrases."""
    return {
        normalized
        for phrase in exclusion_phrases(profile)
        if (normalized := normalized_text(phrase))
    }


def negative_keyword_terms(profile: dict[str, Any]) -> set[str]:
    """Explicit negative keywords, which are substring matches by definition."""
    terms: set[str] = set()
    for keyword in profile.get("negative_keywords", []):
        terms.update(token for token in tokens(str(keyword)) if len(token) >= 4)
    return terms


def phrase_in_text(phrase: str, text: str | None) -> bool:
    normalized_phrase = normalized_text(phrase)
    if not normalized_phrase or not text:
        return False
    normalized_text_value = normalized_text(text)
    pattern = (
        r"(?<![0-9a-zåäö])"
        + re.escape(normalized_phrase)
        + r"(?![0-9a-zåäö])"
    )
    return re.search(pattern, normalized_text_value) is not None


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
    normalized = normalized_text(job_text)
    for term in COMPOUND_HARD_REJECT_TERMS:
        if phrase_in_text(term, normalized):
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
    negative_keyword_term_set = negative_keyword_terms(profile)
    caution_terms = qualification_caution_terms(profile)

    raw_job_text = " ".join(part or "" for part in (job.title, job.employer, job.description))
    language_gate = evaluate_language_requirements(
        profile, title=job.title, text=raw_job_text
    )
    qualification_gate = evaluate_qualification_checks(
        profile, title=job.title, text=raw_job_text
    )
    job_title_terms = expanded_tokens(job.title)
    job_text = normalized_text(raw_job_text)
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
    negative_matches = sorted(
        {
            phrase
            for phrase in negative_terms
            if phrase_in_text(phrase, job.title)
        }
        | {
            term
            for term in negative_keyword_term_set
            if term in job_text
        }
    )
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
    if qualification_gate.cautions:
        concerns.append("Kelpoisuus pitää tarkistaa ennen hakemista.")
    if language_gate.requires_review:
        concerns.append("Kielivaatimuksen taso pitää tarkistaa ennen hakemista.")
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
        and not language_gate.failed
        and not qualification_gate.hard_reject
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

    hard_reasons: list[str] = []
    if negative_matches:
        hard_reasons.append("negative_match")
    if missing_qualification_matches:
        hard_reasons.append("missing_qualification")
    if language_gate.failed:
        hard_reasons.append("language_requirement")
    hard_reasons.extend(qualification_gate.reasons)
    return ScoreResult(
        passes=passes,
        machine_score=round(score, 2),
        vector_score=None,
        rationale=" ".join(rationale_parts),
        concerns=concerns,
        hard_eligible=not hard_reasons,
        hard_reasons=tuple(hard_reasons),
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
            "language_gate": language_gate.to_audit_dict(),
            "qualification_gate": qualification_gate.to_audit_dict(),
        },
    )


def apply_hard_eligibility(
    scored: list[tuple[JobForScoring, ScoreResult]],
    *,
    profile: dict[str, Any],
    as_of: datetime,
) -> list[tuple[JobForScoring, ScoreResult]]:
    """Apply the profile freshness gate as immutable hard eligibility.

    The returned flag survives semantic promotion and travel scoring, so a high
    semantic score can never recover a rejected job.
    """
    policy = load_freshness_policy(profile)
    evaluation_as_of = capture_as_of(as_of)
    gated: list[tuple[JobForScoring, ScoreResult]] = []
    for job, result in scored:
        evaluation = evaluate_freshness(
            published_at=job.published_at,
            first_seen_at=job.first_seen_at,
            expires_at=job.expires_at,
            policy=policy,
            as_of=evaluation_as_of,
        )
        deterministic_result = dict(result.deterministic_result)
        deterministic_result["freshness"] = evaluation.to_audit_dict()
        deterministic_result["freshness_policy_revision"] = policy_revision(policy)
        reasons = result.hard_reasons
        if not evaluation.eligible and evaluation.reason_code not in reasons:
            reasons = (*reasons, evaluation.reason_code)
        hard_eligible = result.hard_eligible and evaluation.eligible
        gated.append(
            (
                job,
                replace(
                    result,
                    passes=result.passes and evaluation.eligible,
                    hard_eligible=hard_eligible,
                    hard_reasons=reasons,
                    deterministic_result=deterministic_result,
                ),
            )
        )
    return gated


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
        adjusted_vector = float(vector_score)
        if job.id in preference_scores:
            adjusted_vector += alpha * preference_scores[job.id]

        pre_anti_vector = max(0.0, min(1.0, adjusted_vector))
        semantic_exploration_candidate = (
            pre_anti_vector >= SEMANTIC_VECTOR_THRESHOLD
            and not deterministic_result.get("title_matches")
        )
        is_exploration = "exploration" in candidate_lanes or semantic_exploration_candidate
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
        passes = result.hard_eligible and (
            result.passes
            or (
                adjusted_vector >= SEMANTIC_VECTOR_THRESHOLD
                and not deterministic_result.get("negative_matches")
                and not deterministic_result.get("missing_qualification_matches")
            )
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
        work_mode_text = " ".join(
            part
            for part in (job.title, job.employer, job.description, job.location)
            if part
        )
        assessment = assess_travel(
            profile=profile,
            location=job.location,
            work_mode_text=work_mode_text,
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


def local_location_terms(profile: dict[str, Any]) -> list[str]:
    location = profile.get("location")
    if not isinstance(location, dict):
        return []

    terms: list[str] = []
    seen: set[str] = set()

    def add_term(value: Any) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        normalized = value.strip().casefold()
        if normalized not in seen:
            seen.add(normalized)
            terms.append(value.strip())

    add_term(location.get("home_city"))
    region_towns = location.get("region_towns")
    if isinstance(region_towns, list):
        for town in region_towns:
            add_term(town)
    return terms


def discovery_pool_terms(
    profile: dict[str, Any],
    configured_queries: list[str],
    *,
    learned_queries: list[str] | None = None,
    term_budget: int = 80,
) -> list[str]:
    """Bounded discovery terms as whole phrases, not loose tokens.

    Configured and application-history/learned title phrases keep their meaning;
    duplicated normalized phrases consume one slot. Terms dropped by the budget
    or the generic/exclusion filter are counted, never logged as private text.
    """
    candidates: list[str] = list(configured_queries)
    for cluster in profile.get("role_clusters", []):
        if isinstance(cluster, dict):
            candidates.extend(str(title) for title in cluster.get("titles_fi", []))
    preferences = profile.get("preferences", {})
    if isinstance(preferences, dict):
        signals = preferences.get("application_history_signals", {})
        if isinstance(signals, dict):
            candidates.extend(str(title) for title in signals.get("boost_titles_fi", []))
    candidates.extend(
        str(title) for title in learned_boosts(profile).get("boost_titles_fi", [])
    )
    if learned_queries:
        candidates.extend(str(query) for query in learned_queries)

    terms: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for candidate in candidates:
        normalized = str(candidate).strip().casefold()
        if (
            not normalized
            or normalized in DISCOVERY_POOL_EXCLUDED_TERMS
            or len(normalized) < 5
            or normalized in seen
        ):
            dropped += 1
            continue
        seen.add(normalized)
        terms.append(normalized)
    if len(terms) > term_budget:
        dropped += len(terms) - term_budget
        logger.warning(
            "event=discovery_pool_truncated kept=%s dropped=%s",
            term_budget,
            dropped,
        )
        terms = terms[:term_budget]
    return terms


def fetch_active_job_rows(
    connection: Connection,
    *,
    max_jobs: int,
    profile: dict[str, Any],
    local_max_jobs: int,
    remote_max_jobs: int,
    discovery_terms: list[str],
) -> tuple[list[dict[str, Any]], int, int, int, set[int]]:
    base_sql = """
        select id, title, employer, description, location, published_at, expires_at, created_at as first_seen_at
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
    recent_rows = [
        dict(row)
        for row in connection.execute(sa.text(base_sql), {"max_jobs": max_jobs}).mappings()
    ]

    rows_by_id: dict[int, dict[str, Any]] = {int(row["id"]): row for row in recent_rows}
    local_rows_added = 0
    terms = local_location_terms(profile)
    if terms and local_max_jobs > 0:
        params: dict[str, Any] = {"local_max_jobs": local_max_jobs}
        clauses: list[str] = []
        for index, term in enumerate(terms):
            key = f"local_location_{index}"
            params[key] = rf"(^|[[:space:],/]){re.escape(term)}([[:space:],/]|$)"
            clauses.append(f"coalesce(location, '') ~* :{key}")

        local_sql = f"""
            select id, title, employer, description, location, published_at, expires_at, created_at as first_seen_at
            from jobs
            where status = 'active'
              and exists (
                  select 1
                  from job_sources js
                  join sources s on s.id = js.source_id
                  where js.job_id = jobs.id
                    and s.enabled = true
              )
              and ({' or '.join(clauses)})
            order by published_at desc nulls last, id desc
            limit :local_max_jobs
            """
        for row in connection.execute(sa.text(local_sql), params).mappings():
            row_dict = dict(row)
            job_id = int(row_dict["id"])
            if job_id not in rows_by_id:
                local_rows_added += 1
                rows_by_id[job_id] = row_dict

    remote_rows_added = 0
    if remote_max_jobs > 0:
        remote_sql = """
            select id, title, employer, description, location, published_at, expires_at, created_at as first_seen_at
            from jobs
            where status = 'active'
              and exists (
                  select 1
                  from job_sources js
                  join sources s on s.id = js.source_id
                  where js.job_id = jobs.id
                    and s.enabled = true
              )
              and (
                  coalesce(location, '') ~* '(^|[[:space:],/])etä([[:space:],/]|$)'
                  or coalesce(location, '') ~* '(^|[[:space:],/])remote([[:space:],/]|$)'
                  or coalesce(title, '') ~* '(^|[^[:alpha:]])etätyö([^[:alpha:]]|$)'
                  or coalesce(title, '') ~* '(^|[^[:alpha:]])remote([^[:alpha:]]|$)'
                  or coalesce(description, '') ~* '(^|[^[:alpha:]])kokopäiväinen etätyö([^[:alpha:]]|$)'
                  or coalesce(description, '') ~* '(^|[^[:alpha:]])täysin etänä([^[:alpha:]]|$)'
                  or coalesce(description, '') ~* '(^|[^[:alpha:]])100 ?% remote([^[:alpha:]]|$)'
                  or coalesce(description, '') ~* '(^|[^[:alpha:]])fully remote([^[:alpha:]]|$)'
              )
              and coalesce(location, '') !~* 'hybridi|hybrid'
              and coalesce(title, '') !~* 'hybridi|hybrid|etätyömahdollisuus|mahdollisuus etä'
              and coalesce(description, '') !~* 'osittain etä|osittainen etä|hybridi|hybrid|etätyömahdollisuus|mahdollisuus etä'
            order by published_at desc nulls last, id desc
            limit :remote_max_jobs
            """
        for row in connection.execute(
            sa.text(remote_sql),
            {"remote_max_jobs": remote_max_jobs},
        ).mappings():
            row_dict = dict(row)
            job_id = int(row_dict["id"])
            if job_id not in rows_by_id:
                remote_rows_added += 1
                rows_by_id[job_id] = row_dict

    discovery_rows_added = 0
    discovery_job_ids: set[int] = set()
    if discovery_terms:
        params: dict[str, Any] = {"discovery_max_jobs": max(max_jobs * 3, max_jobs + 1000)}
        clauses: list[str] = []
        for index, term in enumerate(discovery_terms):
            key = f"discovery_term_{index}"
            params[key] = rf"(^|[^[:alpha:]]){re.escape(term)}([^[:alpha:]]|$)"
            clauses.append(
                f"(coalesce(title, '') || ' ' || coalesce(description, '')) ~* :{key}"
            )
        discovery_sql = f"""
            select id, title, employer, description, location, published_at, expires_at, created_at as first_seen_at
            from jobs
            where status = 'active'
              and exists (
                  select 1
                  from job_sources js
                  join sources s on s.id = js.source_id
                  where js.job_id = jobs.id
                    and s.enabled = true
              )
              and ({' or '.join(clauses)})
            order by published_at desc nulls last, id desc
            limit :discovery_max_jobs
            """
        for row in connection.execute(sa.text(discovery_sql), params).mappings():
            row_dict = dict(row)
            job_id = int(row_dict["id"])
            if job_id not in rows_by_id:
                discovery_rows_added += 1
                rows_by_id[job_id] = row_dict
                discovery_job_ids.add(job_id)

    return (
        list(rows_by_id.values()),
        local_rows_added,
        remote_rows_added,
        discovery_rows_added,
        discovery_job_ids,
    )


def run_deterministic_recommendations(
    connection: Connection,
    *,
    max_jobs: int = 500,
    activate_candidates: bool | None = None,
    deactivate_existing: bool | None = None,
    hosted_calls_allowed: bool = True,
    publication_mode: str = "hosted",
) -> dict[str, int]:
    settings = get_settings()
    # Only deliberate deterministic-only operation may publish unreviewed rows.
    deterministic_only = publication_mode == "deterministic_only"
    if activate_candidates is None:
        activate_candidates = deterministic_only
    if deactivate_existing is None:
        deactivate_existing = deterministic_only
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
    as_of = capture_as_of()
    learned_discovery_terms = [
        str(term)
        for term in (profile.get("learned") or {}).get("discovery_queries") or []
        if str(term).strip()
    ]
    discovery_terms = discovery_pool_terms(
        profile,
        settings.discovery_search_queries,
        learned_queries=learned_discovery_terms,
    )
    (
        job_rows,
        local_pool,
        remote_pool,
        discovery_pool,
        discovery_job_ids,
    ) = fetch_active_job_rows(
        connection,
        max_jobs=max_jobs,
        profile=profile,
        local_max_jobs=settings.matcher_local_max_jobs,
        remote_max_jobs=settings.matcher_remote_max_jobs,
        discovery_terms=discovery_terms,
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
            published_at=row.get("published_at"),
            first_seen_at=row.get("first_seen_at"),
            expires_at=row.get("expires_at"),
        )
        result = score_job(profile, job)
        if learned_discovery_terms and job.id in discovery_job_ids:
            job_text = " ".join(
                part or "" for part in (job.title, job.employer, job.description)
            )
            if any(
                phrase_in_text(term, job_text) for term in learned_discovery_terms
            ):
                deterministic_result = dict(result.deterministic_result)
                lanes = list(deterministic_result.get("candidate_lanes", []))
                if "learned_discovery" not in lanes:
                    lanes.append("learned_discovery")
                deterministic_result["candidate_lanes"] = lanes
                result = replace(result, deterministic_result=deterministic_result)
        all_scored.append((job, result))
    all_scored = apply_hard_eligibility(all_scored, profile=profile, as_of=as_of)

    provider = build_embedding_provider(settings) if hosted_calls_allowed else None
    if provider is not None and all_scored:
        try:
            ensure_profile_embedding(
                connection,
                profile_id=profile_id,
                profile=profile,
                provider=provider,
            )
            eligible_rows = [
                dict(row)
                for row, (_job, result) in zip(job_rows, all_scored, strict=True)
                if result.hard_eligible
            ]
            embedded_count = ensure_job_embeddings(
                connection,
                jobs=eligible_rows,
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

    if deactivate_existing:
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
        deterministic_result["deterministic_rationale"] = result.rationale
        deterministic_result["hard_eligible"] = result.hard_eligible
        deterministic_result["hard_reasons"] = list(result.hard_reasons)
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
                    :is_active
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
                              and (rf.rating <= 1 or rf.action = 'not_relevant')
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
                              and (rf.rating <= 1 or rf.action = 'not_relevant')
                        )
                        then null
                        else excluded.commutable_rank
                    end,
                    commutable_or_full_remote_rank = case
                        when exists (
                            select 1
                            from recommendation_feedback rf
                            where rf.recommendation_id = recommendations.id
                              and (rf.rating <= 1 or rf.action = 'not_relevant')
                        )
                        then null
                        else excluded.commutable_or_full_remote_rank
                    end,
                    nationwide_rank = case
                        when exists (
                            select 1
                            from recommendation_feedback rf
                            where rf.recommendation_id = recommendations.id
                              and (rf.rating <= 1 or rf.action = 'not_relevant')
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
                    rationale = case
                        when recommendations.llm_evaluation_id is not null
                        then recommendations.rationale
                        else excluded.rationale
                    end,
                    concerns = case
                        when recommendations.llm_evaluation_id is not null
                        then recommendations.concerns
                        else excluded.concerns
                    end,
                    is_active = case
                        when :is_active then not exists (
                            select 1
                            from recommendation_feedback rf
                            where rf.recommendation_id = recommendations.id
                              and (rf.rating <= 1 or rf.action = 'not_relevant')
                        )
                        else recommendations.is_active
                    end
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
                "is_active": activate_candidates,
            },
        )

    return {
        "profile_id": profile_id,
        "evaluated": evaluated,
        "candidate_window": len(job_rows),
        "local_pool": local_pool,
        "remote_pool": remote_pool,
        "discovery_pool": discovery_pool,
        "deterministic_passes": len(travel_scored),
        "commutable_candidates": len(commutable_rank_by_job_id),
        "commutable_or_full_remote_candidates": len(commutable_or_full_remote_rank_by_job_id),
        "recommended": len(scored),
    }


def llm_review_bucket_limits(max_jobs: int) -> dict[str, int]:
    if max_jobs <= 0:
        return {"commutable": 0, "remote": 0, "nationwide": 0}
    commutable = min(max_jobs, max(1, round(max_jobs * 0.4)))
    remaining = max_jobs - commutable
    remote = min(remaining, round(max_jobs * 0.2))
    nationwide = max_jobs - commutable - remote
    return {"commutable": commutable, "remote": remote, "nationwide": nationwide}


def commit_if_supported(connection: Connection) -> None:
    commit = getattr(connection, "commit", None)
    if callable(commit):
        commit()


def evaluation_request_context(profile: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """One sanitized request assembly for selection, evaluation and inventory.

    Selection, cache lookup, reconciliation and inventory must agree on the
    exact identity, so they all build the augmented profile summary and learned
    input through this function.
    """
    profile_secrets = collect_forbidden_values(profile)
    learned = profile.get("learned") or {}
    few_shot_examples = sanitize_learned_payload(
        learned.get("few_shot_examples") or [], extra_secrets=profile_secrets
    )
    eval_hints = [
        sanitize_learned_payload(hint, extra_secrets=profile_secrets)
        for hint in (learned.get("eval_hints") or [])
    ]
    summary = minimized_profile_summary(profile)
    if few_shot_examples:
        summary += "\n\nPalauteeseen perustuvat esimerkit:\n" + json.dumps(
            few_shot_examples, ensure_ascii=False
        )
    if eval_hints:
        summary += "\n\nArviointivihjeet:\n" + "\n".join(str(hint) for hint in eval_hints)
    summary = sanitize_outbound(summary, extra_secrets=profile_secrets)
    return summary, {"few_shot_examples": few_shot_examples, "eval_hints": eval_hints}


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
    augmented_profile_summary, learned_input = evaluation_request_context(profile)
    bucket_limits = llm_review_bucket_limits(max_jobs)
    # Over-select so current cache hits (which cost nothing) cannot consume the
    # limited paid-review slots and starve changed requests further down.
    scan_limits = llm_review_bucket_limits(max(max_jobs * 20, 20000))
    structure_predicate, structure_params = structural_eligibility_predicate(
        profile=profile, as_of=capture_as_of()
    )
    rows = list(connection.execute(
        sa.text(
            f"""
            with eligible as (
                select
                    r.id as recommendation_id,
                    r.job_id,
                    r.deterministic_result,
                    r.commutable,
                    r.commutable_or_full_remote,
                    r.commutable_rank,
                    r.commutable_or_full_remote_rank,
                    r.nationwide_rank,
                    r.machine_score,
                    j.title,
                    j.employer,
                    j.description,
                    j.location,
                    case
                        when r.llm_evaluation_id is null then 0
                        when current_eval.prompt_version is distinct from :prompt_version then 1
                        else 2
                    end as review_priority
                from recommendations r
                join jobs j on j.id = r.job_id
                left join llm_evaluations current_eval on current_eval.id = r.llm_evaluation_id
                where r.profile_id = :profile_id
                  and ({structure_predicate})
            ),
            commutable_candidates as (
                select *, 0 as bucket_order
                from eligible
                where commutable = true
                order by
                    review_priority,
                    commutable_rank asc nulls last,
                    nationwide_rank asc nulls last,
                    machine_score desc,
                    recommendation_id desc
                limit :commutable_llm_limit
            ),
            remote_candidates as (
                select *, 1 as bucket_order
                from eligible
                where commutable = false
                  and commutable_or_full_remote = true
                  and recommendation_id not in (
                      select recommendation_id from commutable_candidates
                  )
                order by
                    review_priority,
                    commutable_or_full_remote_rank asc nulls last,
                    nationwide_rank asc nulls last,
                    machine_score desc,
                    recommendation_id desc
                limit :remote_llm_limit
            ),
            nationwide_candidates as (
                select *, 2 as bucket_order
                from eligible
                where recommendation_id not in (
                    select recommendation_id from commutable_candidates
                    union
                    select recommendation_id from remote_candidates
                )
                order by
                    review_priority,
                    nationwide_rank asc nulls last,
                    machine_score desc,
                    recommendation_id desc
                limit :nationwide_llm_limit
            )
            select
                recommendation_id,
                job_id,
                deterministic_result,
                title,
                employer,
                description,
                location
            from (
                select * from commutable_candidates
                union all
                select * from remote_candidates
                union all
                select * from nationwide_candidates
            ) selected
            order by
                bucket_order,
                review_priority,
                coalesce(commutable_rank, commutable_or_full_remote_rank, nationwide_rank) asc nulls last,
                machine_score desc,
                recommendation_id desc
            """
        ),
        {
            "profile_id": profile_id,
            "commutable_llm_limit": scan_limits["commutable"],
            "remote_llm_limit": scan_limits["remote"],
            "nationwide_llm_limit": scan_limits["nationwide"],
            "prompt_version": settings.llm_prompt_version,
            **structure_params,
        },
    ).mappings())
    logger.info(
        "event=llm_evaluation_candidates_selected total=%s paid_budget=%s scan_commutable=%s scan_remote=%s scan_nationwide=%s prompt_version=%s",
        len(rows),
        max_jobs,
        scan_limits["commutable"],
        scan_limits["remote"],
        scan_limits["nationwide"],
        settings.llm_prompt_version,
    )
    commit_if_supported(connection)

    evaluated = 0
    failed = 0
    deferred = 0
    paid_calls = 0
    eval_model = configured_eval_model(settings, provider.provider_name)
    skipped = 0
    profile_revision = str(profile.get("base_revision") or "") or None
    for index, row in enumerate(rows, start=1):
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
            provider=provider.provider_name,
            prompt_version=settings.llm_prompt_version,
            job_id=int(row["job_id"]),
            profile_id=profile_id,
            learned_input=learned_input or None,
        )
        existing = connection.execute(
            sa.text(
                """
                select id, response
                from llm_evaluations
                where request_hash = :request_hash
                  and job_id = :job_id
                  and profile_id = :profile_id
                """
            ),
            {
                "request_hash": request_hash,
                "job_id": int(row["job_id"]),
                "profile_id": profile_id,
            },
        ).mappings().one_or_none()
        commit_if_supported(connection)
        cached_payload = (
            validated_evaluation_payload(existing["response"]) if existing is not None else None
        )
        if existing is not None and cached_payload is None:
            logger.warning(
                "event=llm_evaluation_cache_invalid job_id=%s evaluation_id=%s",
                row["job_id"],
                existing["id"],
            )
        if cached_payload is None and paid_calls >= max_jobs:
            deferred += 1
            logger.info(
                "event=llm_evaluation_deferred reason=paid_budget job_id=%s",
                row["job_id"],
            )
            continue
        if cached_payload is None and profile_revision is not None:
            current_revision = connection.execute(
                sa.text(
                    "select profile->>'base_revision' from job_seeker_profiles where id = :profile_id"
                ),
                {"profile_id": profile_id},
            ).scalar_one_or_none()
            if current_revision is not None and str(current_revision) != profile_revision:
                deferred += 1
                logger.warning(
                    "event=llm_evaluation_deferred reason=profile_revision_changed job_id=%s",
                    row["job_id"],
                )
                break
        try:
            if cached_payload is not None:
                evaluation_id = int(existing["id"])
                response_payload = cached_payload
            else:
                # Consume the paid budget before the call: a billable response
                # that fails parsing still counts as an attempt.
                paid_calls += 1
                started = time.monotonic()
                evaluation, metadata = evaluate_with_parse_retry(
                    provider,
                    profile_summary=augmented_profile_summary,
                    job_summary=job_summary_text,
                    model=eval_model,
                    prompt_version=settings.llm_prompt_version,
                    max_retries=settings.llm_eval_parse_retries,
                )
                latency_ms = int((time.monotonic() - started) * 1000)
                response_payload = evaluation.model_dump()
                accounting = metadata.get("usage_normalized") or {}
                current_row = connection.execute(
                    sa.text(
                        """
                        select r.deterministic_result, j.title, j.employer, j.description, j.location
                        from recommendations r
                        join jobs j on j.id = r.job_id
                        where r.id = :recommendation_id
                          and r.job_id = :job_id
                          and r.profile_id = :profile_id
                        """
                    ),
                    {
                        "recommendation_id": int(row["recommendation_id"]),
                        "job_id": int(row["job_id"]),
                        "profile_id": profile_id,
                    },
                ).mappings().one_or_none()
                current_hash = (
                    evaluation_request_hash(
                        profile_summary=augmented_profile_summary,
                        job_summary_text=job_summary(dict(current_row)),
                        model=eval_model,
                        provider=provider.provider_name,
                        prompt_version=settings.llm_prompt_version,
                        job_id=int(row["job_id"]),
                        profile_id=profile_id,
                        learned_input=learned_input or None,
                    )
                    if current_row is not None
                    else None
                )
                if current_hash != request_hash:
                    deferred += 1
                    logger.warning(
                        "event=llm_evaluation_deferred reason=input_changed_during_call job_id=%s",
                        row["job_id"],
                    )
                    continue
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
                                response,
                                input_tokens,
                                output_tokens,
                                cached_tokens,
                                latency_ms,
                                attempts,
                                outcome,
                                provider_request_id,
                                usage_present
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
                                CAST(:response AS jsonb),
                                :input_tokens,
                                :output_tokens,
                                :cached_tokens,
                                :latency_ms,
                                :attempts,
                                'succeeded',
                                :provider_request_id,
                                :usage_present
                            )
                            on conflict (request_hash)
                            do update set
                                job_id = excluded.job_id,
                                profile_id = excluded.profile_id,
                                provider = excluded.provider,
                                configured_model = excluded.configured_model,
                                returned_model = excluded.returned_model,
                                prompt_version = excluded.prompt_version,
                                schema_version = excluded.schema_version,
                                response = excluded.response,
                                input_tokens = excluded.input_tokens,
                                output_tokens = excluded.output_tokens,
                                cached_tokens = excluded.cached_tokens,
                                latency_ms = excluded.latency_ms,
                                attempts = excluded.attempts,
                                outcome = excluded.outcome,
                                provider_request_id = excluded.provider_request_id,
                                usage_present = excluded.usage_present,
                                created_at = now()
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
                            "input_tokens": accounting.get("input_tokens"),
                            "output_tokens": accounting.get("output_tokens"),
                            "cached_tokens": accounting.get("cached_tokens"),
                            "latency_ms": latency_ms,
                            "attempts": int(metadata.get("attempts") or 1),
                            "provider_request_id": metadata.get("request_id"),
                            "usage_present": bool(accounting.get("usage_present")),
                        },
                    ).scalar_one()
                )
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
                      and job_id = :job_id
                      and profile_id = :profile_id
                    """
                ),
                {
                    "recommendation_id": int(row["recommendation_id"]),
                    "job_id": int(row["job_id"]),
                    "profile_id": profile_id,
                    "llm_evaluation_id": evaluation_id,
                    "llm_score": int(response_payload["score"]),
                    "fit_tier": response_payload["fit_tier"],
                    "suggested_action": response_payload["suggested_action"],
                    "rationale": response_payload["rationale"],
                    "concerns": json.dumps(response_payload["concerns"], ensure_ascii=False),
                },
            )
            evaluated += 1
            commit_if_supported(connection)
            if evaluated == 1 or evaluated % 10 == 0 or index == len(rows):
                logger.info(
                    "event=llm_evaluation_progress evaluated=%s failed=%s skipped=%s total=%s job_id=%s",
                    evaluated,
                    failed,
                    skipped,
                    len(rows),
                    row["job_id"],
                )
        except Exception as exc:
            commit_if_supported(connection)
            failed += 1
            logger.exception("event=llm_evaluation_failed job_id=%s", row["job_id"])
            if isinstance(exc, EvaluationProviderUnavailable):
                mark_provider_unavailable(settings, str(exc))
                break
    logger.info(
        "event=llm_evaluation_completed evaluated=%s paid=%s failed=%s skipped=%s deferred=%s total=%s",
        evaluated,
        paid_calls,
        failed,
        skipped,
        deferred,
        len(rows),
    )
    return {
        "llm_evaluated": evaluated,
        "llm_paid_calls": paid_calls,
        "llm_failed": failed,
        "llm_skipped": skipped,
        "llm_deferred": deferred,
    }


def refresh_active_recommendation_ranks(
    connection: Connection,
    *,
    profile_id: int,
    require_llm_review: bool = False,
    prompt_version: int | None = None,
    deterministic_only: bool = False,
    extra_predicate: str = "true",
    extra_params: dict[str, Any] | None = None,
) -> int:
    """Recompute the valid publication set and its ranks.

    A row is published only when it passes the current LLM gates (in hosted
    mode), the structural eligibility predicate and freshness. Ranking never
    independently resurrects a row: a previous prompt-version mismatch is marked
    awaiting refresh instead of being silently re-activated.
    """
    invalid_predicate = f"""
        coalesce(r.suggested_action, '') = 'skip'
        or (r.llm_score is not null and r.llm_score < 40)
        or coalesce(r.fit_tier, '') in ('generic_customer_service_only', 'not_applicable')
        or coalesce(r.deterministic_result->>'hard_eligible', 'true') = 'false'
        or (
            coalesce(r.deterministic_result->'publication'->>'awaiting_refresh', 'false') = 'true'
            and coalesce(r.deterministic_result->'publication'->>'compatible', 'true') = 'false'
        )
        or (not :deterministic_only and r.llm_evaluation_id is null)
        or exists (
            select 1
            from recommendation_feedback rf
            where rf.recommendation_id = r.id
              and (rf.rating <= 1 or rf.action = 'not_relevant')
        )
        or not ({extra_predicate})
    """
    params: dict[str, Any] = {
        "profile_id": profile_id,
        "deterministic_only": deterministic_only,
        **(extra_params or {}),
    }
    connection.execute(
        sa.text(
            f"""
            update recommendations r
            set is_active = false,
                rank = null,
                commutable_rank = null,
                commutable_or_full_remote_rank = null,
                nationwide_rank = null
            where r.profile_id = :profile_id
              and ({invalid_predicate})
            """
        ),
        params,
    )
    connection.execute(
        sa.text(
            f"""
            update recommendations r
            set is_active = true
            where r.profile_id = :profile_id
              and not ({invalid_predicate})
            """
        ),
        params,
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
    if prompt_version is not None:
        connection.execute(
            sa.text(
                """
                update recommendations r
                set deterministic_result = jsonb_set(
                    coalesce(r.deterministic_result, '{}'::jsonb),
                    '{publication}',
                    coalesce(r.deterministic_result->'publication', '{}'::jsonb)
                      || jsonb_build_object(
                          'awaiting_refresh', true,
                          'refresh_reason', 'prompt_version_changed'
                      ),
                    true
                )
                where r.profile_id = :profile_id
                  and r.is_active = true
                  and r.llm_evaluation_id is not null
                  and exists (
                      select 1
                      from llm_evaluations e
                      where e.id = r.llm_evaluation_id
                        and e.prompt_version is distinct from :prompt_version
                  )
                """
            ),
            {"profile_id": profile_id, "prompt_version": prompt_version},
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


def load_active_profile(connection: Connection) -> dict[str, Any] | None:
    row = connection.execute(
        sa.text(
            """
            select profile
            from job_seeker_profiles
            order by id
            limit 1
            """
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    profile = row["profile"]
    if isinstance(profile, str):
        return json.loads(profile)
    return dict(profile) if isinstance(profile, dict) else None


def hosted_calls_allowed_for_profile(profile: dict[str, Any] | None) -> bool:
    """False when the profile privacy contract forbids or cannot authorize hosting."""
    if profile is None:
        return True
    try:
        return outbound_llm_allowed(profile)
    except PrivacyConfigurationError as exc:
        logger.error("event=privacy_configuration_invalid error=%s", exc)
        return False


def structural_eligibility_predicate(
    *,
    profile: dict[str, Any],
    as_of: datetime,
) -> tuple[str, dict[str, Any]]:
    """SQL predicate every published recommendation must satisfy right now.

    Distinguishes "not fetched" from "known ineligible": it is evaluated against
    existing rows regardless of the bounded retrieval window.
    """
    policy = load_freshness_policy(profile)
    clauses = [
        "coalesce(r.deterministic_result->>'hard_eligible', 'true') <> 'false'",
        """
        not (
            coalesce(r.deterministic_result->'publication'->>'awaiting_refresh', 'false') = 'true'
            and coalesce(r.deterministic_result->'publication'->>'compatible', 'true') = 'false'
        )
        """,
        """
        exists (
            select 1 from jobs j
            where j.id = r.job_id and j.status = 'active'
        )
        """,
        """
        exists (
            select 1
            from job_sources js
            join sources s on s.id = js.source_id
            where js.job_id = r.job_id and s.enabled = true
        )
        """,
        """
        not exists (
            select 1
            from recommendation_feedback rf
            where rf.recommendation_id = r.id
              and (rf.rating <= 1 or rf.action = 'not_relevant')
        )
        """,
    ]
    params: dict[str, Any] = {}
    if policy.max_age_days is not None or policy.drop_past_deadline:
        freshness_clause, freshness_params = freshness_sql_clause(
            policy,
            as_of=as_of,
            published_expression="coalesce(j.published_at, j.created_at)",
        )
        clauses.append(
            f"""
            exists (
                select 1 from jobs j
                where j.id = r.job_id and ({freshness_clause})
            )
            """
        )
        params.update(freshness_params)
    return " and ".join(f"({clause})" for clause in clauses), params


def reconcile_recommendation_publication(
    connection: Connection,
    *,
    profile_id: int,
    profile: dict[str, Any],
    settings: Any,
    as_of: datetime,
    publication_mode: str,
    provider_name: str | None = None,
    eval_model: str | None = None,
) -> dict[str, int]:
    """Re-evaluate every existing recommendation against current hard rules.

    Deactivates rows for inactive/disabled-only jobs, hidden feedback, failed
    freshness, or a deterministic hard rejection discovered outside the bounded
    retrieval window. Marks approvals whose linked evaluation identity no longer
    matches the configured provider/model/prompt as awaiting refresh.
    """
    predicate, predicate_params = structural_eligibility_predicate(
        profile=profile, as_of=as_of
    )
    deactivated = connection.execute(
        sa.text(
            f"""
            update recommendations r
            set is_active = false,
                rank = null,
                commutable_rank = null,
                commutable_or_full_remote_rank = null,
                nationwide_rank = null
            where r.profile_id = :profile_id
              and r.is_active = true
              and not ({predicate})
            """
        ),
        {"profile_id": profile_id, **predicate_params},
    ).rowcount or 0

    hard_rejected = 0
    identity_mode = bool(provider_name and eval_model)
    profile_summary, learned_input = (
        evaluation_request_context(profile) if identity_mode else ("", {})
    )
    if publication_mode in {"hosted", "unavailable"}:
        rows = list(
            connection.execute(
                sa.text(
                    """
                    select
                        r.id as recommendation_id,
                        r.deterministic_result,
                        r.llm_evaluation_id,
                        eval.request_hash as evaluation_request_hash,
                        eval.provider as evaluation_provider,
                        eval.configured_model as evaluation_configured_model,
                        eval.prompt_version as evaluation_prompt_version,
                        j.id as job_id,
                        j.title,
                        j.employer,
                        j.description,
                        j.location,
                        j.published_at,
                        j.expires_at,
                        j.created_at as first_seen_at
                    from recommendations r
                    join jobs j on j.id = r.job_id
                    left join llm_evaluations eval on eval.id = r.llm_evaluation_id
                    where r.profile_id = :profile_id
                    """
                ),
                {"profile_id": profile_id},
            ).mappings()
        )
        for row in rows:
            job = JobForScoring(
                id=int(row["job_id"]),
                title=str(row["title"]),
                employer=row["employer"],
                description=row["description"],
                location=row["location"],
                published_at=row["published_at"],
                first_seen_at=row["first_seen_at"],
                expires_at=row["expires_at"],
            )
            result = score_job(profile, job)
            if result.hard_eligible:
                if identity_mode and row["llm_evaluation_id"] is not None:
                    expected_identity = evaluation_request_hash(
                        profile_summary=profile_summary,
                        job_summary_text=job_summary(dict(row)),
                        model=eval_model or "",
                        provider=provider_name or "",
                        prompt_version=settings.llm_prompt_version,
                        job_id=int(row["job_id"]),
                        profile_id=profile_id,
                        learned_input=learned_input or None,
                    )
                    if row["evaluation_request_hash"] != expected_identity:
                        deterministic_result = dict(row["deterministic_result"] or {})
                        if isinstance(deterministic_result, str):
                            deterministic_result = json.loads(deterministic_result)
                        # Only a prompt/schema-only change is explicitly
                        # compatible; a provider, model, profile, learned or job
                        # content change must be re-reviewed before publishing.
                        compatible = (
                            row["evaluation_provider"] == provider_name
                            and row["evaluation_configured_model"] == eval_model
                        )
                        deterministic_result["publication"] = {
                            **(deterministic_result.get("publication") or {}),
                            "awaiting_refresh": True,
                            "compatible": compatible,
                            "refresh_reason": (
                                "prompt_version_changed"
                                if compatible
                                else "request_identity_changed"
                            ),
                        }
                        connection.execute(
                            sa.text(
                                """
                                update recommendations
                                set deterministic_result = CAST(:deterministic_result AS jsonb)
                                where id = :recommendation_id
                                """
                            ),
                            {
                                "recommendation_id": int(row["recommendation_id"]),
                                "deterministic_result": json.dumps(
                                    deterministic_result, ensure_ascii=False
                                ),
                            },
                        )
                continue
            reasons = ["deterministic_hard_rejection", *result.hard_reasons]
            deterministic_result = dict(row["deterministic_result"] or {})
            if isinstance(deterministic_result, str):
                deterministic_result = json.loads(deterministic_result)
            deterministic_result["hard_eligible"] = False
            deterministic_result["hard_reasons"] = reasons
            connection.execute(
                sa.text(
                    """
                    update recommendations
                    set is_active = false,
                        rank = null,
                        commutable_rank = null,
                        commutable_or_full_remote_rank = null,
                        nationwide_rank = null,
                        deterministic_result = CAST(:deterministic_result AS jsonb)
                    where id = :recommendation_id
                    """
                ),
                {
                    "recommendation_id": int(row["recommendation_id"]),
                    "deterministic_result": json.dumps(deterministic_result, ensure_ascii=False),
                },
            )
            hard_rejected += 1

    awaiting_refresh = 0
    return {
        "reconciled_deactivated": int(deactivated),
        "reconciled_hard_rejected": hard_rejected,
        "awaiting_refresh": int(awaiting_refresh),
    }


def resolve_publication_mode(settings: Any, *, hosted_allowed: bool, provider: Any) -> str:
    """Classify why hosted review is or is not available.

    - hosted: a provider is ready.
    - deterministic_only: deliberate (no provider configured, or privacy forbids
      hosted calls); unreviewed deterministic rows may be published.
    - unavailable: a provider was expected but is in cooldown or misconfigured.
      Unreviewed rows must not be published, and only compatible existing
      approvals stay visible.
    """
    if provider is not None:
        return "hosted"
    if not hosted_allowed or not str(getattr(settings, "llm_provider", "") or "").strip():
        return "deterministic_only"
    return "unavailable"


def inventory_review_backlog(
    connection: Connection,
    *,
    profile_id: int,
    prompt_version: int,
    sample_limit: int = 20,
    profile: dict[str, Any] | None = None,
    provider_name: str | None = None,
    eval_model: str | None = None,
) -> dict[str, Any]:
    """Dry-run inventory of eligible review requests and their expected identity.

    Eligibility is evaluated independently of the current publication flag: in
    hosted mode an unreviewed row is deliberately inactive, yet it is still the
    backlog. When the profile and provider are supplied, staleness is decided by
    the expected request identity (content hash), not by a version counter.
    """
    rows = list(
        connection.execute(
            sa.text(
                """
                select
                    r.id as recommendation_id,
                    r.job_id,
                    r.is_active,
                    r.commutable,
                    r.commutable_or_full_remote,
                    r.deterministic_result,
                    r.llm_evaluation_id,
                    e.request_hash as evaluation_request_hash,
                    e.prompt_version as evaluation_prompt_version,
                    j.title,
                    j.employer,
                    j.description,
                    j.location
                from recommendations r
                join jobs j on j.id = r.job_id
                left join llm_evaluations e on e.id = r.llm_evaluation_id
                where r.profile_id = :profile_id
                  and coalesce(r.deterministic_result->>'hard_eligible', 'true') <> 'false'
                  and j.status = 'active'
                  and exists (
                      select 1
                      from job_sources js
                      join sources s on s.id = js.source_id
                      where js.job_id = j.id
                        and s.enabled = true
                  )
                  and not exists (
                      select 1
                      from recommendation_feedback rf
                      where rf.recommendation_id = r.id
                        and (rf.rating <= 1 or rf.action = 'not_relevant')
                  )
                order by r.id
                """
            ),
            {"profile_id": profile_id},
        ).mappings()
    )
    identity_mode = bool(profile and provider_name and eval_model)
    profile_summary, learned_input = (
        evaluation_request_context(profile) if identity_mode and profile else ("", {})
    )
    by_scope: dict[str, int] = {"commutable": 0, "remote": 0, "nationwide": 0}
    by_state: dict[str, int] = {
        "current": 0,
        "no_evaluation": 0,
        "stale_identity": 0,
        "stale_prompt_version": 0,
    }
    by_publication = {"active": 0, "inactive": 0}
    stale_identities: list[str] = []
    stale_ids: list[int] = []
    for row in rows:
        scope = (
            "commutable"
            if row["commutable"]
            else "remote"
            if row["commutable_or_full_remote"]
            else "nationwide"
        )
        by_scope[scope] = by_scope.get(scope, 0) + 1
        by_publication["active" if row["is_active"] else "inactive"] += 1
        state: str
        expected_identity: str | None = None
        if identity_mode:
            deterministic_result = row["deterministic_result"] or {}
            if isinstance(deterministic_result, str):
                deterministic_result = json.loads(deterministic_result)
            expected_identity = evaluation_request_hash(
                profile_summary=profile_summary or "",
                job_summary_text=job_summary(dict(row)),
                model=eval_model or "",
                provider=provider_name or "",
                prompt_version=prompt_version,
                job_id=int(row["job_id"]),
                profile_id=profile_id,
                learned_input=learned_input or None,
            )
            if row["llm_evaluation_id"] is None:
                state = "no_evaluation"
            elif row["evaluation_request_hash"] == expected_identity:
                state = "current"
            else:
                state = "stale_identity"
        elif row["llm_evaluation_id"] is None:
            state = "no_evaluation"
        elif row["evaluation_prompt_version"] is not None and (
            row["evaluation_prompt_version"] != prompt_version
        ):
            state = "stale_prompt_version"
        else:
            state = "current"
        by_state[state] = by_state.get(state, 0) + 1
        if state != "current":
            stale_ids.append(int(row["recommendation_id"]))
            stale_identities.append(expected_identity or f"id:{row['recommendation_id']}")
    cohort_hash = hashlib.sha256(
        json.dumps(sorted(stale_identities), separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "eligible_total": len(rows),
        "by_scope": by_scope,
        "by_review_state": by_state,
        "by_publication": by_publication,
        "stale_total": len(stale_ids),
        "estimated_paid_calls": len(stale_ids),
        "cached_current": by_state.get("current", 0),
        "identity_mode": identity_mode,
        "cohort_hash": cohort_hash,
        "cohort_sample": stale_ids[: max(0, sample_limit)],
    }


def run_review_backfill(
    *,
    allow_paid: bool = False,
    max_calls: int = 0,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Inventory the stale review cohort; paid execution needs authorization.

    Paid recovery is deliberately gated: the plan requires an explicitly set
    aggregate budget and separate authorization, so this returns the frozen
    inventory and refuses to spend unless both are supplied.
    """
    settings = get_settings()
    engine = get_engine()
    with engine.connect() as connection:
        profile = load_active_profile(connection)
        if profile is None:
            return {"status": "skipped", "reason": "no_profile"}
        row = connection.execute(
            sa.text("select id from job_seeker_profiles order by id limit 1")
        ).mappings().one_or_none()
        profile_id = int(row["id"]) if row is not None else 0
        provider = build_evaluation_provider(settings)
        inventory = inventory_review_backlog(
            connection,
            profile_id=profile_id,
            prompt_version=settings.llm_prompt_version,
            profile=profile,
            provider_name=provider.provider_name if provider is not None else None,
            eval_model=(
                configured_eval_model(settings, provider.provider_name)
                if provider is not None
                else None
            ),
        )
    inventory["status"] = "dry_run" if dry_run else "ready"
    inventory["budget_max_calls"] = max_calls
    if dry_run:
        return inventory
    if not allow_paid or max_calls <= 0:
        inventory["status"] = "blocked"
        inventory["reason"] = "paid_backfill_requires_explicit_authorization_and_budget"
        return inventory
    inventory["status"] = "blocked"
    inventory["reason"] = "resumable_backfill_execution_not_yet_implemented"
    return inventory


def run_matching(max_jobs: int = 500) -> dict[str, int]:
    settings = get_settings()
    engine = get_engine()
    with engine.connect() as connection:
        profile = load_active_profile(connection)
    hosted_allowed = hosted_calls_allowed_for_profile(profile)
    provider = build_evaluation_provider(settings) if hosted_allowed else None
    mode = resolve_publication_mode(settings, hosted_allowed=hosted_allowed, provider=provider)
    as_of = capture_as_of()
    with engine.begin() as connection:
        result = run_deterministic_recommendations(
            connection,
            max_jobs=max_jobs,
            hosted_calls_allowed=hosted_allowed,
            publication_mode=mode,
        )
    profile_id = int(result.get("profile_id") or 0)
    result["publication_mode"] = mode
    structure_predicate = "true"
    structure_params: dict[str, Any] = {}
    if profile is not None:
        structure_predicate, structure_params = structural_eligibility_predicate(
            profile=profile, as_of=as_of
        )
    if profile_id and profile is not None:
        with engine.begin() as connection:
            result.update(
                reconcile_recommendation_publication(
                    connection,
                    profile_id=profile_id,
                    profile=profile,
                    settings=settings,
                    as_of=as_of,
                    publication_mode=mode,
                    provider_name=provider.provider_name if provider is not None else None,
                    eval_model=(
                        configured_eval_model(settings, provider.provider_name)
                        if provider is not None
                        else None
                    ),
                )
            )
    if provider is None:
        result.update({"llm_evaluated": 0, "llm_failed": 0})
        if profile_id:
            with engine.begin() as connection:
                result["recommended"] = refresh_active_recommendation_ranks(
                    connection,
                    profile_id=profile_id,
                    deterministic_only=mode == "deterministic_only",
                    prompt_version=settings.llm_prompt_version,
                    extra_predicate=structure_predicate,
                    extra_params=structure_params,
                )
        return result
    with engine.connect() as connection:
        result.update(
            run_llm_evaluations(
                connection,
                provider=provider,
                max_jobs=settings.llm_eval_max_jobs,
            )
        )
    if profile_id:
        with engine.begin() as connection:
            result["recommended"] = refresh_active_recommendation_ranks(
                connection,
                profile_id=profile_id,
                require_llm_review=True,
                prompt_version=settings.llm_prompt_version,
                extra_predicate=structure_predicate,
                extra_params=structure_params,
            )
    return result
