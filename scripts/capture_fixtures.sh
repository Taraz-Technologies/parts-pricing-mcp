#!/usr/bin/env bash
# Runbook Step 2 smoke tests: saves real DigiKey + Mouser responses into fixtures/.
# Reads keys from .env; never prints them. Uses ~5 DigiKey + ~3 Mouser calls.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
mkdir -p fixtures
PY=$(ls .venv/bin/python .venv/Scripts/python.exe 2>/dev/null | head -1 || true)

TOKEN=$(curl -s -X POST https://api.digikey.com/v1/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "client_id=$DIGIKEY_CLIENT_ID" --data-urlencode "client_secret=$DIGIKEY_CLIENT_SECRET" \
  -d "grant_type=client_credentials" | "$PY" -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
echo "digikey token length: ${#TOKEN}"
[ ${#TOKEN} -gt 20 ] || { echo "DigiKey token failed (check client id/secret, app active?)"; }

dk() { url="$1"; name="$2"; shift 2; curl -s -D "fixtures/$name.headers.txt" -o "fixtures/$name.json" \
  -w "$name -> %{http_code} in %{time_total}s\n" \
  -H "Authorization: Bearer $TOKEN" -H "X-DIGIKEY-Client-Id: $DIGIKEY_CLIENT_ID" \
  -H "X-DIGIKEY-Locale-Site: US" -H "X-DIGIKEY-Locale-Language: en" -H "X-DIGIKEY-Locale-Currency: USD" \
  -H "Accept: application/json" "$@" "$url"; }

if [ ${#TOKEN} -gt 20 ]; then
  dk "https://api.digikey.com/products/v4/search/LM358DR/productdetails" digikey_productdetails_LM358DR
  dk "https://api.digikey.com/products/v4/search/LM1117IMPX-3.3%2FNOPB/productdetails" digikey_productdetails_slash
  dk "https://api.digikey.com/products/v4/search/NOTAREALPART12345XYZ/productdetails" digikey_productdetails_notfound
  dk "https://api.digikey.com/products/v4/search/keyword" digikey_keyword_2N7002 -X POST \
    -H "Content-Type: application/json" -d '{"Keywords":"2N7002","Limit":5,"Offset":0}'
  grep -i ratelimit fixtures/digikey_productdetails_LM358DR.headers.txt || true
fi

# Mouser key is a URL param: curl -w never prints the URL, and we don't save headers.
ms() { curl -s -X POST "https://api.mouser.com/api/v1/search/$1?apiKey=$MOUSER_SEARCH_API_KEY" \
  -H "Content-Type: application/json" -H "Accept: application/json" -d "$3" \
  -o "fixtures/$2.json" -w "$2 -> %{http_code} in %{time_total}s\n"; }

ms partnumber mouser_partnumber_single '{"SearchByPartRequest":{"mouserPartNumber":"LM358DR","partSearchOptions":"Exact"}}'
ms partnumber mouser_partnumber_batch  '{"SearchByPartRequest":{"mouserPartNumber":"LM358DR|GRM188R71C104KA01D|NOTAREALPART12345XYZ","partSearchOptions":"Exact"}}'
ms keyword    mouser_keyword_2N7002     '{"SearchByKeywordRequest":{"keyword":"2N7002","records":5,"startingRecord":0,"searchOptions":"None","searchWithYourSignUpLanguage":"false"}}'
"$PY" - <<'PY'
import json
d = json.load(open("fixtures/mouser_partnumber_batch.json"))
print("mouser batch errors:", d.get("Errors"))
sr = d.get("SearchResults") or {}
print("mouser batch results:", sr.get("NumberOfResult"), [p.get("ManufacturerPartNumber") for p in sr.get("Parts", [])])
PY
