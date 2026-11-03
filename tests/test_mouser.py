import json
import logging
import re

import httpx
import pytest
import respx

from app.config import setup_logging
from app.providers.mouser import Mouser

from .conftest import FAKE_MOUSER_KEY, FIXTURES, MOUSER_HOST, fixture, make_settings, mouser_part, mouser_response


@pytest.fixture
def ms(settings, storage, http):
    return Mouser(settings, storage, http)


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


def part_route(router, **kw):
    return router.post(host=MOUSER_HOST, path="/api/v1/search/partnumber").mock(**kw)


def echo(req):
    """Mouser stand-in: returns a part for each queried MPN except NOTAREAL*."""
    mpns = json.loads(req.content)["SearchByPartRequest"]["mouserPartNumber"].split("|")
    return httpx.Response(200, json=mouser_response([mouser_part(m) for m in mpns if not m.startswith("NOTAREAL")]))


async def test_batch_body_uses_pipe_and_missing_is_not_found(ms, router):
    route = part_route(router, side_effect=echo)
    res = await ms.get_many(["LM358DR", "GRM188R71C104KA01D", "NOTAREALPART12345XYZ"])
    body = json.loads(route.calls.last.request.content)
    assert body == {"SearchByPartRequest": {"mouserPartNumber": "LM358DR|GRM188R71C104KA01D|NOTAREALPART12345XYZ",
                                            "partSearchOptions": "Exact"}}
    assert route.call_count == 1 and route.calls.last.request.url.params["apiKey"] == FAKE_MOUSER_KEY
    assert res["LM358DR"].found and res["GRM188R71C104KA01D"].found
    nf = res["NOTAREALPART12345XYZ"]
    assert nf.found is False and nf.status.ok is True
    assert (await ms.storage.quota("mouser", 1000))["used"] == 1


async def test_chunking_and_pipe_mpn_alone(tmp_path, storage, http, router):
    ms = Mouser(make_settings(tmp_path, mouser_batch_size=2), storage, http)
    route = part_route(router, side_effect=echo)
    await ms.get_many(["A1", "B2", "C3", "D|4"])
    sent = sorted(json.loads(c.request.content)["SearchByPartRequest"]["mouserPartNumber"] for c in route.calls)
    assert sent == ["A1|B2", "C3", "D|4"]


async def test_mapping(ms, router):
    p = mouser_part("LM358DR", stock="12345")
    p["AvailabilityOnOrder"] = [{"Quantity": 100}, {"Quantity": "50"}]
    p["ProductAttributes"].append({"AttributeName": "Packaging", "AttributeValue": "Reel"})
    p["Mult"], p["Min"] = "2500", "2500"
    part_route(router, return_value=httpx.Response(200, json=mouser_response([p])))
    o = (await ms.get_many(["LM358DR"]))["LM358DR"].offers[0]
    assert (o.stock, o.on_order, o.moq, o.multiple, o.lead_time_weeks) == (12345, 150, 2500, 2500, 6)
    assert o.packaging == "Cut Tape, Reel" and o.currency == "USD" and o.sku == "595-LM358DR"
    assert [(b.qty, str(b.unit_price)) for b in o.price_breaks] == [(1, "0.40"), (10, "0.30"), (100, "0.20")]


async def test_not_sold_parts_skipped_with_warning(ms, router):
    p = mouser_part("LM358DR")
    p["MouserPartNumber"] = "N/A"
    part_route(router, return_value=httpx.Response(200, json=mouser_response([p])))
    lk = (await ms.get_many(["LM358DR"]))["LM358DR"]
    assert lk.found is False and lk.warnings == ["mouser_not_sold:Texas Instruments"]


async def test_loose_match_assignment(ms, router):
    part_route(router, return_value=httpx.Response(200, json=mouser_response([mouser_part("74HC595D/118")])))
    lk = (await ms.get_many(["74HC595D,118"]))["74HC595D,118"]
    assert lk.found and lk.offers[0].match == "loose"


async def test_errors_array_is_error(ms, router):
    part_route(router, return_value=httpx.Response(200, json=mouser_response(
        errors=[{"Code": "Invalid", "Message": "Invalid unique identifier."}])))
    lk = (await ms.get_many(["LM358DR"]))["LM358DR"]
    assert lk.found is False and lk.status.error_code == "upstream_error"
    assert lk.status.message == "mouser: Invalid unique identifier."


async def test_rate_limit_message_and_429(ms, router):
    route = part_route(router, return_value=httpx.Response(200, json=mouser_response(
        errors=[{"Message": "Maximum calls per minute exceeded. Too many requests."}])))
    assert (await ms.get_many(["A1"]))["A1"].status.error_code == "rate_limited"
    route.mock(return_value=httpx.Response(429))
    assert (await ms.get_many(["A1"]))["A1"].status.error_code == "rate_limited"


async def test_api_key_never_logged(ms, router, caplog):
    setup_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.DEBUG)  # worst case: someone turns httpx logging back on
    try:
        route = part_route(router, return_value=httpx.Response(503))
        lk = (await ms.get_many(["LM358DR"]))["LM358DR"]
        route.mock(side_effect=httpx.ConnectError("boom"))
        lk2 = (await ms.get_many(["NE555DR"]))["NE555DR"]
        logging.getLogger("x").warning("leak test %s", f"https://api.mouser.com/x?apiKey={FAKE_MOUSER_KEY}&a=1")
    finally:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    assert lk.status.error_code == "upstream_error" and lk2.status.error_code == "upstream_error"
    text = caplog.text + lk.status.message + lk2.status.message
    assert FAKE_MOUSER_KEY not in text
    assert "apiKey=" in caplog.text  # the leak test line was logged...
    assert all(m == "apiKey=***" for m in re.findall(r"apiKey=[^&\s\"']*", caplog.text))  # ...redacted


async def test_keyword(ms, router):
    route = router.post(host=MOUSER_HOST, path="/api/v1/search/keyword").mock(
        return_value=httpx.Response(200, json=mouser_response([mouser_part("2N7002"), mouser_part("2N7002K")])))
    offers, total = await ms.keyword("2N7002", 80)
    assert len(offers) == 2 and total == 2
    assert json.loads(route.calls.last.request.content)["SearchByKeywordRequest"]["records"] == 50


@pytest.mark.skipif(not (FIXTURES / "mouser_partnumber_batch.json").exists(), reason="no real fixture yet")
def test_fixture_mapping(ms):
    for name in ("mouser_partnumber_single", "mouser_partnumber_batch"):
        for p in fixture(name)["SearchResults"]["Parts"]:
            o = ms._map(p, "exact")
            assert o.mpn and o.manufacturer and o.sku and o.currency and o.price_breaks
            assert o.stock is not None and o.moq and o.multiple
