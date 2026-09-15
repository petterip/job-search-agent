import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.feedback_analysis import snapshot_dict
from app.feedback_learning import (
    BOOST_PROMOTION_THRESHOLD,
    EXCLUSION_ENTER_THRESHOLD,
    EXCLUSION_EXIT_THRESHOLD,
    SEMANTIC_ALPHA,
    SEMANTIC_BETA,
    learned_state,
)


def run_feedback_benchmark(connection: Connection) -> dict[str, Any]:
    """Measured feedback benchmark on labelled, published recommendations.

    Denominators are named for what they actually are. `labelled_recall_at_30`
    is recall over user-labelled positives only, and
    `labelled_negative_share_at_30` is the labelled negative share of rated
    top-30 rows. Neither is population recall or a statistical FPR, and a biased
    feedback sample cannot estimate either for the whole catalogue.
    """
    settings = get_settings()
    rows = connection.execute(
        sa.text(
            """
            select
                rf.rating,
                rf.applied,
                rf.scoring_snapshot,
                coalesce(r.nationwide_rank, r.rank) as rank,
                r.deterministic_result
            from recommendation_feedback rf
            left join recommendations r
              on r.id = rf.recommendation_id
             and r.is_active = true
            """
        )
    ).mappings()
    feedback_rows = [dict(row) for row in rows]
    positive_labels = 0
    negative_labels = 0
    positive_in_top_30 = 0
    rated_in_top_30 = 0
    negative_in_top_30 = 0
    exploration_positive = 0
    learned_discovery_positive = 0
    feedback_prefilter_skips = 0
    for row in feedback_rows:
        rating = int(row["rating"])
        applied = bool(row["applied"])
        rank = row["rank"]
        if rank is not None and int(rank) <= 30:
            rated_in_top_30 += 1
        if rating >= 4 or applied:
            positive_labels += 1
            if rank is not None and int(rank) <= 30:
                positive_in_top_30 += 1
            snapshot = snapshot_dict(row["scoring_snapshot"])
            lanes = snapshot.get("candidate_lanes") or []
            if "exploration" in lanes:
                exploration_positive += 1
            if "learned_discovery" in lanes or "feedback_discovery" in lanes:
                learned_discovery_positive += 1
        elif rating <= 2:
            negative_labels += 1
            if rank is not None and int(rank) <= 30:
                negative_in_top_30 += 1
        deterministic_result = snapshot_dict(row.get("deterministic_result"))
        anti_similarity = deterministic_result.get("anti_preference_similarity")
        learned_exclusion_matches = deterministic_result.get("learned_exclusion_matches") or []
        if (
            anti_similarity is not None
            and float(anti_similarity) >= 0.75
            and learned_exclusion_matches
        ):
            feedback_prefilter_skips += 1
    labelled_recall_at_30 = (
        round(positive_in_top_30 / positive_labels, 3) if positive_labels else None
    )
    labelled_negative_share_at_30 = (
        round(negative_in_top_30 / rated_in_top_30, 3) if rated_in_top_30 else None
    )
    exploration_hit_rate = (
        round(exploration_positive / positive_labels, 3) if positive_labels else None
    )
    learned_discovery_contribution = (
        round(learned_discovery_positive / positive_labels, 3) if positive_labels else None
    )
    return {
        "positive_labels": positive_labels,
        "negative_labels": negative_labels,
        "rated_in_top_30": rated_in_top_30,
        "labelled_recall_at_30": labelled_recall_at_30,
        "labelled_negative_share_at_30": labelled_negative_share_at_30,
        "exploration_hit_rate": exploration_hit_rate,
        "learned_discovery_contribution": learned_discovery_contribution,
        "feedback_prefilter_skip_count": feedback_prefilter_skips,
        # Deprecated aliases retained until consumers migrate; the names above
        # state the real denominators.
        "recall_at_30": labelled_recall_at_30,
        "false_positive_rate_at_30": labelled_negative_share_at_30,
        "estimated_llm_call_savings": feedback_prefilter_skips,
        "metric_notes": {
            "labelled_recall_at_30": "user-labelled positive rows in the top 30",
            "labelled_negative_share_at_30": "labelled negatives / rated top-30 rows",
        },
        "tuning_constants": {
            "semantic_alpha": SEMANTIC_ALPHA,
            "semantic_beta": SEMANTIC_BETA,
            "feedback_decay_half_life_days": settings.feedback_decay_half_life_days,
            "boost_promotion_threshold": BOOST_PROMOTION_THRESHOLD,
            "exclusion_enter_threshold": EXCLUSION_ENTER_THRESHOLD,
            "exclusion_exit_threshold": EXCLUSION_EXIT_THRESHOLD,
        },
    }


def latest_learned_profile_summary(connection: Connection) -> dict[str, Any]:
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
        return {}
    profile = row["profile"]
    if isinstance(profile, str):
        profile = json.loads(profile)
    learned = learned_state(dict(profile))
    return {
        "learned_version": learned.get("version"),
        "term_provenance": learned.get("term_provenance", {}),
        "discovery_queries": learned.get("discovery_queries", []),
    }
