"""Shared helpers: tenants, synthetic events, and running the worker until the stream drains."""

import asyncio
import random
import uuid
from datetime import datetime, timezone

from app import db
from app.config import settings
from app.pipeline.events import UsageEvent
from app.pipeline.producer import xadd_events
from app.pipeline.worker import Worker
from app.redis_client import get_redis
from app.timeutil import period_start


async def make_org(name: str = "org", plan: str = "free", created: datetime | None = None) -> dict:
    org = await db.fetch_one("INSERT INTO orgs (name) VALUES (%s) RETURNING id", (name,))
    org_id = str(org["id"])
    effective = created or datetime(2020, 1, 1, tzinfo=timezone.utc)
    await db.execute(
        "INSERT INTO subscriptions (org_id, effective_from, plan_id) VALUES (%s, %s, %s)",
        (org_id, effective, plan),
    )
    project = await db.fetch_one(
        "INSERT INTO projects (org_id, name) VALUES (%s, %s) RETURNING id", (org_id, f"{name}-project")
    )
    return {"org_id": org_id, "project_id": str(project["id"])}


def make_event(tenant: dict, metric: str, quantity: int, when: datetime, event_id: str | None = None) -> UsageEvent:
    return UsageEvent(
        event_id=event_id or str(uuid.uuid4()),
        org_id=tenant["org_id"],
        project_id=tenant["project_id"],
        metric=metric,
        quantity=quantity,
        event_time=when,
        source="test",
    )


def with_duplicates(events: list[UsageEvent], fraction: float, seed: int = 7) -> list[UsageEvent]:
    """Append exact re-sends of a random `fraction` of events and shuffle."""
    rng = random.Random(seed)
    dupes = [rng.choice(events) for _ in range(int(len(events) * fraction))]
    stream = events + dupes
    rng.shuffle(stream)
    return stream


async def produce(events: list[UsageEvent], batch: int = 1000) -> None:
    r = get_redis()
    for i in range(0, len(events), batch):
        await xadd_events(r, events[i : i + batch])


async def stream_drained() -> bool:
    r = get_redis()
    try:
        groups = await r.xinfo_groups(settings.stream_name)
    except Exception:  # noqa: BLE001 - stream not created yet
        return False
    for group in groups:
        if group["name"] == settings.consumer_group:
            return int(group.get("pending") or 0) == 0 and int(group.get("lag") or 0) == 0
    return False


async def wait_for(predicate, timeout: float = 60.0, interval: float = 0.2) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def run_worker_until_drained(timeout: float = 120.0, worker: Worker | None = None) -> Worker:
    worker = worker or Worker(consumer_name=f"test-{uuid.uuid4().hex[:6]}")
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.sleep(0.3)
        assert await wait_for(stream_drained, timeout=timeout), "worker did not drain the stream in time"
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10)
    return worker


async def hourly_totals(org_id: str | None = None, p_start: datetime | None = None) -> dict:
    clauses, params = [], []
    if org_id:
        clauses.append("org_id = %s")
        params.append(org_id)
    if p_start:
        clauses.append("hour_start >= %s AND hour_start < (%s::timestamptz + interval '1 month')")
        params += [p_start, p_start]
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = await db.fetch_all(
        f"SELECT org_id, project_id, metric, SUM(quantity)::bigint AS q FROM usage_hourly {where} "
        "GROUP BY org_id, project_id, metric",
        tuple(params),
    )
    return {(str(r["org_id"]), str(r["project_id"]), r["metric"]): int(r["q"]) for r in rows}


def independent_sum(events: list[UsageEvent]) -> dict:
    """The expected totals, computed without the pipeline: each distinct event counted once."""
    unique = {(e.org_id, e.event_id): e for e in events}
    totals: dict = {}
    for e in unique.values():
        key = (e.org_id, e.project_id, e.metric)
        totals[key] = totals.get(key, 0) + e.quantity
    return totals


def month(year: int, month_: int) -> datetime:
    return period_start(datetime(year, month_, 1, tzinfo=timezone.utc))
