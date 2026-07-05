import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.feedback_analysis import snapshot_dict
from app.feedback_learning import learned_state


def run_feedback_benchmark(connection: Connection) -> dict[str, Any]:
    rows = connection.execute(
        sa.text(
            """
            select
                rf.rating,
                rf.applied,
                rf.scoring_snapshot,
                r.rank
            from recommendation_feedback rf
            left join recommendations r
              on r.id = rf.recommendation_id
            """
        )
    ).mappings()
    feedback_rows = [dict(row) for row in rows]
    positive_labels = 0
    negative_labels = 0
    positive_in_top_30 = 0
    exploration_positive = 0
    for row in feedback_rows:
        rating = int(row["rating"])
        applied = bool(row["applied"])
        rank = row["rank"]
        if rating >= 4 or applied:
            positive_labels += 1
            if rank is not None and int(rank) <= 30:
                positive_in_top_30 += 1
            snapshot = snapshot_dict(row["scoring_snapshot"])
            lanes = snapshot.get("candidate_lanes") or []
            if "exploration" in lanes:
                exploration_positive += 1
        elif rating <= 2:
            negative_labels += 1
    recall_at_30 = (
        round(positive_in_top_30 / positive_labels, 3) if positive_labels else None
    )
    exploration_hit_rate = (
        round(exploration_positive / positive_labels, 3) if positive_labels else None
    )
    return {
        "positive_labels": positive_labels,
        "negative_labels": negative_labels,
        "recall_at_30": recall_at_30,
        "exploration_hit_rate": exploration_hit_rate,
        "tuning_constants": {
            "semantic_alpha": 0.15,
            "semantic_beta": 0.20,
            "feedback_decay_half_life_days": 60,
            "boost_promotion_threshold": 1.5,
            "exclusion_enter_threshold": -2.0,
            "exclusion_exit_threshold": -1.0,
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
