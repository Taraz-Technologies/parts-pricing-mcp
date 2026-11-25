"""MCP server for Claude/agents. Thin client of the REST API: holds NO distributor keys.
Env: PRICING_API_URL, PRICING_API_KEY, MCP_BEARER_TOKEN, MCP_PORT (8090). Endpoint: /mcp
MCP_ENABLE_CART=1 adds the cart tools; run that as a separate deployment with its own bearer token and an API key
whose client is in the API's CART_CLIENTS, so the widely shared read-only URL can never change a cart.
"""
import json
import os
import secrets
from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


async def _api(method: str, path: str, **kw) -> dict:
    base = os.environ.get("PRICING_API_URL", "http://127.0.0.1:8080").rstrip("/")
    headers = {"X-API-Key": os.environ.get("PRICING_API_KEY", "")}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=180) as c:
        r = await c.request(method, path, **kw)
    if r.status_code != 200:  # a 4xx carries the guidance the model needs to fix its call: keep more of it
        raise RuntimeError(f"pricing API {path} returned HTTP {r.status_code}: "
                           f"{r.text[:2000 if r.status_code < 500 else 500]}")
    return r.json()


# --- read-only tools (every deployment)

async def search_parts(keyword: str, limit: int = 10) -> dict:
    """Keyword search on DigiKey and Mouser (e.g. "2N7002", "10k 0603 1%"). Returns one offer per product.
    Prices are live distributor list prices in USD, cached up to 12 h (searches 6 h).
    Costs 1 call per distributor from a ~1000/day quota. Use get_part_offers or price_bom once you know the MPN."""
    return await _api("POST", "/v1/search", json={"keyword": keyword, "limit": limit})


_LEAN = ("mpn", "manufacturer", "description", "sku", "packaging", "stock", "moq", "multiple", "lifecycle", "rohs",
         "lead_time_weeks", "datasheet_url", "product_url")


async def search_by_specs(category: str, specs: dict[str, str | list[str]], in_stock: bool = False,
                          exclude_manufacturers: list[str] | None = None, rohs: bool = True, active_only: bool = True,
                          max_results: int = 50) -> str:
    """Every DigiKey part in one category whose specs match: e.g. all valid alternates for a Taraz part number.
    TZCC10NF50V10%X7R0603 -> category="Ceramic Capacitors", specs={"Capacitance": "10nF", "Voltage - Rated": "50V",
    "Tolerance": "10%", "Temperature Coefficient": "X7R", "Package / Case": "0603"}.
    TZR22K1%0.1W0603 -> category="Chip Resistor - Surface Mount", specs={"Resistance": "22k", "Tolerance": "1%",
    "Power (Watts)": "0.1W", "Package / Case": "0603"}.
    Other leaf categories: Fixed Inductors, Ferrite Beads and Chips, Aluminum Electrolytic Capacitors, Tantalum
    Capacitors, TVS Diodes (a parent such as "Resistors" fails and lists its sub-categories). For a known part,
    get_part_specs shows its category and spec names; a unique start of a name ("Voltage") works.
    Values match unit-aware ("10nF" = "0.01 µF" = "10000 pF"; "0603" never matches "0201 (0603 Metric)"); a list
    means any of them, e.g. ["50V", "63V", "100V"] for 50 V or more. An unknown name or value fails and lists the
    valid or nearest ones: fix it, never drop a spec.
    Defaults: RoHS compliant, Active only, marketplace sellers excluded; in_stock=true keeps stocked parts only;
    exclude_manufacturers=["Vishay"] drops every manufacturer whose name contains it.
    Returns `total` matches and one DigiKey offer per part (cut tape: stock, moq, price_breaks [[qty, unit USD]],
    lifecycle, rohs, links) with the specs that were not searched; `filters` shows exactly which DigiKey values
    were searched. If `truncated`, add specs or raise max_results (up to 500; each part is ~800 characters of
    output). If provider_status ok is false,
    DigiKey failed: say so, it does NOT mean no parts match.
    Prices are DigiKey list prices in USD from its search index (can lag up to 24 h; cached 6 h here). For
    real-time stock and price, or for Mouser (it has no parametric search), price_bom the shortlist, with
    providers=["mouser"] for Mouser only. Costs 1 DigiKey call per 50 results (+1-2 for a category not searched
    in 6 h) from ~1000/day."""
    j = await _api("POST", "/v1/search/specs", json={
        "category": category, "specs": specs, "in_stock": in_stock, "exclude_manufacturers": exclude_manufacturers or [],
        "rohs": rohs, "active_only": active_only, "max_results": max_results})
    searched = set((j.get("filters") or {}).get("specs") or {})
    j["results"] = [{**{k: r.get(k) for k in _LEAN},  # every part shares the searched specs: send the rest only
                     "price_breaks": [[b["qty"], b["unit_price"]] for b in r.get("price_breaks") or []],
                     "specs": {s["name"]: s["value"] for s in r.get("specs") or []
                               if s["name"] not in searched and s["value"] not in (None, "-")},
                     **({"warnings": r["warnings"]} if r.get("warnings") else {})} for r in j.get("results") or []]
    return json.dumps(j, ensure_ascii=False, separators=(",", ":"))  # a dict would go out pretty-printed: 3x the size


async def get_part_offers(mpn: str, qty: int = 1, manufacturer: str | None = None) -> dict:
    """Price and stock for ONE manufacturer part number at a quantity, from DigiKey and Mouser.
    Returns every offer (packaging variants) priced at qty after MOQ, order multiple and price breaks,
    plus `best` (cheapest in stock) and `better_buy` hints. Pass manufacturer to exclude look-alike parts.
    Prices are live distributor list prices in USD, cached up to 12 h. Quota is ~1000 calls/day per
    distributor, so for more than one part use price_bom instead of looping over this tool.
    A provider_status entry with ok=false means that distributor errored: it does NOT mean "not stocked"."""
    params = {"mpn": mpn, "qty": qty}
    if manufacturer:
        params["manufacturer"] = manufacturer
    return await _api("GET", "/v1/part", params=params)


class BomLine(BaseModel):
    mpn: str
    qty: int = Field(ge=1)
    line_id: str | None = None
    manufacturer: str | None = None


async def price_bom(lines: list[BomLine], fresh: bool = False,
                    providers: list[Literal["digikey", "mouser"]] | None = None) -> dict:
    """Price a whole BOM (up to 300 lines) on DigiKey and Mouser in one call. Duplicate MPNs are looked up once
    and Mouser lookups are batched, so this uses far less quota than calling get_part_offers per line.
    Returns per-line offers and best choice, plus totals per distributor and for the best mix.
    Prices are live distributor list prices in USD, cached up to 12 h; fresh=true bypasses the cache
    (uses quota, ~1000 calls/day per distributor) - only use it when the user asks for fresh numbers.
    providers=["mouser"] (or ["digikey"]) asks one distributor only and spends none of the other's quota."""
    return await _api("POST", "/v1/bom/price", json={"lines": [ln.model_dump() for ln in lines], "fresh": fresh,
                                                     **({"providers": providers} if providers else {})})


async def quota_status() -> dict:
    """Today's (UTC) distributor API calls used and remaining for DigiKey and Mouser (~1000/day each)."""
    return await _api("GET", "/v1/quota")


async def get_part_specs(mpn: str) -> dict:
    """Technical specs of one part from DigiKey (package, voltage, tolerance, temperature range, ...),
    plus datasheet and product links. Cached up to 6 h; otherwise costs 1-2 DigiKey calls (~1000/day)."""
    return await _api("GET", "/v1/part/specs", params={"mpn": mpn})


async def find_substitutes(mpn: str) -> dict:
    """DigiKey's substitute parts for one part (use when it is out of stock, obsolete or too expensive), and the
    other packaging options of the same part. Each result has mpn, manufacturer, DigiKey part number, stock and
    unit price as DigiKey reports them. Check specs before recommending a substitute.
    Cached up to 6 h; otherwise costs 2 DigiKey calls (~1000/day)."""
    return await _api("GET", "/v1/part/substitutes", params={"mpn": mpn})


async def get_related_parts(mpn: str) -> dict:
    """Parts DigiKey links to one part: mating connectors, accessories and parts commonly bought with it.
    Cached up to 6 h; otherwise costs 1 DigiKey call (~1000/day)."""
    return await _api("GET", "/v1/part/related", params={"mpn": mpn})


READ_TOOLS = (search_parts, search_by_specs, get_part_offers, price_bom, quota_status, get_part_specs,
              find_substitutes, get_related_parts)


# --- cart tools (only with MCP_ENABLE_CART=1)

async def review_cart() -> dict:
    """Show the company's Mouser cart and the DigiKey cart list (DigiKey has no cart API: items go to a MyList
    that a person moves to the cart on digikey.com). Returns items, quantities and prices per distributor."""
    return await _api("GET", "/v1/cart")


class CartItem(BaseModel):
    provider: Literal["mouser", "digikey"]
    sku: str = Field(min_length=1, description="Distributor part number: the `sku` field of an offer "
                                                "(Mouser e.g. 595-LM358DR, DigiKey e.g. 296-1395-1-ND)")
    qty: int = Field(ge=1, description="Use the offer's order_qty so MOQ and order multiple are respected")
    customer_ref: str | None = Field(None, description="Optional reference, e.g. the BOM line id")


async def add_to_cart(items: list[CartItem]) -> dict:
    """Add parts to the company's Mouser cart and/or the DigiKey cart list. This does NOT place an order:
    a person reviews and checks out on the distributor's website.
    Before calling, show the user the exact items (distributor, part number, quantity, price) and get a yes.
    Take sku and order_qty from get_part_offers or price_bom results; never guess part numbers."""
    return await _api("POST", "/v1/cart/add", json={"items": [i.model_dump() for i in items]})


async def connect_digikey() -> dict:
    """Whether the DigiKey account is connected for the DigiKey cart list, and if not, a link for an admin to
    sign in to DigiKey once. Use it when a DigiKey provider_status has error_code not_connected."""
    return await _api("GET", "/v1/digikey/connect")


def build(cart: bool = False) -> FastMCP:
    server = FastMCP(
        "taraz-parts-cart" if cart else "taraz-parts-pricing",
        stateless_http=True,
        # Non-localhost host disables the SDK's localhost-only Host-header check (would 421 behind Railway's
        # domain). BearerAuth is the gate.
        host="0.0.0.0",
        instructions=(
            "Live DigiKey + Mouser list prices (USD), stock, MOQ, order multiples and lead times. "
            "Results are cached up to 12 h. Each distributor allows only ~1000 calls/day shared by all users: "
            "use price_bom for several parts instead of calling get_part_offers in a loop."
            + (" Cart tools add to the company's Mouser cart and DigiKey cart list; they never place orders. "
               "Always confirm items and quantities with the user before add_to_cart." if cart else "")),
    )
    for fn in READ_TOOLS:
        server.add_tool(fn, annotations=RO)
    if cart:
        server.add_tool(review_cart, annotations=RO)
        server.add_tool(connect_digikey, annotations=RO)
        server.add_tool(add_to_cart, annotations=WRITE)
    return server


mcp = build(cart=os.environ.get("MCP_ENABLE_CART") == "1")


class BearerAuth:
    """Pure-ASGI: every HTTP request needs Authorization: Bearer <MCP_BEARER_TOKEN>, or the token as the first
    path segment (/<token>/mcp) for claude.ai connectors, which can't send headers. Lifespan passes through."""

    def __init__(self, app, token: str):
        self.app, self.token, self.expected = app, token.encode(), f"Bearer {token}".encode()
        self.enabled = bool(token)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            ok = self.enabled and secrets.compare_digest(got, self.expected)
            if self.enabled and not ok:
                first, _, rest = scope["path"].lstrip("/").partition("/")
                if secrets.compare_digest(first.encode(), self.token):
                    ok, scope = True, dict(scope, path="/" + rest, raw_path=("/" + rest).encode())
            if not ok:
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def main():
    import uvicorn
    token = os.environ.get("MCP_BEARER_TOKEN", "")
    if not token:
        raise SystemExit("MCP_BEARER_TOKEN is not set")
    # access_log off: uvicorn would log /<token>/mcp paths, i.e. the secret.
    uvicorn.run(BearerAuth(mcp.streamable_http_app(), token), host="0.0.0.0",
                port=int(os.environ.get("MCP_PORT", "8090")), access_log=False)


if __name__ == "__main__":
    main()
