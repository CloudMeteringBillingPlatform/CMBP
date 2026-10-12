"""Pipeline failure tests (spec section 15): duplicates, crash recovery, poison messages, sampler."""

import asyncio
import os
import signal
import subprocess
import sys
from datetime import timedelta

from app import db
from app.config import settings
from app.jobs import sampler
from app.pipeline.events import METRICS
from app.redis_client import get_redis
from app.timeutil import utcnow, window_start
from tests.conftest import BACKEND, TEST_ENV
from tests.helpers import (
    hourly_totals,
    independent_sum,
    make_event,
    make_org,
    produce,
    run_worker_until_drained,
    stream_drained,
    wait_for,
    with_duplicates,
)

PIPELINE_METRICS = [m for m in METRICS if m != "storage.byte_hours"]


async def synthetic_events(count: int, tenants: list[dict], seed: int = 1):
    import random

    rng = random.Random(seed)
    now = utcnow()
    return [
        make_event(
            rng.choice(tenants),
            rng.choice(PIPELINE_METRICS),
            rng.randint(1, 5000),
            now - timedelta(minutes=rng.randint(0, 600)),
        )
        for _ in range(count)
    ]


async def test_duplicate_suppression(clean):
    """10,000 events with 20% duplicates: aggregates equal an independently computed sum."""
    tenants = [await make_org(f"dup-{i}") for i in range(3)]
    unique = await synthetic_events(10_000, tenants)
    stream = with_duplicates(unique, 0.20)
    assert len(stream) == 12_000

    await produce(stream)
    worker = await run_worker_until_drained()

    assert await hourly_totals() == independent_sum(stream)
    raw = await db.fetch_one("SELECT COUNT(*) AS n FROM raw_events")
    assert raw["n"] == 10_000
    assert worker.processed == 10_000
    assert worker.duplicates == 2_000

    # Re-sending everything again changes nothing.
    await produce(stream[:3000])
    await run_worker_until_drained()
    assert await hourly_totals() == independent_sum(stream)


async def test_crash_recovery(clean):
    """kill -9 the worker midway, restart: the pending list drains and totals are still exact."""
    tenants = [await make_org(f"crash-{i}") for i in range(2)]
    unique = await synthetic_events(10_000, tenants, seed=2)
    stream = with_duplicates(unique, 0.20, seed=3)
    await produce(stream)

    env = {**os.environ, **TEST_ENV, "CONSUMER_NAME": "crash-worker"}

    def start():
        return subprocess.Popen(
            [sys.executable, "-m", "app.worker_main"],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    proc = start()
    try:

        async def some_processed():
            row = await db.fetch_one("SELECT COUNT(*) AS n FROM raw_events")
            return row["n"] >= 2_000

        assert await wait_for(some_processed, timeout=60)
        os.kill(proc.pid, signal.SIGKILL)  # no cleanup, no acknowledgements
        proc.wait(timeout=10)

        processed_at_kill = (await db.fetch_one("SELECT COUNT(*) AS n FROM raw_events"))["n"]
        assert processed_at_kill < 10_000, "worker finished before it could be killed; raise the event count"

        proc = start()
        assert await wait_for(stream_drained, timeout=120)
        # A restart replays its own unacknowledged messages first; duplicates are rejected.
        assert await hourly_totals() == independent_sum(stream)
        assert (await db.fetch_one("SELECT COUNT(*) AS n FROM raw_events"))["n"] == 10_000
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=15)


async def test_crash_between_commit_and_ack(clean):
    """A message committed but never acknowledged is redelivered and adds nothing the second time."""
    tenant = await make_org("ack")
    event = make_event(tenant, "api.requests", 7, utcnow())
    await produce([event])

    r = get_redis()
    from app.pipeline.events import UsageEvent
    from app.pipeline.worker import EVENT_FIELD, Worker, apply_event

    worker = Worker(consumer_name="dies-before-ack")
    await worker.ensure_group()
    resp = await r.xreadgroup(settings.consumer_group, "dies-before-ack", {settings.stream_name: ">"}, count=1)
    msg_id, fields = resp[0][1][0]
    async with db.connection() as conn:
        await apply_event(conn, UsageEvent.from_json(fields[EVENT_FIELD]))
    # ...the process "dies" here: committed, not acknowledged.

    await asyncio.sleep(0.4)  # longer than CLAIM_MIN_IDLE_MS, so another worker claims it
    await run_worker_until_drained()
    totals = await hourly_totals()
    assert totals == {(tenant["org_id"], tenant["project_id"], "api.requests"): 7}


async def test_poison_message(clean):
    """A malformed event lands in dead_letters after 5 deliveries and the stream keeps flowing."""
    tenant = await make_org("poison")
    r = get_redis()
    await r.xadd(settings.stream_name, {"e": "{not json"})
    good = [make_event(tenant, "api.requests", 1, utcnow()) for _ in range(50)]
    await produce(good)

    from app.pipeline.worker import Worker

    worker = Worker(consumer_name="poison-worker")
    task = asyncio.create_task(worker.run())
    try:

        async def dead_lettered():
            row = await db.fetch_one("SELECT COUNT(*) AS n FROM dead_letters")
            return row["n"] == 1

        assert await wait_for(dead_lettered, timeout=30)
        assert await wait_for(stream_drained, timeout=30)
        late_good = [make_event(tenant, "api.requests", 1, utcnow()) for _ in range(10)]
        await produce(late_good)
        assert await wait_for(stream_drained, timeout=30)
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10)

    letter = await db.fetch_one("SELECT payload, error, delivery_count FROM dead_letters")
    assert letter["delivery_count"] == settings.max_deliveries
    assert "InvalidEvent" in letter["error"]
    assert letter["payload"]["raw"] == "{not json"
    totals = await hourly_totals()
    assert totals[(tenant["org_id"], tenant["project_id"], "api.requests")] == 60


async def test_sampler_idempotency(clean):
    """Running the storage sampler twice for one window counts exactly one event."""
    tenant = await make_org("sampler")
    await db.execute(
        """
        INSERT INTO stored_objects (org_id, project_id, name, size_bytes, path)
        VALUES (%s, %s, 'a', 3600000, '/dev/null'), (%s, %s, 'b', 1200000, '/dev/null')
        """,
        (tenant["org_id"], tenant["project_id"], tenant["org_id"], tenant["project_id"]),
    )
    window = window_start(utcnow() - timedelta(minutes=10), settings.sampler_window_seconds)
    first = await sampler.sample_window(window)
    second = await sampler.sample_window(window)
    assert [e.event_id for e in first] == [e.event_id for e in second]

    await run_worker_until_drained()
    raw = await db.fetch_one("SELECT COUNT(*) AS n FROM raw_events WHERE metric = 'storage.byte_hours'")
    assert raw["n"] == 1
    expected = 4_800_000 * settings.sampler_window_seconds // 3600
    totals = await hourly_totals()
    assert totals[(tenant["org_id"], tenant["project_id"], "storage.byte_hours")] == expected


async def test_trim_never_drops_unread_events(clean):
    """Trimming removes only acknowledged entries; unread entries survive any retention setting."""
    tenant = await make_org("trim")
    await produce([make_event(tenant, "api.requests", 1, utcnow()) for _ in range(20)])
    from app.pipeline.worker import Worker

    worker = Worker(consumer_name="trimmer")
    await worker.ensure_group()
    # Nothing read yet: even with zero retention, nothing may be trimmed.
    object.__setattr__(settings, "stream_retention_ms", 0)
    try:
        await worker.trim_once()
        assert await get_redis().xlen(settings.stream_name) == 20
        await run_worker_until_drained(worker=Worker(consumer_name="trimmer"))
        await worker.trim_once()
        assert await get_redis().xlen(settings.stream_name) == 0
    finally:
        object.__setattr__(settings, "stream_retention_ms", 3_600_000)
