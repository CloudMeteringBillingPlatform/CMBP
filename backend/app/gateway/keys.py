"""Resolving an API key hash to (organization, project, key) through a short-lived cache.

The cache TTL is what bounds how long a revoked key keeps working; revocation also deletes
the cache entry, so in practice it stops at once and at worst within the TTL.
"""

import json
from dataclasses import dataclass

import redis.asyncio as redis

from app import db
from app.billing.plans import current_plan
from app.config import settings


@dataclass(frozen=True)
class KeyRecord:
    key_id: str
    org_id: str
    project_id: str
    rate_limit_per_minute: int


def cache_key(key_hash: str) -> str:
    return f"apikey:{key_hash}"


async def resolve_key(r: redis.Redis, key_hash: str) -> KeyRecord | None:
    """Return the key's tenant, or None if unknown or revoked. Raises if Redis or Postgres fail."""
    cached = await r.get(cache_key(key_hash))
    if cached is not None:
        data = json.loads(cached)
        return KeyRecord(**data) if data else None

    record = await _lookup(key_hash)
    payload = json.dumps(record.__dict__ if record else None)
    await r.set(cache_key(key_hash), payload, ex=settings.key_cache_ttl_seconds)
    return record


async def _lookup(key_hash: str) -> KeyRecord | None:
    async with db.connection() as conn:
        cur = await conn.execute(
            """
            SELECT k.id, k.org_id, k.project_id, k.revoked_at
            FROM api_keys k WHERE k.key_hash = %s
            """,
            (key_hash,),
        )
        row = await cur.fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        plan = await current_plan(conn, str(row["org_id"]))
    return KeyRecord(
        key_id=str(row["id"]),
        org_id=str(row["org_id"]),
        project_id=str(row["project_id"]),
        rate_limit_per_minute=int(plan["limits"]["rate_limit_per_minute"]),
    )


async def invalidate(r: redis.Redis, key_hashes: list[str]) -> None:
    if key_hashes:
        await r.delete(*(cache_key(h) for h in key_hashes))
