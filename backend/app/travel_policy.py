from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from app.transit_distance import (
    TransitDistanceResult,
    format_transit_summary,
    location_display_label,
    location_work_mode,
    normalize_destination,
)

TravelStatus = Literal[
    "full_remote",
    "hybrid",
    "exact_home_city",
    "within_limit",
    "over_limit",
    "unknown",
    "unrouteable",
    "vague",
]
EvidenceTone = Literal["good", "warning", "bad"]
ROUTING_PROFILE = "weekday_0800_public_transit"

VAGUE_LOCATION_MARKERS = {"suomi", "finland", "fi"}
WEAK_REMOTE_MARKERS = (
    "etätyömahdollisuus",
    "mahdollisuus etä",
    "osittain etä",
    "osittainen etä",
    "hybridi",
    "hybrid",
)


@dataclass(frozen=True)
class TravelAssessment:
    commutable: bool
    full_remote: bool
    status: TravelStatus
    duration_seconds: int | None
    distance_km: int | None
    score_adjustment: int
    evidence_text: str
    tone: EvidenceTone
    reason_code: str
    origin_address: str
    commute_limit_minutes: int
    routing_profile: str = ROUTING_PROFILE

    @property
    def commutable_or_full_remote(self) -> bool:
        return self.commutable or self.full_remote

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "commutable": self.commutable,
            "full_remote": self.full_remote,
            "commutable_or_full_remote": self.commutable_or_full_remote,
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "distance_km": self.distance_km,
            "score_adjustment": self.score_adjustment,
            "evidence_text": self.evidence_text,
            "tone": self.tone,
            "reason_code": self.reason_code,
            "origin_address": self.origin_address,
            "commute_limit_minutes": self.commute_limit_minutes,
            "routing_profile": self.routing_profile,
        }


def home_city_from_profile(profile: dict[str, Any]) -> str | None:
    location = profile.get("location")
    if not isinstance(location, dict):
        return None
    home_city = location.get("home_city")
    if not isinstance(home_city, str) or not home_city.strip():
        return None
    return home_city.strip()


def normalize_city_name(value: str) -> str:
    return value.split("/")[0].split(",")[0].strip().casefold()


def is_exact_home_city(location: str | None, home_city: str | None) -> bool:
    if not location or not home_city:
        return False
    return normalize_city_name(location) == home_city.strip().casefold()


def is_verified_full_remote(location: str | None) -> bool:
    """True only for listings verified as full-time remote, not hybrid or vague flexibility."""
    if not location or not location.strip():
        return False
    normalized = location.casefold()
    if any(marker in normalized for marker in WEAK_REMOTE_MARKERS):
        return False
    if "/ hybridi" in normalized:
        return False
    stripped = normalized.strip().strip(".")
    if stripped in {"etä", "etätyö", "kokopäiväinen etätyö", "remote", "etätyö koko työaika"}:
        return True
    if "/ etä" in normalized:
        return True
    return location_work_mode(location) == "remote"


def extract_destination_candidates(location: str | None) -> list[str]:
    """Return normalized destination queries for transit lookup."""
    if not location or not location.strip():
        return []

    work_mode = location_work_mode(location)
    text = location.strip()
    lowered = text.casefold()

    for marker in ("/ etä", "/ hybridi"):
        if marker in lowered:
            idx = lowered.index(marker)
            text = text[:idx].strip().rstrip(",")
            lowered = text.casefold()
            break

    if not text:
        return []

    if lowered in VAGUE_LOCATION_MARKERS:
        return []

    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        return []

    queries: list[str] = []
    seen: set[str] = set()
    for part in parts:
        part_lower = part.casefold()
        if part_lower in VAGUE_LOCATION_MARKERS:
            continue
        if work_mode == "remote" and part_lower in {"etä", "eta", "remote"}:
            continue
        query = normalize_destination(part)
        if query and query not in seen:
            seen.add(query)
            queries.append(query)
    return queries


def _duration_score_adjustment(duration_seconds: int) -> int:
    minutes = duration_seconds / 60
    if minutes <= 45:
        return 10
    if minutes <= 90:
        return 6
    return 3


def _best_transit(
    destination_queries: list[str],
    transit_by_destination: Mapping[str, TransitDistanceResult | None],
) -> tuple[TransitDistanceResult | None, str | None]:
    best: TransitDistanceResult | None = None
    best_query: str | None = None
    best_duration: int | None = None
    for query in destination_queries:
        transit = transit_by_destination.get(query)
        if transit is None:
            continue
        if best_duration is None or transit.duration_seconds < best_duration:
            best = transit
            best_query = query
            best_duration = transit.duration_seconds
    return best, best_query


def _evidence_for_transit(
    *,
    location: str | None,
    transit: TransitDistanceResult,
    destination_query: str | None,
    work_mode: Literal["remote", "hybrid"] | None,
) -> tuple[str, EvidenceTone]:
    label = location_display_label(location, destination_query or transit.destination_query)
    label = label or transit.destination_query.removesuffix(", Finland")
    summary = format_transit_summary(
        distance_km=transit.distance_km,
        duration_text=transit.duration_text,
    )
    if work_mode == "hybrid":
        tone: EvidenceTone = "warning" if transit.distance_km <= 250 else "bad"
        return f"{label} · {summary}, hybridityö", tone
    tone = "good" if transit.duration_seconds <= 90 * 60 else "warning"
    if transit.distance_km > 180:
        tone = "bad"
    return f"{label} · {summary}", tone


def _assessment(
    *,
    commutable: bool,
    full_remote: bool,
    status: TravelStatus,
    duration_seconds: int | None,
    distance_km: int | None,
    score_adjustment: int,
    evidence_text: str,
    tone: EvidenceTone,
    reason_code: str,
    origin_address: str,
    commute_limit_minutes: int,
) -> TravelAssessment:
    return TravelAssessment(
        commutable=commutable,
        full_remote=full_remote,
        status=status,
        duration_seconds=duration_seconds,
        distance_km=distance_km,
        score_adjustment=score_adjustment,
        evidence_text=evidence_text,
        tone=tone,
        reason_code=reason_code,
        origin_address=origin_address,
        commute_limit_minutes=commute_limit_minutes,
    )


def assess_travel(
    *,
    profile: dict[str, Any],
    location: str | None,
    transit_by_destination: Mapping[str, TransitDistanceResult | None],
    origin_address: str,
    commute_limit_minutes: int,
    maps_available: bool = True,
) -> TravelAssessment:
    home_city = home_city_from_profile(profile)
    work_mode = location_work_mode(location)
    verified_full_remote = is_verified_full_remote(location)
    limit_seconds = commute_limit_minutes * 60
    common = {
        "origin_address": origin_address,
        "commute_limit_minutes": commute_limit_minutes,
    }

    if is_exact_home_city(location, home_city):
        label = location_display_label(location) or home_city or "Kotikaupunki"
        return _assessment(
            commutable=True,
            full_remote=verified_full_remote,
            status="exact_home_city",
            duration_seconds=None,
            distance_km=None,
            score_adjustment=12,
            evidence_text=f"{label} · kotikaupunki",
            tone="good",
            reason_code="exact_home_city",
            **common,
        )

    if verified_full_remote:
        label = location_display_label(location) or "Etätyö"
        return _assessment(
            commutable=False,
            full_remote=True,
            status="full_remote",
            duration_seconds=None,
            distance_km=None,
            score_adjustment=15,
            evidence_text=f"{label} · kokopäiväinen etätyö",
            tone="good",
            reason_code="full_remote",
            **common,
        )

    destination_queries = extract_destination_candidates(location)
    if not destination_queries:
        text = location.strip() if location else "Sijainti ei tiedossa"
        return _assessment(
            commutable=False,
            full_remote=False,
            status="vague",
            duration_seconds=None,
            distance_km=None,
            score_adjustment=-5,
            evidence_text=f"{text} · sijainti epäselvä",
            tone="warning",
            reason_code="vague",
            **common,
        )

    best_transit, best_query = _best_transit(destination_queries, transit_by_destination)
    if best_transit is None:
        label = location_display_label(location) or location or "Kohde"
        if not maps_available:
            return _assessment(
                commutable=False,
                full_remote=False,
                status="unknown",
                duration_seconds=None,
                distance_km=None,
                score_adjustment=-5,
                evidence_text=f"{label} · julkisen liikenteen matka ei tiedossa (reititys ei käytössä)",
                tone="warning",
                reason_code="transit_unavailable",
                **common,
            )
        had_lookup = any(query in transit_by_destination for query in destination_queries)
        status: TravelStatus = "unrouteable" if had_lookup else "unknown"
        reason_code = "transit_unrouteable" if had_lookup else "transit_unknown"
        return _assessment(
            commutable=False,
            full_remote=False,
            status=status,
            duration_seconds=None,
            distance_km=None,
            score_adjustment=-5,
            evidence_text=f"{label} · julkisen liikenteen matka ei tiedossa",
            tone="warning",
            reason_code=reason_code,
            **common,
        )

    duration_seconds = best_transit.duration_seconds
    distance_km = best_transit.distance_km
    evidence_text, tone = _evidence_for_transit(
        location=location,
        transit=best_transit,
        destination_query=best_query,
        work_mode=work_mode,
    )

    if duration_seconds <= limit_seconds:
        return _assessment(
            commutable=True,
            full_remote=False,
            status="within_limit",
            duration_seconds=duration_seconds,
            distance_km=distance_km,
            score_adjustment=_duration_score_adjustment(duration_seconds),
            evidence_text=evidence_text,
            tone=tone if tone != "bad" else "good",
            reason_code="transit_within_limit",
            **common,
        )

    if work_mode == "hybrid":
        return _assessment(
            commutable=False,
            full_remote=False,
            status="hybrid",
            duration_seconds=duration_seconds,
            distance_km=distance_km,
            score_adjustment=-8,
            evidence_text=evidence_text,
            tone="warning",
            reason_code="transit_over_limit_hybrid",
            **common,
        )

    return _assessment(
        commutable=False,
        full_remote=False,
        status="over_limit",
        duration_seconds=duration_seconds,
        distance_km=distance_km,
        score_adjustment=-15,
        evidence_text=evidence_text,
        tone="bad",
        reason_code="transit_over_limit",
        **common,
    )
