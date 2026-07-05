#!/usr/bin/env python3
"""
Regression gate for Finnish job source endpoints.

Exit 0 = all checks passed. Exit 1 = at least one failure.
Requires network access; stdlib only.
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.http_client import fetch_json, fetch_text, post_json

FAILURES: list[str] = []


def ok(name: str) -> None:
    print(f"  OK  {name}")


def fail(name: str, detail: str) -> None:
    print(f"  FAIL {name}: {detail}")
    FAILURES.append(f"{name}: {detail}")


def check_duunitori() -> None:
    print("Duunitori jobentries")
    data, status, _ = fetch_json(
        "https://duunitori.fi/api/v1/jobentries",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    if status != 200:
        fail("duunitori_http", f"HTTP {status}")
        return
    count = data.get("count", 0)
    if count < 10_000:
        fail("duunitori_count", f"count={count} (expected >10000)")
    else:
        ok(f"count={count}")

    results = data.get("results") or []
    if not results or "descr" not in results[0]:
        fail("duunitori_descr", "missing descr in first result")
    else:
        ok("descr field present")

    capped, cap_status, _ = fetch_json(
        "https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page=1&page_size=200",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    capped_count = len(capped.get("results") or [])
    if cap_status == 200 and capped_count == 100:
        ok("page_size=200 silently caps to 100 results")
    else:
        fail("duunitori_page_size_cap", f"HTTP {cap_status}, results={capped_count}")

    search, s2, _ = fetch_json(
        "https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page=1&page_size=1&search=ohjelmoija",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    if s2 != 200 or search.get("count", count) >= count:
        fail("duunitori_search", f"search filter ineffective (count={search.get('count')})")
    else:
        ok(f"search filter works (count={search.get('count')})")

    muni, s3, _ = fetch_json(
        "https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page=1&page_size=1&municipality=Helsinki",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    if s3 == 200 and muni.get("count") == count:
        ok("municipality param ignored (documented behaviour)")
    else:
        fail("duunitori_municipality", "unexpected municipality filter behaviour")


def check_tmt() -> None:
    print("Työmarkkinatori search v2")
    base = "https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search"

    latest, status, _ = post_json(
        base,
        {
            "query": "",
            "filters": {},
            "paging": {"pageNumber": 0, "pageSize": 20},
            "sorting": "LATEST",
        },
    )
    if status != 200:
        fail("tmt_latest_http", f"HTTP {status}")
        return
    total = latest.get("totalElements", 0)
    if total < 10_000:
        fail("tmt_total", f"totalElements={total}")
    else:
        ok(f"LATEST totalElements={total}")

    _, latest90, _ = post_json(
        base,
        {
            "query": "",
            "filters": {},
            "paging": {"pageNumber": 0, "pageSize": 90},
            "sorting": "LATEST",
        },
    )
    if latest90 == 200:
        ok("LATEST pageSize=90 → HTTP 200")
    else:
        fail("tmt_latest_pagesize_90", f"HTTP {latest90}")

    _, bad99, _ = post_json(
        base,
        {
            "query": "",
            "filters": {},
            "paging": {"pageNumber": 0, "pageSize": 99},
            "sorting": "LATEST",
        },
    )
    if bad99 == 400:
        ok("LATEST pageSize=99 → HTTP 400")
    else:
        fail("tmt_latest_pagesize_99", f"expected 400, got {bad99}")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")
    _, pa90, _ = post_json(
        base,
        {
            "query": "",
            "filters": {"publishedAfter": today},
            "paging": {"pageNumber": 0, "pageSize": 90},
        },
    )
    if pa90 == 200:
        ok("publishedAfter pageSize=90 → HTTP 200")
    else:
        fail("tmt_published_after_90", f"HTTP {pa90}")

    _, pa99, _ = post_json(
        base,
        {
            "query": "",
            "filters": {"publishedAfter": today},
            "paging": {"pageNumber": 0, "pageSize": 99},
        },
    )
    if pa99 == 400:
        ok("publishedAfter pageSize=99 → HTTP 400")
    else:
        fail("tmt_published_after_99", f"expected 400, got {pa99}")

    pages_needed = (total + 89) // 90
    ok(f"full backfill hint: ~{pages_needed} pages @ pageSize=90 (~{pages_needed * 5}s @ 5s/req)")


def check_laura() -> None:
    print("Laura WordPress REST")
    data, status, headers = fetch_json(
        "https://laura.fi/wp-json/wp/v2/job-listings?per_page=1&orderby=date&order=desc"
    )
    if status != 200 or not isinstance(data, list):
        fail("laura_http", f"HTTP {status}")
        return
    total_hdr = headers.get("x-wp-total", "0")
    try:
        total = int(total_hdr)
    except ValueError:
        fail("laura_total", f"bad X-WP-Total: {total_hdr}")
        return
    if total < 10_000:
        fail("laura_count", f"X-WP-Total={total}")
    else:
        ok(f"X-WP-Total={total}")


def check_eures() -> None:
    print("EURES (fi)")
    data, status, _ = post_json(
        "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search",
        {
            "resultsPerPage": 1,
            "page": 1,
            "locationCodes": ["fi"],
            "keywords": [],
        },
    )
    if status != 200:
        fail("eures_http", f"HTTP {status}")
        return
    records = data.get("numberRecords", 0)
    if records < 10_000:
        fail("eures_count", f"numberRecords={records}")
    else:
        ok(f"numberRecords={records}")
    if "jvs" not in data:
        fail("eures_schema", "missing jvs[] field")
    else:
        ok("response uses jvs[] (not jobVacancies)")


def check_jobly_sitemap() -> None:
    print("Jobly sitemap")
    total_urls = 0
    for page in (1, 2):
        xml, status = fetch_text(f"https://www.jobly.fi/sitemap.xml?page={page}")
        if status != 200:
            fail(f"jobly_sitemap_p{page}", f"HTTP {status}")
            return
        total_urls += len(re.findall(r"/tyopaikka/", xml))
    if total_urls < 12_000:
        fail("jobly_url_count", f"only {total_urls} job URLs across 2 pages")
    else:
        ok(f"{total_urls} job URLs (pages 1+2)")


def check_tmt_oulu() -> None:
    print("TMT Oulu municipality filter")
    body = {
        "query": "",
        "filters": {"municipalities": ["564"]},
        "paging": {"pageNumber": 0, "pageSize": 90},
        "sorting": "LATEST",
    }
    data, status, _ = post_json(
        "https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search",
        body,
    )
    if status != 200:
        fail("tmt_oulu_http", f"HTTP {status}")
        return
    total = data.get("totalElements")
    if not isinstance(total, int) or total < 400:
        fail("tmt_oulu_total", f"totalElements={total}")
    else:
        ok(f"municipality 564 totalElements={total}")


def check_kuntarekry_shards() -> None:
    print("Kuntarekry regional shards")
    union_ids: set[int] = set()
    for shard in ("oulu", "oulun-kaupunki", "kemijarvi"):
        data, status, _ = fetch_json(
            f"https://kuntarekry.fi/fi/tyopaikat/{shard}?format=json"
        )
        if status != 200 or not isinstance(data, list) or not data:
            fail(f"kuntarekry_{shard}", f"HTTP {status}, rows={len(data) if isinstance(data, list) else 'n/a'}")
            return
        union_ids.update(int(row["id"]) for row in data if isinstance(row, dict) and row.get("id"))
    if len(union_ids) < 20:
        fail("kuntarekry_union", f"only {len(union_ids)} unique ids across shards")
    else:
        ok(f"{len(union_ids)} unique ids across sample shards")


def check_kirkkorekry_shards() -> None:
    print("Kirkkorekry regional shards")
    union_ids: set[int] = set()
    for shard in ("oulu", "pohjois-pohjanmaa"):
        data, status, _ = fetch_json(
            f"https://kirkkorekry.fi/fi/tyopaikat/{shard}?format=json"
        )
        if status != 200 or not isinstance(data, list) or not data:
            fail(f"kirkkorekry_{shard}", f"HTTP {status}, rows={len(data) if isinstance(data, list) else 'n/a'}")
            return
        union_ids.update(int(row["id"]) for row in data if isinstance(row, dict) and row.get("id"))
    if len(union_ids) < 5:
        fail("kirkkorekry_union", f"only {len(union_ids)} unique ids across shards")
    else:
        ok(f"{len(union_ids)} unique ids across sample shards")


def check_oulu_varbi_rss() -> None:
    print("Oulu Varbi RSS")
    xml, status = fetch_text("https://oulunyliopisto.varbi.com/fi/what:rssfeed/")
    if status != 200:
        fail("oulu_varbi_rss_http", f"HTTP {status}")
        return
    items = len(re.findall(r"<item>", xml))
    if items < 5:
        fail("oulu_varbi_rss_items", f"only {items} RSS items")
    else:
        ok(f"{items} RSS items")
    if "jobID:" not in xml:
        fail("oulu_varbi_rss_links", "missing jobID links in RSS")


def main() -> int:
    print(f"Regression test — {date.today().isoformat()}\n")
    check_duunitori()
    print()
    check_tmt()
    print()
    check_laura()
    print()
    check_eures()
    print()
    check_jobly_sitemap()
    print()
    check_tmt_oulu()
    print()
    check_kuntarekry_shards()
    print()
    check_kirkkorekry_shards()
    print()
    check_oulu_varbi_rss()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):\n  - " + "\n  - ".join(FAILURES))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
