#!/usr/bin/env python3
"""Incremental fetch strategies — uses shared http_client, dynamic dates."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.http_client import fetch_json, post_json

TODAY_UTC = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
TODAY_ISO = TODAY_UTC.strftime("%Y-%m-%dT00:00:00")
TODAY_Z = TODAY_UTC.strftime("%Y-%m-%dT00:00:00.000Z")
# Watermark ~24h ago for Duunitori page-walk demo
WATERMARK = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000000+00:00")


def duunitori_incremental() -> None:
    print("=== 1. Duunitori: ordering=-date_posted page walk ===")
    j1, status, _ = fetch_json(
        "https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page=1&page_size=10",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    if status != 200 or not j1.get("results"):
        print(f"  SKIP: HTTP {status}")
        return
    print(f"  Newest: {j1['results'][0]['date_posted']}")

    capped, cap_status, _ = fetch_json(
        "https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page=1&page_size=200",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    print(f"  page_size=200 → HTTP {cap_status}, results={len(capped.get('results') or [])} (expect 100)")

    new_jobs = []
    for page in range(1, 6):
        j, _, _ = fetch_json(
            f"https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page={page}&page_size=20",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if not j.get("results"):
            break
        for row in j["results"]:
            if row["date_posted"] <= WATERMARK:
                print(f"  Stop at page {page}: {len(new_jobs)} jobs since watermark")
                return
            new_jobs.append(row)
        time.sleep(0.2)
    print(f"  Jobs newer than watermark: {len(new_jobs)}")


def laura_incremental() -> None:
    print("\n=== 2. Laura: after= vs modified_after= ===")
    after_rows, s1, h1 = fetch_json(
        f"https://laura.fi/wp-json/wp/v2/job-listings?per_page=100&orderby=date&order=desc&after={TODAY_ISO}"
    )
    mod_rows, s2, h2 = fetch_json(
        f"https://laura.fi/wp-json/wp/v2/job-listings?per_page=100&orderby=modified&order=desc&modified_after={TODAY_ISO}"
    )
    if s1 == 200 and isinstance(after_rows, list):
        print(f"  after={TODAY_ISO}: {len(after_rows)} (header total {h1.get('x-wp-total', '?')})")
    if s2 == 200 and isinstance(mod_rows, list):
        print(f"  modified_after={TODAY_ISO}: {len(mod_rows)} (header total {h2.get('x-wp-total', '?')})")
    print("  Use after= for new postings; modified_after= catches edits to existing listings.")


def tmt_published_after() -> None:
    print("\n=== 3. TMT: publishedAfter filter variants ===")
    base = "https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search"
    variants = [
        {"publishedAfter": TODAY_Z},
        {"published": {"from": TODAY_Z}},
        {"created": {"from": TODAY_Z}},
    ]
    for filt in variants:
        body = {"query": "", "filters": filt, "paging": {"pageNumber": 0, "pageSize": 5}}
        data, status, _ = post_json(base, body)
        total = data.get("totalElements", data.get("_body", status))
        print(f"  HTTP {status}, {list(filt.keys())}: totalElements={total}")

    _, ok90, _ = post_json(
        base,
        {
            "query": "",
            "filters": {"publishedAfter": TODAY_Z},
            "paging": {"pageNumber": 0, "pageSize": 90},
        },
    )
    print(f"  publishedAfter + pageSize=90 → HTTP {ok90} (expect 200)")

    _, bad, _ = post_json(
        base,
        {
            "query": "",
            "filters": {"publishedAfter": TODAY_Z},
            "paging": {"pageNumber": 0, "pageSize": 99},
        },
    )
    print(f"  publishedAfter + pageSize=99 → HTTP {bad} (expect 400)")


def main() -> int:
    duunitori_incremental()
    laura_incremental()
    tmt_published_after()
    print("\nValmis.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
