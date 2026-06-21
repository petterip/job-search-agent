#!/usr/bin/env python3
"""
Testaa Duunitorin HTML-jäsennystä ja sisäistä API:a
"""
import re
import json
import urllib.request
import urllib.parse
import urllib.error
import html
import time

UA_PLAIN = "curl/8.5.0"
UA_BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

def fetch(url, headers=None, label=""):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA_PLAIN})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace"), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", errors="replace"), e.code, {}
    except Exception as e:
        return str(e), 0, {}

# --- 1. Jäsennä SSR HTML ---
print("=== 1. Duunitori SSR HTML -jäsennys ===")
body, status, hdrs = fetch("https://duunitori.fi/tyopaikat?haku=ohjelmoija")
print(f"HTTP {status}, {len(body)} tavua")

# job-box rakenne
jobs = re.findall(
    r'<a[^>]+href="(/tyopaikat/tyo/[^"]+)"[^>]*>.*?<span[^>]*class="[^"]*job-box__job-heading[^"]*"[^>]*>(.*?)</span>.*?class="[^"]*job-box__company[^"]*"[^>]*>(.*?)</[a-z]+>.*?class="[^"]*job-box__job-location[^"]*"[^>]*>(.*?)</span>',
    body, re.S
)
print(f"\nLöydettyjä ilmoituksia (job-box -rakenne): {len(jobs)}")
for url, title, employer, location in jobs[:5]:
    title = re.sub(r'<[^>]+>', '', title).strip()
    employer = re.sub(r'<[^>]+>', '', employer).strip()
    location = re.sub(r'<[^>]+>', '', location).strip()
    print(f"  [{title}] | {employer} | {location}")
    print(f"  URL: https://duunitori.fi{url}")

# Sivumäärä
pagination = re.findall(r'href="[^"]*\?[^"]*sivu=(\d+)', body)
if pagination:
    print(f"\nSivuja: max sivu = {max(int(p) for p in pagination)}")
else:
    print("\nEi sivutusnumeroita löydetty")

# --- 2. Etsi Duunitorin frontend API ---
print("\n=== 2. Etsi Duunitorin sisäinen API (XHR-kutsut JS:ssä) ===")
api_patterns = re.findall(r'["\'](/api/[^"\'?]+)', body)
fetch_patterns = re.findall(r'fetch\(["\']([^"\']+)["\']', body)
all_scripts = re.findall(r'src=["\'](/_next/static/[^"\']+\.js)', body)
print(f"  /api/ -viittaukset HTML:ssä: {set(api_patterns)}")
print(f"  fetch()-kutsut HTML:ssä: {set(fetch_patterns)}")
print(f"  JS chunk -tiedostoja: {len(all_scripts)}")

# --- 3. Kokeile eri API-endpointeja ---
print("\n=== 3. API-endpoint kokeilu ===")
endpoints = [
    "/api/v1/jobentries?ordering=-date_posted&page=1&page_size=3",
    "/api/v1/jobs?search=ohjelmoija&page_size=3",
    "/api/search?q=ohjelmoija&format=json",
    "/api/tyopaikat.json?haku=ohjelmoija",
    "/api/v1/tyopaikat?haku=ohjelmoija&format=json",
    "/_next/data/xxx/tyopaikat.json?haku=ohjelmoija",
]
for ep in endpoints:
    url = "https://duunitori.fi" + ep
    body2, status2, h2 = fetch(url, headers={"User-Agent": UA_BROWSER, "Accept": "application/json"})
    ct = h2.get("Content-Type", "?")
    snippet = body2[:80].replace("\n", " ")
    print(f"  {status2} [{ct[:30]}] {ep[:60]}")
    if "application/json" in ct:
        print(f"    → JSON! {snippet}")

# --- 4. Kokeile hakusivua paginaation kanssa ---
print("\n=== 4. Paginaatio ===")
pages = [1, 2, 3]
for page in pages:
    url = f"https://duunitori.fi/tyopaikat?haku=ohjelmoija&sivu={page}"
    b, s, _ = fetch(url)
    job_urls = set(re.findall(r'/tyopaikat/tyo/[a-zA-Z0-9_-]+', b))
    print(f"  Sivu {page}: HTTP {s}, {len(job_urls)} uniikkia ilmoitusta ({len(b)} B)")
    time.sleep(0.5)

# --- 5. Kokeile ilman hakusanaa (kaikki ilmoitukset) ---
print("\n=== 5. Kaikki ilmoitukset (ei hakusanaa) ===")
b5, s5, _ = fetch("https://duunitori.fi/tyopaikat")
job_urls_all = set(re.findall(r'/tyopaikat/tyo/[a-zA-Z0-9_-]+', b5))
print(f"HTTP {s5}, uniikkeja ilmoituksia: {len(job_urls_all)}")
pagination5 = re.findall(r'sivu=(\d+)', b5)
if pagination5:
    print(f"Sivumäärä viittaus max: {max(int(p) for p in pagination5)}")

print("\nValmis.")
