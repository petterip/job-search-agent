#!/usr/bin/env python3
"""
Testaa Playwright headless-selaimella sivustoja jotka vaativat JS:n
tai joilla on bot-suojaus.
"""
import sys
import json
import time

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright ei asennettuna. Asennus: pip install playwright && playwright install chromium")
    sys.exit(1)

SITES = {
    "duunitori": "https://duunitori.fi/tyopaikat?haku=ohjelmoija",
    "jobly":     "https://www.jobly.fi/fi/tyopaikat?q=ohjelmoija",
    "indeed":    "https://fi.indeed.com/jobs?q=ohjelmoija",
    "linkedin":  "https://www.linkedin.com/jobs/search/?keywords=ohjelmoija",
}

def test_site(page, name, url):
    print(f"\n=== {name} ===")
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
        status = resp.status if resp else "?"
        print(f"  HTTP: {status}")

        # Odota dynaamista sisältöä
        time.sleep(2)

        content = page.content()
        title = page.title()
        print(f"  Otsikko: {title}")
        print(f"  Sisällön koko: {len(content)} tavua")

        # Tarkista suojaukset
        cf = "cloudflare" in content.lower() or "cf-ray" in content.lower()
        challenge = any(x in content.lower() for x in ["just a moment", "checking", "enable javascript"])
        blocked = any(x in content.lower() for x in ["blocked", "access denied", "security check"])

        print(f"  CF-suojaus: {cf} | Challenge: {challenge} | Blocked: {blocked}")

        # Sivustokohtainen data
        if name == "duunitori":
            jobs = page.query_selector_all(".job-box")
            print(f"  .job-box elementtejä: {len(jobs)}")
            for j in jobs[:3]:
                heading = j.query_selector(".job-box__job-heading")
                company = j.query_selector(".job-box__company")
                h = heading.inner_text().strip() if heading else "?"
                c = company.inner_text().strip() if company else "?"
                print(f"    [{h}] - {c}")

        elif name == "jobly":
            # Kokeile löytää ilmoituksia
            jobs = page.query_selector_all("[class*='job'], [class*='listing'], article")
            print(f"  Job-elementtejä: {len(jobs)}")
            # XHR: onko JSON-dataa verkossa?

        elif name == "indeed":
            jobs = page.query_selector_all("[class*='job_seen_beacon'], [data-jk]")
            print(f"  Indeed job-kortit: {len(jobs)}")

        elif name == "linkedin":
            jobs = page.query_selector_all(".job-search-card, .base-card")
            print(f"  LinkedIn job-kortit: {len(jobs)}")
            if jobs:
                for j in jobs[:3]:
                    title_el = j.query_selector(".base-search-card__title, h3")
                    company_el = j.query_selector(".base-search-card__subtitle, h4")
                    t = title_el.inner_text().strip() if title_el else "?"
                    c = company_el.inner_text().strip() if company_el else "?"
                    print(f"    [{t}] - {c}")

        return True
    except Exception as e:
        print(f"  VIRHE: {e}")
        return False

def test_network_interception(playwright, name, url):
    """Kuuntele XHR/fetch-kutsuja saadaksemme API-endpointit"""
    print(f"\n=== {name} - verkkoliikenne ===")
    api_calls = []

    browser = playwright.chromium.launch(headless=True)
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800},
        locale="fi-FI",
    )
    page = ctx.new_page()

    def on_response(response):
        ct = response.headers.get("content-type", "")
        url_r = response.url
        if ("json" in ct or "api" in url_r) and response.status < 400:
            api_calls.append({"url": url_r, "status": response.status, "ct": ct[:50]})

    page.on("response", on_response)

    try:
        page.goto(url, wait_until="networkidle", timeout=25000)
        time.sleep(2)

        print(f"  API-kutsut (JSON tai /api/):")
        for call in api_calls[:15]:
            print(f"    {call['status']} [{call['ct']}] {call['url'][:100]}")

        content = page.content()
        print(f"  Sivun koko: {len(content)} tavua")
        jobs_count = len(page.query_selector_all("[class*='job']"))
        print(f"  [class*=job] elementtejä: {jobs_count}")

    except Exception as e:
        print(f"  VIRHE: {e}")
    finally:
        browser.close()

    return api_calls

with sync_playwright() as pw:
    # Testi 1: headless Chromium perus
    print("### TESTI A: Headless Chromium, ei stealth ###")
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800},
        locale="fi-FI",
        extra_http_headers={
            "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
        }
    )
    page = browser.new_page()

    for name, url in SITES.items():
        test_site(page, name, url)

    browser.close()

    # Testi 2: Verkkoliikenne LinkedIn:ssä (missä JSON tulee)
    print("\n### TESTI B: XHR-kuuntelu - Jobly ###")
    test_network_interception(pw, "jobly", "https://www.jobly.fi/fi/tyopaikat?q=ohjelmoija")

    print("\n### TESTI C: XHR-kuuntelu - LinkedIn ###")
    test_network_interception(pw, "linkedin", "https://www.linkedin.com/jobs/search/?keywords=ohjelmoija&location=Suomi")

print("\nPlaywright-testit valmis.")
