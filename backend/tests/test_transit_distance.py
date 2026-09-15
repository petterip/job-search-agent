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
    assert "lähialue" in evidence.text
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


def test_compute_transit_distance_classifies_transport_failure(monkeypatch):
    from app.transit_distance import TransitTransientError

    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    get_settings.cache_clear()

    def fake_post(self, url, headers, json):  # noqa: ANN001, ARG001
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(TransitTransientError):
        compute_transit_distance("Helsinki")


def test_compute_transit_distance_classifies_empty_routes_as_no_route(monkeypatch):
    from app.transit_distance import TransitNoRouteError

    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    get_settings.cache_clear()

    def fake_post(self, url, headers, json):  # noqa: ANN001, ARG001
        return httpx.Response(200, request=httpx.Request("POST", url), json={"routes": []})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(TransitNoRouteError):
        compute_transit_distance("Helsinki")


def _transit_settings(tmp_path, **overrides):
    from app.config import Settings

    values = {
        "google_maps_api_key": "test-key",
        "transit_origin_address": "Testikatu 1, Oulu, Finland",
        "transit_cache_ttl_days": 7,
        "transit_provider_backoff_min": 15,
        "storage_dir": str(tmp_path),
    }
    values.update(overrides)
    return Settings(**values)


def test_failed_attempts_consume_the_lookup_budget(monkeypatch, tmp_path):
    from app import transit_distance as transit_module
    from app.transit_distance import TransitInvalidDestinationError

    settings = _transit_settings(tmp_path)
    monkeypatch.setattr(transit_module, "get_settings", lambda: settings)
    monkeypatch.setattr(transit_module, "fetch_cached_transit_distances", lambda *a, **k: {})
    monkeypatch.setattr(transit_module, "fetch_recent_transit_failures", lambda *a, **k: set())
    monkeypatch.setattr(transit_module, "store_transit_failure", lambda *a, **k: None)
    calls: list[str] = []

    def fake_compute(destination, *, origin=None):  # noqa: ANN001, ARG001
        calls.append(destination)
        raise TransitInvalidDestinationError("bad destination")

    monkeypatch.setattr(transit_module, "compute_transit_distance", fake_compute)

    result = transit_module.resolve_transit_for_queries(
        object(),
        ["A, Finland", "B, Finland", "C, Finland", "D, Finland"],
        max_lookups=2,
    )

    assert len(calls) == 2
    assert result.attempted == 2
    assert result.failed_queries == {"A, Finland", "B, Finland"}
    assert result.not_attempted_queries == {"C, Finland", "D, Finland"}
    assert result.no_route_queries == frozenset()


def test_confirmed_no_route_is_the_only_unrouteable_signal(monkeypatch, tmp_path):
    from app import transit_distance as transit_module
    from app.transit_distance import TransitNoRouteError

    settings = _transit_settings(tmp_path)
    monkeypatch.setattr(transit_module, "get_settings", lambda: settings)
    monkeypatch.setattr(transit_module, "fetch_cached_transit_distances", lambda *a, **k: {})
    monkeypatch.setattr(transit_module, "fetch_recent_transit_failures", lambda *a, **k: set())
    stored: list[str] = []
    monkeypatch.setattr(
        transit_module, "store_transit_failure", lambda *a, reason=None, **k: stored.append(reason)
    )

    def fake_compute(destination, *, origin=None):  # noqa: ANN001, ARG001
        raise TransitNoRouteError("no route")

    monkeypatch.setattr(transit_module, "compute_transit_distance", fake_compute)

    result = transit_module.resolve_transit_for_queries(object(), ["A, Finland"], max_lookups=5)

    assert result.no_route_queries == {"A, Finland"}
    assert result["A, Finland"] is None
    assert stored == ["no_route"]


def test_transient_failure_sets_provider_backoff_without_per_destination_poison(
    monkeypatch, tmp_path
):
    from app import transit_distance as transit_module
    from app.transit_distance import TransitTransientError

    settings = _transit_settings(tmp_path)
    monkeypatch.setattr(transit_module, "get_settings", lambda: settings)
    monkeypatch.setattr(transit_module, "fetch_cached_transit_distances", lambda *a, **k: {})
    monkeypatch.setattr(transit_module, "fetch_recent_transit_failures", lambda *a, **k: set())
    failure_writes: list[str] = []
    monkeypatch.setattr(
        transit_module,
        "store_transit_failure",
        lambda *a, reason=None, **k: failure_writes.append(reason),
    )

    def fake_compute(destination, *, origin=None):  # noqa: ANN001, ARG001
        raise TransitTransientError("503")

    monkeypatch.setattr(transit_module, "compute_transit_distance", fake_compute)

    result = transit_module.resolve_transit_for_queries(
        object(), ["A, Finland", "B, Finland"], max_lookups=5
    )

    assert result.provider_backoff is False
    assert failure_writes == []
    assert transit_module.transit_provider_in_backoff(settings) is True

    # A later run short-circuits without any HTTP attempt.
    monkeypatch.setattr(
        transit_module,
        "compute_transit_distance",
        lambda *a, **k: pytest.fail("should not call provider during backoff"),
    )
    second = transit_module.resolve_transit_for_queries(object(), ["C, Finland"], max_lookups=5)
    assert second.provider_backoff is True
    assert second.not_attempted_queries == {"C, Finland"}


def test_provider_backoff_still_serves_cached_routes(monkeypatch, tmp_path):
    from app import transit_distance as transit_module

    settings = _transit_settings(tmp_path)
    monkeypatch.setattr(transit_module, "get_settings", lambda: settings)
    cached_result = TransitDistanceResult(
        origin="Testikatu 1, Oulu, Finland",
        destination="A",
        destination_query="A, Finland",
        distance_meters=10_000,
        distance_km=10,
        duration_seconds=1200,
        duration_text="20 min",
        summary_text="10 km · 20 min (julkiset)",
    )
    monkeypatch.setattr(
        transit_module,
        "fetch_cached_transit_distances",
        lambda *a, **k: {"A, Finland": cached_result},
    )
    monkeypatch.setattr(transit_module, "fetch_recent_transit_failures", lambda *a, **k: set())
    transit_module.mark_transit_provider_backoff(settings, "429")
    monkeypatch.setattr(
        transit_module,
        "compute_transit_distance",
        lambda *a, **k: pytest.fail("must not call provider during backoff"),
    )

    result = transit_module.resolve_transit_for_queries(
        object(), ["A, Finland", "B, Finland"], max_lookups=5
    )

    assert result["A, Finland"] is cached_result
    assert result.provider_backoff is True
    assert result.not_attempted_queries == {"B, Finland"}


def test_transit_http_call_happens_outside_a_transaction(monkeypatch, tmp_path):
    from app import transit_distance as transit_module

    settings = _transit_settings(tmp_path)
    monkeypatch.setattr(transit_module, "get_settings", lambda: settings)
    monkeypatch.setattr(transit_module, "fetch_cached_transit_distances", lambda *a, **k: {})
    monkeypatch.setattr(transit_module, "fetch_recent_transit_failures", lambda *a, **k: set())
    monkeypatch.setattr(transit_module, "store_cached_transit_distance", lambda *a, **k: None)
    monkeypatch.setattr(transit_module, "store_transit_failure", lambda *a, **k: None)

    class TrackingConnection:
        def __init__(self) -> None:
            self.transaction_open = False

        def commit(self) -> None:
            self.transaction_open = False

        def in_transaction(self) -> bool:
            return self.transaction_open

    connection = TrackingConnection()
    seen: list[bool] = []

    def fake_compute(destination, *, origin=None):  # noqa: ANN001, ARG001
        seen.append(connection.in_transaction())
        return transit_module.TransitDistanceResult(
            origin="o",
            destination=destination,
            destination_query=f"{destination}, Finland",
            distance_meters=1000,
            distance_km=1,
            duration_seconds=60,
            duration_text="1 min",
            summary_text="1 km · 1 min (julkiset)",
        )

    monkeypatch.setattr(transit_module, "compute_transit_distance", fake_compute)

    transit_module.resolve_transit_for_queries(
        connection, ["A, Finland", "B, Finland"], max_lookups=5
    )

    assert seen and all(state is False for state in seen)
