from app.travel_policy import (
    TravelAssessment,
    assess_travel,
    is_verified_full_remote,
)
from app.transit_distance import TransitDistanceResult


def _profile() -> dict:
    return {"location": {"home_city": "Oulu"}}


def test_full_remote_qualifies_for_remote_scope_not_commutable() -> None:
    assessment = assess_travel(
        profile=_profile(),
        location="Helsinki / Etä",
        transit_by_destination={},
        origin_address="Jalkatie 2, Oulu, Finland",
        commute_limit_minutes=120,
        maps_available=False,
    )
    assert assessment.full_remote is True
    assert assessment.commutable is False
    assert assessment.commutable_or_full_remote is True
    assert assessment.status == "full_remote"


def test_hybrid_remote_possibility_is_not_full_remote() -> None:
    assert is_verified_full_remote("Helsinki / Hybridi") is False
    assert is_verified_full_remote("Helsinki, etätyömahdollisuus") is False
    assert is_verified_full_remote("Täysin etänä, mutta osittain etä toimistolla") is False


def test_exact_home_city_is_commutable() -> None:
    assessment = assess_travel(
        profile=_profile(),
        location="Oulu",
        transit_by_destination={},
        origin_address="Jalkatie 2, Oulu, Finland",
        commute_limit_minutes=120,
    )
    assert assessment.commutable is True
    assert assessment.full_remote is False
    assert assessment.commutable_or_full_remote is True
    assert assessment.status == "exact_home_city"


def test_multi_city_location_containing_home_city_is_commutable_without_routes() -> None:
    assessment = assess_travel(
        profile=_profile(),
        location="Helsinki, Kuopio, Oulu",
        transit_by_destination={},
        origin_address="Jalkatie 2, Oulu, Finland",
        commute_limit_minutes=120,
        maps_available=False,
    )
    assert assessment.commutable is True
    assert assessment.full_remote is False
    assert assessment.commutable_or_full_remote is True
    assert assessment.status == "exact_home_city"
    assert assessment.reason_code == "contains_home_city"


def test_within_limit_boundary() -> None:
    transit = TransitDistanceResult(
        origin="Jalkatie 2, Oulu, Finland",
        destination="Ylivieska",
        destination_query="Ylivieska, Finland",
        distance_meters=92_000,
        distance_km=92,
        duration_seconds=7200,
        duration_text="2 h",
        summary_text="92 km · 2 h (julkiset)",
    )
    within = assess_travel(
        profile=_profile(),
        location="Ylivieska",
        transit_by_destination={"Ylivieska, Finland": transit},
        origin_address="Jalkatie 2, Oulu, Finland",
        commute_limit_minutes=120,
    )
    over = assess_travel(
        profile=_profile(),
        location="Ylivieska",
        transit_by_destination={
            "Ylivieska, Finland": TransitDistanceResult(
                origin=transit.origin,
                destination=transit.destination,
                destination_query=transit.destination_query,
                distance_meters=transit.distance_meters,
                distance_km=transit.distance_km,
                duration_seconds=7201,
                duration_text="2 h 1 min",
                summary_text=transit.summary_text,
            )
        },
        origin_address="Jalkatie 2, Oulu, Finland",
        commute_limit_minutes=120,
    )
    assert within.commutable is True
    assert over.commutable is False


def test_hybrid_over_limit_is_nationwide_only() -> None:
    transit = TransitDistanceResult(
        origin="Jalkatie 2, Oulu, Finland",
        destination="Helsinki",
        destination_query="Helsinki, Finland",
        distance_meters=600_000,
        distance_km=600,
        duration_seconds=25_000,
        duration_text="7 h",
        summary_text="600 km · 7 h (julkiset)",
    )
    assessment = assess_travel(
        profile=_profile(),
        location="Helsinki / Hybridi",
        transit_by_destination={"Helsinki, Finland": transit},
        origin_address="Jalkatie 2, Oulu, Finland",
        commute_limit_minutes=120,
    )
    assert assessment.commutable is False
    assert assessment.full_remote is False
    assert assessment.commutable_or_full_remote is False
    assert assessment.status == "hybrid"


def test_standalone_remote_location_qualifies_as_full_remote() -> None:
    assert is_verified_full_remote("Etätyö") is True
    assert is_verified_full_remote("Markkinointiharjoittelija. Etätyö") is True
    assessment = assess_travel(
        profile=_profile(),
        location="Etätyö",
        transit_by_destination={},
        origin_address="Jalkatie 2, Oulu, Finland",
        commute_limit_minutes=120,
    )
    assert assessment.full_remote is True
    assert assessment.commutable is False


def test_remote_signal_in_title_qualifies_without_remote_location_marker() -> None:
    assessment = assess_travel(
        profile=_profile(),
        location="Hyvinkää",
        work_mode_text="Oikeudellisen tiedonhaun harjoittelija. Etätyö",
        transit_by_destination={},
        origin_address="Jalkatie 2, Oulu, Finland",
        commute_limit_minutes=120,
        maps_available=False,
    )

    assert assessment.full_remote is True
    assert assessment.commutable is False
    assert assessment.commutable_or_full_remote is True
    assert assessment.status == "full_remote"
    assert assessment.reason_code == "full_remote"


def test_travel_assessment_audit_dict_includes_scope_flags() -> None:
    assessment = TravelAssessment(
        commutable=True,
        full_remote=False,
        status="within_limit",
        duration_seconds=3600,
        distance_km=50,
        score_adjustment=10,
        evidence_text="test",
        tone="good",
        reason_code="transit_within_limit",
        origin_address="Jalkatie 2, Oulu, Finland",
        commute_limit_minutes=120,
    )
    audit = assessment.to_audit_dict()
    assert audit["commutable_or_full_remote"] is True
    assert audit["full_remote"] is False
