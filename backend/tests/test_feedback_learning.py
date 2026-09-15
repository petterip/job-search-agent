from datetime import datetime, timezone

from support import with_privacy
from app.feedback_learning import (
    decay_multiplier,
    effective_discovery_queries,
    learned_exclusion_penalty,
    recompute_learned_state,
    row_weight,
)


def test_row_weight_applied_replaces_rating_weight() -> None:
    assert row_weight(rating=5, applied=True) == 1.5
    assert row_weight(rating=5, applied=False) == 1.0


def test_decay_multiplier_is_slower_for_applied_rows() -> None:
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    applied_decay = decay_multiplier(created_at=created_at, applied=True, half_life_days=60)
    rating_decay = decay_multiplier(created_at=created_at, applied=False, half_life_days=60)
    assert applied_decay > rating_decay


def test_recompute_learned_state_promotes_boost_and_exclusion_terms() -> None:
    profile = with_privacy({"preferences": {}, "role_clusters": [], "learned": {"version": 0}})
    rows = [
        {
            "id": 1,
            "job_id": 10,
            "rating": 5,
            "applied": False,
            "comment": None,
            "analysis_status": "skipped",
            "created_at": datetime.now(timezone.utc),
            "analysis": None,
            "scoring_snapshot": {
                "job_id": 10,
                "title": "Hallintosihteeri",
                "employer": "Kaupunki",
                "location": "Oulu",
                "keyword_matches": ["kirjastopedagogi"],
                "title_matches": ["kirjastopedagogi"],
            },
        },
        {
            "id": 2,
            "job_id": 11,
            "rating": 5,
            "applied": False,
            "comment": None,
            "analysis_status": "skipped",
            "created_at": datetime.now(timezone.utc),
            "analysis": None,
            "scoring_snapshot": {
                "job_id": 11,
                "title": "Hallintovirkailija",
                "employer": "Kunta",
                "location": "Oulu",
                "keyword_matches": ["kirjastopedagogi"],
                "title_matches": ["kirjastopedagogi"],
            },
        },
        {
            "id": 3,
            "job_id": 12,
            "rating": 1,
            "applied": False,
            "comment": None,
            "analysis_status": "skipped",
            "created_at": datetime.now(timezone.utc),
            "analysis": None,
            "scoring_snapshot": {
                "job_id": 12,
                "title": "Myyntiedustaja",
                "employer": "Retail",
                "location": "Oulu",
                "keyword_matches": ["myynti"],
                "negative_matches": ["myynti"],
            },
        },
        {
            "id": 4,
            "job_id": 13,
            "rating": 1,
            "applied": False,
            "comment": None,
            "analysis_status": "skipped",
            "created_at": datetime.now(timezone.utc),
            "analysis": None,
            "scoring_snapshot": {
                "job_id": 13,
                "title": "Myyntipäällikkö",
                "employer": "Retail",
                "location": "Oulu",
                "keyword_matches": ["myynti"],
                "negative_matches": ["myynti"],
            },
        },
    ]
    filler = {
        "rating": 3,
        "applied": False,
        "comment": None,
        "analysis_status": "skipped",
        "created_at": datetime.now(timezone.utc),
        "analysis": None,
        "scoring_snapshot": {
            "title": "Projektisihteeri",
            "employer": "Yhdistys",
            "location": "Helsinki",
            "keyword_matches": ["projekti"],
        },
    }
    for index in range(6):
        rows.append(
            {
                "id": 10 + index,
                "job_id": 20 + index,
                **filler,
            }
        )
    from app.config import get_settings

    updated_profile, changes = recompute_learned_state(
        rows=rows,
        profile=profile,
        settings=get_settings(),
    )

    boosts = updated_profile["preferences"]["learned_boosts"]
    exclusions = updated_profile["learned"]["exclusions"]["terms_fi"]
    assert "kirjastopedagogi" in boosts["boost_keywords_fi"] or "kirjastopedagogi" in boosts["boost_titles_fi"]
    assert "myynti" in exclusions
    assert changes["learned_version"] == 1


def test_recompute_learned_state_preserves_term_first_seen() -> None:
    from app.config import get_settings

    profile = with_privacy({
        "preferences": {},
        "role_clusters": [],
        "learned": {
            "version": 3,
            "term_provenance": {
                "kirjastopedagogi": {
                    "net_weight": 0.5,
                    "jobs": [1],
                    "first_seen": "2026-01-01",
                }
            },
        },
    })
    rows = [
        {
            "id": 1,
            "job_id": 10,
            "rating": 5,
            "applied": False,
            "comment": None,
            "analysis_status": "skipped",
            "created_at": datetime(2026, 3, 4, tzinfo=timezone.utc),
            "analysis": None,
            "scoring_snapshot": {
                "job_id": 10,
                "title": "Hallintosihteeri",
                "employer": "Kaupunki",
                "location": "Oulu",
                "keyword_matches": ["kirjastopedagogi"],
            },
        },
        {
            "id": 2,
            "job_id": 11,
            "rating": 5,
            "applied": False,
            "comment": None,
            "analysis_status": "skipped",
            "created_at": datetime(2026, 4, 5, tzinfo=timezone.utc),
            "analysis": None,
            "scoring_snapshot": {
                "job_id": 11,
                "title": "Hallintovirkailija",
                "employer": "Kunta",
                "location": "Oulu",
                "keyword_matches": ["kirjastopedagogi"],
            },
        },
    ]
    for index in range(8):
        rows.append(
            {
                "id": 10 + index,
                "job_id": 20 + index,
                "rating": 3,
                "applied": False,
                "comment": None,
                "analysis_status": "skipped",
                "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
                "analysis": None,
                "scoring_snapshot": {
                    "job_id": 20 + index,
                    "title": "Projektisihteeri",
                    "employer": "Yhdistys",
                    "location": "Helsinki",
                    "keyword_matches": ["projekti"],
                },
            }
        )

    updated_profile, _changes = recompute_learned_state(
        rows=rows,
        profile=profile,
        settings=get_settings(),
    )

    provenance = updated_profile["learned"]["term_provenance"]["kirjastopedagogi"]
    assert provenance["first_seen"] == "2026-01-01"


def test_learned_exclusion_penalty_is_soft_not_hard_reject() -> None:
    profile = {
        "learned": {
            "exclusions": {"terms_fi": ["myynti"], "employers": [], "sectors": []},
        }
    }
    penalty, matches = learned_exclusion_penalty(
        job_title="Myyntiedustaja",
        job_employer="Retail",
        job_description="Myynti ja asiakaspalvelu",
        profile=profile,
    )

    assert penalty > 0
    assert "myynti" in matches


class FakeConnection:
    def execute(self, *_args, **_kwargs):
        class Result:
            def mappings(self):
                return self

            def one_or_none(self):
                return {
                    "profile": {
                        "learned": {
                            "discovery_queries": ["palveluassistentti"],
                        }
                    }
                }

        return Result()


def test_build_few_shot_examples_limits_positive_and_negative_counts() -> None:
    from app.feedback_learning import build_few_shot_examples

    rows = []
    for index in range(5):
        rows.append(
            {
                "rating": 5,
                "applied": False,
                "scoring_snapshot": {
                    "title": f"Positiivinen {index}",
                    "employer": f"Työnantaja {index}",
                },
            }
        )
    for index in range(4):
        rows.append(
            {
                "rating": 1,
                "applied": False,
                "scoring_snapshot": {
                    "title": f"Negatiivinen {index}",
                    "employer": f"Huono {index}",
                },
            }
        )

    examples = build_few_shot_examples(rows)

    assert len(examples) == 5
    assert sum(1 for item in examples if item["verdict"] != "skip") == 3
    assert sum(1 for item in examples if item["verdict"] == "skip") == 2
    employers = [str(item["employer"]).casefold() for item in examples]
    assert len(employers) == len(set(employers))


def test_evaluation_request_hash_uses_learned_content_not_version_counter() -> None:
    from app.llm import evaluation_request_hash

    base = {
        "profile_summary": "{}",
        "job_summary_text": '{"title":"Test"}',
        "model": "test",
        "prompt_version": 7,
    }
    with_content = evaluation_request_hash(learned_input={"eval_hints": ["a"]}, **base)
    without_content = evaluation_request_hash(learned_input=None, **base)
    same_content = evaluation_request_hash(learned_input={"eval_hints": ["a"]}, **base)

    assert with_content != without_content
    assert with_content == same_content


def test_effective_discovery_queries_merges_learned_terms_without_dropping_baseline() -> None:
    from app.config import Settings

    settings = Settings(
        discovery_search_queries=["kirjastonhoitaja", "kirjasto"],
        learned_discovery_query_cap=12,
    )
    queries = effective_discovery_queries(FakeConnection(), settings)  # type: ignore[arg-type]

    assert "kirjastonhoitaja" in queries
    assert "palveluassistentti" in queries


def _learner_rows() -> list[dict]:
    base = datetime(2026, 3, 4, tzinfo=timezone.utc)
    rows = []
    for index, rating in enumerate([5, 5, 5, 1, 1, 1]):
        rows.append(
            {
                "id": index + 1,
                "job_id": 10 + index,
                "rating": rating,
                "applied": False,
                "comment": None,
                "analysis_status": "skipped",
                "created_at": base,
                "analysis": None,
                "scoring_snapshot": {
                    "job_id": 10 + index,
                    "title": "Kirjastopedagogi" if rating >= 4 else "Myyntiedustaja",
                    "employer": f"Employer {index}",
                    "location": "Oulu",
                    "keyword_matches": ["kirjastopedagogi"] if rating >= 4 else ["myynti"],
                    "title_matches": ["kirjastopedagogi"] if rating >= 4 else ["myynti"],
                },
            }
        )
    return rows


def test_learner_version_is_stable_without_effective_change() -> None:
    from app.config import get_settings

    settings = get_settings()
    rows = _learner_rows()
    profile = with_privacy({"preferences": {}, "role_clusters": [], "learned": {}})

    first_profile, first_changes = recompute_learned_state(
        rows=rows, profile=profile, settings=settings
    )
    second_profile, second_changes = recompute_learned_state(
        rows=rows, profile=first_profile, settings=settings
    )

    assert first_changes["learned_version"] == 1
    assert second_changes["learned_version"] == 1
    assert second_changes["learned_state_changed"] is False
    assert second_profile["learned"]["updated_at"] == first_profile["learned"]["updated_at"]


def test_learner_version_bumps_when_effective_state_changes() -> None:
    from app.config import get_settings

    settings = get_settings()
    rows = _learner_rows()
    profile = with_privacy({"preferences": {}, "role_clusters": [], "learned": {}})
    first_profile, _changes = recompute_learned_state(rows=rows, profile=profile, settings=settings)

    changed = {
        **first_profile,
        "learned": {**first_profile["learned"], "eval_hints": ["uusi vihje"]},
    }
    second_profile, second_changes = recompute_learned_state(
        rows=rows, profile=changed, settings=settings
    )

    assert second_changes["learned_state_changed"] is True
    assert second_changes["learned_version"] == 2
    assert second_profile["learned"]["version"] == 2
