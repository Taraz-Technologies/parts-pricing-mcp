"""Search by specification (DigiKey parametric filters). Fixtures are real responses captured 2026-11-25."""
import asyncio
import json
import string

import httpx
import pytest
import respx

from app.models import Offer
from app.providers.base import ProviderError
from app.providers.digikey import SORT, DigiKey

from .conftest import DK, dk_product, fixture, mock_token

PK = {"X-API-Key": "pk"}
KEYWORD = f"{DK}/products/v4/search/keyword"
EXAMPLE = {"category": "ceramic capacitors", "in_stock": True, "exclude_manufacturers": ["Vishay"], "max_results": 10,
           "specs": {"Capacitance": "10nF", "Voltage": "50V", "Tolerance": "10%", "Temperature Coefficient": "X7R",
                     "Package": "0603"}}  # Ghayur's TZCC10NF50V10%X7R0603


def is_search(request) -> bool:
    """The filter-options call sends only the category; a search always sets MarketPlaceFilter."""
    return "MarketPlaceFilter" in json.loads(request.content)["FilterOptionsRequest"]


def searches(route) -> list[dict]:
    return [json.loads(c.request.content) for c in route.calls if is_search(c.request)]


@pytest.fixture
def upstream():
    with respx.mock(assert_all_called=False) as router:
        mock_token(router)
        router.get(f"{DK}/products/v4/search/categories", name="categories").mock(
            return_value=httpx.Response(200, json=fixture("digikey_categories")))
        options, page = fixture("digikey_keyword_cat60_options"), fixture("digikey_keyword_specs_10nF50V")
        router.post(KEYWORD, name="keyword").mock(
            side_effect=lambda req: httpx.Response(200, json=page if is_search(req) else options))
        yield router


async def test_example_sends_the_live_verified_request(api, upstream):
    c = await api()
    assert (await c.post("/v1/search/specs", json=EXAMPLE)).status_code == 401
    r = await c.post("/v1/search/specs", json=EXAMPLE, headers=PK)
    assert r.status_code == 200
    j = r.json()
    sent = [json.loads(x.request.content) for x in upstream["keyword"].calls]
    assert sent[0] == {"Keywords": "", "Limit": 1, "Offset": 0, "FilterOptionsRequest": {"CategoryFilter": [{"Id": "60"}]}}
    assert sent[1:] == [fixture("digikey_keyword_specs_10nF50V.request")]  # DigiKey answered it with 74 exact matches
    assert j["category"] == {"id": 60, "name": "Ceramic Capacitors"}
    assert j["filters"]["specs"]["Capacitance"] == ["0.01 µF", "10 nF", "10000 pF"]
    assert j["filters"]["specs"]["Package / Case"][0] == "0603 (1608 Metric)"
    assert "Vishay Vitramon" in j["filters"]["excluded_manufacturers"] and j["warnings"] == []
    assert (j["total"], len(j["results"]), j["truncated"]) == (74, 10, True)
    first = j["results"][0]
    assert (first["provider"], first["mpn"], first["manufacturer"]) == ("digikey", "06035C103K4T2A", "KYOCERA AVX")
    assert first["sku"] == "478-KAM15AR71H103KTCT-ND" and first["moq"] == 1 and first["price_breaks"]
    assert first["lifecycle"] == "Active" and first["rohs"] == "ROHS3 Compliant"
    assert first["datasheet_url"] and first["product_url"] and first["stock"] > 0 and first["currency"] == "USD"
    for spec in (("Capacitance", "10000 pF"), ("Voltage - Rated", "50V"), ("Package / Case", "0603 (1608 Metric)")):
        assert {"name": spec[0], "value": spec[1]} in first["specs"]  # ValueText, never DigiKey's numeric ValueId
    assert all(r["lifecycle"] == "Active" and not r["marketplace"] and "Vishay" not in r["manufacturer"]
               for r in j["results"])  # the page holds one marketplace variation: _pick skips it
    st = j["provider_status"][0]
    assert st["ok"] and st["found"] and not st["from_cache"] and st["error_code"] is None


async def test_cache_key_is_the_digikey_request(api, upstream):
    c = await api()
    await c.post("/v1/search/specs", json=EXAMPLE, headers=PK)
    n = len(searches(upstream["keyword"]))
    same = {**EXAMPLE, "specs": dict(reversed({**EXAMPLE["specs"], "Capacitance": "0.01uF", "Voltage": "50 V"}.items()))}
    j = (await c.post("/v1/search/specs", json=same, headers=PK)).json()  # same DigiKey values, other order/spelling
    assert len(searches(upstream["keyword"])) == n
    assert j["provider_status"][0]["from_cache"] and all(r["from_cache"] for r in j["results"])
    assert (await c.get("/v1/quota", headers=PK)).json()["providers"]["digikey"]["used"] == 3  # tree, options, 1 page
    for other in ({**EXAMPLE, "specs": {**EXAMPLE["specs"], "Capacitance": "4.7nF"}}, {**EXAMPLE, "max_results": 5},
                  {**EXAMPLE, "exclude_manufacturers": []}, {**EXAMPLE, "fresh": True}):
        assert (await c.post("/v1/search/specs", json=other, headers=PK)).json()["provider_status"][0]["ok"]
        assert len(searches(upstream["keyword"])) == (n := n + 1)
    assert upstream["categories"].call_count == 2  # fresh refetches the tree and the options too
    await c.post("/v1/search/specs", json={**EXAMPLE, "category": "Tantalum Capacitors"}, headers=PK)
    assert [json.loads(x.request.content)["FilterOptionsRequest"]["CategoryFilter"] for x in upstream["keyword"].calls
            if not is_search(x.request)][-1] == [{"Id": "59"}]  # another category, its own options


async def test_flags_off_send_no_filter(api, upstream):
    c = await api()
    await c.post("/v1/search/specs", json={**EXAMPLE, "rohs": False, "active_only": False, "in_stock": False}, headers=PK)
    await c.post("/v1/search/specs", json={**EXAMPLE, "in_stock": False}, headers=PK)
    off, defaults = (s["FilterOptionsRequest"] for s in searches(upstream["keyword"]))
    assert "StatusFilter" not in off and "SearchOptions" not in off and off["MarketPlaceFilter"] == "ExcludeMarketPlace"
    assert defaults["StatusFilter"] == [{"Id": "0"}] and defaults["SearchOptions"] == ["RohsCompliant"]


@pytest.mark.parametrize("body,needle", [
    ({"specs": {"Dielectric": "X7R"}}, "Temperature Coefficient"),  # lists the category's real parameter names
    ({"specs": {"Lead": "-"}}, "'Lead' is not one"),  # ambiguous: Lead Spacing, Lead Style
    ({"specs": {"Voltage": "50V", "Voltage - Rated": "100V"}}, "both mean 'Voltage - Rated'"),
    ({"specs": {"Capacitance": "47uH"}}, "no DigiKey value matches '47uH'"),
    ({"specs": {"Capacitance": ["10nF", "4.7 nF", "3.3 zF"]}}, "'3.3 zF'"),  # one bad value in a list fails
    ({"specs": {"Capacitance": "10.5nF"}}, "nearest: 0.01 µF | 10 nF | 10000 pF"),  # by value, not spelling
    ({"category": "Ceramic Capacitor"}, "close: Ceramic Capacitors"),
    ({"category": "Accessories"}, "ambiguous"),
    ({"category": "Capacitors"}, "has sub-categories, search one of them: "),
    ({"exclude_manufacturers": list(string.ascii_lowercase)}, "excludes every manufacturer"),
])
async def test_bad_spec_is_422_and_never_searches(api, upstream, body, needle):
    c = await api()
    r = await c.post("/v1/search/specs", json={**EXAMPLE, **body}, headers=PK)
    assert r.status_code == 422 and needle in r.json()["detail"]
    assert not any(is_search(x.request) for x in upstream["keyword"].calls)


async def test_parent_category_lists_its_leaves(api, upstream):
    c = await api()
    detail = (await c.post("/v1/search/specs", json={**EXAMPLE, "category": "Capacitors"}, headers=PK)).json()["detail"]
    assert "60 Ceramic Capacitors" in detail and "58 Aluminum Electrolytic Capacitors" in detail


@pytest.mark.parametrize("body", [
    {"specs": {}}, {"specs": {"Capacitance": []}}, {"specs": {"Capacitance": "x" * 101}},
    {"specs": {"Capacitance": ["10nF"] * 51}}, {"max_results": 501}, {"exclude_manufacturers": ["x" * 101]},
    {"spec": {"Capacitance": "10nF"}}, {"instock": True},  # a misspelled filter is an error, never "no filter"
])
async def test_invalid_request_is_422_before_any_call(api, upstream, body):
    c = await api()
    assert (await c.post("/v1/search/specs", json={**EXAMPLE, **body}, headers=PK)).status_code == 422
    assert not upstream.calls


@pytest.mark.parametrize("where,bad,message", [
    ("search", httpx.Response(503), "HTTP 503"),
    ("search", httpx.Response(400, json={"ErrorMessage": "Invalid ParameterFilterRequest"}),
     "HTTP 400 Invalid ParameterFilterRequest"),
    ("search", httpx.Response(200, json={"ProductsCount": 74, "Products": []}), "returned 0 of 10"),  # short page
    ("search", httpx.Response(200, json={"Products": []}), "no ProductsCount"),  # completeness unknown
    ("options", httpx.Response(200, json={"ProductsCount": 952943, "FilterOptions": {}}), "no filter options"),
    ("categories", httpx.Response(404, json={"title": "Not Found", "status": 404}), "categories: HTTP 404"),
    ("categories", httpx.Response(200, json={"Categories": []}), "empty answer"),
])
async def test_digikey_failure_is_an_error_never_cached(api, upstream, where, bad, message):
    """Reported in provider_status (never as "no matches" or a 422), and the next call goes upstream again."""
    good = {"options": fixture("digikey_keyword_cat60_options"), "search": fixture("digikey_keyword_specs_10nF50V")}
    state = {"broken": True}

    def keyword(req):
        kind = "search" if is_search(req) else "options"
        return bad if state["broken"] and kind == where else httpx.Response(200, json=good[kind])
    upstream["keyword"].mock(side_effect=keyword)
    upstream["categories"].mock(side_effect=lambda req: bad if state["broken"] and where == "categories" else
                                httpx.Response(200, json=fixture("digikey_categories")))
    c = await api()
    r = await c.post("/v1/search/specs", json=EXAMPLE, headers=PK)
    st = r.json()["provider_status"][0]
    assert r.status_code == 200 and st["ok"] is False and st["error_code"] == "upstream_error" and not st["found"]
    assert message in st["message"] and r.json()["results"] == [] and r.json()["total"] is None
    state["broken"] = False
    j = (await c.post("/v1/search/specs", json=EXAMPLE, headers=PK)).json()
    assert j["provider_status"][0]["ok"] and j["total"] == 74 and not j["provider_status"][0]["from_cache"]


@pytest.mark.parametrize("total,max_results,pages", [
    (120, 100, [(0, 50), (50, 50)]), (74, 500, [(0, 50), (50, 50)]), (74, 10, [(0, 10)]), (0, 200, [(0, 50)]),
])
async def test_pages_of_50_up_to_the_total(settings, storage, http, total, max_results, pages):
    def page(request):
        b = json.loads(request.content)
        ids = range(b["Offset"], min(b["Offset"] + b["Limit"], total))
        products = [dk_product(f"P{i}") for i in ids] + ([dk_product("P0")] if b["Offset"] else [])  # overlap: once
        return httpx.Response(200, json={"ProductsCount": total, "Products": products})
    with respx.mock() as router:
        mock_token(router)
        route = router.post(KEYWORD).mock(side_effect=page)
        results, got = await DigiKey(settings, storage, http).spec_search({"CategoryFilter": [{"Id": "60"}]}, max_results)
    assert got == total and [r["mpn"] for r in results] == [f"P{i}" for i in range(min(total, max_results))]
    sent = [json.loads(x.request.content) for x in route.calls]
    assert sorted((b["Offset"], b["Limit"]) for b in sent) == pages and all(b["SortOptions"] == SORT for b in sent)


async def test_shifted_page_fails_the_search(settings, storage, http):
    def page(request):  # P49 shows up again on page 2 and P50 never does: 73 of 74
        b = json.loads(request.content)
        ids = range(50) if b["Offset"] == 0 else range(49, 73)
        return httpx.Response(200, json={"ProductsCount": 74, "Products": [dk_product(f"P{i}") for i in ids]})
    with respx.mock() as router:
        mock_token(router)
        router.post(KEYWORD).mock(side_effect=page)
        with pytest.raises(ProviderError, match="73 of 74"):
            await DigiKey(settings, storage, http).spec_search({"CategoryFilter": [{"Id": "60"}]}, 200)


async def test_failed_page_cancels_the_other_pages(settings, storage, http):
    offsets = []

    def page(request):
        b = json.loads(request.content)
        offsets.append(b["Offset"])
        if b["Offset"] == 50:
            return httpx.Response(400, json={"ErrorMessage": "bad page"})
        return httpx.Response(200, json={"ProductsCount": 500, "Products": [
            dk_product(f"P{i}") for i in range(b["Offset"], b["Offset"] + b["Limit"])]})
    with respx.mock() as router:
        mock_token(router)
        router.post(KEYWORD).mock(side_effect=page)
        with pytest.raises(ProviderError, match="HTTP 400 bad page"):
            await DigiKey(settings, storage, http).spec_search({"CategoryFilter": [{"Id": "60"}]}, 500)
        await asyncio.sleep(0.2)  # pages left running would call DigiKey now
    assert len(offsets) < 10


def test_pick_prefers_digikeys_own_cut_tape():
    def offer(sku, packaging, marketplace=False):
        return Offer(provider="digikey", mpn="X", sku=sku, packaging=packaging, marketplace=marketplace,
                     fetched_at="2026-11-25T00:00:00Z")
    assert DigiKey._pick([offer("tr", "Tape & Reel (TR)"), offer("mp", "Cut Tape (CT)", True),
                          offer("ct", "Cut Tape (CT)")]).sku == "ct"
    assert DigiKey._pick([offer("mp", "Cut Tape (CT)", True), offer("tr", "Tape & Reel (TR)")]).sku == "tr"
    assert DigiKey._pick([offer("mp", "Cut Tape (CT)", True)]).sku == "mp"


def test_part_specs_carry_the_leaf_category():
    p = fixture("digikey_keyword_specs_10nF50V")["Products"][0]  # Category is the root (3 Capacitors) > 60
    assert DigiKey._leaf(p) == {"category": "Ceramic Capacitors", "category_id": 60}
