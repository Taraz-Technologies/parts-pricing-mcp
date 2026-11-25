# Taraz Parts Pricing Service

## Purpose
Read-only service returning live price, stock, MOQ, order multiple and lead time for electronic parts
from DigiKey and Mouser, normalized to one schema. Consumers:
- CoreOps OS Planner console and PRC console → REST API
- Claude / AI agents → MCP server (thin client of the REST API)
Phase 2 adds Arrow, Verical, Future Electronics, Element14, Online Components. Every distributor MUST
sit behind the same `Provider` interface so adding one = adding one file.

## Hard rules
1. NEVER PLACE ORDERS. No code for DigiKey Ordering/Quoting/Order Status or the Mouser Order API.
   The only writes allowed (approved 2026-11-05): Mouser Cart API view + items/insert, and DigiKey
   MyLists (one named list = the DigiKey "cart"; DigiKey has no cart API). Cart endpoints only for
   clients in CART_CLIENTS; cart MCP tools only in a separate deployment (MCP_ENABLE_CART=1).
2. Secrets only from env vars via pydantic-settings. Never log, print, return, or embed them in
   exception messages. Never open or print `.env` or `*.env`. One exception: the DigiKey 3-legged
   refresh token rotates on every use, so it lives in SQLite (kv table, data volume); never return it.
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
| CART_CLIENTS | – | console names allowed to use cart endpoints, e.g. "mcpcart"; empty = cart off |
| MOUSER_CART_API_KEY | – | My Mouser → APIs "Cart and Order" key (NOT the search key) |
| MOUSER_CART_KEY | – | cart to use; empty → first add creates one, remembered in kv |
| DIGIKEY_REDIRECT_URI | – | https://<api-domain>/v1/digikey/callback; must exactly match the DigiKey app |
| DIGIKEY_LIST_NAME | Taraz cart | MyList used as the DigiKey cart |
MCP container only: PRICING_API_URL, PRICING_API_KEY, MCP_BEARER_TOKEN, MCP_PORT (8090),
MCP_ENABLE_CART (cart deployment only, with an API key whose client is in CART_CLIENTS).

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

## DigiKey extras + MyLists (names verified against DigiKey's OAS files, 2026-11-05)
Read-only, 2-legged: specs ← Product.Parameters[{ParameterText, ValueText}] via the same productdetails →
keyword flow, plus the leaf category (end of Product.Category.ChildCategories: what spec search takes); substitutes ← GET /products/v4/search/{mpn}/substitutions (ProductSubstitutes[]) +
/alternatepackaging (AlternatePackagings.AlternatePackaging[]); related ← /associations
(ProductAssociations {MatingProducts, ForUseWithProducts, AssociatedProducts, Kits}). 404 → empty, not error.
MyLists needs 3-legged OAuth: GET {BASE}/v1/oauth2/authorize?response_type=code&client_id&redirect_uri&state
→ callback ?code (valid 1 min) → POST token grant_type=authorization_code. Refresh tokens ROTATE: save the
new one every refresh, under a lock; 400/401 on refresh → delete it, error_code "not_connected".
Lists: GET {BASE}/mylists/v1/lists?startIndex&limit (find ListName), POST /lists {"ListName"}, then re-find
by name (create's string response isn't documented as the id). Add: POST /lists/{id}/parts
[{RequestedPartNumber, Quantities:[{Quantity}], CustomerReference?}]. View: GET /lists/{id}/parts
(countryIso, currencyIso, languageIso) → PartsList[], price from Quantities[0].PackOptions[SelectedPackOptionIndex].
List id remembered in kv; 404 → forget it.

## DigiKey spec search (verified live 2026-11-25 against DigiKey's OAS + fixtures/digikey_categories,
## *_cat60_options, *_specs_10nF50V; the .request.json is exactly what the service sends)
Every DigiKey part in one leaf category whose parametric values match (alternates / SwapDB). Mouser has no
parametric API (its v1/v2 Swagger has no attribute fields): callers price_bom the MPNs, providers=["mouser"].
Category (name any case, or id): GET /products/v4/search/categories → Categories[{CategoryId, Name, Children[]}]
  (1201 nodes) → [id, name, parent id]. 27 names repeat → 422 asking for the id; a parent → 422 listing its leaves
  (DigiKey filters specs per leaf only). An empty tree is an upstream_error, never cached.
Options: POST keyword {"Keywords":"","Limit":1,"Offset":0,"FilterOptionsRequest":{"CategoryFilter":[{"Id":"60"}]}}
  → FilterOptions.ParametricFilters[{ParameterId, ParameterName, FilterValues[{ValueId, ValueName}]}] (every value
  of the category) + Manufacturers[{Id, Value}] (DigiKey.options_from). ValueId is the text for UnitOfMeasure
  params ("10 nF"), a number string otherwise. Products but no Manufacturers → upstream_error, never cached.
Spec name: casefold exact, else a unique prefix ("Voltage" → "Voltage - Rated"); two names for one parameter → 422.
  Value: normalize.match_values. A quantity with a prefix or unit compares by value only (10nF = 0.01 µF =
  10000 pF, DigiKey lists all three; 500 = 500V; 1 mOhms != 1 MOhms); everything else, bare numbers included, as
  text with case, spaces, ± and µ/u ignored (so 1005 != 01005) and NP0 = NPO = C0G = COG. Compared against every
  ",", "|" and letter-"/" piece of a value and the lead/paren of "lead (paren)": "0603" → "0603 (1608 Metric)",
  never "0201 (0603 Metric)"; "630V" → "600V/630V"; never "10W" from "1/10W". A list = any of. No match → 422 with
  the nearest values (by quantity when the want is one); NEVER drop a filter.
Search: same POST + "SortOptions":{"Field":"ManufacturerProductNumber","SortOrder":"Ascending"} (fixed order for
  Offset paging), FilterOptionsRequest {CategoryFilter, MarketPlaceFilter "ExcludeMarketPlace", StatusFilter
  [{"Id":"0"}] (Active; 1 Obsolete, 2 Discontinued at DigiKey, 4 Last Time Buy, 7 NRND), SearchOptions
  ["RohsCompliant","InStock"], ManufacturerFilter (include-only, so: every manufacturer not containing an excluded
  word), ParameterFilterRequest {CategoryFilter {Id}, ParameterFilters [{ParameterId, FilterValues [{Id}]}] sorted
  by ParameterId}}. ProductsCount = exact total. Limit 50, Offset pages in a TaskGroup up to max_results. A failed
  page cancels the rest and fails the search; so do a missing ProductsCount and fewer unique products than
  min(total, max_results). One result per product: _pick (DigiKey-sold Cut Tape, else first non-marketplace) +
  specs [{name, value}] from Parameters [{ParameterText, ValueText}]. DigiKey's keyword data can lag up to 24 h
  (its FAQ): price_bom gives real-time stock/price.
Cache: search_cache keys categories|, spec_options|<id>, spec_search|<filters, max_results, currency>; one fetch
  per key at a time (concurrent searches wait instead of spending quota); fresh=true refetches all three.

## Mouser cart (Cart API v1, MOUSER_CART_API_KEY)
GET /api/v1/cart?apiKey&cartKey; POST /api/v1/cart/items/insert?apiKey {"CartKey", "CartItems":[
{MouserPartNumber, Quantity, CustomerPartNumber?}]} (≤100 items per call; empty CartKey creates a cart).
Never POST /api/v1/cart (replaces the whole cart) or anything under /order.
Response CartItems[]: MouserPartNumber, MfrPartNumber, Quantity, UnitPrice, ExtendedPrice, Errors[],
InfoMessages[]; MerchandiseTotal, CurrencyCode. Cart and MyLists calls count against the same daily quota.

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

## Storage (app/storage.py, aiosqlite, WAL mode, autocommit: every write is one statement; explicit COMMITs
## raced other requests' open cursors on the shared connection)
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
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
  digikey_refresh_token, digikey_list_id, mouser_cart_key.
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
POST /v1/search/specs {"category","specs":{name: value|[values]} (1-30 specs, values 1-100 chars, lists ≤50),
     "rohs":true,"active_only":true,"in_stock":false,"exclude_manufacturers":[],"max_results":200 (≤500),
     "fresh":false} → {"category":{id,name},"filters":{"specs":{name:[DigiKey values]},"excluded_manufacturers",...},
     "total","truncated","results":[Offer + "specs"],"warnings","provider_status"}. DigiKey only. Unknown or
     misspelled fields, and categories/specs/values DigiKey doesn't have → 422 (a dropped filter returns wrong parts).
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
GET  /v1/part/{specs|substitutes|related}?mpn= → DigiKey only, cached SEARCH_TTL_HOURS:
     {"mpn","products"|"substitutes"+"alternate_packaging"|"related":[...],"provider_status":[...]}
Cart (403 unless the client is in CART_CLIENTS):
GET  /v1/cart → {"carts":[Cart],"provider_status":[...]}
POST /v1/cart/add {"items":[{"provider":"mouser"|"digikey","sku","qty","customer_ref"?}]} → same + note
GET  /v1/digikey/connect → {"connected","authorize_url","message"} (one-time state, 10 min)
GET  /v1/digikey/callback?code&state → public; the one-time state is the gate. Log redaction covers code=.

## MCP server (app/mcp_server.py)
FastMCP("taraz-parts-pricing", stateless_http=True). Tools call the REST API with httpx using
PRICING_API_URL + PRICING_API_KEY. NO distributor keys in this process.
Read tools annotated readOnlyHint=True, openWorldHint=True. Return the REST JSON.
  search_parts(keyword:str, limit:int=10)
  search_by_specs(category, specs:dict, in_stock=False, exclude_manufacturers=None, rohs=True, active_only=True,
                  max_results=50)   docstring maps Taraz PNs (TZCC10NF50V10%X7R0603, TZR22K1%0.1W0603) to DigiKey
                  names. Returns compact JSON text (~800 chars/part, so it fits MCP output caps): lean offer
                  fields, price_breaks [[qty, unit]], and only the specs that were not searched.
  get_part_offers(mpn:str, qty:int=1, manufacturer:str|None=None)
  price_bom(lines:list[{mpn:str, qty:int, line_id?:str, manufacturer?:str}], fresh:bool=False,
            providers:list["digikey"|"mouser"]|None=None)   one distributor spends none of the other's quota
  quota_status(), get_part_specs(mpn), find_substitutes(mpn), get_related_parts(mpn)
Cart tools, only when MCP_ENABLE_CART=1 (server name "taraz-parts-cart"):
  review_cart(), connect_digikey() (readOnlyHint), add_to_cart(items) (readOnlyHint=False,
  destructiveHint=False). add_to_cart's docstring tells the model to confirm items with the user first.
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
test_mcp: read server lists 8 tools all readOnlyHint; cart server adds 3; get_part_offers calls mocked REST.
test_specs: the example sends exactly fixtures/digikey_keyword_specs_10nF50V.request.json; filters echo; mapping
(ValueText, cut tape, no marketplace/Vishay); cache key = the DigiKey request (spelling/order-independent; other
values, max_results, exclusions, fresh, category miss); flags off send no filter; bad category/spec/value,
duplicate parameter, parent category → 422 with no search; invalid or misspelled request fields → 422 before any
call; every DigiKey failure (5xx, 4xx, short/shifted page, no ProductsCount, empty options/tree) → provider_status,
not cached; exact page calls; a failed page cancels the rest; _pick; leaf category. test_normalize:
parse_quantity, match_values, closest_values. test_storage: concurrent interleaved requests on one connection.
test_mcp: search_by_specs request + compact output; price_bom providers.
test_cart: specs cached; substitutes/related mapping; cart endpoints 403 for non-cart clients; not
connected → not_connected; OAuth state one-use; refresh token rotation saved; MyList find-or-create + add;
Mouser cart uses the cart key, remembers the created CartKey, never leaks the key.

## Live acceptance (scripts/smoke_live.sh, uses real keys, ~19 calls total)
1. /health both configured. 2. /v1/part?mpn=LM358DR&qty=100 → both providers ok, offers>0.
3. Same call again → from_cache true, response < 50 ms. 4. mpn=LM1117IMPX-3.3/NOPB works.
5. mpn=NOTAREALPART12345XYZ → found false for both, NOT an error.
6. BOM of 20 lines (bench/testset.csv head) → mouser upstream calls ≤ 2 (check /v1/stats).
7. /v1/search/specs 10nF 50V 10% X7R 0603, in stock, no Vishay → total > 0 (74 on 2026-11-25), all of them
   returned (up to 200), every result Active and not Vishay, Capacitance searched as 3 DigiKey values.

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
