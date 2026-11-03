import json
from urllib.parse import unquote

import httpx
import pytest
import respx

from app.api import create_app

from .conftest import DK, MOUSER_HOST, dk_product, make_settings, mock_token, mouser_part, mouser_response

PK = {"X-API-Key": "pk"}


@pytest.fixture
async def api(tmp_path):
    made = []

    async def make(**kw):
        app = create_app(make_settings(tmp_path, **kw))
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        made.append(ctx)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    yield make
    for ctx in made:
        await ctx.__aexit__(None, None, None)


@pytest.fixture
def upstream():
    with respx.mock(assert_all_called=False) as router:
        mock_token(router)
        router.get(url__regex=rf"{DK}/products/v4/search/(?P<mpn>[^/]+)/productdetails", name="details").mock(
            side_effect=lambda req, mpn: httpx.Response(200, json={"Product": dk_product(unquote(mpn))}))
        router.post(f"{DK}/products/v4/search/keyword", name="keyword").mock(
            return_value=httpx.Response(200, json={"Products": [], "ExactMatches": [], "ProductsCount": 0}))

        def mouser(req):
            mpns = json.loads(req.content)["SearchByPartRequest"]["mouserPartNumber"].split("|")
            return httpx.Response(200, json=mouser_response([mouser_part(m) for m in mpns]))
        router.post(host=MOUSER_HOST, path="/api/v1/search/partnumber", name="mouser").mock(side_effect=mouser)
        yield router


async def test_auth(api, upstream):
    c = await api()
    assert (await c.get("/health")).json() == {"status": "ok", "providers": {"digikey": "configured", "mouser": "configured"}}
    assert (await c.get("/v1/part", params={"mpn": "LM358DR"})).status_code == 401
    assert (await c.get("/v1/part", params={"mpn": "LM358DR"}, headers={"X-API-Key": "nope"})).status_code == 401
    assert (await c.get("/v1/quota", headers=PK)).status_code == 200


async def test_part_and_cache(api, upstream):
    c = await api()
    r = (await c.get("/v1/part", params={"mpn": "LM358DR", "qty": 50}, headers=PK)).json()
    assert [s["ok"] for s in r["provider_status"]] == [True, True]
    # DigiKey 50 x 0.297 = 14.85 beats Mouser 50 x 0.30 = 15.00
    assert (r["best"]["provider"], r["best"]["unit_price"], r["best"]["extended_price"]) == ("digikey", 0.297, 14.85)
    assert r["best_reason"] == "cheapest_in_stock"
    assert r["best_by_provider"]["mouser"]["extended_price"] == 15.0
    calls = len(upstream.calls)
    r2 = (await c.get("/v1/part", params={"mpn": "LM358DR", "qty": 50}, headers=PK)).json()
    assert len(upstream.calls) == calls and all(s["from_cache"] for s in r2["provider_status"])
    assert r2["best"]["from_cache"] is True and r2["best"]["extended_price"] == 14.85
    stats = (await c.get("/v1/stats", headers=PK)).json()["providers"]
    assert stats["mouser"]["cache_hits"] == 1 and stats["mouser"]["calls_by_client"] == {"planner": 1}


async def test_partial_result_when_mouser_errors(api, upstream):
    upstream["mouser"].mock(side_effect=None, return_value=httpx.Response(503))
    c = await api()
    r = await c.get("/v1/part", params={"mpn": "LM358DR", "qty": 50}, headers=PK)
    assert r.status_code == 200
    dk, ms = r.json()["provider_status"]
    assert dk["ok"] and dk["found"]
    assert ms["ok"] is False and ms["error_code"] == "upstream_error" and ms["found"] is False
    assert r.json()["best"]["provider"] == "digikey"
    before = upstream["mouser"].call_count  # errors are never cached: next call goes upstream again
    await c.get("/v1/part", params={"mpn": "LM358DR", "qty": 50}, headers=PK)
    assert upstream["mouser"].call_count > before


async def test_mouser_errors_array_is_error_not_notfound(api, upstream):
    upstream["mouser"].mock(side_effect=None, return_value=httpx.Response(
        200, json=mouser_response(errors=[{"Code": "Invalid", "Message": "Invalid unique identifier."}])))
    c = await api()
    r = (await c.post("/v1/bom/price", json={"lines": [{"mpn": "NOPE123", "qty": 1}], "providers": ["mouser"]},
                      headers=PK)).json()
    st = r["lines"][0]["provider_status"][0]
    assert st["error_code"] == "upstream_error" and "Invalid unique identifier" in st["message"]
    assert r["summary"]["not_found"] == 0


async def test_bom_totals_and_dedupe(api, upstream):
    c = await api()
    lines = [{"line_id": "U1", "mpn": "LM358DR", "qty": 50}, {"line_id": "U2", "mpn": "lm358dr ", "qty": 50},
             {"line_id": "U3", "mpn": "NE555DR", "qty": 10}]
    r = (await c.post("/v1/bom/price", json={"lines": lines}, headers=PK)).json()
    assert [ln["line_id"] for ln in r["lines"]] == ["U1", "U2", "U3"]
    assert upstream["details"].call_count == 2
    assert upstream["mouser"].call_count == 1
    body = json.loads(upstream["mouser"].calls[0].request.content)
    assert body["SearchByPartRequest"]["mouserPartNumber"] == "LM358DR|NE555DR"
    s = r["summary"]
    assert (s["lines"], s["priced"], s["not_found"], s["short_stock"], s["currency"]) == (3, 3, 0, 0, "USD")
    assert s["totals"]["digikey"] == {"total": 32.67, "lines_covered": 3, "lines_missing": 0}  # 14.85*2 + 2.97
    assert s["totals"]["mouser"]["total"] == 33.0  # 15*2 + 3
    assert s["totals"]["best_mix"] == {"total": 32.67, "lines_by_provider": {"digikey": 3, "mouser": 0}}
    assert r["quota"]["providers"]["mouser"]["used"] == 1
    assert {p["provider"]: p["ok"] for p in r["provider_status"]} == {"digikey": True, "mouser": True}


async def test_bom_too_big(api, upstream):
    c = await api(bom_max_lines=2)
    r = await c.post("/v1/bom/price", json={"lines": [{"mpn": f"P{i}", "qty": 1} for i in range(3)]}, headers=PK)
    assert r.status_code == 413


async def test_invalid_input(api, upstream):
    c = await api()
    assert (await c.get("/v1/part", params={"mpn": "X", "qty": 0}, headers=PK)).status_code == 422
    assert (await c.get("/v1/part", params={"mpn": "X", "providers": "octopart"}, headers=PK)).status_code == 422


async def test_not_configured(api, upstream):
    c = await api(mouser_search_api_key="")
    r = (await c.get("/v1/part", params={"mpn": "LM358DR"}, headers=PK)).json()
    ms = r["provider_status"][1]
    assert ms["error_code"] == "not_configured" and ms["ok"] is False
