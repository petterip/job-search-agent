#!/usr/bin/env python3
"""Opt-in live probe for LinkedIn guest job search markup."""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.adapters.linkedin import LINKEDIN_GUEST_SEARCH_URL, parse_linkedin_guest_jobs

UA = {"User-Agent": "job-search-agent-probe/0.1 (+local research project)"}


def main() -> int:
    params = urllib.parse.urlencode(
        {
            "keywords": "kirjasto",
            "location": "Finland",
            "f_TPR": "r604800",
            "start": 0,
        }
    )
    request = urllib.request.Request(f"{LINKEDIN_GUEST_SEARCH_URL}?{params}", headers=UA)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            body = response.read().decode("utf-8", "replace")
    except Exception as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False))
        return 1
    if status != 200:
        print(json.dumps({"status": "failed", "reason": f"HTTP {status}"}, ensure_ascii=False))
        return 1
    jobs = parse_linkedin_guest_jobs(body)
    if not jobs:
        reason = "challenge or zero jobs" if "authwall" in body.lower() or "challenge" in body.lower() else "zero jobs"
        print(json.dumps({"status": "failed", "reason": reason}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "ok", "count": len(jobs), "sample": jobs[:3]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
