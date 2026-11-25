import asyncio
import random
from datetime import timedelta

import pytest

from app.limiter import MinuteLimiter, RateLimited
from app.storage import QuotaExhausted, Storage


async def test_cache_ttl(storage, clock):
    await storage.cache_put("digikey", "LM358DR|USD", True, [{"x": 1}])
    assert (await storage.cache_get("digikey", ["LM358DR|USD"]))["LM358DR|USD"][1] == [{"x": 1}]
    clock["now"] += timedelta(hours=11, minutes=59)
    assert "LM358DR|USD" in await storage.cache_get("digikey", ["LM358DR|USD"])
    clock["now"] += timedelta(minutes=2)
    assert await storage.cache_get("digikey", ["LM358DR|USD"]) == {}


async def test_notfound_ttl_is_longer(storage, clock):
    await storage.cache_put("mouser", "NOPE|USD", False, [])
    clock["now"] += timedelta(hours=23)
    found, offers, _ = (await storage.cache_get("mouser", ["NOPE|USD"]))["NOPE|USD"]
    assert found is False and offers == []
    clock["now"] += timedelta(hours=2)
    assert await storage.cache_get("mouser", ["NOPE|USD"]) == {}


async def test_search_cache_ttl(storage, clock):
    await storage.search_put("digikey", "2n7002|10", {"r": 1})
    assert await storage.search_get("digikey", "2n7002|10") == {"r": 1}
    clock["now"] += timedelta(hours=7)
    assert await storage.search_get("digikey", "2n7002|10") is None


async def test_reserve_stops_at_limit_minus_reserve(tmp_path, clock):
    s = await Storage(str(tmp_path), quota_reserve=2).open()
    assert [await s.reserve("digikey", 5) for _ in range(3)] == [2, 1, 0]
    with pytest.raises(QuotaExhausted):
        await s.reserve("digikey", 5)
    q = await s.quota("digikey", 5)
    assert (q["used"], q["remaining"]) == (3, 0)
    # new UTC day resets
    clock["now"] += timedelta(days=1)
    assert await s.reserve("digikey", 5) == 2
    await s.close()


async def test_concurrent_requests_share_the_connection(storage):
    """Concurrent requests share one aiosqlite connection. Explicit COMMITs used to hit another request's open
    cursor: "cannot commit transaction - SQL statements in progress" (an intermittent HTTP 500)."""
    reserved = []

    async def request(i):  # interleaved like real requests (fixed seed); with the old commits 38 of 40 failed
        rnd = random.Random(i)
        for j in range(20):
            match rnd.choice("rclsgq "):
                case "r":
                    reserved.append(await storage.reserve("digikey", 10_000))
                case "c":
                    await storage.cache_put("digikey", f"P{i}-{j}|USD", True, [])
                case "l":
                    await storage.log("digikey", "productdetails", 1, "ok", 200, 5)
                case "s":
                    await storage.search_put("digikey", f"s{i}", {"j": j})
                case "g":
                    await storage.cache_get("digikey", [f"P{i}-{j}|USD"])
                case "q":
                    await storage.search_get("digikey", f"s{i}")
                case _:
                    await asyncio.sleep(0)
    await asyncio.gather(*(request(i) for i in range(40)))
    assert (await storage.quota("digikey", 10_000))["used"] == len(reserved) > 0


async def test_upstream_remaining_and_stats(storage, clock):
    await storage.set_upstream_remaining("digikey", 987)
    await storage.log("digikey", "productdetails", 1, "ok", 200, 120, 987)
    await storage.log("digikey", "productdetails", 1, "error:timeout", None, 20000)
    await storage.log("digikey", "cache", 3, "cache_hit")
    assert (await storage.quota("digikey", 1000))["upstream_remaining_last_seen"] == 987
    s = (await storage.stats(24))["providers"]["digikey"]
    assert (s["upstream_calls"], s["errors"], s["cache_hits"], s["cache_hit_rate"]) == (2, 1, 1, 0.6)
    assert s["latency_ms"] == {"p50": 120, "p95": 20000}


async def test_limiter_waits_then_raises():
    t = {"now": 0.0}
    slept = []

    async def sleep(s):
        slept.append(s)
        t["now"] += s

    lim = MinuteLimiter(2, max_wait=15, clock=lambda: t["now"], sleep=sleep)
    await lim.acquire()
    t["now"] = 50
    await lim.acquire()
    await lim.acquire()  # window full; oldest expires at 60 → waits 10 s
    assert slept == [10]
    with pytest.raises(RateLimited):  # next free slot ~50 s away > max_wait
        await lim.acquire()
