#!/bin/bash
# Testaa mitä suojauksia sivustoilla on - plain curl

SITES=(
    "duunitori|https://duunitori.fi/tyopaikat?haku=ohjelmoija"
    "jobly|https://www.jobly.fi/tyopaikka/kesatoihin-sanoman-kasvokkain-myyntiin-1625464"
    "indeed|https://fi.indeed.com/jobs?q=ohjelmoija"
    "linkedin|https://www.linkedin.com/jobs/search/?keywords=ohjelmoija"
    "tmt|https://tyomarkkinatori.fi/henkiloasiakkaat/avoimet-tyopaikat"
)

mkdir -p /tmp/scrape-test

echo "### Testi 1: plain curl (ei UA-spoofingia) ###"
echo ""

for entry in "${SITES[@]}"; do
    name="${entry%%|*}"
    url="${entry#*|}"
    echo "=== $name ==="
    result=$(curl -s -D /tmp/scrape-test/h_${name}.txt -o /tmp/scrape-test/b_${name}.html \
        -w "%{http_code}|%{size_download}|%{time_total}" \
        --connect-timeout 10 --max-time 15 -L "$url" 2>&1)
    http=$(echo "$result" | cut -d'|' -f1)
    size=$(echo "$result" | cut -d'|' -f2)
    time=$(echo "$result" | cut -d'|' -f3)
    echo "  HTTP: $http | Size: ${size}B | Time: ${time}s"
    echo "  Headers (tärkeimmät):"
    grep -i "^server:\|^cf-ray:\|^x-powered\|^location:\|set-cookie: cf" \
        /tmp/scrape-test/h_${name}.txt 2>/dev/null | sed 's/^/    /'
    cf=$(grep -ci "cloudflare\|cf-ray\|__cf_bm\|jschl" /tmp/scrape-test/b_${name}.html 2>/dev/null || echo 0)
    challenge=$(grep -ci "Just a moment\|Checking.*site\|enable.*JavaScript\|Please enable" /tmp/scrape-test/b_${name}.html 2>/dev/null || echo 0)
    jobs=$(grep -ci "työ\|tyopaikka\|ilmoitus\|rekry\|job posting" /tmp/scrape-test/b_${name}.html 2>/dev/null || echo 0)
    echo "  CF-viitteet: $cf | Challenge: $challenge | Job-viitteet: $jobs"
    echo "  Body-alku: $(head -c 250 /tmp/scrape-test/b_${name}.html | tr '\n' ' ' | sed 's/  */ /g')"
    echo ""
done
