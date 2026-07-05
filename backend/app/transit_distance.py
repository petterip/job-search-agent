from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Literal

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import get_settings

logger = logging.getLogger(__name__)

ROUTES_COMPUTE_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
DEFAULT_ORIGIN = "Jalkatie 2, Oulu, Finland"
DURATION_RE = re.compile(r"^(\d+)s$")
ROUTES_ENABLE_URL = (
    "https://console.developers.google.com/apis/api/routes.googleapis.com/overview"
)
EvidenceTone = Literal["good", "warning", "bad"]


@dataclass(frozen=True)
class TransitDistanceResult:
    origin: str
    destination: str
    destination_query: str
    distance_meters: int
    distance_km: int
    duration_seconds: int
    duration_text: str
    summary_text: str


class TransitDistanceError(RuntimeError):
    pass


def routing_departure_time(*, when: datetime | None = None) -> datetime:
    """Use a stable weekday 08:00 departure so cache entries stay comparable."""
    helsinki = ZoneInfo("Europe/Helsinki")
    if when is not None:
        return when.astimezone(timezone.utc)
    local_now = datetime.now(helsinki)
    departure = local_now.replace(hour=8, minute=0, second=0, microsecond=0)
    while departure.weekday() >= 5 or departure <= local_now:
        departure = (departure + timedelta(days=1)).replace(
            hour=8,
            minute=0,
            second=0,
            microsecond=0,
        )
    return departure.astimezone(timezone.utc)


def parse_duration_seconds(value: str | None) -> int:
    if not value:
        raise TransitDistanceError("Google Routes API did not return a transit duration")
    match = DURATION_RE.match(value.strip())
    if not match:
        raise TransitDistanceError(f"Unexpected duration format: {value!r}")
    return int(match.group(1))


def format_duration_fi(seconds: int) -> str:
    minutes = max(1, (seconds + 59) // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    rem = minutes % 60
    if rem == 0:
        return f"{hours} h"
    return f"{hours} h {rem} min"


def format_distance_km(distance_meters: int) -> int:
    return max(1, round(distance_meters / 1000))


def format_transit_summary(*, distance_km: int, duration_text: str) -> str:
    return f"{distance_km} km · {duration_text} (julkiset)"


def normalize_destination(location: str | None) -> str | None:
    if not location:
        return None
    text = location.strip()
    lowered = text.casefold()
    for marker in ("/ etä", "/ hybridi"):
        if marker in lowered:
            idx = lowered.index(marker)
            text = text[:idx].strip().rstrip(",")
            lowered = text.casefold()
            break
    if not text:
        return None
    if lowered in {"suomi", "finland", "fi"}:
        return None
    if "finland" not in lowered and "suomi" not in lowered:
        text = f"{text}, Finland"
    return text


def _routes_error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if not isinstance(error, dict):
        return "Google Routes API request failed"
    message = str(error.get("message") or "Google Routes API request failed")
    details = error.get("details")
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            if isinstance(metadata, dict) and metadata.get("activationUrl"):
                return f"{message} Enable Routes API: {metadata['activationUrl']}"
    if "Routes API has not been used" in message or "SERVICE_DISABLED" in str(error.get("status", "")):
        return f"{message} Enable Routes API: {ROUTES_ENABLE_URL}"
    return message


def compute_transit_distance(
    destination: str,
    *,
    origin: str | None = None,
    api_key: str | None = None,
    departure_time: datetime | None = None,
    client: httpx.Client | None = None,
) -> TransitDistanceResult:
    settings = get_settings()
    resolved_origin = origin or settings.transit_origin_address or DEFAULT_ORIGIN
    resolved_key = (api_key or settings.google_maps_api_key).strip()
    if not resolved_key:
        raise TransitDistanceError("GOOGLE_MAPS_API_KEY is not configured")

    destination_query = normalize_destination(destination)
    if destination_query is None:
        raise TransitDistanceError(
            f"Destination {destination!r} is too vague for transit routing; need a city or address"
        )

    when = departure_time or routing_departure_time()
    body = {
        "origin": {"address": resolved_origin},
        "destination": {"address": destination_query},
        "travelMode": "TRANSIT",
        "languageCode": "fi",
        "departureTime": when.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": resolved_key,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }

    if client is None:
        with httpx.Client(timeout=30) as owned_client:
            response = owned_client.post(ROUTES_COMPUTE_URL, headers=headers, json=body)
    else:
        response = client.post(ROUTES_COMPUTE_URL, headers=headers, json=body)

    try:
        payload = response.json()
    except ValueError as exc:
        raise TransitDistanceError("Google Routes API returned invalid JSON") from exc

    if response.status_code >= 400:
        raise TransitDistanceError(_routes_error_message(payload if isinstance(payload, dict) else {}))

    if not isinstance(payload, dict):
        raise TransitDistanceError("Google Routes API returned an unexpected payload")

    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes:
        raise TransitDistanceError(
            f"No public transit route found from {resolved_origin!r} to {destination_query!r}"
        )

    route = routes[0]
    if not isinstance(route, dict):
        raise TransitDistanceError("Google Routes API returned an unexpected route payload")

    distance_meters = int(route.get("distanceMeters") or 0)
    if distance_meters <= 0:
        raise TransitDistanceError("Google Routes API returned zero distance")

    duration_seconds = parse_duration_seconds(route.get("duration"))
    duration_text = format_duration_fi(duration_seconds)
    distance_km = format_distance_km(distance_meters)
    summary_text = format_transit_summary(distance_km=distance_km, duration_text=duration_text)

    return TransitDistanceResult(
        origin=resolved_origin,
        destination=destination,
        destination_query=destination_query,
        distance_meters=distance_meters,
        distance_km=distance_km,
        duration_seconds=duration_seconds,
        duration_text=duration_text,
        summary_text=summary_text,
    )


@dataclass(frozen=True)
class LocationEvidence:
    text: str
    tone: EvidenceTone


def location_work_mode(location: str | None) -> Literal["remote", "hybrid"] | None:
    normalized = (location or "").casefold()
    if "/ etä" in normalized or normalized.endswith(" etä"):
        return "remote"
    if "/ hybridi" in normalized:
        return "hybrid"
    return None


def location_display_label(location: str | None, destination_query: str | None = None) -> str | None:
    if destination_query:
        label = destination_query.removesuffix(", Finland").removesuffix(", Suomi").strip()
        if label:
            return label
    if not location:
        return None
    first = location.split("/")[0].split(",")[0].strip()
    return first or None


def build_location_evidence(
    location: str | None,
    transit: TransitDistanceResult | None,
) -> LocationEvidence | None:
    mode = location_work_mode(location)
    label = location_display_label(location, transit.destination_query if transit else None)

    if mode == "remote":
        text = f"{label} / etä · sijainti joustava" if label else "Etätyö · sijainti joustava"
        return LocationEvidence(text=text, tone="good")

    if transit is None:
        if not location:
            return None
        return LocationEvidence(
            text=f"{location} · julkisen liikenteen matka Oulusta ei tiedossa",
            tone="warning",
        )

    label = label or transit.destination_query.removesuffix(", Finland")
    km = transit.distance_km
    summary = transit.summary_text

    if km <= 25:
        return LocationEvidence(
            text=f"{label} · Oulun seutu · {transit.duration_text} (julkiset)",
            tone="good",
        )
    if mode == "hybrid":
        tone: EvidenceTone = "warning" if km <= 250 else "bad"
        return LocationEvidence(text=f"{label} · {summary}, hybridityö", tone=tone)
    tone = "warning" if km <= 180 else "bad"
    return LocationEvidence(text=f"{label} · {summary}", tone=tone)


def apply_location_evidence_to_concerns(
    concerns: list[str],
    evidence: LocationEvidence | None,
) -> list[str]:
    if evidence is None:
        return concerns
    updated: list[str] = []
    replaced = False
    for concern in concerns:
        if re.search(r"sijainti ei osu", concern, re.I):
            updated.append(evidence.text)
            replaced = True
        else:
            updated.append(concern)
    if not replaced and evidence.tone != "good":
        if evidence.text not in updated:
            updated.append(evidence.text)
    return updated


def _row_to_result(row: sa.RowMapping, *, destination: str) -> TransitDistanceResult:
    return TransitDistanceResult(
        origin=str(row["origin_address"]),
        destination=destination,
        destination_query=str(row["destination_query"]),
        distance_meters=int(row["distance_meters"]),
        distance_km=int(row["distance_km"]),
        duration_seconds=int(row["duration_seconds"]),
        duration_text=str(row["duration_text"]),
        summary_text=str(row["summary_text"]),
    )


def _run_cache_query(connection: Connection, query, *, default):
    try:
        with connection.begin_nested():
            return query()
    except sa.exc.ProgrammingError:
        logger.warning("event=transit_distance_cache_unavailable")
        return default


def _run_cache_statement(connection: Connection, statement) -> bool:
    try:
        with connection.begin_nested():
            statement()
        return True
    except sa.exc.ProgrammingError:
        logger.warning("event=transit_distance_cache_unavailable")
        return False


def fetch_cached_transit_distances(
    connection: Connection,
    *,
    origin_address: str,
    destination_queries: list[str],
) -> dict[str, TransitDistanceResult]:
    if not destination_queries:
        return {}

    def _query():
        return connection.execute(
            sa.text(
                """
                select
                    destination_query,
                    origin_address,
                    distance_meters,
                    distance_km,
                    duration_seconds,
                    duration_text,
                    summary_text
                from transit_distance_cache
                where origin_address = :origin_address
                  and destination_query = any(:destination_queries)
                """
            ),
            {"origin_address": origin_address, "destination_queries": destination_queries},
        ).mappings()

    rows = _run_cache_query(connection, _query, default=None)
    if rows is None:
        return {}
    return {
        str(row["destination_query"]): _row_to_result(
            row,
            destination=str(row["destination_query"]).removesuffix(", Finland"),
        )
        for row in rows
    }


def fetch_recent_transit_failures(
    connection: Connection,
    *,
    origin_address: str,
    destination_queries: list[str],
) -> set[str]:
    if not destination_queries:
        return set()

    def _query():
        return connection.execute(
            sa.text(
                """
                select destination_query
                from transit_distance_failures
                where origin_address = :origin_address
                  and destination_query = any(:destination_queries)
                  and failed_at >= now() - interval '7 days'
                """
            ),
            {"origin_address": origin_address, "destination_queries": destination_queries},
        ).scalars()

    rows = _run_cache_query(connection, _query, default=None)
    if rows is None:
        return set()
    return {str(value) for value in rows}


def store_cached_transit_distance(connection: Connection, result: TransitDistanceResult) -> None:
    def _write() -> None:
        connection.execute(
            sa.text(
                """
                insert into transit_distance_cache (
                    destination_query,
                    origin_address,
                    distance_meters,
                    distance_km,
                    duration_seconds,
                    duration_text,
                    summary_text,
                    fetched_at
                )
                values (
                    :destination_query,
                    :origin_address,
                    :distance_meters,
                    :distance_km,
                    :duration_seconds,
                    :duration_text,
                    :summary_text,
                    now()
                )
                on conflict (destination_query, origin_address)
                do update set
                    distance_meters = excluded.distance_meters,
                    distance_km = excluded.distance_km,
                    duration_seconds = excluded.duration_seconds,
                    duration_text = excluded.duration_text,
                    summary_text = excluded.summary_text,
                    fetched_at = excluded.fetched_at
                """
            ),
            {
                "destination_query": result.destination_query,
                "origin_address": result.origin,
                "distance_meters": result.distance_meters,
                "distance_km": result.distance_km,
                "duration_seconds": result.duration_seconds,
                "duration_text": result.duration_text,
                "summary_text": result.summary_text,
            },
        )
        connection.execute(
            sa.text(
                """
                delete from transit_distance_failures
                where destination_query = :destination_query
                  and origin_address = :origin_address
                """
            ),
            {
                "destination_query": result.destination_query,
                "origin_address": result.origin,
            },
        )

    if not _run_cache_statement(connection, _write):
        logger.warning("event=transit_distance_cache_write_failed")


def store_transit_failure(
    connection: Connection,
    *,
    origin_address: str,
    destination_query: str,
    reason: str,
) -> None:
    def _write() -> None:
        connection.execute(
            sa.text(
                """
                insert into transit_distance_failures (
                    destination_query,
                    origin_address,
                    reason,
                    failed_at
                )
                values (
                    :destination_query,
                    :origin_address,
                    :reason,
                    now()
                )
                on conflict (destination_query, origin_address)
                do update set
                    reason = excluded.reason,
                    failed_at = excluded.failed_at
                """
            ),
            {
                "destination_query": destination_query,
                "origin_address": origin_address,
                "reason": reason[:500],
            },
        )

    if not _run_cache_statement(connection, _write):
        logger.warning("event=transit_distance_failure_cache_write_failed")


def resolve_transit_for_locations(
    connection: Connection,
    locations: list[str | None],
    *,
    max_lookups: int = 20,
) -> dict[str, TransitDistanceResult | None]:
    settings = get_settings()
    if not settings.google_maps_configured():
        return {}

    origin = settings.transit_origin_address or DEFAULT_ORIGIN
    query_to_location: dict[str, str] = {}
    for location in locations:
        if not location:
            continue
        query = normalize_destination(location)
        if query:
            query_to_location.setdefault(query, location)

    if not query_to_location:
        return {}

    cached = fetch_cached_transit_distances(
        connection,
        origin_address=origin,
        destination_queries=list(query_to_location.keys()),
    )
    recent_failures = fetch_recent_transit_failures(
        connection,
        origin_address=origin,
        destination_queries=list(query_to_location.keys()),
    )
    results: dict[str, TransitDistanceResult | None] = {}
    lookups = 0
    for query, location in query_to_location.items():
        if query in cached:
            results[location] = cached[query]
            continue
        if query in recent_failures:
            results[location] = None
            continue
        if lookups >= max_lookups:
            results[location] = None
            continue
        try:
            transit = compute_transit_distance(location, origin=origin)
        except TransitDistanceError as exc:
            logger.info("event=transit_distance_lookup_failed location=%s error=%s", location, exc)
            store_transit_failure(
                connection,
                origin_address=origin,
                destination_query=query,
                reason=str(exc),
            )
            results[location] = None
            continue
        store_cached_transit_distance(connection, transit)
        results[location] = transit
        lookups += 1
    return results

