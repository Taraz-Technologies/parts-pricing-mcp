import httpx
import pytest
import respx

from app.providers.digikey import DigiKey

from .conftest import DK, FIXTURES, dk_product, fixture, make_settings, mock_token

DETAILS = rf"{DK}/products/v4/search/[^/]+/productdetails"
KEYWORD = f"{DK}/products/v4/search/keyword"


@pytest.fixture
def dk(settings, storage, http):
    return DigiKey(settings, storage, http)


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        mock_token(r)
        yield r


async def test_token_cached(dk, router):
    router.get(url__regex=DETAILS).mock(return_value=httpx.Response(200, json={"Product": dk_product("LM358DR")}))
    res = await dk.get_many(["LM358DR", "LM358DR "])
    assert all(lk.found for lk in res.values())
    assert router["token"].call_count == 1
    sent = router.calls.last.request.headers
    assert sent["Authorization"] == "Bearer tok-1" and sent["X-DIGIKEY-Client-Id"] == "dk-id"
    assert sent["X-DIGIKEY-Locale-Currency"] == "USD"


async def test_url_encoding_slash(dk, router):
    route = router.get(url__regex=DETAILS).mock(
        return_value=httpx.Response(200, json={"Product": dk_product("LM1117IMPX-3.3/NOPB")}))
    lk = (await dk.get_many(["LM1117IMPX-3.3/NOPB"]))["LM1117IMPX-3.3/NOPB"]
    assert lk.found
    assert route.calls.last.request.url.raw_path == b"/products/v4/search/LM1117IMPX-3.3%2FNOPB/productdetails"


async def test_404_falls_back_to_keyword(dk, router):
    router.get(url__regex=DETAILS).mock(return_value=httpx.Response(404, json={"title": "Not Found"}))
    kw = router.post(KEYWORD).mock(return_value=httpx.Response(200, json={
        "ExactMatches": [dk_product("BSS138", mfr="onsemi"), dk_product("BSS138", mfr="Nexperia")],
        "Products": [dk_product("BSS138W")]}))
    lk = (await dk.get_many(["BSS138"]))["BSS138"]
    assert lk.found and {o.manufacturer for o in lk.offers} == {"onsemi", "Nexperia"}
    assert kw.calls.last.request.read() == b'{"Keywords":"BSS138","Limit":10,"Offset":0}'


async def test_mismatched_mpn_falls_back(dk, router):
    router.get(url__regex=DETAILS).mock(return_value=httpx.Response(200, json={"Product": dk_product("LM358DRG4")}))
    kw = router.post(KEYWORD).mock(return_value=httpx.Response(200, json={"ExactMatches": [], "Products": [
        dk_product("LM358DRG4"), dk_product("LM-358DR")]}))
    lk = (await dk.get_many(["LM358DR"]))["LM358DR"]
    assert kw.called and [o.mpn for o in lk.offers] == ["LM-358DR"] and lk.offers[0].match == "loose"


async def test_not_found_is_not_an_error(dk, router):
    router.get(url__regex=DETAILS).mock(return_value=httpx.Response(404))
    router.post(KEYWORD).mock(return_value=httpx.Response(200, json={"ExactMatches": [], "Products": []}))
    lk = (await dk.get_many(["NOTAREALPART12345XYZ"]))["NOTAREALPART12345XYZ"]
    assert lk.found is False and lk.status.ok is True and lk.status.error_code is None


async def test_401_refreshes_token_once(dk, router):
    router.get(url__regex=DETAILS).mock(side_effect=[
        httpx.Response(401), httpx.Response(200, json={"Product": dk_product("LM358DR")})])
    lk = (await dk.get_many(["LM358DR"]))["LM358DR"]
    assert lk.found and router["token"].call_count == 2


async def test_401_twice_is_auth_failed(dk, router):
    router.get(url__regex=DETAILS).mock(return_value=httpx.Response(401))
    lk = (await dk.get_many(["LM358DR"]))["LM358DR"]
    assert lk.status.error_code == "auth_failed" and "Product Information v4" in lk.status.message
    assert lk.found is False and router["token"].call_count == 2


async def test_bad_credentials(dk, router):
    router["token"].mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    lk = (await dk.get_many(["LM358DR"]))["LM358DR"]
    assert lk.status.error_code == "auth_failed" and "dk-secret" not in lk.status.message


async def test_429_retry_after_short_then_ok(dk, router):
    router.get(url__regex=DETAILS).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"Product": dk_product("LM358DR")}, headers={"X-RateLimit-Remaining": "987"})])
    lk = (await dk.get_many(["LM358DR"]))["LM358DR"]
    assert lk.found
    q = await dk.storage.quota("digikey", 1000)
    assert q["used"] == 2 and q["upstream_remaining_last_seen"] == 987


async def test_429_long_retry_after_is_rate_limited(dk, router):
    router.get(url__regex=DETAILS).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    lk = (await dk.get_many(["LM358DR"]))["LM358DR"]
    assert lk.status.error_code == "rate_limited" and lk.found is False


async def test_5xx_retries_once_then_error(dk, router):
    route = router.get(url__regex=DETAILS).mock(return_value=httpx.Response(502))
    lk = (await dk.get_many(["LM358DR"]))["LM358DR"]
    assert lk.status.error_code == "upstream_error" and route.call_count == 2


async def test_timeout(dk, router):
    router.get(url__regex=DETAILS).mock(side_effect=httpx.ReadTimeout("slow"))
    lk = (await dk.get_many(["LM358DR"]))["LM358DR"]
    assert lk.status.error_code == "timeout"


async def test_quota_exhausted_makes_no_call(tmp_path, storage, http, router):
    dk = DigiKey(make_settings(tmp_path, digikey_daily_limit=51), storage, http)  # 51 - 50 reserve = 1 call
    route = router.get(url__regex=DETAILS).mock(return_value=httpx.Response(200, json={"Product": dk_product("A1")}))
    await dk.get_many(["A1"])
    lk = (await dk.get_many(["B2"]))["B2"]
    assert lk.status.error_code == "quota_exhausted" and route.call_count == 1


async def test_keyword_search_picks_cut_tape(dk, router):
    p = dk_product("2N7002", package="Tape & Reel (TR)")
    p["ProductVariations"].append({**p["ProductVariations"][0], "PackageType": {"Name": "Cut Tape (CT)"},
                                   "DigiKeyProductNumber": "2N7002-CT"})
    router.post(KEYWORD).mock(return_value=httpx.Response(200, json={"Products": [p], "ProductsCount": 41}))
    offers, total = await dk.keyword("2N7002", 5)
    assert total == 41 and [o.sku for o in offers] == ["2N7002-CT"]


def test_reel_multiple_and_digireel_fee(dk):
    p = dk_product("X1", package="Tape & Reel (TR)")
    p["ProductVariations"].append({**p["ProductVariations"][0], "PackageType": {"Name": "Digi-Reel®"},
                                   "DigiReelFee": 7})
    reel, digi = dk._map(p, "exact", "USD")
    assert reel.multiple == 2500 and digi.multiple == 1
    assert str(digi.extra_fee) == "7" and "digi_reel_fee" in digi.warnings


@pytest.mark.skipif(not (FIXTURES / "digikey_productdetails_LM358DR.json").exists(), reason="no real fixture yet")
def test_fixture_mapping(dk):
    j = fixture("digikey_productdetails_LM358DR")
    p = j["Product"]
    offers = dk._map(p, "exact", dk._currency(j))
    assert len(offers) == len(p["ProductVariations"])
    for o, v in zip(offers, p["ProductVariations"]):
        assert o.mpn == "LM358DR" and o.manufacturer and o.sku == v["DigiKeyProductNumber"]
        assert o.packaging and o.currency == "USD" and o.price_breaks or o.marketplace
        assert "missing:manufacturer" not in o.warnings
