import asyncio
import difflib
import json
import logging
import time

from pydantic import ValidationError

from .models import Cart, CartItemIn, Offer, PartQuery, PartResult, ProviderLookup, ProviderStatus
from .normalize import mpn_strict
from .pricing import choose_best, price_offer
from .providers.base import Provider, SpecError
from .storage import Storage, current_client, utcnow

log = logging.getLogger(__name__)
INFO_FIELDS = {"specs": ("products",), "substitutes": ("substitutes", "alternate_packaging"), "related": ("related",)}
CART_PROVIDERS = ("mouser", "digikey")


class PricingService:
    def __init__(self, settings, storage: Storage, providers: dict[str, Provider]):
        self.s, self.storage, self.providers = settings, storage, providers
        self._fetching: dict[str, asyncio.Lock] = {}  # search_cache key -> lock while it is being fetched

    def _key(self, mpn: str) -> str:
        return f"{mpn_strict(mpn)}|{self.s.digikey_currency}"

    def _not_configured(self, name: str) -> ProviderStatus:
        return ProviderStatus(provider=name, ok=False, error_code="not_configured", message=f"{name} API key not set")

    async def _remaining(self, p: Provider) -> int:
        return (await self.storage.quota(p.name, p.daily_limit))["remaining"]

    async def _provider_lookups(self, p: Provider, mpns: list[str], fresh: bool) -> dict[str, ProviderLookup]:
        if not p.configured:
            st = self._not_configured(p.name)
            return {m: ProviderLookup(found=False, status=st) for m in mpns}
        out: dict[str, ProviderLookup] = {}
        if not fresh:
            hits = await self.storage.cache_get(p.name, [self._key(m) for m in mpns])
            if hits:
                remaining = await self._remaining(p)
                for m in mpns:
                    if (hit := hits.get(self._key(m))) is None:
                        continue
                    found, payload, _ = hit
                    try:
                        offers = [Offer.model_validate({**o, "from_cache": True}) for o in payload]
                    except ValidationError:  # cached under an older schema: refetch
                        continue
                    out[m] = ProviderLookup(found=found, offers=offers, status=ProviderStatus(
                        provider=p.name, found=found, from_cache=True, latency_ms=0, quota_remaining_today=remaining))
                if out:
                    await self.storage.log(p.name, "cache", len(out), "cache_hit")
        misses = [m for m in mpns if m not in out]
        if misses:
            fetched = await p.get_many(misses)
            for m in misses:
                lk = fetched[m]
                if lk.status.ok:  # errors are never cached
                    await self.storage.cache_put(p.name, self._key(m), lk.found,
                                                 [o.model_dump(mode="json") for o in lk.offers])
                out[m] = lk
        return out

    async def lookup_many(self, items: list[dict], providers: list[str], fresh: bool = False,
                          client: str | None = None) -> list[PartResult]:
        """items: dicts with mpn, qty, manufacturer?, line_id?. Duplicate MPNs are looked up once."""
        current_client.set(client)
        uniq: dict[str, str] = {}
        for it in items:
            uniq.setdefault(mpn_strict(it["mpn"]), it["mpn"].strip())
        mpns = list(uniq.values())
        results = await asyncio.gather(*(self._provider_lookups(self.providers[n], mpns, fresh) for n in providers))
        by_provider = dict(zip(providers, results))
        lines = []
        for it in items:
            m = uniq[mpn_strict(it["mpn"])]
            lks = {n: by_provider[n][m] for n in providers}
            priced = [price_offer(o, it["qty"]) for lk in lks.values() for o in lk.offers]
            best, reason, by_p, warnings = choose_best(priced, it.get("manufacturer"))
            priced.sort(key=lambda o: (o.provider, o.extended_price is None, o.extended_price or 0))
            lines.append(PartResult(
                line_id=it.get("line_id"), query=PartQuery(mpn=it["mpn"], qty=it["qty"], manufacturer=it.get("manufacturer")),
                offers=priced, best_by_provider={n: by_p.get(n) for n in providers}, best=best, best_reason=reason,
                provider_status=[lks[n].status for n in providers],
                warnings=warnings + [w for lk in lks.values() for w in lk.warnings]))
        return lines

    async def search(self, keyword: str, limit: int, providers: list[str], client: str | None = None) -> dict:
        current_client.set(client)

        async def one(p: Provider):
            if not p.configured:
                return [], None, self._not_configured(p.name)
            key = f"{keyword.strip().casefold()}|{limit}|{self.s.digikey_currency}"
            cached = await self.storage.search_get(p.name, key)
            if cached is not None:
                await self.storage.log(p.name, "search_cache", 1, "cache_hit")
                offers = [Offer.model_validate({**o, "from_cache": True}) for o in cached["offers"]]
                return offers, cached["total"], ProviderStatus(provider=p.name, found=bool(offers), from_cache=True,
                                                                latency_ms=0, quota_remaining_today=await self._remaining(p))
            t0 = time.perf_counter()
            try:
                offers, total = await p.keyword(keyword.strip(), limit)
            except Exception as e:  # noqa: BLE001
                return [], None, p.error_lookup(e, round((time.perf_counter() - t0) * 1000)).status
            await self.storage.search_put(p.name, key, {"offers": [o.model_dump(mode="json") for o in offers],
                                                        "total": total})
            return offers, total, p.status(found=bool(offers), latency_ms=round((time.perf_counter() - t0) * 1000))

        res = await asyncio.gather(*(one(self.providers[n]) for n in providers))
        return {"keyword": keyword, "results": [o for r in res for o in r[0]],
                "total_by_provider": {n: r[1] for n, r in zip(providers, res)},
                "provider_status": [r[2] for r in res]}

    async def digikey_info(self, kind: str, mpn: str, client: str | None = None) -> dict:
        """kind: specs|substitutes|related (DigiKey only). Cached like keyword searches."""
        current_client.set(client)
        p, mpn = self.providers["digikey"], mpn.strip()
        empty = {f: [] for f in INFO_FIELDS[kind]}
        if not p.configured:
            return {"mpn": mpn, **empty, "provider_status": [self._not_configured(p.name)]}
        key = f"info:{kind}|{mpn_strict(mpn)}|{self.s.digikey_currency}"
        if (cached := await self.storage.search_get(p.name, key)) is not None:
            await self.storage.log(p.name, f"{kind}_cache", 1, "cache_hit")
            return {"mpn": mpn, **cached, "provider_status": [ProviderStatus(
                provider=p.name, found=any(cached.values()), from_cache=True, latency_ms=0,
                quota_remaining_today=await self._remaining(p))]}
        t0 = time.perf_counter()
        try:
            data = await getattr(p, kind)(mpn)
        except Exception as e:  # noqa: BLE001 - an error is never "nothing found"
            return {"mpn": mpn, **empty,
                    "provider_status": [p.error_lookup(e, round((time.perf_counter() - t0) * 1000)).status]}
        payload = {f: [m.model_dump(mode="json") for m in v] for f, v in data.items()}
        await self.storage.search_put(p.name, key, payload)
        return {"mpn": mpn, **payload, "provider_status": [
            p.status(found=any(payload.values()), latency_ms=round((time.perf_counter() - t0) * 1000))]}

    async def _cached(self, p: Provider, key: str, fetch, fresh: bool = False) -> tuple[object, bool]:
        """(payload, from_cache) via search_cache (SEARCH_TTL_HOURS). Only successful fetches are stored, and one
        key is fetched once at a time: concurrent searches wait for the first instead of spending quota again."""
        lock = self._fetching.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                if not fresh and (hit := await self.storage.search_get(p.name, key)) is not None:
                    await self.storage.log(p.name, key.partition("|")[0] + "_cache", 1, "cache_hit")
                    return hit, True
                payload = await fetch()
                await self.storage.search_put(p.name, key, payload)
                return payload, False
        finally:
            if not lock.locked():
                self._fetching.pop(key, None)

    async def _category(self, p: Provider, category: str, fresh: bool) -> tuple[int, str]:
        """DigiKey leaf category id + name from a name (any case) or an id."""
        tree, _ = await self._cached(p, "categories|", p.categories, fresh)
        names = {i: n for i, n, _ in tree}
        c = category.strip()
        hits = [x for x in tree if str(x[0]) == c or (x[1] or "").casefold() == c.casefold()]
        if len(hits) > 1:
            raise SpecError(f"category {c!r} is ambiguous, pass its id: "
                            + ", ".join(f"{i} ({names.get(parent)} > {name})" for i, name, parent in hits))
        if not hits:
            close = difflib.get_close_matches(c, [n for n in names.values() if n], n=8, cutoff=0.5)
            raise SpecError(f"unknown DigiKey category {c!r}" + (f"; close: {' | '.join(close)}" if close else ""))
        cat_id, name, _ = hits[0]
        if kids := [f"{i} {n}" for i, n, parent in tree if parent == cat_id]:  # DigiKey filters specs per leaf only
            raise SpecError(f"{name!r} has sub-categories, search one of them: " + ", ".join(kids))
        return cat_id, name

    async def spec_search(self, category: str, specs: dict, *, rohs: bool = True, active_only: bool = True,
                          in_stock: bool = False, exclude_manufacturers: list[str] = (), max_results: int = 200,
                          fresh: bool = False, client: str | None = None) -> dict:
        """DigiKey parametric search (Mouser has no parametric API). Names/values DigiKey doesn't have raise
        SpecError (HTTP 422); DigiKey failures come back in provider_status, never as "no matches"."""
        current_client.set(client)
        p, t0 = self.providers["digikey"], time.perf_counter()
        out = {"category": None, "filters": None, "total": None, "truncated": False, "results": [], "warnings": []}
        if not p.configured:
            return {**out, "provider_status": [self._not_configured(p.name)]}
        try:
            cat_id, cat_name = await self._category(p, category, fresh)
            opts, _ = await self._cached(p, f"spec_options|{cat_id}", lambda: p.spec_options(cat_id), fresh)
            filters, chosen, warnings = p.spec_filters(
                cat_id, opts, specs, rohs=rohs, active_only=active_only, in_stock=in_stock,
                exclude_manufacturers=list(exclude_manufacturers))

            async def fetch():
                results, total = await p.spec_search(filters, max_results)
                return {"results": results, "total": total}
            key = json.dumps([filters, max_results, self.s.digikey_currency], sort_keys=True)
            found, hit = await self._cached(p, "spec_search|" + key, fetch, fresh)
        except SpecError:
            raise
        except Exception as e:  # noqa: BLE001 - an error is never "nothing matches"
            return {**out, "provider_status": [p.error_lookup(e, round((time.perf_counter() - t0) * 1000)).status]}
        results = [{**r, "from_cache": True} for r in found["results"]] if hit else found["results"]
        return {"category": {"id": cat_id, "name": cat_name}, "filters": chosen, "total": found["total"],
                "truncated": found["total"] > len(results), "results": results, "warnings": warnings,
                "provider_status": [ProviderStatus(
                    provider=p.name, found=bool(results), from_cache=hit, quota_remaining_today=await self._remaining(p),
                    latency_ms=0 if hit else round((time.perf_counter() - t0) * 1000))]}

    async def _cart_op(self, name: str, op) -> tuple[Cart | None, ProviderStatus]:
        p, t0 = self.providers[name], time.perf_counter()
        try:
            cart = await op(p)
        except Exception as e:  # noqa: BLE001
            return None, p.error_lookup(e, round((time.perf_counter() - t0) * 1000)).status
        return cart, p.status(found=bool(cart.lines), latency_ms=round((time.perf_counter() - t0) * 1000))

    async def cart(self, client: str | None = None) -> dict:
        current_client.set(client)
        res = await asyncio.gather(*(self._cart_op(n, lambda p: p.cart_view()) for n in CART_PROVIDERS))
        return {"carts": [c for c, _ in res if c], "provider_status": [s for _, s in res]}

    async def cart_add(self, items: list[CartItemIn], client: str | None = None) -> dict:
        current_client.set(client)
        groups = {n: [i for i in items if i.provider == n] for n in CART_PROVIDERS}
        groups = {n: g for n, g in groups.items() if g}
        res = await asyncio.gather(*(self._cart_op(n, lambda p, g=g: p.cart_add(g)) for n, g in groups.items()))
        return {"carts": [c for c, _ in res if c], "provider_status": [s for _, s in res],
                "note": "Items are in the cart/list only; nothing is ordered until a person checks out."}

    async def quota(self) -> dict:
        return {"day": utcnow().date().isoformat(),
                "providers": {n: await self.storage.quota(n, p.daily_limit) for n, p in self.providers.items()}}


def merge_status(statuses: list[ProviderStatus]) -> ProviderStatus:
    """One status per provider for a whole BOM: first error wins, else the last ok status."""
    first_error = next((s for s in statuses if not s.ok), None)
    lat = [s.latency_ms for s in statuses if s.latency_ms is not None]
    rem = [s.quota_remaining_today for s in statuses if s.quota_remaining_today is not None]
    return (first_error or statuses[-1]).model_copy(update={
        "found": any(s.found for s in statuses), "from_cache": all(s.from_cache for s in statuses),
        "latency_ms": max(lat) if lat else None, "quota_remaining_today": min(rem) if rem else None})
