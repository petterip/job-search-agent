#!/usr/bin/env bash
# Smoke test for undocumented JSON APIs. Fails on HTTP errors.
set -euo pipefail

UA="Mozilla/5.0"
FAIL=0

check() {
  local name="$1" url="$2"
  local code body
  code=$(curl -sS -o /tmp/scrape_api_body.json -w "%{http_code}" "$url" \
    -H "Accept: application/json" -H "User-Agent: $UA" --max-time 20)
  if [[ "$code" != "200" ]]; then
    echo "FAIL $name: HTTP $code"
    FAIL=1
    return
  fi
  echo "OK   $name: HTTP $code"
}

echo "=== Duunitori /api/v1/jobentries ==="
check duunitori "https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page=1&page_size=3"
python3 - <<'PY'
import json, sys
j = json.load(open("/tmp/scrape_api_body.json"))
count = j.get("count", 0)
print(f"     count={count}")
if count < 10000:
    sys.exit(1)
r = j["results"][0]
assert "descr" in r, "missing descr"
print(f"     sample: {r['heading'][:50]}")
PY

echo ""
echo "=== Duunitori filters ==="
python3 - <<'PY'
import json, urllib.request
UA = "Mozilla/5.0"
def get(q):
    req = urllib.request.Request(
        f"https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page_size=1&{q}",
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["count"]
all_c = get("")
search_c = get("search=ohjelmoija")
muni_c = get("municipality=Helsinki")
print(f"     search=ohjelmoija: {search_c} (all={all_c})")
assert search_c < all_c, "search filter should reduce count"
print(f"     municipality=Helsinki: {muni_c} (ignored={muni_c == all_c})")
PY

echo ""
echo "=== Laura REST ==="
code=$(curl -sS -D /tmp/laura_hdr.txt -o /tmp/laura_body.json -w "%{http_code}" \
  "https://laura.fi/wp-json/wp/v2/job-listings?per_page=3&orderby=date&order=desc" \
  -H "Accept: application/json" --max-time 20)
if [[ "$code" != "200" ]]; then echo "FAIL laura: HTTP $code"; FAIL=1; else
  total=$(grep -i "^x-wp-total:" /tmp/laura_hdr.txt | awk '{print $2}' | tr -d '\r')
  echo "OK   laura: HTTP $code, X-WP-Total=$total"
fi

echo ""
echo "=== Jobly (no list API — expect 404) ==="
for url in \
  "https://www.jobly.fi/api/v1/jobs?format=json&page_size=3" \
  "https://www.jobly.fi/wp-json/wp/v2/job-listings?per_page=3"; do
  c=$(curl -sS -o /dev/null -w "%{http_code}" "$url" -H "User-Agent: $UA" --max-time 8)
  echo "     $c $url"
done

echo ""
echo "=== Indeed (expect 403) ==="
c=$(curl -sS -o /dev/null -w "%{http_code}" "https://fi.indeed.com/jobs?q=ohjelmoija" \
  -H "User-Agent: $UA" --max-time 10)
echo "     HTTP $c"
[[ "$c" == "403" ]] && echo "OK   indeed blocked as documented" || { echo "WARN indeed returned $c"; }

if [[ "$FAIL" -ne 0 ]]; then exit 1; fi
echo ""
echo "=== Valmis ==="
