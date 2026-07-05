import httpx
import pytest

from app.config import get_settings
from app.transit_distance import (
    LocationEvidence,
    TransitDistanceError,
    TransitDistanceResult,
    apply_location_evidence_to_concerns,
    build_location_evidence,
    compute_transit_distance,
    format_duration_fi,
    format_transit_summary,
    normalize_destination,
    parse_duration_seconds,
)


def test_normalize_destination_strips_work_mode_and_adds_country():
    assert normalize_destination("Helsinki / Etä") == "Helsinki, Finland"
    assert normalize_destination("Rovaniemi / Hybridi") == "Rovaniemi, Finland"
    assert normalize_destination("Suomi") is None
    assert normalize_destination("Suomi / Etä") is None


def test_format_duration_fi():
    assert format_duration_fi(45) == "1 min"
    assert format_duration_fi(3600) == "1 h"
    assert format_duration_fi(5400) == "1 h 30 min"


def test_build_location_evidence_uses_transit_summary():
    transit = TransitDistanceResult(
        origin="Jalkatie 2, Oulu, Finland",
        destination="Helsinki",
        destination_query="Helsinki, Finland",
        distance_meters=690_000,
        distance_km=690,
        duration_seconds=25_800,
        duration_text="7 h 13 min",
        summary_text="690 km · 7 h 13 min (julkiset)",
    )
    evidence = build_location_evidence("Helsinki", transit)
    assert evidence is not None
    assert evidence.text == "Helsinki · 690 km · 7 h 13 min (julkiset)"
    assert evidence.tone == "bad"


def test_build_location_evidence_marks_local_transit_as_good():
    transit = TransitDistanceResult(
        origin="Jalkatie 2, Oulu, Finland",
        destination="Oulu",
        destination_query="Oulu, Finland",
        distance_meters=9_000,
        distance_km=9,
        duration_seconds=2_100,
        duration_text="35 min",
        summary_text="9 km · 35 min (julkiset)",
    )
    evidence = build_location_evidence("Oulu", transit)
    assert evidence is not None
    assert "Oulun seutu" in evidence.text
    assert evidence.tone == "good"


def test_apply_location_evidence_to_concerns():
    evidence = LocationEvidence(text="Helsinki · 690 km · 7 h 13 min (julkiset)", tone="bad")
    updated = apply_location_evidence_to_concerns(
        ["Sijainti ei osu hakijan ensisijaiseen alueeseen."],
        evidence,
    )
    assert updated == ["Helsinki · 690 km · 7 h 13 min (julkiset)"]
    assert format_transit_summary(distance_km=612, duration_text="6 h 15 min") == (
        "612 km · 6 h 15 min (julkiset)"
    )


def test_parse_duration_seconds():
    assert parse_duration_seconds("5400s") == 5400
    with pytest.raises(TransitDistanceError):
        parse_duration_seconds("90m")


def test_compute_transit_distance_parses_routes_response(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    get_settings.cache_clear()

    def fake_post(self, url, headers, json):  # noqa: ANN001, ARG001
        request = httpx.Request("POST", url)
        assert headers["X-Goog-Api-Key"] == "test-key"
        assert json["travelMode"] == "TRANSIT"
        assert json["origin"]["address"] == "Jalkatie 2, Oulu, Finland"
        assert json["destination"]["address"] == "Helsinki, Finland"
        return httpx.Response(
            200,
            request=request,
            json={
                "routes": [
                    {
                        "distanceMeters": 612_000,
                        "duration": "22500s",
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    result = compute_transit_distance("Helsinki")
    assert result.distance_km == 612
    assert result.duration_text == "6 h 15 min"
    assert result.summary_text == "612 km · 6 h 15 min (julkiset)"


def test_compute_transit_distance_rejects_vague_destination(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    get_settings.cache_clear()
    with pytest.raises(TransitDistanceError, match="too vague"):
        compute_transit_distance("Suomi")


def test_compute_transit_distance_surfaces_routes_enablement(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    get_settings.cache_clear()

    def fake_post(self, url, headers, json):  # noqa: ANN001, ARG001
        request = httpx.Request("POST", url)
        return httpx.Response(
            403,
            request=request,
            json={
                "error": {
                    "code": 403,
                    "message": "Routes API has not been used in project 123 before or it is disabled.",
                    "status": "PERMISSION_DENIED",
                }
            },
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(TransitDistanceError, match="Enable Routes API"):
        compute_transit_distance("Helsinki")
