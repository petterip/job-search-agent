#!/bin/bash
# Testaa selaimen UA + headerit - ohittaako yksinkertaisen suojauksen?

SITES=(
    "duunitori|https://duunitori.fi/tyopaikat?haku=ohjelmoija"
    "jobly|https://www.jobly.fi/tyopaikka/kesatoihin-sanoman-kasvokkain-myyntiin-1625464"
    "indeed|https://fi.indeed.com/jobs?q=ohjelmoija"
    "linkedin|https://www.linkedin.com/jobs/search/?keywords=ohjelmoija"
)

mkdir -p /tmp/scrape-test

echo "### Testi 2: Selaimen User-Agent + täydet headerit ###"
echo ""

UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

for entry in "${SITES[@]}"; do
    name="${entry%%|*}"
    url="${entry#*|}"
    echo "=== $name ==="
    result=$(curl -s \
        -D /tmp/scrape-test/h2_${name}.txt \
        -o /tmp/scrape-test/b2_${name}.html \
        -w "%{http_code}|%{size_download}|%{time_total}" \
        --connect-timeout 10 --max-time 20 -L \
        -H "User-Agent: $UA" \
        -H "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8" \
        -H "Accept-Language: fi-FI,fi;q=0.9,en-US;q=0.8,en;q=0.7" \
        -H "Accept-Encoding: gzip, deflate, br" \
        -H "Connection: keep-alive" \
        -H "Upgrade-Insecure-Requests: 1" \
        -H "Sec-Fetch-Dest: document" \
        -H "Sec-Fetch-Mode: navigate" \
        -H "Sec-Fetch-Site: none" \
        -H "Sec-CH-UA: \"Google Chrome\";v=\"125\", \"Chromium\";v=\"125\", \"Not.A/Brand\";v=\"24\"" \
        -H "Sec-CH-UA-Mobile: ?0" \
        -H "Sec-CH-UA-Platform: \"Windows\"" \
        "$url" 2>&1)
    http=$(echo "$result" | cut -d'|' -f1)
    size=$(echo "$result" | cut -d'|' -f2)
    time=$(echo "$result" | cut -d'|' -f3)
    echo "  HTTP: $http | Size: ${size}B | Time: ${time}s"
    grep -i "^server:\|^cf-ray:\|^x-powered\|^location:" \
        /tmp/scrape-test/h2_${name}.txt 2>/dev/null | sed 's/^/    /'
    cf=$(grep -ci "cloudflare\|__cf_bm\|jschl\|cf_clearance" /tmp/scrape-test/b2_${name}.html 2>/dev/null || echo 0)
    challenge=$(grep -ci "Just a moment\|Checking.*site\|enable.*JavaScript" /tmp/scrape-test/b2_${name}.html 2>/dev/null || echo 0)
    jobs=$(grep -ci "työ\|job\|ilmoitus\|rekry" /tmp/scrape-test/b2_${name}.html 2>/dev/null || echo 0)
    echo "  CF-viitteet: $cf | Challenge: $challenge | Job-viitteet: $jobs"
    echo "  Body-alku: $(head -c 300 /tmp/scrape-test/b2_${name}.html | tr '\n' ' ' | sed 's/  */ /g')"
    echo ""
done
