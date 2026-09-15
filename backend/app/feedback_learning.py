import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import Settings, get_settings
from app.db import get_engine
from app.llm import minimized_profile_summary

logger = logging.getLogger("matcher.feedback_learning")

RATING_WEIGHT = {1: -1.0, 2: -0.5, 3: 0.0, 4: 0.5, 5: 1.0}
APPLIED_WEIGHT = 1.5
BOOST_PROMOTION_THRESHOLD = 1.5
BOOST_MIN_JOBS = 2
EXCLUSION_ENTER_THRESHOLD = -2.0
EXCLUSION_EXIT_THRESHOLD = -1.0
EXCLUSION_MIN_JOBS = 2
DOCUMENT_FREQUENCY_CAP = 0.30
LEARNED_EXCLUSION_PENALTY_PER_MATCH = 8.0
CONFIDENCE_MULTIPLIERS = {"high": 1.0, "medium": 0.6, "low": 0.2}
SEMANTIC_ALPHA = 0.15
SEMANTIC_BETA = 0.20
MIN_CENTROID_SUPPORT = 3
THRESHOLD_EPSILON = 1e-6


def _generic_match_terms() -> set[str]:
    from app.matching import GENERIC_MATCH_TERMS

    return GENERIC_MATCH_TERMS


def _tokens(value: str) -> set[str]:
    from app.matching import tokens

    return tokens(value)


def _expanded_tokens(value: str) -> set[str]:
    from app.matching import expanded_tokens

    return expanded_tokens(value)


def _snapshot_dict(value: Any) -> dict[str, Any]:
    from app.feedback_analysis import snapshot_dict

    return snapshot_dict(value)


def _feedback_analysis_prompt_version() -> int:
    from app.feedback_analysis import FEEDBACK_ANALYSIS_PROMPT_VERSION

    return FEEDBACK_ANALYSIS_PROMPT_VERSION


def row_weight(*, rating: int, applied: bool) -> float:
    if applied:
        return APPLIED_WEIGHT
    return RATING_WEIGHT[rating]


def decay_multiplier(*, created_at: datetime, applied: bool, half_life_days: int) -> float:
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - created_at).total_seconds() / 86400.0)
    effective_age = age_days / 2.0 if applied else age_days
    return 0.5 ** (effective_age / max(half_life_days, 1))


def learned_state(profile: dict[str, Any]) -> dict[str, Any]:
    learned = profile.get("learned")
    if isinstance(learned, dict):
        return learned
    return {}


def learned_content_fingerprint(learned: dict[str, Any], preferences: dict[str, Any]) -> str:
    """Fingerprint the effective learned state only.

    `version`, `updated_at`, `source_feedback_count` and diagnostic provenance
    (net weights, first-seen dates) are deliberately excluded: they change on
    every run without changing what scoring or the prompt actually consume. A
    real threshold crossing still changes this fingerprint because the promoted
    or excluded term sets are included.
    """
    boosts = preferences.get("learned_boosts") if isinstance(preferences, dict) else {}
    payload = {
        "exclusions": learned.get("exclusions") or {},
        "discovery_queries": learned.get("discovery_queries") or [],
        "lane_quota_overrides": learned.get("lane_quota_overrides") or {},
        "few_shot_examples": learned.get("few_shot_examples") or [],
        "eval_hints": learned.get("eval_hints") or [],
        "learned_boosts": boosts or {},
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def learned_boosts(profile: dict[str, Any]) -> dict[str, list[str]]:
    preferences = profile.get("preferences", {})
    if not isinstance(preferences, dict):
        return {"boost_titles_fi": [], "boost_keywords_fi": [], "boost_locations": []}
    boosts = preferences.get("learned_boosts", {})
    if not isinstance(boosts, dict):
        return {"boost_titles_fi": [], "boost_keywords_fi": [], "boost_locations": []}
    return {
        "boost_titles_fi": list(boosts.get("boost_titles_fi") or []),
        "boost_keywords_fi": list(boosts.get("boost_keywords_fi") or []),
        "boost_locations": list(boosts.get("boost_locations") or []),
    }


def learned_exclusions(profile: dict[str, Any]) -> dict[str, list[str]]:
    exclusions = learned_state(profile).get("exclusions", {})
    if not isinstance(exclusions, dict):
        return {"terms_fi": [], "employers": [], "sectors": []}
    return {
        "terms_fi": list(exclusions.get("terms_fi") or []),
        "employers": list(exclusions.get("employers") or []),
        "sectors": list(exclusions.get("sectors") or []),
    }


def learned_boost_terms(profile: dict[str, Any], key: str) -> set[str]:
    boosts = learned_boosts(profile)
    terms: set[str] = set()
    for value in boosts.get(key, []):
        terms.update(_tokens(str(value)))
    return terms - _generic_match_terms()


def learned_exclusion_term_set(profile: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    for value in learned_exclusions(profile).get("terms_fi", []):
        terms.update(_tokens(str(value)))
    return {term for term in terms if len(term) >= 4}


def learned_exclusion_employers(profile: dict[str, Any]) -> set[str]:
    return {str(value).casefold() for value in learned_exclusions(profile).get("employers", []) if value}


def learned_exclusion_sectors(profile: dict[str, Any]) -> set[str]:
    return {str(value).casefold() for value in learned_exclusions(profile).get("sectors", []) if value}


def effective_lane_quotas(profile: dict[str, Any]) -> dict[str, int]:
    from app.matching import LANE_QUOTAS

    overrides = learned_state(profile).get("lane_quota_overrides", {})
    quotas = dict(LANE_QUOTAS)
    if isinstance(overrides, dict):
        for lane, value in overrides.items():
            if lane in quotas and isinstance(value, int):
                quotas[lane] = value
    quotas["exploration"] = max(1, quotas.get("exploration", 1))
    return quotas


def snapshot_terms(snapshot: dict[str, Any]) -> set[str]:
    collected: set[str] = set()
    for key in (
        "title",
        "employer",
        "location",
        "title_matches",
        "keyword_matches",
        "application_history_title_matches",
        "application_history_keyword_matches",
        "sector_matches",
        "negative_matches",
        "location_matches",
    ):
        value = snapshot.get(key)
        if isinstance(value, list):
            collected.update(_tokens(" ".join(str(item) for item in value)))
        else:
            collected.update(_tokens(str(value or "")))
    return collected - _generic_match_terms()


def load_feedback_rows(connection: Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        sa.text(
            """
            select
                rf.id,
                rf.job_id,
                rf.rating,
                rf.applied,
                rf.comment,
                rf.scoring_snapshot,
                rf.analysis_status,
                rf.created_at,
                fa.analysis
            from recommendation_feedback rf
            left join feedback_llm_analyses fa on fa.feedback_id = rf.id
            order by rf.id
            """
        )
    ).mappings()
    return [dict(row) for row in rows]


def load_profile_row(connection: Connection) -> dict[str, Any] | None:
    row = connection.execute(
        sa.text(
            """
            select id, profile
            from job_seeker_profiles
            order by id
            limit 1
            """
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    row_data = dict(row)
    profile = row_data["profile"]
    return {
        "profile_id": int(row_data.get("id") or 0),
        "profile": dict(profile) if isinstance(profile, dict) else json.loads(profile),
    }


def merge_analysis_terms(
    term_nets: dict[str, float],
    term_jobs: dict[str, set[int]],
    sector_nets: dict[str, float],
    sector_jobs: dict[str, set[int]],
    *,
    analysis: dict[str, Any] | None,
    snapshot: dict[str, Any],
    profile_summary: str,
    base_sign: float,
) -> None:
    if not analysis:
        return
    from app.feedback_analysis import FeedbackAnalysis, apply_grounding_guard

    try:
        parsed = FeedbackAnalysis.model_validate(analysis)
    except Exception:
        return
    grounded = apply_grounding_guard(parsed, snapshot=snapshot, profile_summary=profile_summary)
    multiplier = CONFIDENCE_MULTIPLIERS.get(grounded.confidence, 0.2)
    job_id = int(snapshot.get("job_id") or 0)
    for term in grounded.suggested_actions.boost_terms_fi:
        weight = abs(base_sign) * multiplier
        if base_sign < 0:
            continue
        term_nets[term] += weight
        if job_id:
            term_jobs[term].add(job_id)
    for term in grounded.suggested_actions.exclude_terms_fi:
        weight = -abs(base_sign) * multiplier
        if base_sign > 0:
            continue
        term_nets[term] += weight
        if job_id:
            term_jobs[term].add(job_id)
    for sector in grounded.suggested_actions.boost_sectors:
        sector_key = sector.casefold()
        weight = abs(base_sign) * multiplier
        if base_sign < 0:
            continue
        sector_nets[sector_key] += weight
        if job_id:
            sector_jobs[sector_key].add(job_id)
    for sector in grounded.suggested_actions.exclude_sectors:
        sector_key = sector.casefold()
        weight = -abs(base_sign) * multiplier
        if base_sign > 0:
            continue
        sector_nets[sector_key] += weight
        if job_id:
            sector_jobs[sector_key].add(job_id)


def _grounded_example_reason(row: dict[str, Any]) -> str:
    """Prefer the user-grounded analysis explanation over the system rationale.

    Reusing the system rationale for a user correction would teach the model the
    very reasoning the user disagreed with, so an ungrounded row omits `reason`.
    """
    analysis = row.get("analysis")
    if isinstance(analysis, str):
        try:
            analysis = json.loads(analysis)
        except ValueError:
            analysis = None
    if isinstance(analysis, dict):
        hypothesis = str(analysis.get("hypothesis_fi") or "").strip()
        if hypothesis:
            return hypothesis[:160]
    return ""


def build_few_shot_examples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positives: list[dict[str, Any]] = []
    negatives: list[dict[str, Any]] = []
    seen_employers: set[str] = set()
    for row in reversed(rows):
        snapshot = _snapshot_dict(row["scoring_snapshot"])
        employer = str(snapshot.get("employer") or "").casefold()
        rating = int(row["rating"])
        applied = bool(row["applied"])
        if (rating >= 5 or applied) and len(positives) < 3:
            if employer and employer in seen_employers:
                continue
            positives.append(
                {
                    "rating": rating,
                    "title": snapshot.get("title"),
                    "employer": snapshot.get("employer"),
                    "verdict": "apply" if applied or rating >= 5 else "consider",
                    "reason": _grounded_example_reason(row),
                }
            )
            if employer:
                seen_employers.add(employer)
        elif rating <= 2 and len(negatives) < 2:
            if employer and employer in seen_employers:
                continue
            negatives.append(
                {
                    "rating": rating,
                    "title": snapshot.get("title"),
                    "employer": snapshot.get("employer"),
                    "verdict": "skip",
                    "reason": _grounded_example_reason(row),
                }
            )
            if employer:
                seen_employers.add(employer)
    return positives + negatives


def collect_eval_hints(rows: list[dict[str, Any]]) -> list[str]:
    from app.feedback_analysis import FeedbackAnalysis

    hints: list[str] = []
    for row in reversed(rows):
        analysis = row.get("analysis")
        if not isinstance(analysis, dict):
            continue
        if row.get("analysis_status") != "completed":
            continue
        try:
            parsed = FeedbackAnalysis.model_validate(analysis)
        except Exception:
            continue
        if parsed.confidence not in {"high", "medium"}:
            continue
        hint = parsed.suggested_actions.llm_eval_hint.strip()
        if hint and hint not in hints:
            hints.append(hint[:240])
        if len(hints) >= 3:
            break
    return hints


def recompute_learned_state(
    *,
    rows: list[dict[str, Any]],
    profile: dict[str, Any],
    settings: Settings,
) -> tuple[dict[str, Any], dict[str, Any]]:
    profile_summary = minimized_profile_summary(profile)
    previous = learned_state(profile)
    previous_term_provenance = previous.get("term_provenance") or {}
    if not isinstance(previous_term_provenance, dict):
        previous_term_provenance = {}
    previous_exclusions = set(learned_exclusions(profile).get("terms_fi", []))
    term_nets: dict[str, float] = defaultdict(float)
    term_jobs: dict[str, set[int]] = defaultdict(set)
    employer_nets: dict[str, float] = defaultdict(float)
    employer_jobs: dict[str, set[int]] = defaultdict(set)
    sector_nets: dict[str, float] = defaultdict(float)
    sector_jobs: dict[str, set[int]] = defaultdict(set)
    discovery_scores: dict[str, float] = defaultdict(float)
    discovery_remove: set[str] = set()
    lane_overrides: dict[str, int] = {}
    alignment_counts: dict[str, int] = defaultdict(int)
    snapshot_count = max(len(rows), 1)

    for row in rows:
        snapshot = _snapshot_dict(row["scoring_snapshot"])
        job_id = int(row.get("job_id") or snapshot.get("job_id") or 0)
        rating = int(row["rating"])
        applied = bool(row["applied"])
        created_at = row["created_at"]
        if not isinstance(created_at, datetime):
            continue
        base = row_weight(rating=rating, applied=applied) * decay_multiplier(
            created_at=created_at,
            applied=applied,
            half_life_days=settings.feedback_decay_half_life_days,
        )
        if base == 0.0:
            continue
        for term in snapshot_terms(snapshot):
            term_nets[term] += base
            if job_id:
                term_jobs[term].add(job_id)
        employer = str(snapshot.get("employer") or "").strip()
        if employer:
            employer_nets[employer.casefold()] += base
            if job_id:
                employer_jobs[employer.casefold()].add(job_id)
        for sector in snapshot.get("sector_matches") or []:
            sector_nets[str(sector).casefold()] += base
            if job_id:
                sector_jobs[str(sector).casefold()].add(job_id)
        if rating >= 4 or applied:
            for term in snapshot_terms(snapshot):
                discovery_scores[term] += base
        elif rating <= 2:
            for term in snapshot_terms(snapshot):
                discovery_scores[term] -= abs(base)
        analysis = row.get("analysis")
        if isinstance(analysis, str):
            analysis = json.loads(analysis)
        merge_analysis_terms(
            term_nets,
            term_jobs,
            sector_nets,
            sector_jobs,
            analysis=analysis if isinstance(analysis, dict) else None,
            snapshot=snapshot,
            profile_summary=profile_summary,
            base_sign=base,
        )
        if isinstance(analysis, dict):
            from app.feedback_analysis import FeedbackAnalysis

            try:
                parsed = FeedbackAnalysis.model_validate(analysis)
            except Exception:
                parsed = None
            if parsed is not None and parsed.system_alignment == "over_ranked" and rating <= 2:
                lane_overrides["exploration"] = max(1, lane_overrides.get("exploration", 2))
            if parsed is not None:
                alignment_counts[parsed.system_alignment] += 1
                multiplier = CONFIDENCE_MULTIPLIERS.get(parsed.confidence, 0.2)
                if multiplier >= 0.6:
                    for query in parsed.suggested_actions.discovery_queries_add:
                        discovery_scores[query.casefold()] += multiplier
                    discovery_remove.update(
                        query.casefold() for query in parsed.suggested_actions.discovery_queries_remove
                    )

    frequent_terms = {
        term
        for term, jobs in term_jobs.items()
        if len(jobs) / snapshot_count > DOCUMENT_FREQUENCY_CAP
    }

    boost_titles: list[str] = []
    boost_keywords: list[str] = []
    boost_locations: list[str] = []
    exclusion_terms: list[str] = []
    exclusion_employers: list[str] = []
    exclusion_sectors: list[str] = []
    term_provenance: dict[str, Any] = {}

    for term, net in sorted(term_nets.items(), key=lambda item: item[1], reverse=True):
        if term in frequent_terms or term in _generic_match_terms():
            continue
        jobs = term_jobs[term]
        previous_first_seen = None
        previous_term = previous_term_provenance.get(term)
        if isinstance(previous_term, dict):
            previous_first_seen = previous_term.get("first_seen")
        first_seen = previous_first_seen or min(
            (
                row["created_at"].date().isoformat()
                for row in rows
                if int(row.get("job_id") or 0) in jobs
                and isinstance(row.get("created_at"), datetime)
            ),
            default=datetime.now(timezone.utc).date().isoformat(),
        )
        term_provenance[term] = {
            "net_weight": round(net, 3),
            "jobs": sorted(jobs),
            "first_seen": first_seen,
        }
        if net >= BOOST_PROMOTION_THRESHOLD - THRESHOLD_EPSILON and len(jobs) >= BOOST_MIN_JOBS:
            title_like = any(
                term in _expanded_tokens(str(_snapshot_dict(row["scoring_snapshot"]).get("title")))
                for row in rows
                if int(row.get("job_id") or 0) in jobs
            )
            location_like = any(
                term in _expanded_tokens(str(_snapshot_dict(row["scoring_snapshot"]).get("location")))
                for row in rows
                if int(row.get("job_id") or 0) in jobs
            )
            if title_like:
                boost_titles.append(term)
            elif location_like:
                boost_locations.append(term)
            else:
                boost_keywords.append(term)
        was_excluded = term in previous_exclusions
        if (
            not was_excluded
            and net <= EXCLUSION_ENTER_THRESHOLD + THRESHOLD_EPSILON
            and len(jobs) >= EXCLUSION_MIN_JOBS
        ) or (
            was_excluded and net <= EXCLUSION_EXIT_THRESHOLD + THRESHOLD_EPSILON
        ):
            exclusion_terms.append(term)

    for employer, net in employer_nets.items():
        if net <= EXCLUSION_ENTER_THRESHOLD + THRESHOLD_EPSILON and len(employer_jobs[employer]) >= EXCLUSION_MIN_JOBS:
            exclusion_employers.append(employer)
    for sector, net in sector_nets.items():
        if net <= EXCLUSION_ENTER_THRESHOLD + THRESHOLD_EPSILON and len(sector_jobs[sector]) >= EXCLUSION_MIN_JOBS:
            exclusion_sectors.append(sector)

    exclusion_cap = settings.learned_exclusion_cap
    exclusion_terms = sorted(
        exclusion_terms,
        key=lambda term: term_nets.get(term, 0.0),
    )[:exclusion_cap]

    baseline_queries = {query.casefold() for query in settings.discovery_search_queries}
    learned_discovery = [
        term
        for term, score in sorted(discovery_scores.items(), key=lambda item: item[1], reverse=True)
        if score > 0 and term not in baseline_queries and term not in discovery_remove
    ][: settings.learned_discovery_query_cap]

    new_learned = {
        "exclusions": {
            "terms_fi": exclusion_terms,
            "employers": exclusion_employers,
            "sectors": exclusion_sectors,
        },
        "term_provenance": term_provenance,
        "discovery_queries": learned_discovery,
        "lane_quota_overrides": lane_overrides,
        "few_shot_examples": build_few_shot_examples(rows),
        "eval_hints": collect_eval_hints(rows),
        "source_feedback_count": len(rows),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    preferences = dict(profile.get("preferences") or {})
    new_preferences = {
        "boost_titles_fi": boost_titles[:20],
        "boost_keywords_fi": boost_keywords[:30],
        "boost_locations": boost_locations[:10],
    }
    previous_fingerprint = learned_content_fingerprint(previous, preferences)
    effective_preferences = dict(preferences)
    effective_preferences["learned_boosts"] = new_preferences
    new_fingerprint = learned_content_fingerprint(new_learned, effective_preferences)
    state_changed = new_fingerprint != previous_fingerprint
    if state_changed or previous.get("version") is None:
        version = int(previous.get("version") or 0) + 1
    else:
        # Nothing effective changed: keep the previous version and timestamp so
        # repeated runs preserve evaluation request identity.
        version = int(previous.get("version") or 0)
        new_learned["updated_at"] = previous.get("updated_at") or new_learned["updated_at"]
    new_learned["version"] = version
    learned = new_learned
    preferences["learned_boosts"] = new_preferences
    profile["preferences"] = preferences
    profile["learned"] = learned

    previous_prov = set((previous.get("term_provenance") or {}).keys())
    promoted_boosts = [*boost_titles, *boost_keywords, *boost_locations]
    changes = {
        "boost_terms_added": [term for term in promoted_boosts if term not in previous_prov],
        "exclusion_terms": exclusion_terms,
        "discovery_queries": learned_discovery,
        "learned_version": version,
        "learned_state_changed": state_changed,
        "alignment_counts": dict(alignment_counts),
    }
    return profile, changes


def save_profile(
    connection: Connection,
    *,
    profile_id: int,
    profile: dict[str, Any],
    expected_base_revision: str | None = None,
) -> bool:
    """Write learner-owned state with an optimistic base-revision check.

    Returns False when an import changed the base profile while learning ran, so
    the caller can retry instead of restoring a stale user configuration.
    """
    result = connection.execute(
        sa.text(
            """
            update job_seeker_profiles
            set profile = CAST(:profile AS jsonb),
                updated_at = now()
            where id = :profile_id
              and (
                  CAST(:expected_base_revision AS text) is null
                  or coalesce(profile->>'base_revision', '') = :expected_base_revision
              )
            """
        ),
        {
            "profile_id": profile_id,
            "profile": json.dumps(profile, ensure_ascii=False),
            "expected_base_revision": expected_base_revision,
        },
    )
    return bool(getattr(result, "rowcount", 1))


def start_learning_run(connection: Connection) -> int:
    return int(
        connection.execute(
            sa.text(
                """
                insert into learning_runs (status)
                values ('running')
                returning id
                """
            )
        ).scalar_one()
    )


def finish_learning_run(
    connection: Connection,
    *,
    run_id: int,
    status: str,
    feedback_total: int,
    analysis_completed: int,
    learned_version: int | None,
    changes_applied: dict[str, Any],
    error_summary: str | None = None,
) -> None:
    settings = get_settings()
    connection.execute(
        sa.text(
            """
            update learning_runs
            set status = :status,
                finished_at = now(),
                feedback_total = :feedback_total,
                analysis_completed = :analysis_completed,
                learned_version = :learned_version,
                changes_applied = CAST(:changes_applied AS jsonb),
                feedback_analysis_prompt_version = :feedback_analysis_prompt_version,
                job_fit_prompt_version = :job_fit_prompt_version,
                error_summary = :error_summary
            where id = :run_id
            """
        ),
        {
            "run_id": run_id,
            "status": status,
            "feedback_total": feedback_total,
            "analysis_completed": analysis_completed,
            "learned_version": learned_version,
            "changes_applied": json.dumps(changes_applied, ensure_ascii=False),
            "feedback_analysis_prompt_version": _feedback_analysis_prompt_version(),
            "job_fit_prompt_version": settings.llm_prompt_version,
            "error_summary": error_summary,
        },
    )


def learn_from_feedback(connection: Connection | None = None) -> dict[str, Any]:
    settings = get_settings()
    if connection is None:
        engine = get_engine()
        with engine.begin() as conn:
            return _learn_from_feedback_impl(conn, settings)
    return _learn_from_feedback_impl(connection, settings)


def _learn_from_feedback_impl(connection: Connection, settings: Settings) -> dict[str, Any]:
    profile_row = load_profile_row(connection)
    if profile_row is None:
        return {"status": "skipped", "reason": "no_profile"}
    run_id = start_learning_run(connection)
    rows = load_feedback_rows(connection)
    analysis_completed = sum(1 for row in rows if row.get("analysis_status") == "completed")
    try:
        profile = None
        changes: dict[str, Any] = {}
        saved = False
        for _attempt in range(3):
            profile_row = load_profile_row(connection)
            if profile_row is None:
                return {"status": "skipped", "reason": "no_profile"}
            expected_revision = profile_row["profile"].get("base_revision")
            profile, changes = recompute_learned_state(
                rows=rows,
                profile=profile_row["profile"],
                settings=settings,
            )
            saved = save_profile(
                connection,
                profile_id=profile_row["profile_id"],
                profile=profile,
                expected_base_revision=(
                    str(expected_revision) if expected_revision is not None else None
                ),
            )
            if saved:
                break
            logger.warning(
                "event=learn_from_feedback_base_revision_changed run_id=%s", run_id
            )
        if not saved:
            finish_learning_run(
                connection,
                run_id=run_id,
                status="conflict",
                feedback_total=len(rows),
                analysis_completed=analysis_completed,
                learned_version=None,
                changes_applied={"reason": "profile_base_revision_changed"},
            )
            return {"status": "conflict", "run_id": run_id, "reason": "profile_changed"}
        assert profile is not None
        from app.embeddings import recompute_learned_preference_embeddings

        centroid_result = recompute_learned_preference_embeddings(
            connection,
            profile_id=profile_row["profile_id"],
            rows=rows,
            settings=settings,
        )
        changes["centroids"] = centroid_result
        from app.feedback_benchmark import run_feedback_benchmark

        changes["benchmark"] = run_feedback_benchmark(connection)
        finish_learning_run(
            connection,
            run_id=run_id,
            status="completed",
            feedback_total=len(rows),
            analysis_completed=analysis_completed,
            learned_version=int(profile.get("learned", {}).get("version") or 0),
            changes_applied=changes,
        )
        logger.info(
            "event=learn_from_feedback_completed run_id=%s feedback_total=%s learned_version=%s",
            run_id,
            len(rows),
            profile.get("learned", {}).get("version"),
        )
        return {
            "status": "completed",
            "run_id": run_id,
            "feedback_total": len(rows),
            "analysis_completed": analysis_completed,
            "learned_version": profile.get("learned", {}).get("version"),
            "changes": changes,
        }
    except Exception as exc:
        finish_learning_run(
            connection,
            run_id=run_id,
            status="failed",
            feedback_total=len(rows),
            analysis_completed=analysis_completed,
            learned_version=None,
            changes_applied={},
            error_summary=str(exc)[:1000],
        )
        logger.exception("event=learn_from_feedback_failed run_id=%s", run_id)
        raise


def effective_discovery_queries(connection: Connection, settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    profile_row = load_profile_row(connection)
    learned: list[str] = []
    if profile_row is not None:
        learned = list(learned_state(profile_row["profile"]).get("discovery_queries") or [])
    merged: list[str] = []
    seen: set[str] = set()
    for query in [*settings.discovery_search_queries, *learned]:
        key = query.casefold().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(query.strip())
    return merged


def get_discovery_search_queries(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    engine = get_engine()
    with engine.connect() as connection:
        return effective_discovery_queries(connection, settings)


def learned_exclusion_penalty(
    *,
    job_title: str | None,
    job_employer: str | None,
    job_description: str | None,
    profile: dict[str, Any],
) -> tuple[float, list[str]]:
    job_text = " ".join(part or "" for part in (job_title, job_employer, job_description))
    job_text_folded = job_text.casefold()
    job_text_terms = _expanded_tokens(job_text)
    employer_text = (job_employer or "").casefold()
    matches: list[str] = []
    for term in learned_exclusion_term_set(profile):
        folded = term.casefold()
        if folded in job_text_folded or folded in job_text_terms:
            matches.append(term)
    for employer in learned_exclusion_employers(profile):
        folded = employer.casefold()
        if folded and folded in employer_text:
            matches.append(employer)
    for sector in learned_exclusion_sectors(profile):
        if sector.casefold() in job_text_folded:
            matches.append(sector)
    penalty = len(matches) * LEARNED_EXCLUSION_PENALTY_PER_MATCH
    return penalty, sorted(set(matches))
