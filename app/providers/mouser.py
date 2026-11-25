import asyncio
import time
from datetime import datetime, timezone

import httpx

from ..models import Cart, CartItemIn, CartLine, Offer, PriceBreak, ProviderLookup
from ..normalize import match_kind, parse_int_stock, parse_lead_time_weeks, parse_price, to_int
from .base import Provider, ProviderError, flag_missing


CART_KEY = "mouser_cart_key"  # kv: cart created by the first add when MOUSER_CART_KEY is empty


class Mouser(Provider):
    """Mouser Search API v1 + Cart API v1 (separate "Cart and Order" key). Keys are URL query params:
    never put the URL in a message or log."""
    name = "mouser"

    def __init__(self, settings, storage, http):
        super().__init__(settings, storage, http, settings.mouser_per_minute)
        self.daily_limit = settings.mouser_daily_limit
        self.base = settings.mouser_api_base.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.s.mouser_search_api_key.get_secret_value())

    def _check(self, r: httpx.Response) -> None:
        try:
            j = r.json()
        except ValueError:
            raise ProviderError("upstream_error", f"mouser: non-JSON response (HTTP {r.status_code})", r.status_code)
        errors = j.get("Errors") or []
        if errors:
            e0 = errors[0]
            msg = str(e0.get("Message") if isinstance(e0, dict) else e0)
            if isinstance(e0, dict) and e0.get("PropertyName"):  # e.g. "Required" + "API Key"
                msg += f" ({e0['PropertyName']})"
            for key in (self.s.mouser_search_api_key, self.s.mouser_cart_api_key):
                if key.get_secret_value():
                    msg = msg.replace(key.get_secret_value(), "***")
            low = msg.lower()
            code = "rate_limited" if any(w in low for w in ("too many", "rate", "limit")) else "upstream_error"
            raise ProviderError(code, f"mouser: {msg}", r.status_code)
        if r.status_code >= 400:
            code = "auth_failed" if r.status_code in (401, 403) else "upstream_error"
            raise ProviderError(code, f"mouser: HTTP {r.status_code}", r.status_code)

    async def _post(self, op: str, body: dict, n_parts: int) -> dict:
        path = f"/api/v1/search/{op}"
        r = await self.upstream(op, "POST", self.base + path, path=path, n_parts=n_parts, check=self._check,
                                params={"apiKey": self.s.mouser_search_api_key.get_secret_value()}, json=body)
        return r.json()

    async def get_many(self, mpns: list[str]) -> dict[str, ProviderLookup]:
        n = max(1, self.s.mouser_batch_size)
        alone = [[m] for m in mpns if "|" in m]  # "|" is Mouser's batch separator
        rest = [m for m in mpns if "|" not in m]
        chunks = alone + [rest[i:i + n] for i in range(0, len(rest), n)]
        out = {}
        for part in await asyncio.gather(*(self._batch(c) for c in chunks)):
            out.update(part)
        return out

    async def _batch(self, chunk: list[str]) -> dict[str, ProviderLookup]:
        t0 = time.perf_counter()
        try:
            j = await self._post("partnumber", {"SearchByPartRequest": {
                "mouserPartNumber": "|".join(chunk), "partSearchOptions": "Exact"}}, len(chunk))
            parts = (j.get("SearchResults") or {}).get("Parts") or []
            offers = {m: [] for m in chunk}
            warnings = {m: [] for m in chunk}
            for part in parts:
                mpn = part.get("ManufacturerPartNumber")
                kinds = {m: match_kind(m, mpn) for m in chunk}
                target = next((m for m, k in kinds.items() if k == "exact"), None) or \
                    next((m for m, k in kinds.items() if k == "loose"), None)
                if target is None:
                    continue
                sku = (part.get("MouserPartNumber") or "").strip()
                if sku in ("", "N/A") or "NOT" in sku.upper():
                    warnings[target].append(f"mouser_not_sold:{part.get('Manufacturer')}")
                    continue
                offers[target].append(self._map(part, kinds[target]))
        except Exception as e:  # noqa: BLE001 - any failure is an error, never "not found"
            lookup = self.error_lookup(e, round((time.perf_counter() - t0) * 1000))
            return {m: lookup for m in chunk}
        ms = round((time.perf_counter() - t0) * 1000)
        return {m: ProviderLookup(found=bool(offers[m]), offers=offers[m], warnings=warnings[m],
                                  status=self.status(found=bool(offers[m]), latency_ms=ms)) for m in chunk}

    async def keyword(self, q: str, limit: int) -> tuple[list[Offer], int | None]:
        j = await self._post("keyword", {"SearchByKeywordRequest": {
            "keyword": q, "records": min(limit, 50), "startingRecord": 0, "searchOptions": "None",
            "searchWithYourSignUpLanguage": "false"}}, 1)
        sr = j.get("SearchResults") or {}
        return [self._map(p, "exact") for p in sr.get("Parts") or []], sr.get("NumberOfResult")

    def _map(self, p: dict, kind: str) -> Offer:
        breaks = []
        for b in p.get("PriceBreaks") or []:
            price = parse_price(b.get("Price"))
            if price is not None and to_int(b.get("Quantity")):
                breaks.append(PriceBreak(qty=to_int(b["Quantity"]), unit_price=price))
        on_order = sum(to_int(o.get("Quantity")) or 0 for o in p.get("AvailabilityOnOrder") or [])
        stock = parse_int_stock(p.get("AvailabilityInStock"))
        packaging = [a.get("AttributeValue") for a in p.get("ProductAttributes") or []
                     if a.get("AttributeName") == "Packaging" and a.get("AttributeValue")]
        return flag_missing(Offer(
            provider="mouser", mpn=p.get("ManufacturerPartNumber"), manufacturer=p.get("Manufacturer"),
            sku=p.get("MouserPartNumber"), description=p.get("Description"),
            packaging=", ".join(packaging) or None,
            stock=stock if stock is not None else parse_int_stock(p.get("Availability")),
            on_order=on_order or None, moq=to_int(p.get("Min")), multiple=to_int(p.get("Mult")),
            price_breaks=breaks, currency=(p.get("PriceBreaks") or [{}])[0].get("Currency"),
            lead_time_weeks=parse_lead_time_weeks(p.get("LeadTime")), lifecycle=p.get("LifecycleStatus"),
            rohs=p.get("ROHSStatus"), datasheet_url=p.get("DataSheetUrl"), product_url=p.get("ProductDetailUrl"),
            image_url=p.get("ImagePath"), match=kind, fetched_at=datetime.now(timezone.utc)))

    # --- Cart API. Never the Order API.
    async def _cart_call(self, op: str, method: str, path: str, n_parts: int = 1, **kw) -> dict:
        key = self.s.mouser_cart_api_key.get_secret_value()
        if not key:
            raise ProviderError("not_configured", "mouser cart key (MOUSER_CART_API_KEY) not set")
        params = {"apiKey": key, **kw.pop("params", {})}
        r = await self.upstream(op, method, self.base + path, path=path, n_parts=n_parts, check=self._check,
                                params=params, **kw)
        return r.json()

    async def _cart_key(self) -> str:
        return self.s.mouser_cart_key or await self.storage.kv_get(CART_KEY) or ""

    async def cart_view(self) -> Cart:
        key = await self._cart_key()
        if not key:
            if not self.s.mouser_cart_api_key.get_secret_value():
                raise ProviderError("not_configured", "mouser cart key (MOUSER_CART_API_KEY) not set")
            return Cart(provider="mouser", warnings=["empty: no cart yet (the first add creates one)"])
        return self._map_cart(await self._cart_call("cart", "GET", "/api/v1/cart", params={"cartKey": key}))

    async def cart_add(self, items: list[CartItemIn]) -> Cart:
        key, j = await self._cart_key(), {}
        for i in range(0, len(items), 100):  # Mouser takes at most 100 items per request
            chunk = items[i:i + 100]
            j = await self._cart_call("cart_insert", "POST", "/api/v1/cart/items/insert", n_parts=len(chunk), json={
                "CartKey": key, "CartItems": [{"MouserPartNumber": it.sku, "Quantity": it.qty,
                                               **({"CustomerPartNumber": it.customer_ref} if it.customer_ref else {})}
                                              for it in chunk]})
            if not key and j.get("CartKey"):  # empty CartKey: Mouser created a cart; keep using it
                key = j["CartKey"]
                if not self.s.mouser_cart_key:
                    await self.storage.kv_set(CART_KEY, key)
        return self._map_cart(j)

    @staticmethod
    def _map_cart(j: dict) -> Cart:
        lines = []
        for ln in j.get("CartItems") or []:
            line = CartLine(sku=ln.get("MouserPartNumber"), mpn=ln.get("MfrPartNumber"),
                            manufacturer=ln.get("Manufacturer"), description=ln.get("Description"),
                            qty=to_int(ln.get("Quantity")), unit_price=parse_price(ln.get("UnitPrice")),
                            extended_price=parse_price(ln.get("ExtendedPrice")),
                            customer_ref=ln.get("CartItemCustPartNumber") or None)
            line.warnings += [str(e.get("Message") if isinstance(e, dict) else e) for e in ln.get("Errors") or []]
            line.warnings += [str(m.get("Message") if isinstance(m, dict) else m) for m in ln.get("InfoMessages") or []]
            line.warnings += [f"missing:{f}" for f in ("unit_price", "extended_price") if getattr(line, f) is None]
            lines.append(line)
        return Cart(provider="mouser", cart_id=j.get("CartKey"), currency=j.get("CurrencyCode"), lines=lines,
                    total=parse_price(j.get("MerchandiseTotal")))
