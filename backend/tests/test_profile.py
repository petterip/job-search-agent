import json
from datetime import date

import pytest

from app.profile import load_profile_document, profile_json


def test_load_profile_document_reads_yaml(tmp_path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("role_clusters:\n  - titles_fi:\n      - Ohjaaja\n", encoding="utf-8")

    assert load_profile_document(path) == {
        "role_clusters": [{"titles_fi": ["Ohjaaja"]}],
    }


def test_load_profile_document_reads_json(tmp_path) -> None:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"location": {"home_city": "Oulu"}}), encoding="utf-8")

    assert load_profile_document(path) == {"location": {"home_city": "Oulu"}}


def test_load_profile_document_rejects_non_mapping(tmp_path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("- invalid\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping"):
        load_profile_document(path)


def test_profile_json_serializes_dates() -> None:
    assert json.loads(profile_json({"updated": date(2026, 6, 20)})) == {
        "updated": "2026-06-20",
    }


def _importable_profile(**overrides: object) -> dict:
    from support import DEFAULT_PRIVACY

    profile = {
        "schema_version": 2,
        "privacy": DEFAULT_PRIVACY,
        "freshness": {"max_age_days": 30, "drop_past_deadline": True},
        "location": {"home_city": "Testikaupunki"},
        "role_clusters": [{"titles_fi": ["kirjastonhoitaja"], "keywords_fi": ["kirjasto"]}],
        "exclusions": {"hard_negative_titles_fi": ["myynti"]},
        "preferences": {"application_history_signals": {"boost_titles_fi": ["kirjastovirkailija"]}},
    }
    profile.update(overrides)
    return profile


def test_validate_profile_document_accepts_consumed_contract() -> None:
    from app.profile import validate_profile_document

    assert validate_profile_document(_importable_profile()) == []


def test_validate_profile_document_rejects_unsupported_schema_version() -> None:
    from app.profile import ProfileValidationError, validate_profile_document

    with pytest.raises(ProfileValidationError, match="schema_version"):
        validate_profile_document(_importable_profile(schema_version=99))


def test_validate_profile_document_rejects_unknown_privacy_directive() -> None:
    from app.profile import ProfileValidationError, validate_profile_document
    from support import DEFAULT_PRIVACY

    privacy = dict(DEFAULT_PRIVACY)
    privacy["llm_allowed_fields"] = ["objective", "made up directive"]
    with pytest.raises(ProfileValidationError):
        validate_profile_document(_importable_profile(privacy=privacy))


def test_validate_profile_document_rejects_malformed_freshness() -> None:
    from app.profile import ProfileValidationError, validate_profile_document

    with pytest.raises(ProfileValidationError):
        validate_profile_document(
            _importable_profile(freshness={"max_age_days": "thirty"})
        )


def test_validate_profile_document_warns_about_unconsumed_keys() -> None:
    from app.profile import validate_profile_document

    warnings = validate_profile_document(_importable_profile(radius_km=200))

    assert any("radius_km" in warning for warning in warnings)


def test_validate_profile_document_rejects_wrong_list_types() -> None:
    from app.profile import ProfileValidationError, validate_profile_document

    with pytest.raises(ProfileValidationError):
        validate_profile_document(_importable_profile(role_clusters={"titles_fi": ["x"]}))


def test_merge_derived_state_preserves_learned_and_boosts() -> None:
    from app.profile import merge_derived_state

    existing = {
        "learned": {"version": 4, "eval_hints": ["keep me"]},
        "preferences": {"learned_boosts": {"boost_titles_fi": ["kirjastovirkailija"]}},
    }
    imported = {"preferences": {"sectors_preferred": ["kirjasto"]}}

    merged = merge_derived_state(imported, existing)

    assert merged["learned"]["version"] == 4
    assert merged["preferences"]["learned_boosts"]["boost_titles_fi"] == ["kirjastovirkailija"]
    assert merged["preferences"]["sectors_preferred"] == ["kirjasto"]


def test_merge_derived_state_reset_drops_learned() -> None:
    from app.profile import merge_derived_state

    existing = {"learned": {"version": 4}, "preferences": {"learned_boosts": {"a": ["b"]}}}

    merged = merge_derived_state({"preferences": {}}, existing, reset_derived=True)

    assert "learned" not in merged
    assert "learned_boosts" not in merged["preferences"]


def test_base_revision_ignores_learned_state_but_tracks_base_edits() -> None:
    from app.profile import profile_base_revision

    base = _importable_profile()
    with_learned = {**base, "learned": {"version": 9}}
    edited = {**base, "location": {"home_city": "Toinenkaupunki"}}

    assert profile_base_revision(base) == profile_base_revision(with_learned)
    assert profile_base_revision(base) != profile_base_revision(edited)
