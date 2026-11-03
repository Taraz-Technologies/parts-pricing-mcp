import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.storage import Storage

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
FAKE_MOUSER_KEY = "mouser-secret-abc123"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def make_settings(tmp_path, **kw) -> Settings:
    base = dict(digikey_client_id="dk-id", digikey_client_secret="dk-secret", mouser_search_api_key=FAKE_MOUSER_KEY,
                data_dir=str(tmp_path), console_api_keys="planner:pk,prc:rk,mcp:mk", max_wait_seconds=2)
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
async def storage(settings):
    s = await Storage(settings.data_dir, settings.cache_ttl_hours, settings.notfound_ttl_hours,
                      settings.search_ttl_hours, settings.quota_reserve).open()
    yield s
    await s.close()


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def clock(monkeypatch):
    """Controllable UTC clock for storage TTL/day tests."""
    state = {"now": datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr("app.storage.utcnow", lambda: state["now"])
    return state


# --- minimal upstream payload builders (shapes per fixtures/)
DK = "https://api.digikey.com"
MOUSER_HOST = "api.mouser.com"


def dk_product(mpn, mfr="Texas Instruments", stock=1000, breaks=((1, 0.42), (10, 0.297), (100, 0.186)),
               package="Cut Tape (CT)"):
    return {"ManufacturerProductNumber": mpn, "Manufacturer": {"Name": mfr},
            "Description": {"ProductDescription": f"{mpn} desc"}, "QuantityAvailable": stock,
            "ProductStatus": {"Status": "Active"}, "ManufacturerLeadWeeks": "6",
            "ProductVariations": [{"DigiKeyProductNumber": f"{mpn}-CT", "PackageType": {"Name": package},
                                   "QuantityAvailableforPackageType": stock, "MinimumOrderQuantity": 1,
                                   "StandardPackage": 2500, "MarketPlace": False,
                                   "StandardPricing": [{"BreakQuantity": q, "UnitPrice": p} for q, p in breaks]}]}


def mouser_part(mpn, mfr="Texas Instruments", stock="5000", breaks=((1, "$0.40"), (10, "$0.30"), (100, "$0.20"))):
    return {"ManufacturerPartNumber": mpn, "Manufacturer": mfr, "MouserPartNumber": f"595-{mpn}",
            "Description": f"{mpn} desc", "AvailabilityInStock": stock, "Availability": f"{stock} In Stock",
            "AvailabilityOnOrder": [], "Min": "1", "Mult": "1", "LeadTime": "42 Days",
            "ProductAttributes": [{"AttributeName": "Packaging", "AttributeValue": "Cut Tape"}],
            "PriceBreaks": [{"Quantity": q, "Price": p, "Currency": "USD"} for q, p in breaks]}


def mouser_response(parts=(), errors=()):
    return {"Errors": list(errors), "SearchResults": {"NumberOfResult": len(parts), "Parts": list(parts)}}


def mock_token(router):
    return router.post(f"{DK}/v1/oauth2/token", name="token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 600}))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retries sleep 1 s / Retry-After; tests don't wait."""
    async def fast(_s):
        return None
    monkeypatch.setattr("app.providers.base.asyncio_sleep", fast)
