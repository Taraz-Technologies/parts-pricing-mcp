"""MCP server for Claude/agents. Thin client of the REST API: holds NO distributor keys.
Env: PRICING_API_URL, PRICING_API_KEY, MCP_BEARER_TOKEN, MCP_PORT (8090). Endpoint: /mcp
"""
import os
import secrets

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

mcp = FastMCP(
    "taraz-parts-pricing",
    stateless_http=True,
    instructions=(
        "Live DigiKey + Mouser list prices (USD), stock, MOQ, order multiples and lead times. "
        "Results are cached up to 12 h. Each distributor allows only ~1000 calls/day shared by all users: "
        "use price_bom for several parts instead of calling get_part_offers in a loop."),
)
RO = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


async def _api(method: str, path: str, **kw) -> dict:
    base = os.environ.get("PRICING_API_URL", "http://127.0.0.1:8080").rstrip("/")
    headers = {"X-API-Key": os.environ.get("PRICING_API_KEY", "")}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=180) as c:
        r = await c.request(method, path, **kw)
    if r.status_code != 200:
        raise RuntimeError(f"pricing API {path} returned HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


@mcp.tool(annotations=RO)
async def search_parts(keyword: str, limit: int = 10) -> dict:
    """Keyword search on DigiKey and Mouser (e.g. "2N7002", "10k 0603 1%"). Returns one offer per product.
    Prices are live distributor list prices in USD, cached up to 12 h (searches 6 h).
    Costs 1 call per distributor from a ~1000/day quota. Use get_part_offers or price_bom once you know the MPN."""
    return await _api("POST", "/v1/search", json={"keyword": keyword, "limit": limit})


@mcp.tool(annotations=RO)
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


@mcp.tool(annotations=RO)
async def price_bom(lines: list[BomLine], fresh: bool = False) -> dict:
    """Price a whole BOM (up to 300 lines) on DigiKey and Mouser in one call. Duplicate MPNs are looked up once
    and Mouser lookups are batched, so this uses far less quota than calling get_part_offers per line.
    Returns per-line offers and best choice, plus totals per distributor and for the best mix.
    Prices are live distributor list prices in USD, cached up to 12 h; fresh=true bypasses the cache
    (uses quota, ~1000 calls/day per distributor) - only use it when the user asks for fresh numbers."""
    return await _api("POST", "/v1/bom/price",
                      json={"lines": [ln.model_dump() for ln in lines], "fresh": fresh})


@mcp.tool(annotations=RO)
async def quota_status() -> dict:
    """Today's (UTC) distributor API calls used and remaining for DigiKey and Mouser (~1000/day each)."""
    return await _api("GET", "/v1/quota")


class BearerAuth:
    """Pure-ASGI: every HTTP request needs Authorization: Bearer <MCP_BEARER_TOKEN>. Lifespan passes through."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()
        self.enabled = bool(token)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not (self.enabled and secrets.compare_digest(got, self.expected)):
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
    uvicorn.run(BearerAuth(mcp.streamable_http_app(), token), host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8090")))


if __name__ == "__main__":
    main()
