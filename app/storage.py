import contextvars
import json
import os
from datetime import datetime, timedelta, timezone

import aiosqlite

# Console name ("planner", "prc", "mcp") of the current request, set by the API.
current_client: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_client", default=None)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QuotaExhausted(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS part_cache(provider TEXT, cache_key TEXT, found INTEGER NOT NULL,
  payload TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(provider, cache_key));
CREATE TABLE IF NOT EXISTS search_cache(provider TEXT, cache_key TEXT, payload TEXT, fetched_at TEXT,
  PRIMARY KEY(provider, cache_key));
CREATE TABLE IF NOT EXISTS quota_usage(provider TEXT, day TEXT, used INTEGER NOT NULL DEFAULT 0,
  upstream_remaining INTEGER, PRIMARY KEY(provider, day));
CREATE TABLE IF NOT EXISTS request_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, client TEXT,
  provider TEXT, op TEXT, n_parts INTEGER, status TEXT, http_status INTEGER, latency_ms INTEGER,
  upstream_remaining INTEGER);
CREATE INDEX IF NOT EXISTS request_log_ts ON request_log(ts);
"""


class Storage:
    def __init__(self, data_dir: str, cache_ttl_hours: float = 12, notfound_ttl_hours: float = 24,
                 search_ttl_hours: float = 6, quota_reserve: int = 50):
        self.path = os.path.join(data_dir, "pricing.db")
        os.makedirs(data_dir, exist_ok=True)
        self.cache_ttl = timedelta(hours=cache_ttl_hours)
        self.notfound_ttl = timedelta(hours=notfound_ttl_hours)
        self.search_ttl = timedelta(hours=search_ttl_hours)
        self.quota_reserve = quota_reserve
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> "Storage":
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.executescript(SCHEMA)
        await self.db.commit()
        return self

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    # --- part cache: payload is a JSON list of Offer dicts; [] when not found. Errors are never cached.
    async def cache_get(self, provider: str, keys: list[str]) -> dict[str, tuple[bool, list, datetime]]:
        if not keys:
            return {}
        q = f"SELECT cache_key, found, payload, fetched_at FROM part_cache WHERE provider=? AND cache_key IN ({','.join('?' * len(keys))})"
        now, out = utcnow(), {}
        async with self.db.execute(q, (provider, *keys)) as cur:
            for key, found, payload, fetched_at in await cur.fetchall():
                ts = datetime.fromisoformat(fetched_at)
                if now - ts < (self.cache_ttl if found else self.notfound_ttl):
                    out[key] = (bool(found), json.loads(payload), ts)
        return out

    async def cache_put(self, provider: str, key: str, found: bool, offers: list[dict]) -> None:
        await self.db.execute("INSERT OR REPLACE INTO part_cache VALUES(?,?,?,?,?)",
                              (provider, key, int(found), json.dumps(offers), utcnow().isoformat()))
        await self.db.commit()

    async def search_get(self, provider: str, key: str) -> dict | None:
        async with self.db.execute("SELECT payload, fetched_at FROM search_cache WHERE provider=? AND cache_key=?",
                                   (provider, key)) as cur:
            row = await cur.fetchone()
        if row and utcnow() - datetime.fromisoformat(row[1]) < self.search_ttl:
            return json.loads(row[0])
        return None

    async def search_put(self, provider: str, key: str, payload: dict) -> None:
        await self.db.execute("INSERT OR REPLACE INTO search_cache VALUES(?,?,?,?)",
                              (provider, key, json.dumps(payload), utcnow().isoformat()))
        await self.db.commit()

    # --- quota: UTC day, reserve is atomic (single upsert statement)
    async def reserve(self, provider: str, daily_limit: int) -> int:
        """Count one upstream call. Returns calls remaining today; raises QuotaExhausted at limit-reserve."""
        cap = daily_limit - self.quota_reserve
        if cap < 1:
            raise QuotaExhausted(provider)
        async with self.db.execute(
            "INSERT INTO quota_usage(provider, day, used) VALUES(?,?,1) "
            "ON CONFLICT(provider, day) DO UPDATE SET used = used + 1 WHERE used + 1 <= ? RETURNING used",
            (provider, utcnow().date().isoformat(), cap),
        ) as cur:
            row = await cur.fetchone()
        await self.db.commit()
        if row is None:
            raise QuotaExhausted(provider)
        return cap - row[0]

    async def set_upstream_remaining(self, provider: str, remaining: int) -> None:
        await self.db.execute(
            "INSERT INTO quota_usage(provider, day, used, upstream_remaining) VALUES(?,?,0,?) "
            "ON CONFLICT(provider, day) DO UPDATE SET upstream_remaining=excluded.upstream_remaining",
            (provider, utcnow().date().isoformat(), remaining))
        await self.db.commit()

    async def quota(self, provider: str, daily_limit: int) -> dict:
        async with self.db.execute("SELECT used, upstream_remaining FROM quota_usage WHERE provider=? AND day=?",
                                   (provider, utcnow().date().isoformat())) as cur:
            used, upstream = (await cur.fetchone()) or (0, None)
        return {"used": used, "limit": daily_limit, "reserve": self.quota_reserve,
                "remaining": max(0, daily_limit - self.quota_reserve - used), "upstream_remaining_last_seen": upstream}

    # --- request log
    async def log(self, provider: str, op: str, n_parts: int, status: str, http_status: int | None = None,
                  latency_ms: int | None = None, upstream_remaining: int | None = None) -> None:
        await self.db.execute(
            "INSERT INTO request_log(ts, client, provider, op, n_parts, status, http_status, latency_ms, upstream_remaining)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (utcnow().isoformat(), current_client.get(), provider, op, n_parts, status, http_status, latency_ms,
             upstream_remaining))
        await self.db.commit()

    async def stats(self, hours: float = 24) -> dict:
        since = (utcnow() - timedelta(hours=hours)).isoformat()
        async with self.db.execute("SELECT provider, client, n_parts, status, latency_ms FROM request_log WHERE ts >= ?",
                                   (since,)) as cur:
            rows = await cur.fetchall()
        out = {}
        for provider, client, n_parts, status, latency in rows:
            s = out.setdefault(provider, {"upstream_calls": 0, "errors": 0, "cache_hits": 0, "_hit_parts": 0,
                                          "_parts": 0, "_lat": [], "calls_by_client": {}})
            s["_parts"] += n_parts or 0
            if status == "cache_hit":
                s["cache_hits"] += 1
                s["_hit_parts"] += n_parts or 0
            else:
                s["upstream_calls"] += 1
                s["errors"] += status.startswith("error:")
                if latency is not None:
                    s["_lat"].append(latency)
                s["calls_by_client"][client or "unknown"] = s["calls_by_client"].get(client or "unknown", 0) + 1
        for s in out.values():
            lat = sorted(s.pop("_lat"))
            hit, parts = s.pop("_hit_parts"), s.pop("_parts")
            s["cache_hit_rate"] = round(hit / parts, 3) if parts else None  # share of part lookups served from cache
            s["latency_ms"] = {"p50": _pct(lat, 50), "p95": _pct(lat, 95)}
        return {"hours": hours, "providers": out}


def _pct(xs: list[int], p: int) -> int | None:
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))] if xs else None
