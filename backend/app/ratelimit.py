"""Fixed-window rate limiting in Redis (INCR plus EXPIRE, run atomically as one Lua script)."""

import time
from dataclasses import dataclass

import redis.asyncio as redis

# INCR and EXPIRE in one script: if they were two commands, a crash between them could
# leave a counter with no TTL that never resets.
_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {count, redis.call('TTL', KEYS[1])}
"""


@dataclass
class Decision:
    allowed: bool
    count: int
    limit: int
    retry_after: int


async def hit(r: redis.Redis, bucket: str, limit: int, window_seconds: int = 60) -> Decision:
    """Count one hit for `bucket` in the current window. Raises RedisError if Redis is down."""
    window = int(time.time()) // window_seconds
    key = f"rl:{bucket}:{window}"
    count, ttl = await r.eval(_LUA, 1, key, window_seconds)
    count = int(count)
    retry_after = int(ttl) if int(ttl) > 0 else window_seconds
    return Decision(allowed=count <= limit, count=count, limit=limit, retry_after=retry_after)
