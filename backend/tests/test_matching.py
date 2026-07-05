from typing import Any

from app.config import Settings
from app.matching import (
    JobForScoring,
    apply_travel_to_scored_candidates,
    expanded_tokens,
    exclusion_terms,
    merge_semantic_scores,
    profile_terms,
    rank_scored_candidates_for_review,
    refresh_active_recommendation_ranks,
    run_llm_evaluations,
    run_deterministic_recommendations,
    score_job,
    tokens,
)


def test_tokens_handle_finnish_casefolding() -> None:
    assert tokens("Kirjasto- ja kulttuurityö OULU") == {"kirjasto", "ja", "kulttuurityö", "oulu"}


def test_expanded_tokens_handle_common_finnish_job_inflections() -> None:
    result = expanded_tokens("Hallinnon tehtävät, tapahtumien järjestämistä ja opastusta")

    assert "hallinto" in result
    assert "tapahtuma" in result
    assert "opastus" in result


def test_score_job_recommends_role_and_location_match() -> None:
    profile = {
        "location": {"home_city": "Oulu", "region_towns": ["Rovaniemi"]},
        "role_clusters": [
            {
                "titles_fi": ["kirjastonhoitaja"],
                "keywords_fi": ["kirjasto", "kokoelma", "asiakaspalvelu"],
            }
        ],
    }
    job = JobForScoring(
        id=1,
        title="Kirjastonhoitaja",
        employer="Oulun kaupunki",
        description="Kirjasto, kokoelma ja asiakaspalvelu.",
        location="Oulu",
    )

    result = score_job(profile, job)

    assert result.passes
    assert result.machine_score >= 45
    assert "kirjastonhoitaja" in result.deterministic_result["title_matches"]
    assert "oulu" in result.deterministic_result["location_matches"]


def test_score_job_boosts_application_history_signals() -> None:
    profile = {
        "location": {"home_city": "Oulu"},
        "role_clusters": [
            {
                "titles_fi": ["kirjastonhoitaja"],
                "keywords_fi": ["kirjasto"],
            }
        ],
        "preferences": {
            "application_history_signals": {
                "boost_titles_fi": ["palvelusihteeri"],
                "boost_keywords_fi": ["laskutus", "hallinto"],
                "boost_locations": ["Kokkola"],
            }
        },
    }
    job = JobForScoring(
        id=1,
        title="Palvelusihteeri",
        employer="Kokkolan kaupunki",
        description="Työssä painottuvat laskutus, hallinto ja asiakaspalvelu.",
        location="Kokkola",
    )

    result = score_job(profile, job)

    assert result.passes
    assert result.machine_score >= 30
    assert result.deterministic_result["application_history_title_matches"] == ["palvelusihteeri"]
    assert result.deterministic_result["application_history_keyword_matches"] == ["hallinto", "laskutus"]
    assert result.deterministic_result["application_history_location_matches"] == ["kokkola"]
    assert "Aiemmin kiinnostaviksi valitut tehtävät" in result.rationale


def test_score_job_marks_hidden_transferable_opportunity() -> None:
    profile = {
        "location": {"home_city": "Oulu"},
        "role_clusters": [
            {
                "titles_fi": ["kirjastonhoitaja"],
                "keywords_fi": ["tapahtumat", "opastus", "neuvonta"],
            }
        ],
        "preferences": {"sectors_preferred": ["museo", "kunta"]},
    }
    job = JobForScoring(
        id=1,
        title="Yleisötyön koordinaattori",
        employer="Kaupungin museo",
        description="Tehtävässä järjestetään tapahtumat, opastus ja neuvonta asiakkaille.",
        location="Oulu",
    )

    result = score_job(profile, job)

    assert result.passes
    assert result.deterministic_result["hidden_opportunity"] is True
    assert "transferable_duty" in result.deterministic_result["candidate_lanes"]
    assert "sector_context" in result.deterministic_result["candidate_lanes"]


def test_score_job_promotes_music_literature_and_publication_strengths() -> None:
    profile = {
        "location": {"home_city": "Oulu"},
        "role_clusters": [
            {
                "titles_fi": ["musiikkituottaja", "sisällöntuottaja"],
                "keywords_fi": ["musiikki", "viulu", "julkaisu", "kirjoittaminen", "kirjallisuus"],
            }
        ],
        "preferences": {"sectors_preferred": ["kulttuuri"]},
    }
    job = JobForScoring(
        id=1,
        title="Kulttuurisisältöjen koordinaattori",
        employer="Kulttuurikeskus",
        description="Tehtävässä suunnitellaan musiikki- ja kirjallisuustapahtumia sekä kirjoitetaan julkaisuja.",
        location="Oulu",
    )

    result = score_job(profile, job)

    assert result.passes
    assert result.deterministic_result["hidden_opportunity"] is True
    assert {"julkaisu", "kirjallisuus", "kirjoittaminen", "musiikki"} <= set(
        result.deterministic_result["keyword_matches"]
    )
    assert "transferable_duty" in result.deterministic_result["candidate_lanes"]


def test_score_job_finds_hidden_opportunity_from_inflected_terms() -> None:
    profile = {
        "location": {"home_city": "Oulu"},
        "role_clusters": [
            {
                "titles_fi": ["kirjastonhoitaja"],
                "keywords_fi": ["tapahtuma", "opastus", "neuvonta", "hallinto"],
            }
        ],
        "preferences": {"sectors_preferred": ["museo"]},
    }
    job = JobForScoring(
        id=1,
        title="Yleisötyön suunnittelija",
        employer="Museopalvelut",
        description="Työ sisältää hallinnon tukea, tapahtumien järjestämistä, opastusta ja neuvonnan kehittämistä.",
        location="Oulu",
    )

    result = score_job(profile, job)

    assert result.passes
    assert result.deterministic_result["hidden_opportunity"] is True
    assert {"hallinto", "neuvonta", "opastus", "tapahtuma"} <= set(result.deterministic_result["keyword_matches"])


def test_candidate_ranking_reserves_room_for_hidden_lanes() -> None:
    direct_profile = {
        "location": {"home_city": "Oulu"},
        "role_clusters": [{"titles_fi": ["kirjastonhoitaja"], "keywords_fi": ["kirjasto"]}],
    }
    hidden_profile = {
        "location": {"home_city": "Oulu"},
        "role_clusters": [{"titles_fi": ["kirjastonhoitaja"], "keywords_fi": ["tapahtumat", "opastus"]}],
        "preferences": {
            "application_history_signals": {
                "boost_titles_fi": ["palveluassistentti"],
                "boost_keywords_fi": ["museo", "opastus"],
            }
        },
    }
    direct_jobs = [
        (
            JobForScoring(
                id=i,
                title="Kirjastonhoitaja",
                employer="Oulun kaupunki",
                description="Kirjasto ja asiakaspalvelu.",
                location="Oulu",
            ),
            score_job(direct_profile, JobForScoring(
                id=i,
                title="Kirjastonhoitaja",
                employer="Oulun kaupunki",
                description="Kirjasto ja asiakaspalvelu.",
                location="Oulu",
            )),
        )
        for i in range(1, 16)
    ]
    hidden_job = JobForScoring(
        id=99,
        title="Palveluassistentti",
        employer="Kaupungin museo",
        description="Museon asiakaspalvelu, opastus ja tapahtumat.",
        location="Oulu",
    )
    ranked = rank_scored_candidates_for_review(
        [
            *direct_jobs,
            (hidden_job, score_job(hidden_profile, hidden_job)),
        ]
    )

    ranked_ids = [job.id for job, _result in ranked]
    assert ranked_ids.index(99) < 15


def test_semantic_scores_promote_non_obvious_candidates() -> None:
    profile = {
        "location": {"home_city": "Oulu"},
        "role_clusters": [{"titles_fi": ["kirjastonhoitaja"], "keywords_fi": ["kirjasto"]}],
    }
    job = JobForScoring(
        id=1,
        title="Yhteisökoordinaattori",
        employer="Kulttuuritalo",
        description="Yleisötyö, ryhmät ja tapahtumat.",
        location=None,
    )
    result = score_job(profile, job)
    assert not result.passes

    merged = merge_semantic_scores([(job, result)], {1: 0.84})
    merged_result = merged[0][1]

    assert merged_result.passes
    assert merged_result.vector_score == 0.84
    assert "semantic_similarity" in merged_result.deterministic_result["candidate_lanes"]
    assert merged_result.deterministic_result["hidden_opportunity"] is True


def test_profile_terms_remove_generic_title_words() -> None:
    profile = {"role_clusters": [{"titles_fi": ["vastaava työnjohtaja", "ohjaaja"]}]}

    assert "vastaava" not in profile_terms(profile, "titles_fi")
    assert "ohjaaja" in profile_terms(profile, "titles_fi")


def test_exclusion_terms_only_use_hard_negative_titles() -> None:
    profile = {
        "exclusions": {
            "reject_if_required_qualification_missing": ["C- tai CE-ajokortti"],
            "hard_negative_titles_fi": ["lähihoitaja"],
        }
    }

    assert exclusion_terms(profile) == {"lähihoitaja"}


def test_score_job_rejects_negative_terms() -> None:
    profile = {
        "negative_keywords": ["myynti"],
        "role_clusters": [{"titles_fi": ["asiakaspalvelija"], "keywords_fi": ["asiakaspalvelu"]}],
    }
    job = JobForScoring(
        id=1,
        title="Asiakaspalvelija",
        employer="Example Oy",
        description="Tehtävä on aktiivista myyntiä.",
        location=None,
    )

    result = score_job(profile, job)

    assert not result.passes
    assert result.deterministic_result["negative_matches"] == ["myynti"]


def test_score_job_rejects_missing_teacher_qualification_requirement() -> None:
    profile = {
        "role_clusters": [{"titles_fi": ["ohjaaja"], "keywords_fi": ["opetus", "ohjaus"]}],
    }
    job = JobForScoring(
        id=1,
        title="Aineenopettaja",
        employer="Example",
        description="Kelpoisuusvaatimuksena on opettajan kelpoisuus.",
        location=None,
    )

    result = score_job(profile, job)

    assert not result.passes
    assert "opettajan_kelpoisuus" in result.deterministic_result["missing_qualification_matches"]


def test_score_job_does_not_reject_library_instruction_without_teacher_requirement() -> None:
    profile = {
        "role_clusters": [{"titles_fi": ["kirjastonhoitaja"], "keywords_fi": ["kirjastonkäytön opetus"]}],
    }
    job = JobForScoring(
        id=1,
        title="Kirjastonhoitaja",
        employer="Example",
        description="Tehtävään kuuluu kirjastonkäytön opetusta ja ryhmävierailuja.",
        location=None,
    )

    result = score_job(profile, job)

    assert result.passes
    assert result.deterministic_result["missing_qualification_matches"] == []


def test_score_job_rejects_library_bus_driver_role() -> None:
    profile = {
        "role_clusters": [{"titles_fi": ["kirjastovirkailija"], "keywords_fi": ["kirjasto", "asiakaspalvelu"]}],
    }
    job = JobForScoring(
        id=1,
        title="Kirjastoautonkuljettaja-virkailija",
        employer="Example",
        description="Tehtävä sisältää kirjastoauton kuljettamista.",
        location=None,
    )

    result = score_job(profile, job)

    assert not result.passes
    assert "kirjastoautonkuljettaja" in result.deterministic_result["missing_qualification_matches"]


class FakeMappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def one_or_none(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Any:
        return iter(self.rows)


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    def mappings(self) -> FakeMappings:
        return FakeMappings(self.rows)

    def scalar_one(self) -> int:
        return 1


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> FakeResult:
        sql = str(statement)
        self.statements.append(sql)
        if "from job_seeker_profiles" in sql:
            return FakeResult(
                [
                    {
                        "id": 1,
                        "profile": {
                            "location": {"home_city": "Oulu"},
                            "role_clusters": [
                                {
                                    "titles_fi": ["kirjastonhoitaja"],
                                    "keywords_fi": ["kirjasto"],
                                }
                            ],
                        },
                    }
                ]
            )
        if "from jobs" in sql:
            return FakeResult(
                [
                    {
                        "id": 10,
                        "title": "Kirjastonhoitaja",
                        "employer": "Oulun kaupunki",
                        "description": "Kirjasto",
                        "location": "Oulu",
                    }
                ]
            )
        return FakeResult()


def test_score_job_learned_exclusion_penalizes_rank_but_keeps_eligible() -> None:
    profile = {
        "location": {"home_city": "Oulu"},
        "role_clusters": [
            {
                "titles_fi": ["myyntiedustaja"],
                "keywords_fi": ["myynti", "asiakaspalvelu"],
            }
        ],
        "learned": {
            "exclusions": {"terms_fi": ["myynti"], "employers": [], "sectors": []},
        },
    }
    job = JobForScoring(
        id=1,
        title="Myyntiedustaja",
        employer="Retail",
        description="Myynti ja asiakaspalvelu.",
        location="Oulu",
    )

    result = score_job(profile, job)

    assert result.passes
    assert result.deterministic_result["learned_exclusion_penalty"] > 0
    assert result.machine_score < result.deterministic_result["unpenalized_machine_score"]


def test_merge_semantic_scores_updates_unpenalized_machine_score() -> None:
    job = JobForScoring(id=1, title="Test", employer="X", description="d", location="Oulu")
    base = score_job(
        {
            "location": {"home_city": "Oulu"},
            "role_clusters": [{"titles_fi": ["test"], "keywords_fi": ["data"]}],
        },
        job,
    )
    merged = merge_semantic_scores([(job, base)], {1: 0.9})
    _, result = merged[0]

    assert result.deterministic_result["unpenalized_machine_score"] >= result.machine_score


def test_apply_travel_preserves_learned_exclusion_penalty(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "app.matching.resolve_transit_for_queries",
        lambda _connection, _queries, max_lookups=20: {},
    )
    profile = {
        "location": {"home_city": "Oulu"},
        "role_clusters": [{"titles_fi": ["myyntiedustaja"], "keywords_fi": ["myynti"]}],
        "learned": {"exclusions": {"terms_fi": ["myynti"], "employers": [], "sectors": []}},
    }
    job = JobForScoring(
        id=1,
        title="Myyntiedustaja",
        employer="Retail",
        description="Myynti ja asiakaspalvelu.",
        location="Oulu",
    )
    base = score_job(profile, job)
    penalized_before_travel = base.machine_score

    travel_scored = apply_travel_to_scored_candidates(
        [(job, base)],
        profile=profile,
        connection=RecordingConnection(),  # type: ignore[arg-type]
    )
    _, adjusted, assessment = travel_scored[0]
    expected = max(0.0, min(100.0, penalized_before_travel + assessment.score_adjustment))

    assert adjusted.machine_score == round(expected, 2)
    assert base.deterministic_result["learned_exclusion_penalty"] > 0
    assert penalized_before_travel < base.deterministic_result["unpenalized_machine_score"]


def test_apply_travel_uses_recommendation_transit_lookup_budget(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_resolve_transit(
        _connection: Any,
        queries: list[str],
        *,
        max_lookups: int = 20,
    ) -> dict[str, Any]:
        captured["queries"] = queries
        captured["max_lookups"] = max_lookups
        return {}

    monkeypatch.setattr(
        "app.matching.get_settings",
        lambda: Settings(
            google_maps_api_key="configured",
            llm_eval_max_jobs=1,
            recommendation_transit_lookup_budget=3,
        ),
    )
    monkeypatch.setattr("app.matching.resolve_transit_for_queries", fake_resolve_transit)
    profile = {
        "location": {"home_city": "Oulu"},
        "role_clusters": [{"titles_fi": ["asiantuntija"], "keywords_fi": ["palvelu"]}],
    }
    scored = []
    for job_id, location in enumerate(["Ylivieska", "Kemi", "Rovaniemi", "Kajaani"], start=1):
        job = JobForScoring(
            id=job_id,
            title="Asiantuntija",
            employer="Työnantaja",
            description="Palvelu ja asiantuntijatyö.",
            location=location,
        )
        scored.append((job, score_job(profile, job)))

    apply_travel_to_scored_candidates(
        scored,
        profile=profile,
        connection=RecordingConnection(),  # type: ignore[arg-type]
    )

    assert captured["queries"] == [
        "Ylivieska, Finland",
        "Kemi, Finland",
        "Rovaniemi, Finland",
        "Kajaani, Finland",
    ]
    assert captured["max_lookups"] == 3


def test_deterministic_recommendations_deactivate_and_upsert_without_deleting_feedback_targets(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        "app.matching.resolve_transit_for_queries",
        lambda _connection, _queries, max_lookups=20: {},
    )
    connection = RecordingConnection()

    result = run_deterministic_recommendations(connection, max_jobs=10)  # type: ignore[arg-type]

    executed_sql = "\n".join(connection.statements).lower()
    assert result["evaluated"] == 1
    assert result["recommended"] == 1
    assert "delete from recommendations" not in executed_sql
    assert "set is_active = false" in executed_sql
    assert "on conflict (profile_id, job_id)" in executed_sql
    assert "s.enabled = true" in executed_sql
    assert "recommendation_feedback rf" in executed_sql
    assert "rf.rating = 1" in executed_sql
    assert "commutable_or_full_remote" in executed_sql
    assert "full_remote" in executed_sql


def test_refresh_active_recommendation_ranks_honors_rating_one_feedback() -> None:
    connection = RecordingConnection()

    refresh_active_recommendation_ranks(
        connection,
        profile_id=1,
        require_llm_review=False,
        prompt_version=5,
    )  # type: ignore[arg-type]

    executed_sql = "\n".join(connection.statements).lower()
    assert "rf.rating = 1" in executed_sql
    assert "commutable_or_full_remote_rank = null" in executed_sql


def test_refresh_active_recommendation_ranks_deactivates_weak_llm_rows() -> None:
    connection = RecordingConnection()

    active_count = refresh_active_recommendation_ranks(
        connection,
        profile_id=1,
        require_llm_review=True,
        prompt_version=5,
    )  # type: ignore[arg-type]

    executed_sql = "\n".join(connection.statements).lower()
    assert active_count == 1
    assert "suggested_action = 'skip'" in executed_sql
    assert "llm_score < 40" in executed_sql
    assert "llm_evaluation_id is null" in executed_sql
    assert "e.prompt_version is distinct from :prompt_version" in executed_sql
    assert "generic_customer_service_only" in executed_sql
    assert "not_applicable" in executed_sql
    assert "set is_active = false" in executed_sql
    assert "row_number() over" in executed_sql
    assert "llm_score desc nulls last" in executed_sql


class UnusedProvider:
    provider_name = "openai"

    def evaluate_job_fit(self, **kwargs: Any) -> None:
        raise AssertionError("provider should not be called without candidate rows")


class PrefilterSkipConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> FakeResult:
        sql = str(statement)
        self.statements.append(sql)
        if "from job_seeker_profiles" in sql:
            return FakeResult(
                [
                    {
                        "id": 1,
                        "profile": {
                            "location": {"home_city": "Oulu"},
                            "role_clusters": [{"titles_fi": ["kirjastonhoitaja"], "keywords_fi": ["kirjasto"]}],
                            "learned": {"version": 3, "few_shot_examples": [], "eval_hints": []},
                        },
                    }
                ]
            )
        if "from recommendations r" in sql:
            return FakeResult(
                [
                    {
                        "recommendation_id": 99,
                        "job_id": 42,
                        "deterministic_result": {
                            "anti_preference_similarity": 0.82,
                            "learned_exclusion_matches": ["myynti"],
                        },
                        "title": "Myyntiedustaja",
                        "employer": "Retail",
                        "description": "Myynti",
                        "location": "Oulu",
                    }
                ]
            )
        return FakeResult()


def test_llm_evaluations_skip_high_anti_similarity_with_learned_exclusions() -> None:
    connection = PrefilterSkipConnection()

    result = run_llm_evaluations(connection, provider=UnusedProvider(), max_jobs=10)  # type: ignore[arg-type]

    assert result == {"llm_evaluated": 0, "llm_failed": 0, "llm_skipped": 1}


def test_llm_evaluations_do_not_skip_rows_just_because_old_evaluation_id_exists() -> None:
    connection = RecordingConnection()

    result = run_llm_evaluations(connection, provider=UnusedProvider(), max_jobs=10)  # type: ignore[arg-type]

    executed_sql = "\n".join(connection.statements).lower()
    assert result == {"llm_evaluated": 0, "llm_failed": 0, "llm_skipped": 0}
    assert "from recommendations r" in executed_sql
    assert "where r.llm_evaluation_id is null" not in executed_sql
    assert "current_eval.prompt_version is distinct from :prompt_version" in executed_sql
