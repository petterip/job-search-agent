#!/usr/bin/env python3
"""Opt-in live probe for Jobly static/browser enrichment inputs."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.adapters.jobly import extract_jobly_detail_payload
from lib.http_client import fetch_text

UA = {"User-Agent": "job-search-agent-probe/0.1 (+local research project)"}


def main() -> int:
    sitemap, status = fetch_text("https://www.jobly.fi/sitemap.xml?page=1", headers=UA)
    if status != 200:
        print(json.dumps({"status": "failed", "reason": f"sitemap HTTP {status}"}))
        return 1
    urls = re.findall(r"<loc>(https://www\.jobly\.fi/tyopaikka/[^<]+)</loc>", sitemap)
    if not urls:
        print(json.dumps({"status": "failed", "reason": "no Jobly job URLs in sitemap"}))
        return 1
    checked = []
    for url in urls[:10]:
        html, detail_status = fetch_text(url, headers=UA)
        if detail_status != 200:
            checked.append({"url": url, "status": detail_status, "description_chars": 0})
            continue
        payload = extract_jobly_detail_payload(html, source_url=url)
        description = payload.get("description")
        chars = len(description.strip()) if isinstance(description, str) else 0
        checked.append({"url": url, "status": detail_status, "description_chars": chars})
        if chars >= 40:
            print(json.dumps({"status": "ok", "checked": checked}, ensure_ascii=False))
            return 0
    print(json.dumps({"status": "failed", "reason": "no usable description in sample", "checked": checked}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
