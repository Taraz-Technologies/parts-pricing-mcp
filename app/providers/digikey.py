import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from ..models import Offer, PriceBreak, ProviderLookup
from ..normalize import match_kind, parse_int_stock, parse_lead_time_weeks, parse_price, to_int
from .base import Provider, ProviderError, flag_missing


class DigiKey(Provider):
    """DigiKey Product Information v4, 2-legged OAuth (client credentials)."""
    name = "digikey"

    def __init__(self, settings, storage, http):
        super().__init__(settings, storage, http, settings.digikey_per_minute)
        self.daily_limit = settings.digikey_daily_limit
        self.base = settings.digikey_api_base.rstrip("/")
        self.sem = asyncio.Semaphore(settings.digikey_concurrency)
        self._token: str | None = None
        self._token_exp = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.s.digikey_client_id and self.s.digikey_client_secret.get_secret_value())

    async def _get_token(self, stale: str | None = None) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_exp and self._token != stale:
                return self._token
            try:
                r = await self.http.post(f"{self.base}/v1/oauth2/token", timeout=self.s.request_timeout_seconds, data={
                    "client_id": self.s.digikey_client_id,
                    "client_secret": self.s.digikey_client_secret.get_secret_value(),
                    "grant_type": "client_credentials"})
            except httpx.TimeoutException:
                raise ProviderError("timeout", "digikey token: timed out")
            except httpx.HTTPError as e:
                raise ProviderError("upstream_error", f"digikey token: {type(e).__name__}")
            if r.status_code != 200:
                raise ProviderError("auth_failed", f"digikey token: HTTP {r.status_code} "
                                    "(check client id/secret and that the app is active)", r.status_code)
            j = r.json()
            self._token = j["access_token"]
            self._token_exp = time.monotonic() + int(j.get("expires_in", 600)) - 60
            return self._token

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "X-DIGIKEY-Client-Id": self.s.digikey_client_id,
                "X-DIGIKEY-Locale-Site": self.s.digikey_site, "X-DIGIKEY-Locale-Language": self.s.digikey_language,
                "X-DIGIKEY-Locale-Currency": self.s.digikey_currency, "Accept": "application/json"}

    async def _call(self, op: str, method: str, path: str, **kw) -> httpx.Response:
        token = await self._get_token()
        r = await self.upstream(op, method, self.base + path, path=path, headers=self._headers(token), **kw)
        if r.status_code == 401:  # token revoked/expired early: refresh once, retry once
            token = await self._get_token(stale=token)
            r = await self.upstream(op, method, self.base + path, path=path, headers=self._headers(token), **kw)
        if r.status_code in (401, 403):
            raise ProviderError("auth_failed", "digikey: unauthorized - check app is subscribed to Product Information v4",
                                r.status_code)
        return r

    def _currency(self, j: dict) -> str:
        return (j.get("SearchLocaleUsed") or {}).get("Currency") or self.s.digikey_currency

    async def _keyword_raw(self, keywords: str, limit: int) -> dict:
        r = await self._call("keyword", "POST", "/products/v4/search/keyword",
                             json={"Keywords": keywords, "Limit": limit, "Offset": 0})
        if r.status_code != 200:
            raise ProviderError("upstream_error", f"digikey keyword: HTTP {r.status_code}", r.status_code)
        return r.json()

    async def _lookup(self, mpn: str) -> ProviderLookup:
        t0 = time.perf_counter()
        ms = lambda: round((time.perf_counter() - t0) * 1000)  # noqa: E731
        async with self.sem:
            try:
                offers = []
                r = await self._call("productdetails", "GET", f"/products/v4/search/{quote(mpn, safe='')}/productdetails")
                if r.status_code == 200:
                    j = r.json()
                    p = j.get("Product") or {}
                    kind = match_kind(mpn, p.get("ManufacturerProductNumber"))
                    if kind:
                        offers = self._map(p, kind, self._currency(j))
                elif r.status_code not in (400, 404):
                    raise ProviderError("upstream_error", f"digikey productdetails: HTTP {r.status_code}", r.status_code)
                if not offers:  # 404, or DigiKey answered with a different part: try keyword search
                    j = await self._keyword_raw(mpn, 10)
                    for p in j.get("ExactMatches") or j.get("Products") or []:
                        kind = match_kind(mpn, p.get("ManufacturerProductNumber"))
                        if kind:
                            offers += self._map(p, kind, self._currency(j))
            except Exception as e:  # noqa: BLE001 - any failure is an error, never "not found"
                return self.error_lookup(e, ms())
        return ProviderLookup(found=bool(offers), offers=offers, status=self.status(found=bool(offers), latency_ms=ms()))

    async def get_many(self, mpns: list[str]) -> dict[str, ProviderLookup]:
        results = await asyncio.gather(*(self._lookup(m) for m in mpns))
        return dict(zip(mpns, results))

    async def keyword(self, q: str, limit: int) -> tuple[list[Offer], int | None]:
        j = await self._keyword_raw(q, limit)
        out = []
        for p in j.get("Products") or []:
            offers = self._map(p, "exact", self._currency(j))
            out.append(next((o for o in offers if o.packaging and "Cut Tape" in o.packaging), offers[0]))
        return out, j.get("ProductsCount")

    def _map(self, p: dict, kind: str, currency: str) -> list[Offer]:
        desc = p.get("Description") or {}
        common = dict(
            provider="digikey", mpn=p.get("ManufacturerProductNumber"),
            manufacturer=(p.get("Manufacturer") or {}).get("Name"),
            description=desc.get("ProductDescription") or desc.get("DetailedDescription"),
            currency=currency, lead_time_weeks=parse_lead_time_weeks(p.get("ManufacturerLeadWeeks")),
            lifecycle=(p.get("ProductStatus") or {}).get("Status"), rohs=(p.get("Classifications") or {}).get("RohsStatus"),
            datasheet_url=p.get("DatasheetUrl"), product_url=p.get("ProductUrl"), image_url=p.get("PhotoUrl"),
            match=kind, fetched_at=datetime.now(timezone.utc))
        variations = p.get("ProductVariations") or []
        if not variations:
            return [flag_missing(Offer(**common, stock=parse_int_stock(p.get("QuantityAvailable")),
                                       warnings=["no_variations"]))]
        offers = []
        for v in variations:
            packaging = (v.get("PackageType") or {}).get("Name")
            stock = v.get("QuantityAvailableforPackageType")
            warnings, fee = [], None
            if packaging and "Digi-Reel" in packaging:
                fee = parse_price(v.get("DigiReelFee"))
                warnings.append("digi_reel_fee")
            breaks = [PriceBreak(qty=b["BreakQuantity"], unit_price=parse_price(b["UnitPrice"]))
                      for b in v.get("StandardPricing") or [] if parse_price(b.get("UnitPrice")) is not None]
            offers.append(flag_missing(Offer(
                **common, sku=v.get("DigiKeyProductNumber"), packaging=packaging,
                stock=parse_int_stock(stock if stock is not None else p.get("QuantityAvailable")),
                moq=to_int(v.get("MinimumOrderQuantity")),
                multiple=to_int(v.get("StandardPackage")) if packaging and "Tape & Reel" in packaging else 1,
                price_breaks=breaks, marketplace=bool(v.get("MarketPlace")), extra_fee=fee, warnings=warnings)))
        return offers
