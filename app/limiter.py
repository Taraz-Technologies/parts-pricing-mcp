import asyncio
import time
from collections import deque


class RateLimited(Exception):
    def __init__(self, wait_seconds: float):
        super().__init__(f"per-minute limit reached; would wait {wait_seconds:.0f}s")
        self.wait_seconds = wait_seconds


class MinuteLimiter:
    """Sliding 60 s window. acquire() waits for a free slot, or raises RateLimited if the wait > max_wait."""

    def __init__(self, per_minute: int, max_wait: float, clock=time.monotonic, sleep=asyncio.sleep):
        self.per_minute, self.max_wait, self.clock, self.sleep = per_minute, max_wait, clock, sleep
        self.calls: deque[float] = deque()
        self.lock = asyncio.Lock()

    def _trim(self, now: float) -> None:
        while self.calls and self.calls[0] <= now - 60:
            self.calls.popleft()

    async def acquire(self) -> None:
        # ponytail: one lock serializes waiters, so time queued for the lock isn't capped by max_wait.
        async with self.lock:
            now = self.clock()
            self._trim(now)
            if len(self.calls) >= self.per_minute:
                wait = self.calls[0] + 60 - now
                if wait > self.max_wait:
                    raise RateLimited(wait)
                await self.sleep(wait)
                self._trim(self.clock())
            self.calls.append(self.clock())
