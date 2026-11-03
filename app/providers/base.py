import asyncio
import logging
import time
from abc import ABC, abstractmethod

import httpx

from ..config import Settings
from ..limiter import MinuteLimiter, RateLimited
from ..models import Offer, ProviderLookup, ProviderStatus
from ..storage import QuotaExhausted, Storage

asyncio_sleep = asyncio.sleep  # module attr so tests can skip real waits
log = logging.getLogger(__name__)


class ProviderError(Exception):
    def __init__(self, code: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


_KEY_FIELDS = ("manufacturer", "stock", "moq", "price_breaks", "currency")


def flag_missing(offer: Offer) -> Offer:
    """Never invent data: a missing key field stays null and is named in warnings."""
    offer.warnings += [f"missing:{f}" for f in _KEY_FIELDS if getattr(offer, f) in (None, [])]
    return offer


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


class Provider(ABC):
    """One distributor. Adding a distributor = one subclass in its own file + one line in api.build_providers."""
    name: str
    daily_limit: int

    def __init__(self, settings: Settings, storage: Storage, http: httpx.AsyncClient, per_minute: int):
        self.s, self.storage, self.http = settings, storage, http
        self.limiter = MinuteLimiter(per_minute, settings.max_wait_seconds)
        self.quota_remaining: int | None = None

    @property
    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    async def get_many(self, mpns: list[str]) -> dict[str, ProviderLookup]:
        """Keyed by the MPN strings passed in."""

    @abstractmethod
    async def keyword(self, q: str, limit: int) -> tuple[list[Offer], int | None]:
        """(offers, total results upstream)."""

    def status(self, *, found: bool = False, error: ProviderError | None = None, latency_ms: int | None = None,
               from_cache: bool = False) -> ProviderStatus:
        return ProviderStatus(provider=self.name, ok=error is None, found=found, from_cache=from_cache,
                              error_code=error.code if error else None, message=error.message if error else None,
                              latency_ms=latency_ms, quota_remaining_today=self.quota_remaining)

    def error_lookup(self, e: Exception, latency_ms: int | None = None) -> ProviderLookup:
        if not isinstance(e, ProviderError):
            log.error("%s: unexpected %s while parsing response", self.name, type(e).__name__)
            e = ProviderError("upstream_error", f"{self.name}: unexpected response ({type(e).__name__})")
        return ProviderLookup(found=False, status=self.status(error=e, latency_ms=latency_ms))

    async def upstream(self, op: str, method: str, url: str, *, path: str, n_parts: int = 1, check=None,
                       **kw) -> httpx.Response:
        """One logical upstream call. Every HTTP attempt takes 1 limiter slot + 1 quota and is logged.
        Retries once: 5xx/timeout after 1 s; 429 if Retry-After <= MAX_WAIT_SECONDS.
        Returns any other response; `check(resp)` may raise ProviderError.
        `path` is used in messages instead of the URL (the Mouser key is a query param)."""
        for attempt in (1, 2):
            last = attempt == 2
            try:
                await self.limiter.acquire()
            except RateLimited as e:
                raise ProviderError("rate_limited", f"{self.name}: {e}")
            try:
                self.quota_remaining = await self.storage.reserve(self.name, self.daily_limit)
            except QuotaExhausted:
                self.quota_remaining = 0
                raise ProviderError("quota_exhausted", f"{self.name}: daily call budget used (resets 00:00 UTC)")
            t0 = time.perf_counter()
            try:
                r = await self.http.request(method, url, timeout=self.s.request_timeout_seconds, **kw)
            except httpx.TimeoutException:
                await self._log(op, n_parts, "error:timeout", None, t0)
                if last:
                    raise ProviderError("timeout", f"{self.name} {path}: timed out")
                await asyncio_sleep(1)
                continue
            except httpx.HTTPError as e:
                await self._log(op, n_parts, "error:upstream_error", None, t0)
                if last:
                    raise ProviderError("upstream_error", f"{self.name} {path}: {type(e).__name__}")
                await asyncio_sleep(1)
                continue
            remaining = _int(r.headers.get("X-RateLimit-Remaining"))
            if remaining is not None:
                await self.storage.set_upstream_remaining(self.name, remaining)
            if r.status_code == 429:
                wait = _int(r.headers.get("Retry-After"))
                await self._log(op, n_parts, "error:rate_limited", r, t0, remaining)
                if last or wait is None or wait > self.s.max_wait_seconds:
                    hint = f", retry after {wait}s" if wait is not None else ""
                    raise ProviderError("rate_limited", f"{self.name} {path}: HTTP 429{hint}", 429)
                await asyncio_sleep(wait)
                continue
            if r.status_code >= 500:
                await self._log(op, n_parts, "error:upstream_error", r, t0, remaining)
                if last:
                    raise ProviderError("upstream_error", f"{self.name} {path}: HTTP {r.status_code}", r.status_code)
                await asyncio_sleep(1)
                continue
            try:
                if check:
                    check(r)
            except ProviderError as e:
                await self._log(op, n_parts, f"error:{e.code}", r, t0, remaining)
                raise
            sc = r.status_code
            status = ("ok" if sc < 400 else "not_found" if sc == 404 else
                      "error:auth_failed" if sc in (401, 403) else "error:upstream_error")
            await self._log(op, n_parts, status, r, t0, remaining)
            return r

    async def _log(self, op, n_parts, status, r, t0, remaining=None):
        await self.storage.log(self.name, op, n_parts, status, r.status_code if r is not None else None,
                               round((time.perf_counter() - t0) * 1000), remaining)
