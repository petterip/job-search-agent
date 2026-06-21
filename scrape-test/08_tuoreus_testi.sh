#!/usr/bin/env bash
# Freshness and incremental parameter probes (exploratory; use regression_test.py for gates).
set -euo pipefail

UA="Mozilla/5.0"
TODAY=$(date -u +%Y-%m-%d)
TODAY_ISO="${TODAY}T00:00:00"

echo "=== Duunitori: uusimmat (ordering=-date_posted) ==="
curl -sS "https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page=1&page_size=5" \
  -H "Accept: application/json" -H "User-Agent: $UA" | python3 -c "
import sys, json
j = json.load(sys.stdin)
print(f'count: {j[\"count\"]}')
for r in j['results']:
    print(f'  {r[\"date_posted\"][:19]} | {r[\"heading\"][:50]} | {r[\"company_name\"]}')
"

echo ""
echo "=== Duunitori: date filter params (most ignored) ==="
for param in "date_posted__gte=${TODAY}" "created_after=${TODAY}" "published_after=${TODAY}" "since=${TODAY}"; do
  count=$(curl -sS "https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page_size=1&${param}" \
    -H "Accept: application/json" -H "User-Agent: $UA" | \
    python3 -c "import sys,json; print(json.load(sys.stdin).get('count','?'))")
  echo "  ${param} → count=${count} (compare to unfiltered total)"
done

echo ""
echo "=== Laura REST: uusimmat ==="
curl -sS "https://laura.fi/wp-json/wp/v2/job-listings?per_page=5&orderby=date&order=desc" \
  -H "Accept: application/json" | python3 -c "
import sys, json
j = json.load(sys.stdin)
for r in j:
    print(f'  {r[\"date\"][:19]} | {r[\"title\"][\"rendered\"][:60]}')
"

echo ""
echo "=== Laura REST: julkaistu tänään (after=) ==="
curl -sS "https://laura.fi/wp-json/wp/v2/job-listings?per_page=5&orderby=date&order=desc&after=${TODAY_ISO}" \
  -H "Accept: application/json" | python3 -c "
import sys, json
j = json.load(sys.stdin)
print(f'Julkaistu tänään (after=): {len(j)} kpl')
for r in j[:3]:
    print(f'  {r[\"date\"][:19]} | {r[\"title\"][\"rendered\"][:60]}')
"

echo ""
echo "=== Laura REST: muokattu tänään (modified_after=) ==="
curl -sS "https://laura.fi/wp-json/wp/v2/job-listings?per_page=5&orderby=modified&order=desc&modified_after=${TODAY_ISO}" \
  -H "Accept: application/json" | python3 -c "
import sys, json
j = json.load(sys.stdin)
print(f'Muokattu tänään (modified_after=): {len(j)} kpl')
for r in j[:3]:
    print(f'  {r[\"modified\"][:19]} | {r[\"title\"][\"rendered\"][:60]}')
"

echo ""
echo "=== TMT: publishedAfter (today, pageSize=5) ==="
curl -sS -X POST "https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"\",\"filters\":{\"publishedAfter\":\"${TODAY}T00:00:00.000Z\"},\"paging\":{\"pageNumber\":0,\"pageSize\":5}}" | \
  python3 -c "
import sys, json
j = json.load(sys.stdin)
print(f'totalElements: {j.get(\"totalElements\")}')
for r in j.get('content', [])[:3]:
    title = r.get('title', {})
    t = title.get('fi') or title.get('en') or str(title)[:50]
    print(f'  {r.get(\"created\", \"?\")[:19]} | {t[:50]}')
"

echo ""
echo "=== Valmis ==="
