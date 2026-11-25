import json

import httpx
import respx
from mcp.shared.memory import create_connected_server_and_client_session

from app.mcp_server import BearerAuth, build, mcp

API = "http://pricing.test"
READ = ["find_substitutes", "get_part_offers", "get_part_specs", "get_related_parts", "price_bom", "quota_status",
        "search_by_specs", "search_parts"]


async def test_list_tools_all_read_only():
    async with create_connected_server_and_client_session(mcp) as client:
        tools = (await client.list_tools()).tools
    assert sorted(t.name for t in tools) == READ
    assert all(t.annotations.readOnlyHint and t.annotations.openWorldHint for t in tools)


async def test_cart_server_adds_cart_tools():
    async with create_connected_server_and_client_session(build(cart=True)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert sorted(tools) == sorted(READ + ["add_to_cart", "connect_digikey", "review_cart"])
    assert not tools["add_to_cart"].annotations.readOnlyHint and not tools["add_to_cart"].annotations.destructiveHint
    assert tools["review_cart"].annotations.readOnlyHint


@respx.mock
async def test_add_to_cart_calls_rest(monkeypatch):
    monkeypatch.setenv("PRICING_API_URL", API)
    route = respx.post(f"{API}/v1/cart/add").mock(return_value=httpx.Response(200, json={"results": []}))
    async with create_connected_server_and_client_session(build(cart=True)) as client:
        res = await client.call_tool("add_to_cart", {"items": [{"provider": "mouser", "sku": "595-LM358DR", "qty": 100}]})
    assert not res.isError
    assert json.loads(route.calls.last.request.read()) == {
        "items": [{"provider": "mouser", "sku": "595-LM358DR", "qty": 100, "customer_ref": None}]}


@respx.mock
async def test_get_part_offers_calls_rest(monkeypatch):
    monkeypatch.setenv("PRICING_API_URL", API)
    monkeypatch.setenv("PRICING_API_KEY", "mcp-key")
    route = respx.get(f"{API}/v1/part").mock(return_value=httpx.Response(200, json={"best": None, "offers": []}))
    async with create_connected_server_and_client_session(mcp) as client:
        res = await client.call_tool("get_part_offers", {"mpn": "LM1117IMPX-3.3/NOPB", "qty": 50})
    assert not res.isError
    req = route.calls.last.request
    assert req.headers["X-API-Key"] == "mcp-key"
    assert req.url.params["mpn"] == "LM1117IMPX-3.3/NOPB" and req.url.params["qty"] == "50"
    assert json.loads(res.content[0].text) == {"best": None, "offers": []}


@respx.mock
async def test_search_by_specs_calls_rest_and_returns_compact_json(monkeypatch):
    monkeypatch.setenv("PRICING_API_URL", API)
    rest = {"filters": {"specs": {"Capacitance": ["10000 pF"]}}, "total": 1, "results": [{
        "provider": "digikey", "mpn": "X1", "manufacturer": "M", "sku": "X1-CT", "stock": 5, "moq": 1,
        "price_breaks": [{"qty": 1, "unit_price": 0.1}, {"qty": 10, "unit_price": 0.05}], "image_url": "https://img",
        "fetched_at": "2026-11-25T00:00:00Z", "warnings": [], "from_cache": False,
        "specs": [{"name": "Capacitance", "value": "10000 pF"}, {"name": "Features", "value": "-"},
                  {"name": "Ratings", "value": "AEC-Q200"}]}]}
    route = respx.post(f"{API}/v1/search/specs").mock(return_value=httpx.Response(200, json=rest))
    async with create_connected_server_and_client_session(mcp) as client:
        res = await client.call_tool("search_by_specs", {
            "category": "Ceramic Capacitors", "specs": {"Capacitance": "10nF", "Temperature Coefficient": ["X7R", "X5R"]},
            "exclude_manufacturers": ["Vishay"]})
    assert not res.isError
    assert json.loads(route.calls.last.request.read()) == {
        "category": "Ceramic Capacitors", "specs": {"Capacitance": "10nF", "Temperature Coefficient": ["X7R", "X5R"]},
        "in_stock": False, "exclude_manufacturers": ["Vishay"], "rohs": True, "active_only": True, "max_results": 50}
    text = res.content[0].text
    assert "\n" not in text and '", "' not in text  # compact: a pretty-printed dict is ~3x the size
    part = json.loads(text)["results"][0]
    assert part["price_breaks"] == [[1, 0.1], [10, 0.05]] and part["specs"] == {"Ratings": "AEC-Q200"}  # searched/"-" go
    assert not {"image_url", "fetched_at", "warnings", "from_cache", "provider"} & set(part)


@respx.mock
async def test_price_bom_can_skip_a_distributor(monkeypatch):
    monkeypatch.setenv("PRICING_API_URL", API)
    route = respx.post(f"{API}/v1/bom/price").mock(return_value=httpx.Response(200, json={"lines": []}))
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("price_bom", {"lines": [{"mpn": "LM358DR", "qty": 10}], "providers": ["mouser"]})
    assert json.loads(route.calls.last.request.read())["providers"] == ["mouser"]


@respx.mock
async def test_price_bom_and_rest_error(monkeypatch):
    monkeypatch.setenv("PRICING_API_URL", API)
    route = respx.post(f"{API}/v1/bom/price").mock(return_value=httpx.Response(413, json={"detail": "too big"}))
    async with create_connected_server_and_client_session(mcp) as client:
        res = await client.call_tool("price_bom", {"lines": [{"mpn": "LM358DR", "qty": 10}]})
    assert res.isError and "413" in res.content[0].text
    assert route.calls.last.request.read() == b'{"lines":[{"mpn":"LM358DR","qty":10,"line_id":null,"manufacturer":null}],"fresh":false}'


async def test_bearer_auth():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": scope["path"].encode()})

    async def call(app, headers, path="/mcp"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(path, headers=headers)
            return r.status_code if r.status_code != 200 else r.text

    app = BearerAuth(inner, "s3cret")
    assert await call(app, {}) == 401
    assert await call(app, {"Authorization": "Bearer wrong"}) == 401
    assert await call(app, {"Authorization": "Bearer s3cret"}) == "/mcp"
    assert await call(app, {}, "/s3cret/mcp") == "/mcp"  # claude.ai: token in path, stripped before FastMCP
    assert await call(app, {}, "/wrong/mcp") == 401
    assert await call(BearerAuth(inner, ""), {"Authorization": "Bearer "}) == 401  # empty token fails closed
    assert await call(BearerAuth(inner, ""), {}, "//mcp") == 401
