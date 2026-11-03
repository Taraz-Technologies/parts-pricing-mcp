import asyncio
import logging
import time

from pydantic import ValidationError

from .models import Offer, PartQuery, PartResult, ProviderLookup, ProviderStatus
from .normalize import mpn_strict
from .pricing import choose_best, price_offer
from .providers.base import Provider
from .storage import Storage, current_client, utcnow

log = logging.getLogger(__name__)


class PricingService:
    def __init__(self, settings, storage: Storage, providers: dict[str, Provider]):
        self.s, self.storage, self.providers = settings, storage, providers

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
