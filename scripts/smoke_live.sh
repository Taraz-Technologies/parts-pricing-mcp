#!/usr/bin/env bash
# Live acceptance checks (CLAUDE.md), ~15 upstream calls with real keys.
# Needs: API running on $API (default http://127.0.0.1:8080), CONSOLE_KEY exported, curl, jq.
#   export CONSOLE_KEY=<a key from CONSOLE_API_KEYS>; bash scripts/smoke_live.sh
set -uo pipefail
cd "$(dirname "$0")/.."
API=${API:-http://127.0.0.1:8080}
: "${CONSOLE_KEY:?export CONSOLE_KEY=<a console key from CONSOLE_API_KEYS>}"
PY=$(ls .venv/bin/python .venv/Scripts/python.exe 2>/dev/null | head -1); PY=${PY:-python3}
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
pass=0; fail=0
check() { if [ "$2" = "true" ]; then echo "PASS $1"; pass=$((pass+1)); else echo "FAIL $1"; fail=$((fail+1)); fi; }
get() { curl -s -H "X-API-Key: $CONSOLE_KEY" "$API$1"; }
brief='{best: (.best | if . then {provider, sku, order_qty, unit_price, extended_price, stock} else null end),
        status: [.provider_status[] | {provider, ok, found, from_cache, error_code, latency_ms}]}'

h=$(curl -s "$API/health")
check "1 /health: both providers configured" \
  "$(jq '.providers.digikey == "configured" and .providers.mouser == "configured"' <<<"$h")"

r=$(get "/v1/part?mpn=LM358DR&qty=100")
check "2 LM358DR qty 100: both providers ok, offers > 0" \
  "$(jq '([.provider_status[].ok] | all) and (.offers | length > 0)' <<<"$r")"
jq -c "$brief" <<<"$r"

ms=$(curl -s -o "$TMP" -w '%{time_total}' -H "X-API-Key: $CONSOLE_KEY" "$API/v1/part?mpn=LM358DR&qty=100")
ms=$(awk -v t="$ms" 'BEGIN { printf "%d", t * 1000 }')
check "3 repeat: from_cache true for all, ${ms} ms < 50 ms" \
  "$(jq --argjson ms "$ms" '([.provider_status[].from_cache] | all) and $ms < 50' "$TMP")"

r=$(get "/v1/part?mpn=LM1117IMPX-3.3%2FNOPB&qty=50")
check "4 LM1117IMPX-3.3/NOPB: both ok, offers > 0" \
  "$(jq '([.provider_status[].ok] | all) and (.offers | length > 0)' <<<"$r")"
jq -c "$brief" <<<"$r"

r=$(get "/v1/part?mpn=NOTAREALPART12345XYZ&qty=1")
check "5 NOTAREALPART12345XYZ: found false for both, NOT an error" \
  "$(jq '[.provider_status[] | .ok and (.found | not)] | all' <<<"$r")"
jq -c "$brief" <<<"$r"

before=$(get "/v1/stats?hours=1" | jq '.providers.mouser.upstream_calls // 0')
bom=$("$PY" -c "
import csv, json, sys
rows = list(csv.DictReader(open('bench/testset.csv', newline='', encoding='utf-8')))[:20]
print(json.dumps({'lines': [{'line_id': f'L{i+1}', 'mpn': r['mpn'], 'qty': int(r['qty']),
                             'manufacturer': r['manufacturer'] or None} for i, r in enumerate(rows)]}))")
r=$(curl -s -X POST -H "X-API-Key: $CONSOLE_KEY" -H "Content-Type: application/json" -d "$bom" "$API/v1/bom/price")
after=$(get "/v1/stats?hours=1" | jq '.providers.mouser.upstream_calls // 0')
calls=$((after - before))
check "6 20-line BOM: mouser upstream calls ${calls} <= 2" "$( [ "$calls" -le 2 ] && echo true || echo false )"
jq -c '{summary, provider_status: [.provider_status[] | {provider, ok, error_code, message}]}' <<<"$r"

echo "---- $pass passed, $fail failed"
[ "$fail" -eq 0 ]
