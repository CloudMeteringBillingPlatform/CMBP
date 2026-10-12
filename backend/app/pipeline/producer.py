"""Emitting usage events to the stream, with a local spool when Redis is unreachable.

If an emit fails after the request was already served, the event is appended to a spool
file and a background task drains it into the stream later. Spooled events arrive late
and take the late-event path for a real reason. Event ids are fixed before spooling, so a
partially drained spool that is drained again only produces duplicates the worker rejects.
"""

import asyncio
import logging
import os
from pathlib import Path

import redis.asyncio as redis

from app.config import settings
from app.pipeline.events import UsageEvent
from app.pipeline.worker import EVENT_FIELD
from app.redis_client import get_redis

log = logging.getLogger("producer")

_spool_lock = asyncio.Lock()


async def xadd_events(r: redis.Redis, events: list[UsageEvent]) -> None:
    async with r.pipeline(transaction=False) as pipe:
        for event in events:
            pipe.xadd(settings.stream_name, {EVENT_FIELD: event.to_json()})
        await pipe.execute()


async def emit(events: list[UsageEvent]) -> bool:
    """Send events to the stream; spool them on failure. Returns True if sent directly."""
    if not events:
        return True
    try:
        await xadd_events(get_redis(), events)
        return True
    except (redis.RedisError, OSError) as exc:
        log.warning("emit failed (%s); spooling %d events", exc, len(events))
        await spool(events)
        return False


async def spool(events: list[UsageEvent]) -> None:
    path = Path(settings.spool_path)
    async with _spool_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for event in events:
                fh.write(event.to_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())


async def drain_spool_once() -> int:
    """Move spooled events into the stream. Returns the number drained."""
    path = Path(settings.spool_path)
    async with _spool_lock:
        if not path.exists() or path.stat().st_size == 0:
            return 0
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        r = get_redis()
        try:
            async with r.pipeline(transaction=False) as pipe:
                for line in lines:
                    pipe.xadd(settings.stream_name, {EVENT_FIELD: line})
                await pipe.execute()
        except (redis.RedisError, OSError):
            return 0
        path.write_text("", encoding="utf-8")
        log.info("drained %d spooled events", len(lines))
        return len(lines)


async def spool_drainer(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await drain_spool_once()
        except Exception:  # noqa: BLE001
            log.exception("spool drain failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.spool_drain_interval_seconds)
        except asyncio.TimeoutError:
            pass
