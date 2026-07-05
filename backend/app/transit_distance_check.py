from __future__ import annotations

import argparse
import json
import sys

from app.config import get_settings
from app.transit_distance import (
    TransitDistanceError,
    compute_transit_distance,
    normalize_destination,
)


DEFAULT_SAMPLES = (
    "Helsinki",
    "Rovaniemi",
    "Oulu",
    "Suomi",
    "Suomi / Etä",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Proof-of-concept check for Google Routes API transit distance "
            "from the configured home address."
        ),
    )
    parser.add_argument(
        "destinations",
        nargs="*",
        help="Job locations to route (default: sample Finnish cities)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.google_maps_configured():
        print("GOOGLE_MAPS_API_KEY is not set", file=sys.stderr)
        return 1

    destinations = args.destinations or list(DEFAULT_SAMPLES)
    results: list[dict[str, object]] = []
    routing_failures = 0

    for destination in destinations:
        normalized = normalize_destination(destination)
        entry: dict[str, object] = {
            "destination_input": destination,
            "destination_query": normalized,
        }
        if normalized is None:
            entry["ok"] = False
            entry["expected"] = True
            entry["error"] = "Destination too vague for transit routing"
            results.append(entry)
            continue
        try:
            result = compute_transit_distance(destination)
        except TransitDistanceError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            routing_failures += 1
        else:
            entry["ok"] = True
            entry["origin"] = result.origin
            entry["distance_km"] = result.distance_km
            entry["duration_text"] = result.duration_text
            entry["summary_text"] = result.summary_text
        results.append(entry)

    payload = {
        "ok": routing_failures == 0,
        "origin": settings.transit_origin_address,
        "results": results,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Transit distance PoC from {settings.transit_origin_address}")
        for entry in results:
            label = entry["destination_input"]
            if entry.get("ok"):
                print(
                    f"  OK  {label}: {entry['summary_text']} "
                    f"(query={entry['destination_query']})"
                )
            elif entry.get("expected"):
                print(f"  SKIP {label}: {entry['error']}")
            else:
                print(f"  FAIL {label}: {entry['error']}")
        if routing_failures:
            print(
                "\nNote: server-side routing needs Google Routes API enabled and a key "
                "without HTTP referrer restrictions.",
                file=sys.stderr,
            )

    return 0 if routing_failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
