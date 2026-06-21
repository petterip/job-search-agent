#!/usr/bin/env python3
"""
Testaa:
1. cloudscraper - Cloudflare-bypass
2. LinkedIn guest search API
3. Jobly oikeilla parametreilla
4. Duunitorin lisäkentät (descr, lat/lon)
"""
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.http_client import DEFAULT_UA, fetch_json

# --- 1. cloudscraper ---
print("=== 1. cloudscraper (Cloudflare-bypass) ===")
try:
    import cloudscraper
    scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})

    # Duunitori
    r = scraper.get("https://duunitori.fi/tyopaikat?haku=ohjelmoija", timeout=20)
    print(f"Duunitori cloudscraper: HTTP {r.status_code}, {len(r.text)} tavua")
    import re
    jobs = re.findall(r'/tyopaikat/tyo/[a-zA-Z0-9_-]+', r.text)
    print(f"  Ilmoitus-URL:t: {len(set(jobs))} uniikkia")

    # Indeed
    r2 = scraper.get("https://fi.indeed.com/jobs?q=ohjelmoija&l=Helsinki", timeout=20)
    print(f"Indeed cloudscraper: HTTP {r2.status_code}, {len(r2.text)} tavua")
    if r2.status_code == 200:
        jobs2 = re.findall(r'data-jk="([^"]+)"', r2.text)
        print(f"  Job-ID:t (data-jk): {len(jobs2)}")

    # Jobly
    r3 = scraper.get("https://www.jobly.fi/tyopaikat", timeout=20)
    print(f"Jobly /tyopaikat cloudscraper: HTTP {r3.status_code}, {len(r3.text)} tavua")
    jobs3 = re.findall(r'/tyopaikka/[^"\']+', r3.text)
    print(f"  Tyopaikka-URL:t: {len(set(jobs3))}")

except ImportError:
    print("cloudscraper ei asennettuna. Asennetaan...")
    import subprocess
    subprocess.run(["pip", "install", "cloudscraper", "--break-system-packages", "-q"])
    print("Asennettuna — aja uudelleen")

# --- 2. LinkedIn guest-hakurajapinta ---
print("\n=== 2. LinkedIn guest-hakurajapinta ===")
# LinkedIn tarjoaa julkisen guest search API:n (ei kirjautumista)
linkedin_urls = [
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=ohjelmoija&location=Suomi&start=0",
    "https://www.linkedin.com/jobs/search/?keywords=ohjelmoija&location=Suomi&f_TPR=r86400",
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=developer&location=Finland&start=0&count=10",
]
for url in linkedin_urls:
    j, status, _ = fetch_json(url, headers={"Accept": "text/html,application/json"})
    print(f"  HTTP {status}: {url[:80]}")
    if isinstance(j, dict) and status == 200:
        print(f"    JSON-vastaus: {list(j.keys())[:6]}")
    elif status == 200:
        print(f"    Vastaus (ei JSON): {str(j)[:100]}")
    else:
        print(f"    Virhe: {j}")
    time.sleep(0.3)

# --- 3. LinkedIn HTML guest search ---
print("\n=== 3. LinkedIn HTML guest search (ei kirjautumista) ===")
try:
    req = urllib.request.Request(
        "https://www.linkedin.com/jobs/search/?keywords=developer&location=Finland&f_TPR=r86400",
        headers={
            "User-Agent": DEFAULT_UA,
            "Accept": "text/html",
            "Accept-Language": "fi-FI,fi;q=0.9",
        }
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read().decode("utf-8", errors="replace")
    import re
    jobs_li = re.findall(r'<li[^>]+data-occludable-job-id="(\d+)"', body)
    job_titles = re.findall(r'"title":"([^"]+)"', body)
    print(f"  HTTP 200, {len(body)} tavua")
    print(f"  Job ID:t (data-occludable-job-id): {len(jobs_li)}")
    print(f"  'title' kentät: {len(job_titles)}")
    for t in job_titles[:5]:
        print(f"    - {t}")
except Exception as e:
    print(f"  Virhe: {e}")

# --- 4. Jobly oikeat URL:t ---
print("\n=== 4. Jobly oikeat URL:t ===")
jobly_urls = [
    "https://www.jobly.fi/tyopaikat",
    "https://www.jobly.fi/hae-toita",
    "https://www.jobly.fi/fi/hae-toita",
    "https://www.jobly.fi/fi/avoimet-tyopaikat",
]
for url in jobly_urls:
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", errors="replace")
            import re
            job_urls = re.findall(r'href="(/tyopaikka/[^"]+)"', body)
            print(f"  {r.status} {url}: {len(set(job_urls))} tyopaikka-URL")
    except urllib.error.HTTPError as e:
        print(f"  {e.code} {url}")
    except Exception as e:
        print(f"  ERR {url}: {e}")

# --- 5. Duunitori syvempi data ---
print("\n=== 5. Duunitori jobentries — kaikki kentät ===")
j, status, _ = fetch_json(
    "https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page=1&page_size=1",
    headers={"User-Agent": "Mozilla/5.0"},
)
if j.get("results"):
    r = j["results"][0]
    print(f"Kaikki kentät: {list(r.keys())}")
    print(f"heading: {r.get('heading')}")
    print(f"company_name: {r.get('company_name')}")
    print(f"date_posted: {r.get('date_posted')}")
    print(f"slug: {r.get('slug')}")
    print(f"municipality_name: {r.get('municipality_name')}")
    print(f"descr (200 merkkiä): {str(r.get('descr',''))[:200]}")
    print(f"latitude: {r.get('latitude')}, longitude: {r.get('longitude')}")
    print(f"export_image_url: {r.get('export_image_url')}")

# Duunitori: onko lisäkenttöjä jos haetaan yksittäinen?
slug = j["results"][0].get("slug", "") if j.get("results") else ""
if slug:
    j2, s2, _ = fetch_json(
        f"https://duunitori.fi/api/v1/jobentries/{slug}/",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    print(f"\nYksittäinen /{slug}/: HTTP {s2}")
    if s2 == 200 and isinstance(j2, dict):
        print(f"Lisäkentät: {list(j2.keys())}")

print("\nValmis.")
