#!/usr/bin/env bash
# EURES public search API — regression smoke test.
set -euo pipefail

echo "=== EURES fi search ==="
curl -sS -X POST "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search" \
  -H "Content-Type: application/json" \
  -d '{"resultsPerPage":5,"page":1,"locationCodes":["fi"],"keywords":[]}' \
  -o /tmp/eures.json -w "HTTP:%{http_code}\n"

python3 - <<'PY'
import json, sys
j = json.load(open("/tmp/eures.json"))
records = j.get("numberRecords")
if records is None:
    print("FAIL: missing numberRecords (not numberOfRecords)")
    sys.exit(1)
if "jvs" not in j:
    print("FAIL: missing jvs[]")
    sys.exit(1)
if records < 10000:
    print(f"FAIL: numberRecords={records}")
    sys.exit(1)
print(f"OK: numberRecords={records}, jvs sample={len(j['jvs'])}")
PY

echo "=== Valmis ==="
