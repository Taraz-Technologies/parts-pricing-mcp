# Parts Pricing Integration: DigiKey + Mouser
**Project:** TSL OS → Parts pricing · **Owner:** Inam Ullah · **Date:** 2 Nov 2026
**Consumers:** CoreOps OS Planner console and PRC console (live part search, price/stock comparison)
**Phase 1:** DigiKey + Mouser. **Phase 2:** Verical, Arrow, Future Electronics, Element14, Online Components.
**Skipped:** Octopart/Nexar (free tier too small, paid plans charge per matched part).

---

## 1. Summary

Two options are being evaluated:

1. **Partuno 4.0.1**: open-source MCP server wrapping the DigiKey and Mouser APIs (https://github.com/JPMarhefka/Partuno).
2. **Direct APIs**: DigiKey Product Information v4 + Mouser Search API, called from our own service (or n8n).

**Working hypothesis (to confirm with today's tests):** build our own small Parts Pricing Service. It has a REST API for the consoles, a shared cache, a quota counter and one adapter per distributor. A thin MCP server on top lets Claude and agents use the same service. Partuno is the reference for normalization and comparison logic, and is tested side by side.

### Three findings that shape the decision

**1. API quotas matter more than which option we pick.** Both options call the same distributor APIs.
- DigiKey Product Information (standard tier): 120 requests/minute and 1,000/day, limited per app. The part-number lookup takes one part number per call.
- Mouser Search: up to 50 results per call, 30 calls/minute, 1,000 calls/day.
- A 200-line BOM priced on DigiKey uses about 20% of a day's quota. With two consoles sharing the keys, a shared cache and quota counter are needed whichever option we choose.
- DigiKey has a ProductInformation V4 plan with 250,000 requests/day. It needs manual approval, so apply early.

**2. Partuno is built for AI assistants, not app screens.** Native MCP is its main interface. Its REST and Custom GPT Action endpoints are kept only for backward compatibility. The consoles need fixed JSON over HTTP with fast responses.

**3. Partuno is young and has one maintainer.** The repo has 3 stars, 1 fork and 31 commits, and requires Python 3.12+. It covers only DigiKey and Mouser, so Phase 2 would mean forking it or building our own.
- Its local mode needs no database or Redis, so there is no persistent cache shared between consoles.
- Its strengths: matching manufacturer and MPN across providers; cost at the requested quantity, respecting MOQ, order multiples and price breaks; partial results when one provider fails.
- It is Apache-2.0, so we can reuse that logic.

### Preliminary comparison (fill in with measured numbers in Step 8)

| | Partuno 4.0.1 (MCP) | Direct APIs (own service) |
|---|---|---|
| Speed | Same upstream calls, plus MCP overhead; no shared cache | Same upstream calls; shared cache answers repeat lookups almost instantly |
| Data quality | Strong normalization and MPN matching | As good as we build; copy Partuno's rules |
| API limits | Same 1,000/day per distributor; budget per instance | One shared cache and quota counter for both consoles |
| Security | Search-only keys supported; remote use needs TLS + OAuth | Keys in our .env; Mouser key in URL, so logs are redacted |
| Maintenance | Low effort to start; depends on one external maintainer | We own the adapters and fix them when APIs change |
| Phase 2 distributors | Not covered | One adapter each |
| Fit for consoles | Poor | Good |
| Fit for Claude/agents | Excellent | Good, with the thin MCP layer |

### Choices made for today (change if your setup differs)
- The Pakistan server is Linux with Docker.
- The service is Python 3.12 + FastAPI + SQLite: no extra infrastructure, and the same language as Partuno.
- Claude Code runs on the server over SSH, so live tests use the real keys there.
- The MCP container never holds distributor keys. It calls the REST API.

---

## 2. Today's schedule (PKT)

| Time | Step | Done when |
|---|---|---|
| 12:30–13:00 | 1. Request keys, prepare server | DigiKey apps created, Mouser key requested |
| 13:00–13:45 | 2. curl smoke tests, save real responses | `fixtures/` has real JSON |
| 13:45–16:30 | 3–4. Claude Code builds the service (8 prompts) | Tests pass, live lookup works |
| 16:30–17:00 | 5. Deploy with Docker | `/health` OK on server |
| 15:00–17:00 (parallel) | 6. Partuno 4.0.1 test | Tool list, security probe, bench run |
| 17:00–17:45 | 7. Benchmark both, spot-check against websites | `comparison.csv` filled in |
| 17:45–18:30 | 8. Post the write-up to TSL OS → Parts pricing | Posted |

**If you run late, cut in this order:** `/v1/stats`, Docker (run uvicorn directly), keyword search. Never cut the cache and quota counter.

---

## 3. Step 1: Keys (do this first)

**Mouser**
- Request a Search API key at mouser.com/api-hub. It arrives by email and can take hours, so request it first.
- Do **not** request an Order/Cart key. If Taraz already has a Mouser Search key in the vault, use that.

**DigiKey** (developer.digikey.com → Organization → Production Apps → Create Production App)
- App `taraz-parts-pricing`: subscribe to **Product Information v4 only**. Leave Ordering, Quoting, MyLists, Order Status and Barcode unticked. Use client credentials (2-legged). The callback URL can be `https://localhost`.
- App `taraz-partuno-test`: same settings. Quotas apply per app, so the Partuno test won't eat the service's 1,000/day.
- Request the higher quota tier (the 250k/day Product Information plan) for the production app.

---

## 4. Step 2: Server prep and smoke tests

```bash
sudo apt-get update && sudo apt-get install -y jq git curl
curl -LsSf https://astral.sh/uv/install.sh | sh   # Python 3.12 without touching system Python
mkdir -p ~/taraz-parts-pricing/fixtures && cd ~/taraz-parts-pricing
git init
touch .env && chmod 600 .env
```

Put these lines in `.env` for now (the full file is in Step 4):
```
DIGIKEY_CLIENT_ID=...
DIGIKEY_CLIENT_SECRET=...
MOUSER_SEARCH_API_KEY=...
```

### DigiKey smoke test
This saves real responses so Claude Code maps the fields correctly.
```bash
set -a; source .env; set +a
TOKEN=$(curl -s -X POST https://api.digikey.com/v1/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "client_id=$DIGIKEY_CLIENT_ID&client_secret=$DIGIKEY_CLIENT_SECRET&grant_type=client_credentials" | jq -r .access_token)
echo "token length: ${#TOKEN}"   # should be > 20; never echo the token itself

# usage: dk <url> <fixture-name> [extra curl options]
dk() { url="$1"; name="$2"; shift 2; curl -s -D "fixtures/$name.headers.txt" -o "fixtures/$name.json" \
  -w "$name -> %{http_code} in %{time_total}s\n" \
  -H "Authorization: Bearer $TOKEN" -H "X-DIGIKEY-Client-Id: $DIGIKEY_CLIENT_ID" \
  -H "X-DIGIKEY-Locale-Site: US" -H "X-DIGIKEY-Locale-Language: en" -H "X-DIGIKEY-Locale-Currency: USD" \
  -H "Accept: application/json" "$@" "$url"; }

dk "https://api.digikey.com/products/v4/search/LM358DR/productdetails" digikey_productdetails_LM358DR
dk "https://api.digikey.com/products/v4/search/LM1117IMPX-3.3%2FNOPB/productdetails" digikey_productdetails_slash
dk "https://api.digikey.com/products/v4/search/NOTAREALPART12345XYZ/productdetails" digikey_productdetails_notfound
dk "https://api.digikey.com/products/v4/search/keyword" digikey_keyword_2N7002 -X POST \
  -H "Content-Type: application/json" -d '{"Keywords":"2N7002","Limit":5,"Offset":0}'
grep -i ratelimit fixtures/digikey_productdetails_LM358DR.headers.txt
```

### Mouser smoke test
Run this when the key arrives. It also checks whether batching up to 10 part numbers separated by `|` works.
```bash
# usage: ms <endpoint> <fixture-name> <json-body>
ms() { curl -s -X POST "https://api.mouser.com/api/v1/search/$1?apiKey=$MOUSER_SEARCH_API_KEY" \
  -H "Content-Type: application/json" -H "Accept: application/json" -d "$3" \
  -o "fixtures/$2.json" -w "$2 -> %{http_code} in %{time_total}s\n"; }

ms partnumber mouser_partnumber_single '{"SearchByPartRequest":{"mouserPartNumber":"LM358DR","partSearchOptions":"Exact"}}'
ms partnumber mouser_partnumber_batch  '{"SearchByPartRequest":{"mouserPartNumber":"LM358DR|GRM188R71C104KA01D|NOTAREALPART12345XYZ","partSearchOptions":"Exact"}}'
ms keyword    mouser_keyword_2N7002     '{"SearchByKeywordRequest":{"keyword":"2N7002","records":5,"startingRecord":0,"searchOptions":"None","searchWithYourSignUpLanguage":"false"}}'
jq '.Errors, .SearchResults.NumberOfResult, [.SearchResults.Parts[].ManufacturerPartNumber]' fixtures/mouser_partnumber_batch.json
```

### If the Mouser key hasn't arrived
Save this as `fixtures/mouser_partnumber_SYNTHETIC.json` and replace it with the real file later. The prices are made up.
```json
{"Errors":[],"SearchResults":{"NumberOfResult":1,"Parts":[{"Availability":"12,345 In Stock","AvailabilityInStock":"12345","AvailabilityOnOrder":[],"DataSheetUrl":"https://example.invalid/ds.pdf","Description":"Operational Amplifiers - Op Amps Dual","FactoryStock":"0","ImagePath":"https://example.invalid/img.jpg","Category":"Operational Amplifiers - Op Amps","LeadTime":"42 Days","LifecycleStatus":null,"Manufacturer":"Texas Instruments","ManufacturerPartNumber":"LM358DR","Min":"1","Mult":"1","MouserPartNumber":"595-LM358DR","ProductAttributes":[{"AttributeName":"Packaging","AttributeValue":"Reel"},{"AttributeName":"Packaging","AttributeValue":"Cut Tape"}],"PriceBreaks":[{"Quantity":1,"Price":"$0.42","Currency":"USD"},{"Quantity":10,"Price":"$0.297","Currency":"USD"},{"Quantity":100,"Price":"$0.186","Currency":"USD"},{"Quantity":2500,"Price":"$0.104","Currency":"USD"}],"ProductDetailUrl":"https://example.invalid/pd","Reeling":true,"ROHSStatus":"RoHS Compliant","SuggestedReplacement":"","ProductCompliance":[{"ComplianceName":"USHTS","ComplianceValue":"8542330001"}]}]}}
```

---

## 5. Step 3: Repo files

### `.gitignore` and `.dockerignore` (same content)
```
.env
*.env
!.env.example
data/
results/
.venv/
__pycache__/
```

### `.claude/settings.json` (stops Claude Code from reading secrets)
```json
{ "permissions": { "deny": ["Read(./.env)", "Read(./**/*.env)"] } }
```

### `bench/testset.csv`
This is a starter set of 20 parts. Add about 30 real MPNs from recent PRC BOMs, including 2–3 known EOL or NRND parts, to reach about 50.
```csv
mpn,manufacturer,qty,note
LM358DR,Texas Instruments,100,baseline IC
NE555DR,Texas Instruments,50,baseline IC
STM32F103C8T6,STMicroelectronics,20,MCU
ATMEGA328P-AU,Microchip Technology,25,MCU
ESP32-WROOM-32E-N4,Espressif Systems,10,module
GRM188R71C104KA01D,Murata,4000,passive reel qty
RC0603FR-0710KL,YAGEO,5000,passive reel qty
CL10A106KP8NNNC,Samsung Electro-Mechanics,500,passive
LM1117IMPX-3.3/NOPB,Texas Instruments,50,slash in MPN
MCP2515-I/SO,Microchip Technology,10,slash in MPN
"74HC595D,118",Nexperia,100,comma in MPN
B2B-XH-A(LF)(SN),JST,200,parentheses in MPN
MAX232CPE+,Analog Devices,5,plus sign in MPN
0022232041,Molex,100,leading zeros
2N7002,,500,ambiguous manufacturer
BSS138,,500,ambiguous manufacturer
TPS62160DGKR,Texas Instruments,30,power IC
W25Q128JVSIQ,Winbond,30,flash
USB4105-GF-A,GCT,50,connector
CH340G,WCH,20,probably not stocked: not-found test
```

### `CLAUDE.md` (full spec for Claude Code; paste as-is)

````markdown
# Taraz Parts Pricing Service

## Purpose
Read-only service returning live price, stock, MOQ, order multiple and lead time for electronic parts
from DigiKey and Mouser, normalized to one schema. Consumers:
- CoreOps OS Planner console and PRC console → REST API
- Claude / AI agents → MCP server (thin client of the REST API)
Phase 2 adds Arrow, Verical, Future Electronics, Element14, Online Components. Every distributor MUST
sit behind the same `Provider` interface so adding one = adding one file.

## Hard rules
1. READ-ONLY. No code for DigiKey Ordering/Quoting/MyLists/Order Status or any Mouser cart/order API.
2. Secrets only from env vars via pydantic-settings. Never log, print, return, or embed them in
   exception messages. Never open or print `.env` or `*.env`.
3. Mouser's key is a URL query param (`?apiKey=`). Set loggers `httpx` and `httpcore` to WARNING.
   Install a logging.Filter on the root logger that rewrites `apiKey=[^&\s"']+` → `apiKey=***`.
   When converting httpx exceptions, never include `str(request.url)`; use provider + path only.
4. Never invent data. Missing field → null + an entry in `warnings`. A provider error is NEVER
   reported as "not found".
5. Run uvicorn with exactly 1 worker (rate limiter and token cache live in-process).
6. Money: compute with `decimal.Decimal`; serialize as JSON numbers rounded to 5 dp.
7. All timestamps ISO-8601 UTC.

## Stack
Python 3.12, FastAPI, uvicorn[standard], httpx (async), pydantic v2, pydantic-settings, aiosqlite,
mcp (official Python SDK, FastMCP; latest 1.x), pytest, pytest-asyncio, respx.
After install, pin exact versions in requirements.txt (pip freeze of direct deps).

## Layout
app/
  config.py        Settings (pydantic-settings), logging setup + redaction filter
  models.py        Pydantic models (below)
  normalize.py     parse_price, parse_lead_time_weeks, parse_int_stock, mpn_strict, mpn_loose
  pricing.py       price_offer(), choose_best(), bom_summary()
  storage.py       SQLite: cache, quota, request_log
  limiter.py       MinuteLimiter
  providers/base.py     Provider ABC: name, configured, get_many(mpns) -> dict[str, ProviderLookup], keyword(q, limit)
  providers/digikey.py
  providers/mouser.py
  service.py       orchestration: cache → providers → pricing
  api.py           FastAPI app
  mcp_server.py    FastMCP server calling the REST API
bench/ bench_direct.py, compare.py, testset.csv
fixtures/          real captured responses (use in tests)
tests/
scripts/smoke_live.sh
Dockerfile, docker-compose.yml, .env.example

## Config (env)
| var | default | notes |
|---|---|---|
| DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET | – | provider "not_configured" if empty |
| DIGIKEY_API_BASE | https://api.digikey.com | sandbox: https://sandbox-api.digikey.com |
| DIGIKEY_SITE / DIGIKEY_LANGUAGE / DIGIKEY_CURRENCY | US / en / USD | sent as locale headers |
| DIGIKEY_DAILY_LIMIT | 1000 | |
| DIGIKEY_PER_MINUTE | 100 | upstream allows 120 |
| DIGIKEY_CONCURRENCY | 4 | |
| MOUSER_SEARCH_API_KEY | – | |
| MOUSER_API_BASE | https://api.mouser.com | |
| MOUSER_DAILY_LIMIT | 1000 | |
| MOUSER_PER_MINUTE | 25 | upstream allows 30 |
| MOUSER_BATCH_SIZE | 10 | set 1 if pipe batching fails |
| QUOTA_RESERVE | 50 | calls kept back per provider per day |
| CACHE_TTL_HOURS | 12 | found parts |
| NOTFOUND_TTL_HOURS | 24 | not-found results (saves quota) |
| SEARCH_TTL_HOURS | 6 | keyword search |
| MAX_WAIT_SECONDS | 15 | max wait for per-minute limiter / Retry-After |
| REQUEST_TIMEOUT_SECONDS | 20 | |
| BOM_MAX_LINES | 300 | |
| DATA_DIR | ./data | sqlite file: DATA_DIR/pricing.db |
| CONSOLE_API_KEYS | – | "planner:KEY,prc:KEY,mcp:KEY" (name:key pairs) |
| LOG_LEVEL | INFO | |
MCP container only: PRICING_API_URL, PRICING_API_KEY, MCP_BEARER_TOKEN, MCP_PORT (8090).

## Models (app/models.py)
PriceBreak { qty:int, unit_price:Decimal }
Offer {
  provider:"digikey"|"mouser", mpn:str, manufacturer:str|None, sku:str|None, description:str|None,
  packaging:str|None, marketplace:bool=False, stock:int|None, on_order:int|None,
  moq:int|None, multiple:int|None, price_breaks:list[PriceBreak], currency:str|None,
  extra_fee:Decimal|None, lead_time_weeks:int|None, lifecycle:str|None, rohs:str|None,
  datasheet_url, product_url, image_url: str|None,
  match:"exact"|"loose", fetched_at:datetime, from_cache:bool, warnings:list[str]
}
PricedOffer = Offer + {
  requested_qty:int, order_qty:int|None, unit_price:Decimal|None, extended_price:Decimal|None,
  in_stock:bool|None, better_buy:{qty,unit_price,extended_price}|None,
  excluded_from_best:str|None   # "marketplace"|"manufacturer_mismatch"|"loose_match"|"no_price"
}
ProviderStatus { provider, ok:bool, found:bool, from_cache:bool,
  error_code: None|"not_configured"|"rate_limited"|"quota_exhausted"|"auth_failed"|"upstream_error"|"timeout",
  message:str|None, latency_ms:int|None, quota_remaining_today:int|None }
ProviderLookup (internal) { found:bool, offers:list[Offer], status:ProviderStatus }
PartResult {
  line_id:str|None, query:{mpn, qty, manufacturer},
  offers:list[PricedOffer],                      # sorted: provider, then extended_price asc
  best_by_provider:{digikey:PricedOffer|None, mouser:PricedOffer|None},
  best:PricedOffer|None, best_reason:str|None,   # "cheapest_in_stock"|"no_stock_anywhere"|None
  provider_status:list[ProviderStatus], warnings:list[str]
}
BomResult { lines:list[PartResult], summary:{...see REST}, provider_status, quota }

## Normalization (app/normalize.py)
mpn_strict(s) = s.strip().upper() with internal whitespace removed.
mpn_loose(s)  = mpn_strict(s) with "-", "/", ".", ",", "#", "(", ")", "+" removed.
Offer.match = "exact" if mpn_strict equal, "loose" if only mpn_loose equal; else not a match (drop).
parse_price(str) -> Decimal. Strip currency symbols/letters/spaces. If both "," and "." present, the
LAST one is the decimal separator. If only "," : decimal if followed by 1–2 or 4+ digits, thousands if
followed by exactly 3 digits. Tests:
  "$0.54"→0.54 | "0,54 €"→0.54 | "1.234,56 €"→1234.56 | "$1,234.56"→1234.56 | "PKR 120.00"→120.00
  "1,234"→1234 | "12,5"→12.5 | "$0.00012"→0.00012 | ""/None→None
parse_lead_time_weeks: "42 Days"→6, "45 Days"→7 (ceil), "10 Weeks"→10, "52"→52, ""/None/"N/A"→None
parse_int_stock: int or "12345"→12345, "1,234 In Stock"→1234, "None"/"On Order"/""→0, None→None

## DigiKey adapter (Product Information v4)
Token: POST {BASE}/v1/oauth2/token, form-urlencoded: client_id, client_secret,
grant_type=client_credentials. Cache token until expires_in-60s. On 401 from a data call: refresh once,
retry once; second 401 → error_code auth_failed (message: "check app is subscribed to Product Information v4").
Headers on data calls: Authorization: Bearer <t>, X-DIGIKEY-Client-Id, X-DIGIKEY-Locale-Site,
X-DIGIKEY-Locale-Language, X-DIGIKEY-Locale-Currency, Accept: application/json.
Lookup by MPN: GET {BASE}/products/v4/search/{quote(mpn, safe="")}/productdetails → {"Product": {...}}
  - 404 → fallback; 200 but Product.ManufacturerProductNumber not exact/loose match → fallback.
  - fallback: POST {BASE}/products/v4/search/keyword {"Keywords": mpn, "Limit": 10, "Offset": 0}
    → use "ExactMatches" if non-empty else filter "Products" by match. None → found=false.
429: read Retry-After; if <= MAX_WAIT_SECONDS sleep and retry once, else rate_limited. 5xx/timeout:
retry once after 1 s, then upstream_error/timeout. Store response header X-RateLimit-Remaining.
Mapping: ONE Offer per Product.ProductVariations[] item:
  mpn ← Product.ManufacturerProductNumber; manufacturer ← Product.Manufacturer.Name
  description ← Product.Description.ProductDescription (fallback DetailedDescription)
  sku ← var.DigiKeyProductNumber; packaging ← var.PackageType.Name
  stock ← var.QuantityAvailableforPackageType (fallback Product.QuantityAvailable)
  moq ← var.MinimumOrderQuantity
  multiple ← var.StandardPackage if "Tape & Reel" in packaging else 1
  price_breaks ← var.StandardPricing[] {BreakQuantity, UnitPrice}
  marketplace ← var.MarketPlace; extra_fee ← var.DigiReelFee if "Digi-Reel" in packaging (warning "digi_reel_fee")
  currency ← response SearchLocaleUsed.Currency, else DIGIKEY_CURRENCY
  lead_time_weeks ← parse(Product.ManufacturerLeadWeeks); lifecycle ← Product.ProductStatus.Status
  rohs ← Product.Classifications.RohsStatus; datasheet_url ← Product.DatasheetUrl
  product_url ← Product.ProductUrl; image_url ← Product.PhotoUrl
FIRST check these names against fixtures/digikey_*.json and fix the mapping where they differ; list differences.
Concurrency: asyncio.Semaphore(DIGIKEY_CONCURRENCY). Each upstream call reserves 1 quota.
Keyword search (for /v1/search): POST keyword with Limit=limit; one Offer per Product using the
"Cut Tape" variation if present, else the first variation.

## Mouser adapter (Search API v1)
Part lookup: POST {BASE}/api/v1/search/partnumber?apiKey=KEY
  body {"SearchByPartRequest":{"mouserPartNumber":"A|B|C","partSearchOptions":"Exact"}}
  Chunk MPNs by MOUSER_BATCH_SIZE (an MPN containing "|" goes alone). 1 quota per HTTP call.
  Assign each returned Part to the queried MPN it matches (strict, then loose). Queried MPNs with no
  matching Part → found=false. Skip parts whose MouserPartNumber is "", "N/A" or contains "NOT" (warning).
Keyword: POST {BASE}/api/v1/search/keyword?apiKey=KEY
  body {"SearchByKeywordRequest":{"keyword":q,"records":min(limit,50),"startingRecord":0,
        "searchOptions":"None","searchWithYourSignUpLanguage":"false"}}
Errors: non-empty "Errors" array → upstream_error with Errors[0].Message (if message mentions
"too many"/"rate"/"limit" → rate_limited). HTTP 429 → rate_limited. 5xx/timeout → retry once after 1 s.
Mapping per Part:
  mpn ← ManufacturerPartNumber; manufacturer ← Manufacturer; sku ← MouserPartNumber
  description ← Description; packaging ← join of ProductAttributes[AttributeName=="Packaging"].AttributeValue
  stock ← parse_int_stock(AvailabilityInStock) else parse_int_stock(Availability)
  on_order ← sum(AvailabilityOnOrder[].Quantity) or None
  moq ← int(Min); multiple ← int(Mult)
  price_breaks ← PriceBreaks[] {Quantity, parse_price(Price)}; currency ← PriceBreaks[0].Currency
  lead_time_weeks ← parse(LeadTime); lifecycle ← LifecycleStatus; rohs ← ROHSStatus
  datasheet_url ← DataSheetUrl; product_url ← ProductDetailUrl; image_url ← ImagePath
Check names against fixtures/mouser_*.json first.

## Pricing (app/pricing.py)
price_offer(offer, qty):
  order_qty = max(qty, offer.moq or 1)
  if multiple > 1: order_qty = ceil(order_qty / multiple) * multiple   (note "rounded_to_multiple")
  breaks sorted asc; none → unit/extended None, excluded_from_best "no_price"
  if order_qty < breaks[0].qty: order_qty = breaks[0].qty (note "raised_to_first_break")
  unit = price of the largest break.qty <= order_qty
  extended = unit*order_qty + (extra_fee or 0)
  in_stock = None if stock is None else stock >= order_qty
  better_buy = the break b with b.qty > order_qty minimising b.qty*b.price, only if < extended
choose_best(priced, manufacturer):
  exclude marketplace, match=="loose", no price, and manufacturer mismatch when a manufacturer was
  requested (casefold; mismatch if neither name contains the other's first word).
  If remaining offers have >1 currency → best None, warning "currency_mismatch".
  Prefer in_stock==True; among them min extended, tie → higher stock. reason "cheapest_in_stock".
  If none in stock → min extended, reason "no_stock_anywhere".
  best_by_provider uses the same rules per provider.
Worked tests (breaks (1,0.42),(10,0.297),(100,0.186), moq 1, mult 1 unless stated):
  qty 50 → order 50, unit 0.297, ext 14.85, better_buy None
  qty 90 → unit 0.297, ext 26.73, better_buy {100, 0.186, 18.60}
  breaks (2500,0.104), moq 2500, mult 2500, qty 100 → order 2500, ext 260.00
  mult 4, qty 10 → order 12
  breaks start at 10, moq None, qty 5 → order 10
  stock 30, qty 50 → in_stock False
  no breaks → unit None, excluded "no_price"

## Storage (app/storage.py, aiosqlite, WAL mode)
CREATE TABLE IF NOT EXISTS part_cache(provider TEXT, cache_key TEXT, found INTEGER NOT NULL,
  payload TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(provider, cache_key));
  cache_key = mpn_strict + "|" + currency. TTL: CACHE_TTL_HOURS if found else NOTFOUND_TTL_HOURS.
  Errors are NEVER cached.
CREATE TABLE IF NOT EXISTS search_cache(provider TEXT, cache_key TEXT, payload TEXT, fetched_at TEXT,
  PRIMARY KEY(provider, cache_key));
CREATE TABLE IF NOT EXISTS quota_usage(provider TEXT, day TEXT, used INTEGER NOT NULL DEFAULT 0,
  upstream_remaining INTEGER, PRIMARY KEY(provider, day));
  reserve(provider): in one transaction, if used+1 > DAILY_LIMIT-QUOTA_RESERVE → raise QuotaExhausted,
  else used+=1. day = UTC date.
CREATE TABLE IF NOT EXISTS request_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, client TEXT,
  provider TEXT, op TEXT, n_parts INTEGER, status TEXT, http_status INTEGER, latency_ms INTEGER,
  upstream_remaining INTEGER);
  status ∈ ok|not_found|cache_hit|error:<code>. Log every upstream call AND every cache hit.
MinuteLimiter(per_minute, max_wait): sliding 60 s window; acquire() waits; if it would wait longer
than max_wait → raise RateLimited.

## Service flow (app/service.py)
lookup_many(items, providers, fresh, client):
  for each provider: split MPNs into cache hits / misses (skip cache when fresh) → provider.get_many(misses)
  → write found/not-found to cache → merge → price → choose best → PartResult per line.
  Providers run concurrently. One provider failing never fails the request (partial result).

## REST API (app/api.py)
Auth: header X-API-Key must match a key in CONSOLE_API_KEYS; client name stored in request_log.
/health is public. Responses always 200 with provider_status unless input is invalid.
GET  /health → {"status":"ok","providers":{"digikey":"configured|not_configured","mouser":...}}
GET  /v1/part?mpn=&qty=1&manufacturer=&providers=digikey,mouser&fresh=false → PartResult
     (MPN is a query param, NOT a path segment: MPNs contain "/", "#", ",").
POST /v1/search {"keyword":str,"limit":10,"providers":[...]} →
     {"keyword","results":[Offer],"total_by_provider":{},"provider_status":[]}
POST /v1/bom/price {"lines":[{"line_id":"R1","mpn":"...","qty":100,"manufacturer":null}],
     "providers":["digikey","mouser"],"fresh":false} → BomResult; >BOM_MAX_LINES → 413.
     Duplicate MPNs are looked up once.
     summary = {"lines":n,"priced":n,"not_found":n,"short_stock":n,"currency":"USD"|null,
       "totals":{"digikey":{"total":x,"lines_covered":n,"lines_missing":n},
                 "mouser":{...},
                 "best_mix":{"total":x,"lines_by_provider":{"digikey":n,"mouser":n}}}}
GET  /v1/quota → {"day":"YYYY-MM-DD","providers":{"digikey":{"used","limit","reserve","remaining",
     "upstream_remaining_last_seen"},"mouser":{...}}}
GET  /v1/stats?hours=24 → per provider: upstream_calls, errors, cache_hits, cache_hit_rate,
     latency_ms p50/p95 (upstream only), calls_by_client.

## MCP server (app/mcp_server.py)
FastMCP("taraz-parts-pricing", stateless_http=True). Tools call the REST API with httpx using
PRICING_API_URL + PRICING_API_KEY. NO distributor keys in this process.
All tools annotated readOnlyHint=True, openWorldHint=True. Return the REST JSON.
  search_parts(keyword:str, limit:int=10)
  get_part_offers(mpn:str, qty:int=1, manufacturer:str|None=None)
  price_bom(lines:list[{mpn:str, qty:int, line_id?:str, manufacturer?:str}], fresh:bool=False)
  quota_status()
Docstrings must tell the model: prices are live distributor list prices (USD), cached up to 12 h, and
quota is ~1000 calls/day per distributor, so use price_bom for many parts instead of looping.
Serve: app = mcp.streamable_http_app(); wrap it in a pure-ASGI BearerAuth middleware (check
Authorization == "Bearer " + MCP_BEARER_TOKEN for scope type "http"; pass "lifespan" through;
401 JSON otherwise); uvicorn.run(wrapped, host="0.0.0.0", port=MCP_PORT). Endpoint path: /mcp.

## Tests (all must pass, no network: respx + fixtures)
test_normalize: every parse case above. test_pricing: every worked case + best selection
(in-stock preferred, marketplace/loose/manufacturer mismatch excluded, currency_mismatch).
test_digikey: token cached; fixture mapping (variation count = offers); 404 → keyword fallback;
mismatched MPN → fallback; 401 → refresh once; 429 Retry-After; URL-encoding of "LM1117IMPX-3.3/NOPB".
test_mouser: batch body uses "|"; fixture mapping; missing MPN → found false; Errors array → error;
caplog contains no real key and no "apiKey=" followed by anything except "***".
test_storage: TTL expiry; not-found TTL; reserve stops at limit-reserve; new UTC day resets.
test_api: 401 without key; partial result when Mouser errors; BOM totals; dedupe; 413.
test_mcp: list_tools shows 4 tools all readOnlyHint; get_part_offers calls mocked REST.

## Live acceptance (scripts/smoke_live.sh, uses real keys, ~15 calls total)
1. /health both configured. 2. /v1/part?mpn=LM358DR&qty=100 → both providers ok, offers>0.
3. Same call again → from_cache true, response < 50 ms. 4. mpn=LM1117IMPX-3.3/NOPB works.
5. mpn=NOTAREALPART12345XYZ → found false for both, NOT an error.
6. BOM of 20 lines (bench/testset.csv head) → mouser upstream calls ≤ 2 (check /v1/stats).

## Benchmark (bench/bench_direct.py)
Read bench/testset.csv. Save /v1/quota before. Pass 1: POST /v1/bom/price fresh=true (time it).
Pass 2: same with fresh=false. Pass 3: GET /v1/part for each line, fresh=false (per-call ms).
Save each PartResult to results/direct/<safe_mpn>.json, quota after, /v1/stats, and
results/direct/summary.json {bom_cold_ms, bom_warm_ms, part_warm_p50, part_warm_p95, quota_used}.

## Commands
uv venv --python 3.12 .venv && . .venv/bin/activate && uv pip install -r requirements.txt
pytest -q
uvicorn app.api:app --host 127.0.0.1 --port 8080 --workers 1
python -m app.mcp_server
````

---

## 6. Step 4: Claude Code prompts (paste in order, one at a time)

Start Claude Code in `~/taraz-parts-pricing`. After each prompt, check that its tests pass before sending the next one.

```
1) Read CLAUDE.md fully and every file in fixtures/. Do not write code yet. Report: (a) every place
the DigiKey/Mouser field names in the spec differ from the fixtures, (b) whether Mouser pipe batching
worked in fixtures/mouser_partnumber_batch.json, (c) your build plan. Never open .env.
```
```
2) Create the project skeleton, requirements.txt, app/config.py (with the apiKey redaction filter),
app/models.py, app/normalize.py, app/pricing.py, and tests/test_normalize.py + tests/test_pricing.py
with every case listed in CLAUDE.md. Run pytest until green.
```
```
3) Implement app/storage.py and app/limiter.py exactly per CLAUDE.md with tests/test_storage.py.
Use a temp DATA_DIR in tests. pytest green.
```
```
4) Implement providers/base.py and providers/digikey.py using the real fixtures (apply the mapping
fixes you found in step 1). tests/test_digikey.py with respx covering every case in CLAUDE.md.
```
```
5) Implement providers/mouser.py with tests/test_mouser.py, including the caplog test proving the API
key never appears in logs. If fixtures show batching fails, default MOUSER_BATCH_SIZE to 1.
```
```
6) Implement app/service.py and app/api.py with tests/test_api.py. Then write scripts/smoke_live.sh
(reads keys from the environment only, uses curl + jq against 127.0.0.1:8080, X-API-Key from
$CONSOLE_KEY) covering the 6 live acceptance checks. Show me how to run it; do not print secrets.
```
```
7) Implement app/mcp_server.py per CLAUDE.md (FastMCP, read-only annotations, pure-ASGI bearer
middleware) and tests/test_mcp.py using the MCP python client against the in-process app with the REST
API mocked. Then give me a one-line command to list its tools against a running instance.
```
```
8) Write Dockerfile (python:3.12-slim, non-root user uid 10001, /data owned by that user),
docker-compose.yml (services api + mcp; api uses env_file .env and named volume pricing-data:/data,
port 127.0.0.1:8080:8080, 1 worker; mcp gets ONLY PRICING_API_URL=http://api:8080,
PRICING_API_KEY=${MCP_CONSOLE_KEY}, MCP_BEARER_TOKEN=${MCP_BEARER_TOKEN}, port 127.0.0.1:8090:8090;
restart unless-stopped), .env.example with every var from CLAUDE.md, and bench/bench_direct.py.
```

### Full `.env` (chmod 600)
Generate each secret with `openssl rand -hex 32`. Use a different key for each console so you can see who uses the quota and revoke one without affecting the others.
```
DIGIKEY_CLIENT_ID=
DIGIKEY_CLIENT_SECRET=
DIGIKEY_SITE=US
DIGIKEY_LANGUAGE=en
DIGIKEY_CURRENCY=USD
MOUSER_SEARCH_API_KEY=
CONSOLE_API_KEYS=planner:<hex>,prc:<hex>,mcp:<hex>
MCP_CONSOLE_KEY=<same hex as mcp: above>
MCP_BEARER_TOKEN=<hex>
DATA_DIR=/data
```

---

## 7. Step 5: Deploy

```bash
docker compose up -d --build
curl -s localhost:8080/health | jq
export CONSOLE_KEY=<planner hex>
curl -s -H "X-API-Key: $CONSOLE_KEY" "localhost:8080/v1/part?mpn=LM358DR&qty=100" | jq '.best, .provider_status'
bash scripts/smoke_live.sh
docker compose logs api | grep -c "apiKey="   # every match must be apiKey=***
```

- **Consoles on another host:** put the API behind your existing nginx or Caddy with TLS and allow only the CoreOps OS host. Keep the `127.0.0.1` port binding.
- **Claude Code on the server:** add the MCP server as an HTTP server at `http://127.0.0.1:8090/mcp` with the header `Authorization: Bearer <MCP_BEARER_TOKEN>`. Run `claude mcp add --help` for the header flag in your version.

---

## 8. Step 6: Partuno 4.0.1 test (separate folder, in parallel)

### Facts from Partuno's docs
- **Env variables:** `PARTUNO_MODE=local`, `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET`, an optional `DIGIKEY_ACCESS_TOKEN`, `DIGIKEY_SITE`, `DIGIKEY_LANGUAGE`, `DIGIKEY_CURRENCY`, `MOUSER_SEARCH_API_KEY`, `MOUSER_ACCOUNT_API_KEY`, `MOUSER_MINUTE_LIMIT`, `MOUSER_DAILY_LIMIT`.
- **Mouser tools:** `search_mouser_products`, `compare_component_offers`, `search_mouser_order_history`, `get_mouser_order`, `get_mouser_cart`, `preview_mouser_cart_change`, `execute_mouser_cart_change`.
- **Transport:** stdio by default, which opens no network socket. An optional loopback HTTP mode serves `http://127.0.0.1:8000/mcp`.
- **Storage:** none. Local mode has no database or Redis.

### Install and security review
```bash
mkdir -p ~/partuno-test/results && cd ~/partuno-test
uv venv --python 3.12 .venv && . .venv/bin/activate
uv pip install "partuno==4.0.1" mcp
uv pip freeze > partuno-4.0.1.lock.txt
uvx pip-audit -r partuno-4.0.1.lock.txt | tee results/pip_audit.txt
partuno-mcp --help | tee results/help.txt

# Which hosts does the code call? Any telemetry?
pip show -f partuno | tee results/files.txt
SITE=$(python -c "import site; print(site.getsitepackages()[0])")
grep -rhoE "https?://[A-Za-z0-9.-]+" $SITE/partuno* 2>/dev/null | sort -u | tee results/hosts.txt
grep -rliE "telemetry|analytics|sentry|posthog" $SITE/partuno* 2>/dev/null | tee results/telemetry.txt
```
If `grep` finds nothing, the modules may be installed top-level. Use the file list in `results/files.txt` to grep the right paths.

### `~/partuno-test/partuno.env` (chmod 600)
This uses the second DigiKey app and has no Mouser account key, which disables the cart and history tools.
```
PARTUNO_MODE=local
DIGIKEY_CLIENT_ID=<taraz-partuno-test app>
DIGIKEY_CLIENT_SECRET=<taraz-partuno-test app>
DIGIKEY_API_BASE=https://api.digikey.com
DIGIKEY_SITE=US
DIGIKEY_LANGUAGE=en
DIGIKEY_CURRENCY=USD
MOUSER_SEARCH_API_KEY=<same Mouser search key>
MOUSER_API_BASE=https://api.mouser.com
MOUSER_MINUTE_LIMIT=25
MOUSER_DAILY_LIMIT=300
```

### `~/partuno-test/bench_partuno.py`
This runs Partuno over stdio and refuses to call any tool that could change something.
```python
#!/usr/bin/env python3
"""Partuno 4.0.1 benchmark over MCP stdio.
  python bench_partuno.py list
  python bench_partuno.py probe
  python bench_partuno.py run --tool compare_component_offers \
      --args '{"manufacturer":"__MFR__","manufacturer_part_number":"__MPN__","quantity":__QTY__}' --passes 2
Fill --args from the input_schema shown in results/partuno_tools.json.
"""
import argparse, asyncio, csv, json, os, re, statistics, time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
OUT = HERE / "results"
BLOCKED = re.compile(r"execute|submit|create|add|update|delete|remove|place", re.I)

def load_env(path):
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #")[0].strip().strip('"').strip("'")
        if v:
            env[k.strip()] = v
    return env

def params():
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), **load_env(HERE / "partuno.env")}
    return StdioServerParameters(command=str(HERE / ".venv/bin/partuno-mcp"), args=[], env=env)

def dump(result):
    content = []
    for c in result.content:
        content.append(c.text if getattr(c, "text", None) is not None else c.model_dump())
    return {"is_error": bool(result.isError), "structured": getattr(result, "structuredContent", None), "content": content}

def pct(xs, p):
    if not xs: return None
    if len(xs) == 1: return xs[0]
    return statistics.quantiles(xs, n=100)[p - 1]

async def session_run(fn):
    async with stdio_client(params()) as (r, w):
        async with ClientSession(r, w) as s:
            t = time.perf_counter(); await s.initialize()
            print(f"initialize: {round((time.perf_counter()-t)*1000)} ms")
            return await fn(s)

async def cmd_list(s):
    tools = (await s.list_tools()).tools
    data = [{"name": t.name, "description": t.description,
             "annotations": t.annotations.model_dump() if t.annotations else None,
             "input_schema": t.inputSchema} for t in tools]
    (OUT / "partuno_tools.json").write_text(json.dumps(data, indent=2))
    for t in data:
        ro = (t["annotations"] or {}).get("readOnlyHint")
        print(f"{'READ ' if ro else 'WRITE?'}  {t['name']}")
    print(f"\n{len(data)} tools -> results/partuno_tools.json")

async def cmd_probe(s):
    """Prove account tools are unusable with search-only keys. Read-only calls only."""
    names = {t.name for t in (await s.list_tools()).tools}
    report = {}
    for tool in ["get_mouser_cart", "search_mouser_order_history"]:
        if tool not in names:
            report[tool] = "not exposed"; continue
        try:
            res = await asyncio.wait_for(s.call_tool(tool, {}), timeout=60)
            report[tool] = dump(res)
        except Exception as e:
            report[tool] = f"{type(e).__name__}: {e}"
    (OUT / "security_probe.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str)[:3000])

def fill(template, row):
    esc = lambda v: (v or "").replace("\\", "\\\\").replace('"', '\\"')
    s = template.replace("__MPN__", esc(row["mpn"])).replace("__MFR__", esc(row.get("manufacturer"))).replace("__QTY__", str(int(row["qty"])))
    return json.loads(s)

def make_run(tool, template, testset, passes):
    async def run(s):
        rows = list(csv.DictReader(open(testset, newline="")))
        outdir = OUT / "partuno"; outdir.mkdir(parents=True, exist_ok=True)
        summary = {}
        for p in range(1, passes + 1):
            times, errors = [], 0
            t_pass = time.perf_counter()
            for row in rows:
                args = fill(template, row)
                t = time.perf_counter()
                try:
                    res = dump(await asyncio.wait_for(s.call_tool(tool, args), timeout=120))
                except Exception as e:
                    res = {"is_error": True, "content": [f"{type(e).__name__}: {e}"]}
                ms = round((time.perf_counter() - t) * 1000); times.append(ms)
                errors += res["is_error"]
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", row["mpn"])
                (outdir / f"pass{p}_{safe}.json").write_text(json.dumps({"mpn": row["mpn"], "qty": row["qty"], "ms": ms, "args": args, **res}, indent=2, default=str))
                print(f"pass{p} {row['mpn']:<26} {'ERR' if res['is_error'] else 'ok '} {ms:>6} ms")
            summary[f"pass{p}"] = {"total_ms": round((time.perf_counter() - t_pass) * 1000), "p50_ms": pct(times, 50),
                                   "p95_ms": pct(times, 95), "errors": errors, "calls": len(rows)}
        (OUT / "partuno_summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2))
    return run

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "probe", "run"])
    ap.add_argument("--tool"); ap.add_argument("--args")
    ap.add_argument("--testset", default=str(HERE / "testset.csv"))
    ap.add_argument("--passes", type=int, default=2)
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    if a.cmd == "list": fn = cmd_list
    elif a.cmd == "probe": fn = cmd_probe
    else:
        if not a.tool or not a.args: ap.error("run needs --tool and --args")
        if BLOCKED.search(a.tool): raise SystemExit(f"refusing to call possibly-mutating tool: {a.tool}")
        fn = make_run(a.tool, a.args, a.testset, a.passes)
    asyncio.run(session_run(fn))

if __name__ == "__main__":
    main()
```

### Run it
```bash
cp ~/taraz-parts-pricing/bench/testset.csv ~/partuno-test/
python bench_partuno.py list     # record every tool and whether it's marked read-only
python bench_partuno.py probe    # expect "not configured"-type errors for cart/history
python bench_partuno.py run --tool compare_component_offers --args '<built from the schema>' --passes 2
```

### Things to record during the Partuno test
1. **DigiKey auth with only client ID and secret.** If DigiKey calls fail, that's a key finding. As a test-only workaround, get a 2-legged token with the curl from Step 2, put it in `DIGIKEY_ACCESS_TOKEN`, and rerun. The token expires in about 10 minutes, so for server use Partuno would need a refresh wrapper.
2. **Unexpected outbound connections.** During a run, check `ss -tnp | grep partuno` from another shell for connections to anything other than DigiKey or Mouser.
3. **DigiKey account tools.** If `list` shows MyList or quote tools, confirm they fail because the app isn't subscribed to those APIs. Never call an `execute_*` tool (the script blocks them anyway).

---

## 9. Step 7: Benchmark and compare

```bash
cd ~/taraz-parts-pricing && python bench/bench_direct.py
```

Then give Claude Code this prompt:
```
9) Read results/direct/*.json and ~/partuno-test/results/partuno/pass1_*.json (look at a few first to
learn Partuno's output shape). Write bench/compare.py producing results/comparison.csv with columns:
mpn, qty, dk_unit_direct, dk_unit_partuno, dk_stock_direct, dk_stock_partuno, mouser_unit_direct,
mouser_unit_partuno, mouser_stock_direct, mouser_stock_partuno, best_direct, best_partuno, agree
(prices within 0.5%, stock within 1%), notes; plus results/comparison_summary.md with agreement %,
not-found counts, error counts, and both latency summaries side by side.
```

**Manual spot check.** Pick 10 parts, including the slash, comma and ambiguous ones. Look each up on digikey.com and mouser.com at the test quantity, and record: `mpn, qty, distributor, site unit price, site stock, direct value, partuno value, match`. Price and stock change minute to minute, so check within an hour of the bench run.

**Quota budget for today** (approximate):

| Use | DigiKey calls | Mouser calls |
|---|---|---|
| Smoke tests | ~5 | ~3 |
| Live acceptance | ~15 | ~5 |
| Direct benchmark | ~50–70 | ~5–10 |
| Partuno benchmark (separate DigiKey app) | ~100–150 | ~100 (capped at 300) |

---

## 10. Step 8: Write-up for TSL OS → Parts pricing

```
Parts pricing: DigiKey + Mouser evaluation (Phase 1)

Recommendation: <Direct API service | Partuno> — <one sentence why>

Results (50-part test set from the Pakistan server, <date>)
| | Partuno 4.0.1 | Direct service |
| Cold lookup p50 / p95 | | |
| Warm (cached) p50 / p95 | | |
| 50-line BOM cold / warm | | |
| Website spot-check match (10 parts) | | |
| Parts found DK / Mouser | | |
| Errors | | |
| API calls used for the BOM (DK / Mouser) | | |
| Shared cache across consoles | No (local mode, no DB) | Yes (SQLite, 12 h) |
| Works headless on server (DigiKey auth) | <finding> | Yes (client credentials) |
| Write tools exposed | <list from partuno_tools.json> | None |
| Fits Planner/PRC consoles (REST/JSON) | <finding> | Yes |
| Adding Phase 2 distributors | Fork needed | One adapter each |
| Maintenance owner | 1 external maintainer | Us |

Security: search-only keys; DigiKey app subscribed to Product Information v4 only; no Mouser
order key; ports bound to localhost; keys only in .env (600); API key redacted from logs.
Quota: 1000/day per distributor; requested DigiKey upgrade on <date>.
Next: connect Planner + PRC consoles to /v1/part and /v1/bom/price; Phase 2 adapters.
```

---

## 11. If something goes wrong

| Problem | Likely cause and fix |
|---|---|
| DigiKey token returns `invalid_client` | Wrong secret, or the new production app isn't active yet. Wait a few minutes and retry |
| DigiKey data call returns 401 with a valid token | The app isn't subscribed to Product Information v4, or sandbox credentials are used against the production URL |
| Mouser batch returns only one part | Set `MOUSER_BATCH_SIZE=1`. It costs more quota, so note it in the write-up |
| Mouser key not here by evening | Ship with the synthetic fixture, run the Mouser live test tomorrow, and say so in the post |
| Daily quota numbers disagree with DigiKey's | Our counter resets at midnight UTC. DigiKey's `X-RateLimit-Remaining` header is the real number |
| Partuno won't start | Save the `partuno-mcp --help` output and the error, and record it as a finding |

---

## 12. Open questions for the team

1. **How will the consoles call this?** Will they hit an HTTP API directly, or does CoreOps OS route through n8n?
2. **What does "live" mean?** Is a 4–24 hour cache acceptable for prices and stock, or does PRC need fresh numbers every time?
3. **Do we need contract pricing?** List prices are enough for comparison. Taraz account pricing uses extra DigiKey calls per part.
4. **What currency should we show?** USD on both sides, or converted to PKR in the service?
5. **Phase 2:** Verical is an Arrow marketplace. Check whether Arrow's API covers both before planning two adapters.

---

## Sources
- Partuno README: https://github.com/JPMarhefka/Partuno
- Partuno capabilities: https://github.com/JPMarhefka/Partuno/blob/main/docs/capabilities.md
- Partuno security model: https://github.com/JPMarhefka/Partuno/blob/main/docs/security.md
- Partuno .env.example: https://github.com/JPMarhefka/Partuno/blob/main/.env.example
- Partuno local deployment: https://github.com/JPMarhefka/Partuno/blob/main/docs/deployment/local.md
- Partuno Mouser MCP guide: https://github.com/JPMarhefka/Partuno/blob/main/docs/guides/mouser-mcp-server.md
- DigiKey API rate limits: https://developer.digikey.com/documentations
- DigiKey API FAQ: https://developer.digikey.com/faq/products-plans-and-apis
- DigiKey ProductInformation V4 250000 plan: https://developer.digikey.com/ja/node/4361
- Mouser Search API: https://hu.mouser.com/api-search
