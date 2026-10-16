"""Circuit breaker whose state lives in Redis, so every gateway replica (and
the dashboard) sees the same state.

closed    -> calls flow; failures are recorded in a sliding window
open      -> after `threshold` failures inside `window_s`; calls are refused
             immediately for `open_s` seconds (fail fast, protect the budget)
half_open -> after `open_s`; exactly one probe call is allowed through. Success
             closes the breaker, failure re-opens it.
"""
from __future__ import annotations

import time

import redis

CLOSED, HALF_OPEN, OPEN = "closed", "half_open", "open"


class CircuitBreaker:
    def __init__(self, r: redis.Redis, name: str, threshold: int, window_s: int, open_s: int):
        self.r, self.name = r, name
        self.threshold, self.window_s, self.open_s = threshold, window_s, open_s
        self.k_fail = f"cb:{name}:failures"
        self.k_open = f"cb:{name}:open_until"
        self.k_probe = f"cb:{name}:probe"

    def state(self) -> str:
        open_until = float(self.r.get(self.k_open) or 0)
        if open_until == 0:
            return CLOSED
        return OPEN if time.time() < open_until else HALF_OPEN

    def allow(self) -> bool:
        st = self.state()
        if st == CLOSED:
            return True
        if st == OPEN:
            return False
        # half-open: one probe at a time
        return bool(self.r.set(self.k_probe, "1", nx=True, ex=max(5, self.open_s)))

    def record_success(self) -> None:
        if self.state() != CLOSED:
            pipe = self.r.pipeline()
            pipe.delete(self.k_open, self.k_probe, self.k_fail)
            pipe.execute()

    def record_failure(self) -> None:
        now = time.time()
        st = self.state()
        if st == HALF_OPEN:
            self._open(now)
            return
        pipe = self.r.pipeline()
        pipe.zadd(self.k_fail, {f"{now}": now})
        pipe.zremrangebyscore(self.k_fail, 0, now - self.window_s)
        pipe.zcard(self.k_fail)
        pipe.expire(self.k_fail, self.window_s * 2)
        _, _, count, _ = pipe.execute()
        if count >= self.threshold:
            self._open(now)

    def _open(self, now: float) -> None:
        pipe = self.r.pipeline()
        pipe.set(self.k_open, now + self.open_s)
        pipe.delete(self.k_probe, self.k_fail)
        pipe.execute()

    def reset(self) -> None:
        self.r.delete(self.k_open, self.k_probe, self.k_fail)

    def snapshot(self) -> dict:
        open_until = float(self.r.get(self.k_open) or 0)
        return {
            "name": self.name,
            "state": self.state(),
            "recent_failures": int(self.r.zcount(self.k_fail, time.time() - self.window_s, "+inf")),
            "open_until": open_until or None,
            "threshold": self.threshold,
            "window_s": self.window_s,
            "open_s": self.open_s,
        }
