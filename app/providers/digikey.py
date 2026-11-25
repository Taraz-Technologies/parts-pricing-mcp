import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote, urlencode

import httpx

from ..models import Cart, CartItemIn, CartLine, Offer, PartRef, PriceBreak, ProductSpecs, ProviderLookup, Spec
from ..normalize import (closest_values, match_kind, match_values, parse_int_stock, parse_lead_time_weeks, parse_price,
                         to_int)
from .base import Provider, ProviderError, SpecError, flag_missing

REFRESH_KEY = "digikey_refresh_token"  # kv: rotates on every refresh, so it lives in SQLite, not env
LIST_KEY = "digikey_list_id"
ML = "/mylists/v1"
ASSOCIATION_KINDS = {"MatingProducts": "mating", "ForUseWithProducts": "for_use_with",
                     "AssociatedProducts": "associated", "Kits": "kit"}
# ProductStatus ids seen live 2026-11-25: 0 Active, 1 Obsolete, 2 Discontinued at DigiKey, 4 Last Time Buy, 7 NRND
STATUS_ACTIVE = "0"
PAGE = 50  # keyword search Limit maximum
SORT = {"Field": "ManufacturerProductNumber", "SortOrder": "Ascending"}  # a fixed order, so Offset pages can't overlap


class DigiKey(Provider):
    """DigiKey Product Information v4 with 2-legged OAuth (client credentials).
    MyLists v1 (the DigiKey "cart": DigiKey has no cart API) with 3-legged OAuth: an admin signs in once."""
    name = "digikey"

    def __init__(self, settings, storage, http):
        super().__init__(settings, storage, http, settings.digikey_per_minute)
        self.daily_limit = settings.digikey_daily_limit
        self.base = settings.digikey_api_base.rstrip("/")
        self.sem = asyncio.Semaphore(settings.digikey_concurrency)
        self._token: str | None = None
        self._token_exp = 0.0
        self._token_lock = asyncio.Lock()
        self._user_token: str | None = None
        self._user_exp = 0.0
        self._user_lock = asyncio.Lock()  # refresh tokens rotate: never refresh twice at once

    @property
    def configured(self) -> bool:
        return bool(self.s.digikey_client_id and self.s.digikey_client_secret.get_secret_value())

    # --- OAuth
    async def _token_post(self, form: dict, label: str) -> dict:
        try:
            r = await self.http.post(f"{self.base}/v1/oauth2/token", timeout=self.s.request_timeout_seconds, data={
                "client_id": self.s.digikey_client_id,
                "client_secret": self.s.digikey_client_secret.get_secret_value(), **form})
        except httpx.TimeoutException:
            raise ProviderError("timeout", f"digikey {label}: timed out")
        except httpx.HTTPError as e:
            raise ProviderError("upstream_error", f"digikey {label}: {type(e).__name__}")
        if r.status_code != 200:
            raise ProviderError("auth_failed", f"digikey {label}: HTTP {r.status_code} "
                                "(check client id/secret and that the app is active)", r.status_code)
        return r.json()

    async def _get_token(self, stale: str | None = None) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_exp and self._token != stale:
                return self._token
            j = await self._token_post({"grant_type": "client_credentials"}, "token")
            self._token = j["access_token"]
            self._token_exp = time.monotonic() + int(j.get("expires_in", 600)) - 60
            return self._token

    def authorize_url(self, state: str) -> str:
        return f"{self.base}/v1/oauth2/authorize?" + urlencode({
            "response_type": "code", "client_id": self.s.digikey_client_id,
            "redirect_uri": self.s.digikey_redirect_uri, "state": state})

    async def connect(self, code: str) -> None:
        """Exchange the one-time auth code (valid 1 minute) from the OAuth callback."""
        async with self._user_lock:
            await self._save_user(await self._token_post({
                "grant_type": "authorization_code", "code": code, "redirect_uri": self.s.digikey_redirect_uri},
                "sign-in"))

    async def connected(self) -> bool:
        return bool(await self.storage.kv_get(REFRESH_KEY))

    async def _save_user(self, j: dict) -> None:
        self._user_token = j["access_token"]
        self._user_exp = time.monotonic() + int(j.get("expires_in", 1800)) - 60
        if j.get("refresh_token"):
            await self.storage.kv_set(REFRESH_KEY, j["refresh_token"])

    async def _get_user_token(self, stale: str | None = None) -> str:
        async with self._user_lock:
            if self._user_token and time.monotonic() < self._user_exp and self._user_token != stale:
                return self._user_token
            refresh = await self.storage.kv_get(REFRESH_KEY)
            if not refresh:
                raise ProviderError("not_connected", "digikey: account not connected - an admin must sign in once "
                                    "(connect_digikey)")
            try:
                j = await self._token_post({"grant_type": "refresh_token", "refresh_token": refresh}, "refresh")
            except ProviderError as e:
                if e.http_status in (400, 401):  # expired (~90 days) or revoked: the stored token is dead
                    await self.storage.kv_set(REFRESH_KEY, None)
                    raise ProviderError("not_connected", "digikey: sign-in expired or revoked - an admin must "
                                        "sign in again (connect_digikey)", e.http_status)
                raise
            await self._save_user(j)
            return self._user_token

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "X-DIGIKEY-Client-Id": self.s.digikey_client_id,
                "X-DIGIKEY-Locale-Site": self.s.digikey_site, "X-DIGIKEY-Locale-Language": self.s.digikey_language,
                "X-DIGIKEY-Locale-Currency": self.s.digikey_currency, "Accept": "application/json"}

    async def _call(self, op: str, method: str, path: str, user: bool = False, **kw) -> httpx.Response:
        get = self._get_user_token if user else self._get_token
        token = await get()
        r = await self.upstream(op, method, self.base + path, path=path, headers=self._headers(token), **kw)
        if r.status_code == 401:  # token revoked/expired early: refresh once, retry once
            token = await get(stale=token)
            r = await self.upstream(op, method, self.base + path, path=path, headers=self._headers(token), **kw)
        if r.status_code in (401, 403):
            api = "MyLists" if user else "Product Information v4"
            raise ProviderError("auth_failed", f"digikey: unauthorized - check app is subscribed to {api}",
                                r.status_code)
        return r

    @staticmethod
    def _ok(r: httpx.Response, what: str) -> None:
        if r.status_code != 200:
            try:
                detail = str(r.json().get("ErrorMessage") or r.json().get("detail") or "")[:200]
            except (ValueError, AttributeError):
                detail = ""
            raise ProviderError("upstream_error", f"digikey {what}: HTTP {r.status_code} {detail}".rstrip(),
                                r.status_code)

    def _currency(self, j: dict) -> str:
        return (j.get("SearchLocaleUsed") or {}).get("Currency") or self.s.digikey_currency

    # --- Product Information v4
    async def _keyword_raw(self, keywords: str, limit: int, offset: int = 0, filters: dict | None = None,
                           sort: dict | None = None) -> dict:
        body = {"Keywords": keywords, "Limit": limit, "Offset": offset}
        if filters:
            body["FilterOptionsRequest"] = filters
        if sort:
            body["SortOptions"] = sort
        r = await self._call("keyword", "POST", "/products/v4/search/keyword", json=body)
        self._ok(r, "keyword")
        return r.json()

    async def _find(self, mpn: str) -> list[tuple[dict, str, str]]:
        """Matching raw Products as (product, match kind, currency): productdetails, else keyword search."""
        found = []
        r = await self._call("productdetails", "GET", f"/products/v4/search/{quote(mpn, safe='')}/productdetails")
        if r.status_code == 200:
            j = r.json()
            p = j.get("Product") or {}
            if kind := match_kind(mpn, p.get("ManufacturerProductNumber")):
                found.append((p, kind, self._currency(j)))
        elif r.status_code not in (400, 404):
            raise ProviderError("upstream_error", f"digikey productdetails: HTTP {r.status_code}", r.status_code)
        if not found:  # 404, or DigiKey answered with a different part: try keyword search
            j = await self._keyword_raw(mpn, 10)
            for p in j.get("ExactMatches") or j.get("Products") or []:
                if kind := match_kind(mpn, p.get("ManufacturerProductNumber")):
                    found.append((p, kind, self._currency(j)))
        return found

    async def _lookup(self, mpn: str) -> ProviderLookup:
        t0 = time.perf_counter()
        ms = lambda: round((time.perf_counter() - t0) * 1000)  # noqa: E731
        async with self.sem:
            try:
                offers = [o for p, kind, cur in await self._find(mpn) for o in self._map(p, kind, cur)]
            except Exception as e:  # noqa: BLE001 - any failure is an error, never "not found"
                return self.error_lookup(e, ms())
        return ProviderLookup(found=bool(offers), offers=offers, status=self.status(found=bool(offers), latency_ms=ms()))

    async def get_many(self, mpns: list[str]) -> dict[str, ProviderLookup]:
        results = await asyncio.gather(*(self._lookup(m) for m in mpns))
        return dict(zip(mpns, results))

    @staticmethod
    def _pick(offers: list[Offer]) -> Offer:
        """One offer per product: DigiKey's own (not marketplace) cut tape if there is one - MOQ 1, every break."""
        own = [o for o in offers if not o.marketplace] or offers
        return next((o for o in own if o.packaging and "Cut Tape" in o.packaging), own[0])

    async def keyword(self, q: str, limit: int) -> tuple[list[Offer], int | None]:
        j = await self._keyword_raw(q, limit)
        return [self._pick(self._map(p, "exact", self._currency(j))) for p in j.get("Products") or []], \
            j.get("ProductsCount")

    # --- search by specification (parametric filters), verified live 2026-11-25
    async def categories(self) -> list[list]:
        """Every DigiKey category as [id, name, parent id]."""
        r = await self._call("categories", "GET", "/products/v4/search/categories")
        self._ok(r, "categories")
        out, todo = [], [(c, None) for c in r.json().get("Categories") or []]
        while todo:
            c, parent = todo.pop()
            out.append([c.get("CategoryId"), c.get("Name"), parent])
            todo += [(ch, c.get("CategoryId")) for ch in c.get("Children") or []]
        if not out:  # cached for hours: an empty tree would turn every search into "unknown category"
            raise ProviderError("upstream_error", "digikey categories: empty answer")
        return out

    async def spec_options(self, category_id: int) -> dict:
        """What one category can be filtered on: its parametric filters with every value, and its manufacturers."""
        return self.options_from(await self._keyword_raw("", 1, filters={"CategoryFilter": [{"Id": str(category_id)}]}),
                                 category_id)

    @staticmethod
    def options_from(j: dict, category_id: int) -> dict:
        fo = j.get("FilterOptions") or {}
        if (to_int(j.get("ProductsCount")) or 0) > 0 and not fo.get("Manufacturers"):  # cached, so never half
            raise ProviderError("upstream_error", f"digikey keyword: no filter options for category {category_id}")
        return {"parameters": [{"id": f.get("ParameterId"), "name": f.get("ParameterName"),
                                "values": [[v.get("ValueId"), v.get("ValueName")] for v in f.get("FilterValues") or []]}
                               for f in fo.get("ParametricFilters") or [] if f.get("ParameterName")],
                "manufacturers": [[m.get("Id"), m.get("Value")] for m in fo.get("Manufacturers") or []]}

    @staticmethod
    def spec_filters(category_id: int, opts: dict, specs: dict[str, str | list[str]], *, rohs: bool,
                     active_only: bool, in_stock: bool, exclude_manufacturers: list[str]) -> tuple[dict, dict, list[str]]:
        """(FilterOptionsRequest, what it means in DigiKey's words, warnings). A spec name or value DigiKey doesn't
        have raises SpecError: dropping that filter would return parts that don't match."""
        fo: dict = {"CategoryFilter": [{"Id": str(category_id)}], "MarketPlaceFilter": "ExcludeMarketPlace"}
        if active_only:
            fo["StatusFilter"] = [{"Id": STATUS_ACTIVE}]
        if search_options := [o for o, on in (("RohsCompliant", rohs), ("InStock", in_stock)) if on]:
            fo["SearchOptions"] = search_options
        params, chosen, warnings = [], {}, []
        names = {p["name"].casefold(): p for p in opts["parameters"]}
        for name, want in specs.items():
            key = name.strip().casefold()
            hits = [p for n, p in names.items() if n == key] or [p for n, p in names.items() if n.startswith(key)]
            if len(hits) != 1:
                raise SpecError(f"{name!r} is not one of this category's DigiKey parameters: "
                                + (", ".join(p["name"] for p in opts["parameters"]) or "none"))
            p, values = hits[0], [v for _, v in hits[0]["values"] if v]
            if p["name"] in chosen:  # two filters on one parameter: DigiKey's answer would not match the echo
                raise SpecError(f"{name!r} and another spec both mean {p['name']!r}: give it once (a list means any of)")
            picked = set()
            for w in [want] if isinstance(want, str) else want:
                if not (m := match_values(w, values)):
                    close = closest_values(w, values)
                    raise SpecError(f"{p['name']}: no DigiKey value matches {w!r}"
                                    + (f"; nearest: {' | '.join(close)}" if close else ""))
                picked.update(m)
            if not picked:
                raise SpecError(f"{p['name']}: no value given")
            params.append({"ParameterId": p["id"], "FilterValues": [{"Id": i} for i, v in p["values"] if v in picked]})
            chosen[p["name"]] = [v for v in values if v in picked]
        if params:  # sorted: the same search is the same request (and cache key) whatever order the specs came in
            fo["ParameterFilterRequest"] = {"CategoryFilter": {"Id": str(category_id)},
                                            "ParameterFilters": sorted(params, key=lambda x: x["ParameterId"])}
        excluded = []
        if words := [w.strip().casefold() for w in exclude_manufacturers if w.strip()]:
            # DigiKey can only include manufacturers: send all the others ("Vishay" drops every Vishay brand)
            excluded = [n for _, n in opts["manufacturers"] if any(w in (n or "").casefold() for w in words)]
            warnings += [f"exclude_manufacturers: nothing in this category matches {w!r}" for w in words
                         if not any(w in (n or "").casefold() for n in excluded)]
            if excluded:
                keep = [{"Id": str(i)} for i, n in opts["manufacturers"] if n not in excluded]
                if not keep:
                    raise SpecError("exclude_manufacturers excludes every manufacturer in this category")
                fo["ManufacturerFilter"] = keep
        return fo, {"specs": chosen, "excluded_manufacturers": excluded, "rohs": rohs, "active_only": active_only,
                    "in_stock": in_stock, "marketplace": False}, warnings

    async def spec_search(self, filters: dict, max_results: int) -> tuple[list[dict], int]:
        """Every product matching a FilterOptionsRequest, up to max_results (1 call per 50), as _pick's offer plus all
        the product's specs. A failed, short or shifted page fails the search: a partial list would read as
        "these are all the matches"."""
        async def page(offset: int) -> dict:
            async with self.sem:
                return await self._keyword_raw("", min(PAGE, max_results - offset), offset, filters, SORT)
        first = await page(0)
        if (total := to_int(first.get("ProductsCount"))) is None:
            raise ProviderError("upstream_error", "digikey keyword: no ProductsCount in the answer")
        try:  # one failed page cancels the others instead of letting them spend quota
            async with asyncio.TaskGroup() as tg:
                rest = [tg.create_task(page(o)) for o in range(PAGE, min(total, max_results), PAGE)]
        except ExceptionGroup as eg:
            raise eg.exceptions[0]
        products: dict[tuple, dict] = {}
        for j in (first, *(t.result() for t in rest)):
            for p in j.get("Products") or []:
                products.setdefault((p.get("ManufacturerProductNumber"), (p.get("Manufacturer") or {}).get("Name")), p)
        if len(products) < (want := min(total, max_results)):
            raise ProviderError("upstream_error", f"digikey returned {len(products)} of {want} matching products; retry")
        cur = self._currency(first)
        return [{**self._pick(self._map(p, "exact", cur)).model_dump(mode="json"),
                 "specs": [{"name": x.get("ParameterText"), "value": x.get("ValueText")} for x in p.get("Parameters") or []]}
                for p in products.values()][:max_results], total

    async def specs(self, mpn: str) -> dict[str, list]:
        async with self.sem:
            found = await self._find(mpn)
        return {"products": [ProductSpecs(
            mpn=p.get("ManufacturerProductNumber"), manufacturer=(p.get("Manufacturer") or {}).get("Name"),
            description=(p.get("Description") or {}).get("DetailedDescription")
            or (p.get("Description") or {}).get("ProductDescription"),
            skus=[v["DigiKeyProductNumber"] for v in p.get("ProductVariations") or [] if v.get("DigiKeyProductNumber")],
            datasheet_url=p.get("DatasheetUrl"), product_url=p.get("ProductUrl"), match=kind, **self._leaf(p),
            specs=[Spec(name=x.get("ParameterText"), value=x.get("ValueText")) for x in p.get("Parameters") or []])
            for p, kind, _ in found]}

    @staticmethod
    def _leaf(p: dict) -> dict:
        """Product.Category is the root; its ChildCategories chain ends at the leaf category spec search takes."""
        c = p.get("Category") or {}
        while c.get("ChildCategories"):
            c = c["ChildCategories"][0]
        return {"category": c.get("Name"), "category_id": c.get("CategoryId")}

    async def _product_get(self, mpn: str, what: str) -> dict | None:
        """GET /products/v4/search/{mpn}/{what}; None when DigiKey doesn't know the part."""
        async with self.sem:
            r = await self._call(what, "GET", f"/products/v4/search/{quote(mpn, safe='')}/{what}")
        if r.status_code in (400, 404):
            return None
        self._ok(r, what)
        return r.json()

    @staticmethod
    def _ref(p: dict, kind: str | None) -> PartRef:
        return PartRef(kind=kind, mpn=p.get("ManufacturerProductNumber"),
                       manufacturer=(p.get("Manufacturer") or {}).get("Name"), sku=p.get("DigiKeyProductNumber"),
                       description=p.get("Description"), stock=to_int(p.get("QuantityAvailable")),
                       unit_price=parse_price(p.get("UnitPrice")), product_url=p.get("ProductUrl"))

    async def substitutes(self, mpn: str) -> dict[str, list]:
        subs, alt = await asyncio.gather(self._product_get(mpn, "substitutions"),
                                         self._product_get(mpn, "alternatepackaging"))
        return {"substitutes": [self._ref(p, p.get("SubstituteType"))
                                for p in (subs or {}).get("ProductSubstitutes") or []],
                "alternate_packaging": [self._ref(p, "alternate_packaging") for p in
                                        ((alt or {}).get("AlternatePackagings") or {}).get("AlternatePackaging") or []]}

    async def related(self, mpn: str) -> dict[str, list]:
        groups = ((await self._product_get(mpn, "associations")) or {}).get("ProductAssociations") or {}
        return {"related": [self._ref(p, kind) for field, kind in ASSOCIATION_KINDS.items()
                            for p in groups.get(field) or []]}

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

    # --- MyLists: one named list is the DigiKey "cart"; a person moves it to the cart on digikey.com
    async def _find_list(self, name: str) -> str | None:
        for start in range(0, 1000, 50):
            r = await self._call("mylists", "GET", f"{ML}/lists", user=True, params={"startIndex": start, "limit": 50})
            self._ok(r, "mylists")
            page = r.json() or []
            if hit := next((x.get("Id") for x in page if x.get("ListName") == name), None):
                return str(hit)
            if len(page) < 50:
                return None
        return None

    async def _list_id(self, create: bool) -> str | None:
        if lid := await self.storage.kv_get(LIST_KEY):
            return lid
        name = self.s.digikey_list_name
        lid = await self._find_list(name)
        if lid is None and create:
            self._ok(await self._call("mylists_create", "POST", f"{ML}/lists", user=True, json={"ListName": name}),
                     "create list")
            lid = await self._find_list(name)  # the create response's string isn't documented as the id
            if lid is None:
                raise ProviderError("upstream_error", f"digikey: created list '{name}' but can't find it")
        if lid:
            await self.storage.kv_set(LIST_KEY, lid)
        return lid

    def _empty_cart(self, warning: str) -> Cart:
        return Cart(provider="digikey", name=self.s.digikey_list_name, currency=self.s.digikey_currency,
                    warnings=[warning])

    async def cart_view(self) -> Cart:
        if not self.configured:
            raise ProviderError("not_configured", "digikey API key not set")
        lid = await self._list_id(create=False)
        if lid is None:
            return self._empty_cart("empty: list not created yet")
        lines, start, total = [], 0, None
        while total is None or start < total:
            r = await self._call("mylists_parts", "GET", f"{ML}/lists/{lid}/parts", user=True, params={
                "countryIso": self.s.digikey_site, "currencyIso": self.s.digikey_currency,
                "languageIso": self.s.digikey_language, "startIndex": start, "limit": 100})
            if r.status_code == 404:  # list deleted on the website
                await self.storage.kv_set(LIST_KEY, None)
                return self._empty_cart("empty: list was deleted on digikey.com")
            self._ok(r, "list parts")
            j = r.json()
            page = j.get("PartsList") or []
            lines += [self._map_list_part(p) for p in page]
            total, start = to_int(j.get("TotalParts")) or 0, start + 100
            if not page:
                break
        known = [ln.extended_price for ln in lines]
        return Cart(provider="digikey", cart_id=lid, name=self.s.digikey_list_name, currency=self.s.digikey_currency,
                    lines=lines, total=sum(known, Decimal(0)) if lines and None not in known else None,
                    warnings=["digikey_list: open My Lists on digikey.com and add this list to the cart to order"])

    @staticmethod
    def _map_list_part(p: dict) -> CartLine:
        q = (p.get("Quantities") or [{}])[0]
        opts = q.get("PackOptions") or []
        i = q.get("SelectedPackOptionIndex")
        opt = opts[i] if isinstance(i, int) and 0 <= i < len(opts) else (opts[0] if opts else {})
        line = CartLine(sku=opt.get("DigiKeyPartNumber") or p.get("DigiKeyPartNumber") or p.get("RequestedPartNumber"),
                        mpn=p.get("ManufacturerPartNumber"), manufacturer=p.get("Manufacturer"),
                        description=p.get("Description"), qty=to_int(q.get("QuantityRequested")),
                        unit_price=parse_price(opt.get("CalculatedUnitPrice")),
                        extended_price=parse_price(opt.get("ExtendedPrice")),
                        customer_ref=p.get("CustomerReference") or None)
        calc = to_int(q.get("CalculatedQuantity"))
        if calc is not None and calc != line.qty:
            line.warnings.append(f"digikey_calculated_qty:{calc}")
        line.warnings += [f"missing:{f}" for f in ("unit_price", "extended_price") if getattr(line, f) is None]
        return line

    async def cart_add(self, items: list[CartItemIn]) -> Cart:
        if not self.configured:
            raise ProviderError("not_configured", "digikey API key not set")
        body = [{"RequestedPartNumber": i.sku, "Quantities": [{"Quantity": i.qty}],
                 **({"CustomerReference": i.customer_ref} if i.customer_ref else {})} for i in items]
        for attempt in (1, 2):
            lid = await self._list_id(create=True)
            r = await self._call("mylists_add", "POST", f"{ML}/lists/{lid}/parts", user=True, n_parts=len(items),
                                 json=body)
            if r.status_code == 404 and attempt == 1:  # list deleted on the website: make it again
                await self.storage.kv_set(LIST_KEY, None)
                continue
            self._ok(r, "add parts")
            break
        return await self.cart_view()
