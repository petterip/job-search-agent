#!/bin/bash
# Analysoi Duunitorin plain curl -vastaus tarkemmin

curl -s -L --connect-timeout 10 --max-time 20 \
    'https://duunitori.fi/tyopaikat?haku=ohjelmoija' \
    -o /tmp/duunitori_plain.html

echo "=== Tiedoston koko ==="
wc -c /tmp/duunitori_plain.html

echo ""
echo "=== Ilmoitusten URL-polut ==="
grep -oE 'tyopaikat/tyo/[a-zA-Z0-9_-]+' /tmp/duunitori_plain.html | head -20

echo ""
echo "=== job-ilmoitusten lkm (URL-polkujen mukaan) ==="
grep -oE 'tyopaikat/tyo/[a-zA-Z0-9_-]+' /tmp/duunitori_plain.html | sort -u | wc -l

echo ""
echo "=== Sisältääkö Next.js __NEXT_DATA__ ==="
grep -c '__NEXT_DATA__' /tmp/duunitori_plain.html

echo ""
echo "=== JSON-rakenne jos löytyy ==="
python3 -c "
import re, json, sys
html = open('/tmp/duunitori_plain.html').read()
m = re.search(r'<script id=\"__NEXT_DATA__\" type=\"application/json\">(.*?)</script>', html, re.S)
if m:
    data = json.loads(m.group(1))
    print('NEXT_DATA löytyi, avaimet:', list(data.keys()))
    # Etsi jobs-data
    def find_jobs(obj, depth=0):
        if depth > 6:
            return
        if isinstance(obj, list) and len(obj) > 0 and isinstance(obj[0], dict):
            keys = list(obj[0].keys()) if obj else []
            if any(k in keys for k in ['title','heading','slug','id','employer']):
                print(f'  Mahdollinen jobs-lista (depth={depth}): {len(obj)} kpl, kentät: {keys[:8]}')
                if len(obj) > 0:
                    print(f'  Esimerkki: {json.dumps(obj[0], ensure_ascii=False)[:300]}')
        if isinstance(obj, dict):
            for k, v in obj.items():
                find_jobs(v, depth+1)
        elif isinstance(obj, list):
            for item in obj[:5]:
                find_jobs(item, depth+1)
    find_jobs(data)
else:
    print('Ei NEXT_DATA -lohkoa')
    # Etsi muita JSON-lohkoja
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
    for i, s in enumerate(scripts[:20]):
        if 'jobHeading' in s or 'employer' in s or 'tyopaikka' in s:
            print(f'Script {i} sisältää job-viittauksia: {s[:200]}')
" 2>&1

echo ""
echo "=== HTML-rakenne: tyopaikka-elementit ==="
grep -oE '<[a-z]+ [^>]*class="[^"]*job[^"]*"[^>]*>' /tmp/duunitori_plain.html | head -10

echo ""
echo "=== Toimiva haku-API (v1 jobentries) ==="
curl -s "https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page=1&page_size=3" \
    -H "Accept: application/json" \
    -H "User-Agent: Mozilla/5.0" \
    -w "\nHTTP: %{http_code}" | head -c 600
