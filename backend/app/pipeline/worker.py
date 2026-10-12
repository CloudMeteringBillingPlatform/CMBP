"""Stream consumer: at-least-once delivery from Redis Streams, exactly-once effect in Postgres.

For every message (spec section 7):
  1. BEGIN
  2. take a shared lock on the billing-period row containing event_time
  3. INSERT INTO raw_events ... ON CONFLICT DO NOTHING   (the idempotency guard)
  4. only if a row was inserted: add to usage_hourly (open period) or usage_adjustments (closed)
  5. COMMIT, then XACK

A crash after COMMIT but before XACK means redelivery; step 3 then conflicts and nothing is
added twice. A crash before COMMIT rolls everything back and the redelivery does the work once.
"""

import asyncio
import json
import logging
import socket
import time
from dataclasses import dataclass

import redis.asyncio as redis
from psycopg import AsyncConnection

from app import db
from app.config import settings
from app.pipeline.events import InvalidEvent, UsageEvent
from app.redis_client import get_redis
from app.timeutil import hour_start, iso, period_start

log = logging.getLogger("worker")

EVENT_FIELD = "e"


def live_channel(org_id: str) -> str:
    return f"live:{org_id}"


@dataclass
class Outcome:
    inserted: bool
    target: str  # "hourly", "adjustment" or "duplicate"
    seq: int | None = None
    hour_quantity: int | None = None


async def apply_event(conn: AsyncConnection, event: UsageEvent) -> Outcome:
    """Count one event exactly once, inside one transaction on `conn`."""
    p_start = period_start(event.event_time)
    h_start = hour_start(event.event_time)
    async with conn.transaction():
        # Billing periods are created lazily by the first event (or by the scheduler).
        await conn.execute(
            "INSERT INTO billing_periods (org_id, period_start) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (event.org_id, p_start),
        )
        # Shared lock: a period close (FOR UPDATE) waits for in-flight events, and events that
        # arrive during a close wait for it to finish and then see status = 'closed'.
        cur = await conn.execute(
            "SELECT status FROM billing_periods WHERE org_id = %s AND period_start = %s FOR SHARE",
            (event.org_id, p_start),
        )
        status = (await cur.fetchone())["status"]

        cur = await conn.execute(
            """
            INSERT INTO raw_events (org_id, event_id, project_id, metric, quantity, event_time, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (org_id, event_id) DO NOTHING
            """,
            (
                event.org_id,
                event.event_id,
                event.project_id,
                event.metric,
                event.quantity,
                event.event_time,
                event.source,
            ),
        )
        if cur.rowcount == 0:
            return Outcome(inserted=False, target="duplicate")

        if status == "open":
            cur = await conn.execute(
                """
                INSERT INTO usage_hourly (org_id, project_id, metric, hour_start, quantity)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (org_id, project_id, metric, hour_start)
                DO UPDATE SET quantity = usage_hourly.quantity + EXCLUDED.quantity
                RETURNING quantity
                """,
                (event.org_id, event.project_id, event.metric, h_start, event.quantity),
            )
            hour_quantity = (await cur.fetchone())["quantity"]
            # The live sequence advances in the same transaction as the aggregate, so a
            # dashboard snapshot and its sequence number are always consistent.
            cur = await conn.execute(
                """
                INSERT INTO live_sequences (org_id, seq) VALUES (%s, 1)
                ON CONFLICT (org_id) DO UPDATE SET seq = live_sequences.seq + 1
                RETURNING seq
                """,
                (event.org_id,),
            )
            seq = (await cur.fetchone())["seq"]
            return Outcome(inserted=True, target="hourly", seq=seq, hour_quantity=hour_quantity)

        await conn.execute(
            """
            INSERT INTO usage_adjustments
                (org_id, project_id, metric, hour_start, period_start, quantity, event_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (event.org_id, event.project_id, event.metric, h_start, p_start, event.quantity, event.event_id),
        )
        return Outcome(inserted=True, target="adjustment")


async def publish_change(r: redis.Redis, event: UsageEvent, outcome: Outcome) -> None:
    if outcome.target != "hourly":
        return
    message = {
        "type": "usage",
        "seq": outcome.seq,
        "project_id": event.project_id,
        "metric": event.metric,
        "hour_start": iso(hour_start(event.event_time)),
        "delta": event.quantity,
        "hour_quantity": outcome.hour_quantity,
    }
    try:
        await r.publish(live_channel(event.org_id), json.dumps(message))
    except redis.RedisError:
        # Live updates are best effort; the dashboard snapshot is the accurate record.
        log.warning("live publish failed for org %s", event.org_id)


class Worker:
    def __init__(self, consumer_name: str | None = None, concurrency: int = 4):
        self.r = get_redis()
        self.stream = settings.stream_name
        self.group = settings.consumer_group
        self.consumer = consumer_name or settings.consumer_name or socket.gethostname()
        self.concurrency = concurrency
        self.processed = 0
        self.duplicates = 0
        self.dead_lettered = 0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def ensure_group(self) -> None:
        try:
            # Start from 0 so events written before the first worker ever ran are not skipped.
            await self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    # ------------------------------------------------------------- message handling
    async def handle(self, msg_id: str, fields: dict, delivery_count: int | None = None) -> None:
        """Process one message; ack on success, dead-letter after max deliveries."""
        try:
            event = UsageEvent.from_json(fields.get(EVENT_FIELD))
            async with db.connection() as conn:
                outcome = await apply_event(conn, event)
        except Exception as exc:  # noqa: BLE001 - any failure is retried, then dead-lettered
            count = delivery_count if delivery_count is not None else await self.delivery_count(msg_id)
            if count >= settings.max_deliveries:
                await self.dead_letter(msg_id, fields, exc, count)
            else:
                level = logging.INFO if isinstance(exc, InvalidEvent) else logging.WARNING
                log.log(level, "message %s failed (delivery %s): %s", msg_id, count, exc)
            return
        await publish_change(self.r, event, outcome)
        await self.r.xack(self.stream, self.group, msg_id)
        if outcome.inserted:
            self.processed += 1
        else:
            self.duplicates += 1

    async def delivery_count(self, msg_id: str) -> int:
        entries = await self.r.xpending_range(self.stream, self.group, min=msg_id, max=msg_id, count=1)
        if not entries:
            return 1
        return int(entries[0]["times_delivered"])

    async def dead_letter(self, msg_id: str, fields: dict, exc: Exception, count: int) -> None:
        raw = fields.get(EVENT_FIELD)
        try:
            payload = json.loads(raw) if raw is not None else {"fields": fields}
            if not isinstance(payload, dict):
                payload = {"raw": raw}
        except (TypeError, json.JSONDecodeError):
            payload = {"raw": raw, "fields": fields}
        await db.execute(
            "INSERT INTO dead_letters (stream_id, payload, error, delivery_count) VALUES (%s, %s, %s, %s)",
            (msg_id, json.dumps(payload), f"{type(exc).__name__}: {exc}"[:2000], count),
        )
        await self.r.xack(self.stream, self.group, msg_id)
        self.dead_lettered += 1
        log.error("message %s dead-lettered after %s deliveries: %s", msg_id, count, exc)

    async def handle_batch(self, messages: list[tuple[str, dict]]) -> None:
        sem = asyncio.Semaphore(self.concurrency)

        async def one(msg_id: str, fields: dict) -> None:
            async with sem:
                await self.handle(msg_id, fields)

        await asyncio.gather(*(one(mid, f) for mid, f in messages))

    # ------------------------------------------------------------- loops
    async def drain_own_pending(self) -> None:
        """After a restart, finish the messages this consumer had read but not acknowledged."""
        last_id = "0"
        while not self._stop.is_set():
            resp = await self.r.xreadgroup(self.group, self.consumer, {self.stream: last_id}, count=100)
            messages = resp[0][1] if resp else []
            messages = [(mid, f) for mid, f in messages if f]
            if not messages:
                return
            await self.handle_batch(messages)
            last_id = messages[-1][0]

    async def consume(self) -> None:
        await self.ensure_group()
        await self.drain_own_pending()
        while not self._stop.is_set():
            try:
                resp = await self.r.xreadgroup(
                    self.group, self.consumer, {self.stream: ">"}, count=100, block=1000
                )
            except redis.ConnectionError:
                log.warning("redis unavailable; retrying")
                await asyncio.sleep(1)
                continue
            except redis.ResponseError as exc:
                if "NOGROUP" in str(exc):
                    await self.ensure_group()
                    continue
                raise
            if resp:
                await self.handle_batch(resp[0][1])

    async def recover(self) -> None:
        """Claim messages idle longer than the threshold (their consumer probably died)."""
        while not self._stop.is_set():
            try:
                await self.claim_idle()
            except redis.RedisError as exc:
                log.warning("recovery pass failed: %s", exc)
            await self._sleep(settings.recovery_interval_seconds)

    async def claim_idle(self) -> int:
        claimed_total = 0
        cursor = "0-0"
        while True:
            result = await self.r.xautoclaim(
                self.stream,
                self.group,
                self.consumer,
                min_idle_time=settings.claim_min_idle_ms,
                start_id=cursor,
                count=100,
            )
            cursor, messages = result[0], result[1]
            live = [(mid, f) for mid, f in messages if f]
            for mid, fields in live:
                count = await self.delivery_count(mid)
                await self.handle(mid, fields, delivery_count=count)
            claimed_total += len(live)
            if cursor in ("0-0", b"0-0") or not messages:
                return claimed_total

    async def trim(self) -> None:
        while not self._stop.is_set():
            await self._sleep(settings.trim_interval_seconds)
            try:
                await self.trim_once()
            except redis.RedisError as exc:
                log.warning("trim failed: %s", exc)

    async def trim_once(self) -> None:
        """Trim only entries that are acknowledged AND older than the retention window.

        Never trim by length: a lagging worker would lose unread events.
        """
        cutoff_ms = int(time.time() * 1000) - settings.stream_retention_ms
        min_id = f"{cutoff_ms}-0"
        summary = await self.r.xpending(self.stream, self.group)
        if summary and summary.get("pending"):
            min_id = _min_stream_id(min_id, summary["min"])
        for group in await self.r.xinfo_groups(self.stream):
            last = group.get("last-delivered-id")
            if last:
                # Entries after last-delivered-id have not been read yet.
                ms, seq = (int(x) for x in str(last).split("-"))
                min_id = _min_stream_id(min_id, f"{ms}-{seq + 1}")
        await self.r.xtrim(self.stream, minid=min_id, approximate=False)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        log.info("worker %s consuming %s as group %s", self.consumer, self.stream, self.group)
        await asyncio.gather(self.consume(), self.recover(), self.trim())


def _min_stream_id(a: str, b: str) -> str:
    def key(x: str) -> tuple[int, int]:
        ms, _, seq = str(x).partition("-")
        return int(ms), int(seq or 0)

    return a if key(a) <= key(b) else b
