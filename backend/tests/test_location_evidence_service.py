from unittest.mock import MagicMock

import sqlalchemy as sa

from app.location_evidence_service import (
    LocationEvidenceView,
    enrich_location_evidence,
)
from app.transit_distance import (
    LocationEvidence,
    TransitDistanceResult,
    apply_location_evidence_to_concerns,
    build_location_evidence,
    fetch_cached_transit_distances,
    routing_departure_time,
)


class _RecommendationStub:
    def __init__(
        self,
        *,
        location: str | None,
        location_evidence: LocationEvidenceView | None = None,
    ) -> None:
        self.location = location
        self.location_evidence = location_evidence

    def model_copy(self, *, update):
        return _RecommendationStub(
            location=update.get("location", self.location),
            location_evidence=update.get("location_evidence", self.location_evidence),
        )


def test_routing_departure_time_uses_weekday_morning():
    departure = routing_departure_time(when=None)
    assert departure.tzinfo is not None
    assert departure.weekday() < 5


def test_apply_location_evidence_does_not_duplicate_existing_text():
    evidence = LocationEvidence(text="Helsinki · 690 km · 7 h 13 min (julkiset)", tone="bad")
    concerns = ["Helsinki · 690 km · 7 h 13 min (julkiset)"]
    assert apply_location_evidence_to_concerns(concerns, evidence) == concerns


def test_build_location_evidence_remote_skips_transit_lookup_message():
    evidence = build_location_evidence("Helsinki / Etä", None)
    assert evidence is not None
    assert evidence.tone == "good"
    assert "joustava" in evidence.text


def test_enrich_location_evidence_prefers_fresh_transit_over_stored(monkeypatch):
    connection = MagicMock()
    stored = LocationEvidenceView(
        text="Helsinki · julkisen liikenteen matka Oulusta ei tiedossa",
        tone="warning",
    )
    fresh = LocationEvidence(
        text="Helsinki · 687 km · 7 h 18 min (julkiset)",
        tone="bad",
    )
    monkeypatch.setattr(
        "app.location_evidence_service.resolve_transit_for_locations",
        lambda *_args, **_kwargs: {
            "Helsinki": TransitDistanceResult(
                origin="Jalkatie 2, Oulu, Finland",
                destination="Helsinki",
                destination_query="Helsinki, Finland",
                distance_meters=687_000,
                distance_km=687,
                duration_seconds=26_000,
                duration_text="7 h 18 min",
                summary_text="687 km · 7 h 18 min (julkiset)",
            )
        },
    )
    monkeypatch.setattr(
        "app.location_evidence_service.build_location_evidence",
        lambda _location, _transit: fresh,
    )

    enriched = enrich_location_evidence(
        connection,
        [_RecommendationStub(location="Helsinki", location_evidence=stored)],
        max_lookups=1,
    )

    assert enriched[0].location_evidence == LocationEvidenceView(
        text=fresh.text,
        tone=fresh.tone,
    )


def test_fetch_cached_transit_distances_uses_savepoint_instead_of_full_rollback():
    connection = MagicMock()
    connection.begin_nested.return_value.__enter__.side_effect = sa.exc.ProgrammingError(
        "select",
        {},
        Exception("missing table"),
    )

    assert fetch_cached_transit_distances(
        connection,
        origin_address="Jalkatie 2, Oulu, Finland",
        destination_queries=["Helsinki, Finland"],
    ) == {}
    connection.rollback.assert_not_called()
