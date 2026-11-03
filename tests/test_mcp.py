import json

import httpx
import respx
from mcp.shared.memory import create_connected_server_and_client_session

from app.mcp_server import BearerAuth, mcp

API = "http://pricing.test"


async def test_list_tools_all_read_only():
    async with create_connected_server_and_client_session(mcp) as client:
        tools = (await client.list_tools()).tools
    assert sorted(t.name for t in tools) == ["get_part_offers", "price_bom", "quota_status", "search_parts"]
    assert all(t.annotations.readOnlyHint and t.annotations.openWorldHint for t in tools)


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
        await send({"type": "http.response.body", "body": b"ok"})

    async def call(app, headers):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return (await c.post("/mcp", headers=headers)).status_code

    app = BearerAuth(inner, "s3cret")
    assert await call(app, {}) == 401
    assert await call(app, {"Authorization": "Bearer wrong"}) == 401
    assert await call(app, {"Authorization": "Bearer s3cret"}) == 200
    assert await call(BearerAuth(inner, ""), {"Authorization": "Bearer "}) == 401  # empty token fails closed
