"""Per-client token bucket in Redis (D7). Rate limiting is a cost control: one
client looping against a multi-step agent could exhaust the model budget in
minutes. Shared across API replicas because the bucket lives in Redis."""
from __future__ import annotations

import time

import redis

LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])      -- tokens per second
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local d = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(d[1]) or burst
local ts = tonumber(d[2]) or now
tokens = math.min(burst, tokens + (now - ts) * rate)
local allowed = 0
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  wait = (1 - tokens) / rate
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(burst / rate) + 60)
return {allowed, tostring(wait)}
"""


class RateLimiter:
    def __init__(self, r: redis.Redis, per_minute: int, burst: int):
        self.r, self.rate, self.burst = r, per_minute / 60.0, burst
        self.script = r.register_script(LUA)

    def allow(self, client_id: str) -> tuple[bool, float]:
        if self.rate <= 0:
            return True, 0.0
        try:
            allowed, wait = self.script(keys=[f"rl:{client_id}"], args=[self.rate, self.burst, time.time()])
            return bool(int(allowed)), float(wait)
        except redis.RedisError:
            return True, 0.0  # fail open on the limiter, the queue bound still protects the system
