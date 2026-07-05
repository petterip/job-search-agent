from __future__ import annotations

import argparse
import json
import sys

from app.browserbase_client import SESSION_DASHBOARD_URL, verify_browserbase_access
from app.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Browserbase API access by creating and releasing a cloud session.",
    )
    parser.add_argument(
        "--region",
        default="eu-central-1",
        help="Browserbase region (default: eu-central-1)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.browserbase_configured():
        print("BROWSERBASE_API_KEY is not set", file=sys.stderr)
        return 1

    try:
        session = verify_browserbase_access(region=args.region)
    except Exception as exc:
        print(f"Browserbase verification failed: {exc}", file=sys.stderr)
        return 1

    payload = {
        "ok": True,
        "session_id": session.session_id,
        "dashboard_url": SESSION_DASHBOARD_URL.format(session_id=session.session_id),
        "region": session.region,
        "enrichment_provider": settings.enrichment_provider,
        "enrichment_enabled": settings.enrichment_enabled,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print("Browserbase access OK")
        print(f"  region: {payload['region']}")
        print(f"  session: {payload['session_id']}")
        print(f"  dashboard: {payload['dashboard_url']}")
        print(f"  enrichment_enabled: {payload['enrichment_enabled']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
