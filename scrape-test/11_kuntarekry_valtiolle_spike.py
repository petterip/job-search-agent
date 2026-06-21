#!/usr/bin/env python3
"""Spike: Kuntarekry and Valtiolle harvest paths (stdlib + optional Playwright)."""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.http_client import fetch_json, fetch_text

UA = {"User-Agent": "JobResearch/1.0 (+local research project)"}


def probe_format_json_listing(base_url: str, label: str) -> None:
    print(f"\n=== {label}: format=json probes ===")
    sorts = ["", "-changetime", "changetime", "-publication_date", "publication_date"]
    seen: set[int] = set()
    for sort in sorts:
        params = {"format": "json", "limit": "500"}
        if sort:
            params["sort"] = sort
        url = base_url + "?" + urllib.parse.urlencode(params)
        data, status, _ = fetch_json(url, headers={**UA, "Accept": "application/json"})
        if status != 200 or not isinstance(data, list):
            print(f"  sort={sort or '(none)'} HTTP {status} non-list")
            continue
        ids = {row["id"] for row in data if isinstance(row, dict) and "id" in row}
        new = ids - seen
        seen |= ids
        print(f"  sort={sort or '(none)'} batch={len(data)} unique_total={len(seen)} new={len(new)}")


def fetch_filters_data(site_root: str) -> dict:
    data, status, _ = fetch_json(
        f"{site_root}/fi/api/filters-data/",
        headers={**UA, "Accept": "application/json"},
    )
    return data if status == 200 and isinstance(data, dict) else {}


def probe_organisation_shards(site_root: str, label: str, sample: int | None = None) -> None:
    """Best stdlib path: iterate organisation IDs from filters-data."""
    print(f"\n=== {label}: organisation filter shards ===")
    filters = fetch_filters_data(site_root)
    orgs: list[str] = []
    seen: set[str] = set()
    for row in filters.get("organisations", []):
        oid = str(row.get("id", ""))
        if oid and oid not in seen:
            seen.add(oid)
            orgs.append(oid)
    if not orgs:
        print("  filters-data organisations missing")
        return
    target = orgs if sample is None else orgs[:sample]
    print(f"  organisations in filters-data: {len(orgs)} (probing {len(target)})")
    all_ids: set[int] = set()
    orgs_with_jobs = 0
    for oid in target:
        params = urllib.parse.urlencode(
            {"format": "json", "limit": "500", "sort": "-changetime", "organisation": oid}
        )
        url = f"{site_root}/fi/tyopaikat/?{params}"
        data, st, _ = fetch_json(url, headers={**UA, "Accept": "application/json"})
        if st != 200 or not isinstance(data, list) or not data:
            if st == 429:
                print(f"  HTTP 429 at organisation={oid} — rate limited; stopping org sweep")
                break
            continue
        orgs_with_jobs += 1
        ids = {row["id"] for row in data if isinstance(row, dict) and "id" in row}
        all_ids |= ids
        time.sleep(0.15)
    print(f"  orgs_with_jobs={orgs_with_jobs} unique_job_ids={len(all_ids)}")


def probe_employer_pages(site_root: str, label: str, sample: int = 15) -> None:
    print(f"\n=== {label}: employer HTML page sample ===")
    html, status = fetch_text(f"{site_root}/fi/tyonantajat/", headers=UA)
    if status != 200:
        print(f"  employer index HTTP {status}")
        return
    paths = sorted(set(re.findall(r'href="(/fi/tyonantajat/[a-z0-9\-]+/)"', html)))[:sample]
    print(f"  employer paths in index (sample {len(paths)}; pagination repeats same set)")
    all_ids: set[int] = set()
    for path in paths:
        try:
            page_html, page_st = fetch_text(f"{site_root}{path}", headers=UA)
        except Exception as exc:
            print(f"  {path.split('/')[-2][:28]:28} fetch failed: {exc}")
            continue
        ids_in_html = {int(x) for x in re.findall(r'"id"\s*:\s*(\d+)', page_html)}
        new = ids_in_html - all_ids
        all_ids |= ids_in_html
        print(f"  {path.split('/')[-2][:28]:28} HTML {page_st} embedded_ids={len(ids_in_html)} new={len(new)}")
    print(f"  unique embedded ids from employer HTML sample: {len(all_ids)}")


def probe_tmt_state_overlap() -> None:
    print("\n=== TMT: state employer keyword overlap ===")
    base = "https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search"
    for query in ["Valtiolle", "valtio", "ministeriö", "virasto", "Puolustusvoimat"]:
        data, status, _ = fetch_json(
            base,
            method="POST",
            body={"query": query, "filters": {}, "paging": {"pageNumber": 0, "pageSize": 5}, "sorting": "LATEST"},
            headers={**UA, "Content-Type": "application/json"},
        )
        total = data.get("totalElements") if isinstance(data, dict) else None
        print(f"  query={query!r} totalElements={total}")


def playwright_spike(url: str, label: str) -> None:
    print(f"\n=== {label}: Playwright network capture ===")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  SKIP: pip install playwright && playwright install chromium")
        return

    captured: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(response) -> None:
            try:
                if response.status != 200:
                    return
                ctype = response.headers.get("content-type", "")
                if "json" not in ctype and "format=json" not in response.url:
                    return
                text = response.text()
                if not text.startswith("[") and not text.startswith("{"):
                    return
                payload = json.loads(text)
                if isinstance(payload, list):
                    captured.append({"url": response.url[:120], "count": len(payload)})
                elif isinstance(payload, dict):
                    captured.append(
                        {
                            "url": response.url[:120],
                            "count": len(payload.get("jobs", [])),
                            "total": payload.get("total"),
                        }
                    )
            except Exception:
                return

        page.on("response", on_response)
        page.goto(url, wait_until="networkidle", timeout=90000)
        time.sleep(2)
        link_count = page.locator('a[href*="/tyopaikat/"]').count()
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)
        print(f"  tyopaikat anchor count (incl nav): {link_count}")
        print(f"  JSON responses captured: {len(captured)}")
        for row in captured[:12]:
            print(f"    {row}")
        browser.close()


def main() -> int:
    import os

    sample_raw = os.environ.get("SPIKE_ORG_SAMPLE", "50")
    org_sample = int(sample_raw) if sample_raw else None

    probe_format_json_listing("https://www.kuntarekry.fi/fi/tyopaikat/", "Kuntarekry")
    probe_format_json_listing("https://valtiolle.fi/fi/tyopaikat/", "Valtiolle")
    probe_organisation_shards("https://www.kuntarekry.fi", "Kuntarekry", sample=org_sample)
    probe_organisation_shards("https://valtiolle.fi", "Valtiolle", sample=org_sample)
    probe_tmt_state_overlap()
    playwright_spike("https://www.kuntarekry.fi/fi/tyopaikat/", "Kuntarekry")
    playwright_spike("https://valtiolle.fi/fi/tyopaikat/", "Valtiolle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
