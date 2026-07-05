from __future__ import annotations

from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel
from sqlalchemy.engine import Connection

from app.transit_distance import (
    LocationEvidence,
    build_location_evidence,
    resolve_transit_for_locations,
)

EvidenceTone = Literal["good", "warning", "bad"]


class LocationEvidenceView(BaseModel):
    text: str
    tone: EvidenceTone


class SupportsLocationEvidence(Protocol):
    location: str | None
    location_evidence: LocationEvidenceView | None


T = TypeVar("T", bound=SupportsLocationEvidence)


def location_evidence_from_deterministic_result(
    deterministic_result: dict[str, Any] | None,
) -> LocationEvidenceView | None:
    if not isinstance(deterministic_result, dict):
        return None
    stored = deterministic_result.get("location_evidence")
    if not isinstance(stored, dict):
        return None
    text = stored.get("text")
    tone = stored.get("tone")
    if not isinstance(text, str) or not text.strip():
        return None
    if tone not in {"good", "warning", "bad"}:
        return None
    return LocationEvidenceView(text=text, tone=tone)


def to_location_evidence_view(evidence: LocationEvidence | None) -> LocationEvidenceView | None:
    if evidence is None:
        return None
    return LocationEvidenceView(text=evidence.text, tone=evidence.tone)


def resolve_location_evidence_for_job(
    connection: Connection,
    location: str | None,
    *,
    fallback: LocationEvidenceView | None = None,
    max_lookups: int = 1,
) -> LocationEvidenceView | None:
    if not location:
        return fallback
    transit_map = resolve_transit_for_locations(
        connection,
        [location],
        max_lookups=max_lookups,
    )
    fresh = to_location_evidence_view(
        build_location_evidence(location, transit_map.get(location))
    )
    return fresh or fallback


def enrich_location_evidence(
    connection: Connection,
    items: list[T],
    *,
    max_lookups: int = 20,
) -> list[T]:
    locations = [item.location for item in items if item.location]
    if not locations:
        return items

    transit_map = resolve_transit_for_locations(
        connection,
        locations,
        max_lookups=max_lookups,
    )
    enriched: list[T] = []
    for item in items:
        if not item.location:
            enriched.append(item)
            continue
        fresh = to_location_evidence_view(
            build_location_evidence(item.location, transit_map.get(item.location))
        )
        payload = fresh or item.location_evidence
        if payload is item.location_evidence:
            enriched.append(item)
        else:
            enriched.append(item.model_copy(update={"location_evidence": payload}))
    return enriched
