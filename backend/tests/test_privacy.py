import json

import pytest

from app import embeddings, llm
from app.privacy import (
    PrivacyConfigurationError,
    load_privacy_policy,
    outbound_llm_allowed,
    project_profile_for_llm,
    sanitize_feedback_snapshot_fields,
    sanitize_job_travel,
    sanitize_learned_payload,
    sanitize_outbound,
)
from support import DEFAULT_PRIVACY, with_privacy

# Synthetic forbidden sentinels. Never use real private profile values here.
POSTAL_SENTINEL = "99999"
ADDRESS_SENTINEL = "Salaisen Kujan 7"
PHONE_SENTINEL = "+358 40 5550100"
EMAIL_SENTINEL = "sentinel@example.invalid"


def sensitive_profile(**overrides: object) -> dict:
    profile = {
        "objective": "safe objective",
        "location": {
            "home_city": "Testikaupunki",
            "home_postal": POSTAL_SENTINEL,
            "address": ADDRESS_SENTINEL,
            "region_towns": ["Lähikunta"],
        },
        "career_evidence": {
            "contact": {"email": EMAIL_SENTINEL, "phone": PHONE_SENTINEL},
            "qualifications": ["kirjastoalan kelpoisuus"],
        },
        "languages": [{"code": "fi", "level": "C2"}],
        "role_clusters": [{"titles_fi": ["kirjastonhoitaja"]}],
        "skills": [{"name": "esimiestyö"}],
        "strength_signals": [{"name": "kokonaisuuksien hallinta"}],
        "preferences": {"sectors_preferred": ["kirjasto"]},
        "exclusions": {"hard_negative_titles_fi": ["myynti"]},
        "freshness": {"max_age_days": 30},
        "llm_guidance": {"objective": "löydä sopivat työt"},
        "raw_cv": "raw private cv",
        "email": EMAIL_SENTINEL,
    }
    profile.update(overrides)
    return with_privacy(profile)


def _text_of(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def test_forbidden_nested_fields_win_over_allowed_parents() -> None:
    projected = project_profile_for_llm(sensitive_profile())

    assert projected["location"]["home_city"] == "Testikaupunki"
    assert "home_postal" not in projected["location"]
    assert "address" not in projected["location"]
    assert "email" not in projected["career_evidence"]["contact"]
    assert "phone" not in projected["career_evidence"]["contact"]
    assert POSTAL_SENTINEL not in _text_of(projected)
    assert ADDRESS_SENTINEL not in _text_of(projected)
    assert EMAIL_SENTINEL not in _text_of(projected)
    assert PHONE_SENTINEL not in _text_of(projected)


def test_outbound_profile_summary_has_no_forbidden_sentinels() -> None:
    summary = llm.minimized_profile_summary(sensitive_profile())

    assert "Testikaupunki" in summary
    assert "kirjastoalan kelpoisuus" in summary
    for sentinel in (POSTAL_SENTINEL, ADDRESS_SENTINEL, EMAIL_SENTINEL, PHONE_SENTINEL):
        assert sentinel not in summary


def test_embedding_input_has_no_forbidden_sentinels_and_stays_positive_only() -> None:
    text = embeddings.profile_embedding_text(sensitive_profile())

    assert "kirjastonhoitaja" in text
    assert "myynti" not in text
    for sentinel in (POSTAL_SENTINEL, ADDRESS_SENTINEL, EMAIL_SENTINEL, PHONE_SENTINEL):
        assert sentinel not in text


def test_job_summary_drops_routing_origin_but_keeps_safe_travel_fields() -> None:
    summary = llm.job_summary(
        {
            "title": "Kirjastonhoitaja",
            "location": "Testikaupunki",
            "description": "d",
            "deterministic_result": {
                "travel_assessment": {
                    "commutable": True,
                    "status": "within_limit",
                    "duration_seconds": 1800,
                    "origin_address": ADDRESS_SENTINEL,
                    "routing_profile": "weekday_0800_public_transit",
                }
            },
        }
    )
    data = json.loads(summary)

    assert data["travel_assessment"]["commutable"] is True
    assert data["travel_assessment"]["status"] == "within_limit"
    assert data["travel_assessment"]["duration_seconds"] == 1800
    assert "origin_address" not in data["travel_assessment"]
    assert ADDRESS_SENTINEL not in summary


def test_sanitize_job_travel_returns_none_for_missing_assessment() -> None:
    assert sanitize_job_travel(None) is None
    assert sanitize_job_travel({"commutable": False}) == {"commutable": False}


def test_feedback_snapshot_sanitization_removes_address() -> None:
    sanitized = sanitize_feedback_snapshot_fields(
        {
            "travel_origin_address": ADDRESS_SENTINEL,
            "travel_assessment": {"origin_address": ADDRESS_SENTINEL, "status": "within_limit"},
            "title": "Kirjastonhoitaja",
        }
    )

    assert "travel_origin_address" not in sanitized
    assert "origin_address" not in sanitized["travel_assessment"]
    assert sanitized["title"] == "Kirjastonhoitaja"


def test_learned_payload_sanitization_strips_nested_forbidden_keys() -> None:
    sanitized = sanitize_learned_payload(
        [
            {"title": "Kirjastonhoitaja", "email": EMAIL_SENTINEL, "note": "keep"},
            {"nested": {"phone": PHONE_SENTINEL, "keep": "yes"}},
        ]
    )

    assert sanitized[0] == {"title": "Kirjastonhoitaja", "note": "keep"}
    assert sanitized[1] == {"nested": {"keep": "yes"}}


def test_sanitize_outbound_redacts_email_and_phone_patterns_in_text() -> None:
    sanitized = sanitize_outbound({"note": f"soita {PHONE_SENTINEL} tai {EMAIL_SENTINEL}"})

    assert EMAIL_SENTINEL not in sanitized["note"]
    assert PHONE_SENTINEL not in sanitized["note"]


def test_missing_privacy_block_fails_closed() -> None:
    with pytest.raises(PrivacyConfigurationError):
        load_privacy_policy({"objective": "no privacy block"})


def test_unknown_privacy_directive_is_rejected() -> None:
    profile = with_privacy({}, llm_allowed_fields=["objective", "totally unknown directive"])

    with pytest.raises(PrivacyConfigurationError):
        load_privacy_policy(profile)


def test_non_boolean_llm_allowed_fails_closed() -> None:
    profile = with_privacy({}, llm_allowed="yes")

    with pytest.raises(PrivacyConfigurationError):
        load_privacy_policy(profile)


def test_llm_allowed_false_disables_hosted_calls() -> None:
    profile = with_privacy({}, llm_allowed=False)

    assert outbound_llm_allowed(profile) is False
    assert outbound_llm_allowed(sensitive_profile()) is True


def test_unallowed_directive_is_not_projected() -> None:
    profile = with_privacy(
        {"languages": [{"code": "fi"}], "skills": [{"name": "x"}]},
        llm_allowed_fields=["languages"],
    )

    projected = project_profile_for_llm(profile)

    assert "languages" in projected
    assert "skills" not in projected


def test_default_privacy_fixture_matches_supported_directives() -> None:
    # Guards the shared fixture against drifting from the supported vocabulary.
    policy = load_privacy_policy({"privacy": DEFAULT_PRIVACY})

    assert policy.llm_allowed is True
    assert policy.allows("location policy summary")


def test_forbidden_values_quoted_in_allowed_prose_are_redacted() -> None:
    profile = with_privacy(
        {
            "objective": "Asun osoitteessa Salaisen Kujan 7 ja postinumero on 99999.",
            "location": {"home_city": "Testikaupunki", "home_postal": "99999", "address": "Salaisen Kujan 7"},
        }
    )

    summary = llm.minimized_profile_summary(profile)

    assert "Salaisen Kujan 7" not in summary
    assert "99999" not in summary
    assert "osoite poistettu" in summary


def test_learned_hint_containing_forbidden_value_is_redacted() -> None:
    from app.privacy import collect_forbidden_values, sanitize_learned_payload

    profile = with_privacy(
        {"location": {"home_city": "Testikaupunki", "address": "Salaisen Kujan 7"}}
    )
    sanitized = sanitize_learned_payload(
        {"hint": "Muista mainita Salaisen Kujan 7"},
        extra_secrets=collect_forbidden_values(profile),
    )

    assert "Salaisen Kujan 7" not in json.dumps(sanitized, ensure_ascii=False)
    assert "osoite poistettu" in sanitized["hint"]


def test_numeric_forbidden_value_is_redacted() -> None:
    profile = with_privacy(
        {
            "objective": "Postinumero 99999 on kotini.",
            "location": {"home_city": "Testikaupunki", "home_postal": 99999},
        }
    )

    summary = llm.minimized_profile_summary(profile)

    assert "99999" not in summary
