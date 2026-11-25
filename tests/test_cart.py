"""Specs/substitutes/related (read-only) and cart features (Mouser cart, DigiKey MyList + 3-legged OAuth)."""
import json
import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from .conftest import DK, MOUSER_HOST, dk_product

CART_KEY = "mouser-cart-secret-xyz"
CK = {"X-API-Key": "ck"}  # cart client
PK = {"X-API-Key": "pk"}  # planner: read-only
CART = dict(console_api_keys="planner:pk,mcp:mk,cart:ck", cart_clients="cart", mouser_cart_api_key=CART_KEY,
            digikey_redirect_uri="https://api.test/v1/digikey/callback")


@pytest.fixture
def up():
    """DigiKey (both OAuth flows + MyLists) and Mouser cart, with just enough state."""
    state = {"lists": [], "parts": [], "refresh": []}
    with respx.mock(assert_all_called=False) as r:
        def token(req):
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            if form["grant_type"] == "client_credentials":
                return httpx.Response(200, json={"access_token": "app-tok", "expires_in": 600})
            if form["grant_type"] == "authorization_code":
                assert form["redirect_uri"] == CART["digikey_redirect_uri"]
                return httpx.Response(200, json={"access_token": "user-1", "refresh_token": "r1", "expires_in": 1799})
            state["refresh"].append(form["refresh_token"])
            return httpx.Response(200, json={"access_token": "user-2", "refresh_token": "r2", "expires_in": 1799})
        r.post(f"{DK}/v1/oauth2/token", name="token").mock(side_effect=token)

        def create(req):
            state["lists"].append({"Id": "L1", "ListName": json.loads(req.content)["ListName"]})
            return httpx.Response(200, json="ok")
        r.get(f"{DK}/mylists/v1/lists", name="lists").mock(side_effect=lambda req: httpx.Response(200, json=state["lists"]))
        r.post(f"{DK}/mylists/v1/lists", name="create").mock(side_effect=create)

        def add(req):
            state["parts"] += json.loads(req.content)
            return httpx.Response(200, json=["u1"])
        r.post(f"{DK}/mylists/v1/lists/L1/parts", name="add").mock(side_effect=add)
        r.get(f"{DK}/mylists/v1/lists/L1/parts", name="parts").mock(side_effect=lambda req: httpx.Response(200, json={
            "TotalParts": len(state["parts"]), "PartsList": [{
                "RequestedPartNumber": p["RequestedPartNumber"], "ManufacturerPartNumber": "LM358DR",
                "Manufacturer": "Texas Instruments", "CustomerReference": p.get("CustomerReference"),
                "Quantities": [{"QuantityRequested": p["Quantities"][0]["Quantity"], "SelectedPackOptionIndex": 0,
                                "CalculatedQuantity": p["Quantities"][0]["Quantity"], "PackOptions": [{
                                    "DigiKeyPartNumber": p["RequestedPartNumber"], "CalculatedUnitPrice": 0.186,
                                    "ExtendedPrice": 18.6}]}]} for p in state["parts"]]}))
        r.post(host=MOUSER_HOST, path="/api/v1/cart/items/insert", name="m_insert").mock(
            side_effect=lambda req: httpx.Response(200, json={
                "Errors": [], "CartKey": json.loads(req.content)["CartKey"] or "cart-123", "CurrencyCode": "USD",
                "MerchandiseTotal": 20.0, "CartItems": [{
                    "MouserPartNumber": i["MouserPartNumber"], "MfrPartNumber": "LM358DR", "Quantity": i["Quantity"],
                    "UnitPrice": 0.2, "ExtendedPrice": 20.0, "CartItemCustPartNumber": i.get("CustomerPartNumber"),
                    "Errors": []} for i in json.loads(req.content)["CartItems"]]}))
        r.get(host=MOUSER_HOST, path="/api/v1/cart", name="m_cart").mock(return_value=httpx.Response(200, json={
            "Errors": [], "CartKey": "cart-123", "CurrencyCode": "USD", "CartItems": [], "MerchandiseTotal": 0}))
        r.state = state
        yield r


async def _connect(c):
    j = (await c.get("/v1/digikey/connect", headers=CK)).json()
    st = parse_qs(urlparse(j["authorize_url"]).query)
    assert st["redirect_uri"] == [CART["digikey_redirect_uri"]] and st["response_type"] == ["code"]
    return await c.get("/v1/digikey/callback", params={"code": "the-code", "state": st["state"][0]})


async def test_specs_cached(api, up):
    p = dk_product("LM358DR")
    p["Parameters"] = [{"ParameterText": "Voltage - Supply Span (Max)", "ValueText": "32 V"}]
    route = up.get(url__regex=rf"{DK}/products/v4/search/LM358DR/productdetails").mock(
        return_value=httpx.Response(200, json={"Product": p}))
    c = await api(**CART)
    r = (await c.get("/v1/part/specs", params={"mpn": "LM358DR"}, headers=PK)).json()
    assert r["products"][0]["specs"] == [{"name": "Voltage - Supply Span (Max)", "value": "32 V"}]
    assert r["products"][0]["skus"] == ["LM358DR-CT"] and r["provider_status"][0]["found"]
    r2 = (await c.get("/v1/part/specs", params={"mpn": "lm358dr"}, headers=PK)).json()
    assert route.call_count == 1 and r2["provider_status"][0]["from_cache"] and r2["products"] == r["products"]


async def test_substitutes_and_related(api, up):
    summary = {"ManufacturerProductNumber": "LM358BIDR", "Manufacturer": {"Id": 1, "Name": "Texas Instruments"},
               "DigiKeyProductNumber": "296-LM358BIDRCT-ND", "Description": "IC OPAMP", "UnitPrice": "$0.15",
               "QuantityAvailable": 9000, "ProductUrl": "https://dk/p"}
    up.get(f"{DK}/products/v4/search/LM358DR/substitutions").mock(return_value=httpx.Response(200, json={
        "ProductSubstitutesCount": 1, "ProductSubstitutes": [{**summary, "SubstituteType": "Similar"}]}))
    up.get(f"{DK}/products/v4/search/LM358DR/alternatepackaging").mock(return_value=httpx.Response(404))
    up.get(f"{DK}/products/v4/search/LM358DR/associations").mock(return_value=httpx.Response(200, json={
        "ProductAssociations": {"Kits": [], "MatingProducts": [summary], "AssociatedProducts": [], "ForUseWithProducts": []}}))
    c = await api(**CART)
    r = (await c.get("/v1/part/substitutes", params={"mpn": "LM358DR"}, headers=PK)).json()
    assert r["substitutes"] == [{"kind": "Similar", "mpn": "LM358BIDR", "manufacturer": "Texas Instruments",
                                 "sku": "296-LM358BIDRCT-ND", "description": "IC OPAMP", "stock": 9000,
                                 "unit_price": 0.15, "product_url": "https://dk/p"}]
    assert r["alternate_packaging"] == [] and r["provider_status"][0]["ok"]
    rel = (await c.get("/v1/part/related", params={"mpn": "LM358DR"}, headers=PK)).json()
    assert [(x["kind"], x["mpn"]) for x in rel["related"]] == [("mating", "LM358BIDR")]
    assert (await c.get("/v1/part/bogus", params={"mpn": "X"}, headers=PK)).status_code == 422


async def test_cart_needs_cart_client(api, up):
    c = await api(**CART)
    assert (await c.get("/v1/cart", headers=PK)).status_code == 403
    assert (await c.post("/v1/cart/add", json={"items": [{"provider": "mouser", "sku": "x", "qty": 1}]},
                         headers=PK)).status_code == 403
    assert (await c.get("/v1/digikey/connect", headers=PK)).status_code == 403
    off = await api(**{**CART, "cart_clients": ""})  # default: nobody may use the cart
    assert (await off.get("/v1/cart", headers=CK)).status_code == 403


async def test_digikey_not_connected_is_an_error(api, up):
    c = await api(**CART)
    r = (await c.post("/v1/cart/add", json={"items": [{"provider": "digikey", "sku": "296-1395-1-ND", "qty": 5}]},
                      headers=CK)).json()
    st = r["provider_status"][0]
    assert st["ok"] is False and st["error_code"] == "not_connected" and r["carts"] == []
    assert (await c.get("/v1/digikey/connect", headers=CK)).json()["connected"] is False


async def test_digikey_oauth_callback_state(api, up):
    c = await api(**CART)
    assert (await c.get("/v1/digikey/callback", params={"code": "x", "state": "forged"})).status_code == 400
    r = await _connect(c)
    assert r.status_code == 200 and "connected" in r.text
    assert (await c.get("/v1/digikey/connect", headers=CK)).json()["connected"] is True
    tok = up["token"].calls.last.request
    assert not tok.url.query and parse_qs(tok.content.decode())["code"] == ["the-code"]  # code in body, not URL
    state = parse_qs(urlparse(str(r.request.url)).query)["state"][0]
    assert (await c.get("/v1/digikey/callback", params={"code": "y", "state": state})).status_code == 400  # one use


async def test_digikey_list_add_and_view(api, up):
    c = await api(**CART)
    await _connect(c)
    r = (await c.post("/v1/cart/add", json={"items": [
        {"provider": "digikey", "sku": "296-1395-1-ND", "qty": 100, "customer_ref": "U1"}]}, headers=CK)).json()
    assert r["provider_status"][0]["ok"], r
    assert up["create"].call_count == 1 and json.loads(up["create"].calls[0].request.content) == {"ListName": "Taraz cart"}
    assert up.state["parts"] == [{"RequestedPartNumber": "296-1395-1-ND", "Quantities": [{"Quantity": 100}],
                                  "CustomerReference": "U1"}]
    sent = up["add"].calls[0].request.headers
    assert sent["Authorization"] == "Bearer user-1" and sent["X-DIGIKEY-Client-Id"] == "dk-id"
    cart = r["carts"][0]
    assert (cart["provider"], cart["cart_id"], cart["total"]) == ("digikey", "L1", 18.6)
    assert cart["lines"][0] == {"sku": "296-1395-1-ND", "mpn": "LM358DR", "manufacturer": "Texas Instruments",
                                "description": None, "qty": 100, "unit_price": 0.186, "extended_price": 18.6,
                                "customer_ref": "U1", "warnings": []}
    # second add reuses the remembered list id: no new list
    await c.post("/v1/cart/add", json={"items": [{"provider": "digikey", "sku": "A-ND", "qty": 1}]}, headers=CK)
    assert up["create"].call_count == 1


async def test_digikey_refresh_token_rotates(api, up):
    c = await api(**CART)
    await _connect(c)
    dk = c._transport.app.state.service.providers["digikey"]
    dk._user_exp = 0  # access token expired
    r = (await c.get("/v1/cart", headers=CK)).json()
    assert {s["provider"]: s["ok"] for s in r["provider_status"]}["digikey"]
    assert up.state["refresh"] == ["r1"]
    assert up["lists"].calls.last.request.headers["Authorization"] == "Bearer user-2"
    assert await dk.storage.kv_get("digikey_refresh_token") == "r2"  # the rotated token was saved


async def test_digikey_dead_refresh_token_means_reconnect(api, up):
    c = await api(**CART)
    await _connect(c)
    dk = c._transport.app.state.service.providers["digikey"]
    dk._user_exp = 0
    up["token"].mock(side_effect=None, return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    st = {s["provider"]: s for s in (await c.get("/v1/cart", headers=CK)).json()["provider_status"]}["digikey"]
    assert st["error_code"] == "not_connected"
    assert await dk.storage.kv_get("digikey_refresh_token") is None


async def test_mouser_cart_add_uses_cart_key(api, up, caplog):
    caplog.set_level(logging.DEBUG)
    c = await api(**CART)
    r = (await c.post("/v1/cart/add", json={"items": [
        {"provider": "mouser", "sku": "595-LM358DR", "qty": 100, "customer_ref": "U1"}]}, headers=CK)).json()
    req = up["m_insert"].calls[0].request
    assert req.url.params["apiKey"] == CART_KEY  # the Cart key, never the search key
    assert json.loads(req.content) == {"CartKey": "", "CartItems": [
        {"MouserPartNumber": "595-LM358DR", "Quantity": 100, "CustomerPartNumber": "U1"}]}
    cart = r["carts"][0]
    assert (cart["cart_id"], cart["total"], cart["lines"][0]["qty"], cart["lines"][0]["customer_ref"]) == \
        ("cart-123", 20.0, 100, "U1")
    # the created cart is remembered: next add and view use it
    await c.post("/v1/cart/add", json={"items": [{"provider": "mouser", "sku": "595-NE555DR", "qty": 10}]}, headers=CK)
    assert json.loads(up["m_insert"].calls[1].request.content)["CartKey"] == "cart-123"
    await c.get("/v1/cart", headers=CK)
    assert up["m_cart"].calls.last.request.url.params["cartKey"] == "cart-123"
    assert CART_KEY not in caplog.text


async def test_mouser_cart_errors(api, up):
    up["m_insert"].mock(side_effect=None, return_value=httpx.Response(200, json={
        "Errors": [{"Code": "Invalid", "Message": f"Invalid key {CART_KEY}"}], "CartItems": []}))
    c = await api(**CART)
    r = (await c.post("/v1/cart/add", json={"items": [{"provider": "mouser", "sku": "595-X", "qty": 1}]},
                      headers=CK)).json()
    st = r["provider_status"][0]
    assert st["error_code"] == "upstream_error" and CART_KEY not in st["message"] and "***" in st["message"]
    no_key = await api(**{**CART, "mouser_cart_api_key": ""})
    st = (await no_key.get("/v1/cart", headers=CK)).json()["provider_status"]
    assert {s["provider"]: s["error_code"] for s in st}["mouser"] == "not_configured"
