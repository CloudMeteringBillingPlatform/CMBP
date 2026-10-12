"""Storage sampler: snapshot stored bytes every window and convert to byte-hours.

Each run samples the most recent COMPLETE window. Event ids are derived from
(metric, organization, project, window start), so a retried or duplicated run for the same
window produces the same ids and the worker's constraint rejects the repeats.
Approximate by design: usage between snapshots is assumed constant.
"""

from datetime import datetime, timedelta

from app import db
from app.config import settings
from app.pipeline.events import UsageEvent, sampler_event_id
from app.pipeline.producer import emit
from app.timeutil import utcnow, window_start

METRIC = "storage.byte_hours"


async def sample_window(window: datetime) -> list[UsageEvent]:
    seconds = settings.sampler_window_seconds
    rows = await db.fetch_all(
        """
        SELECT org_id, project_id, SUM(size_bytes) AS stored
        FROM stored_objects GROUP BY org_id, project_id HAVING SUM(size_bytes) > 0
        """
    )
    events = [
        UsageEvent(
            event_id=sampler_event_id(METRIC, str(r["org_id"]), str(r["project_id"]), window),
            org_id=str(r["org_id"]),
            project_id=str(r["project_id"]),
            metric=METRIC,
            quantity=int(r["stored"]) * seconds // 3600,
            event_time=window,
            source="sampler",
        )
        for r in rows
    ]
    events = [e for e in events if e.quantity > 0]
    await emit(events)
    return events


def last_complete_window(now: datetime | None = None) -> datetime:
    seconds = settings.sampler_window_seconds
    now = now or utcnow()
    return window_start(now - timedelta(seconds=seconds), seconds)


async def run_once(now: datetime | None = None) -> list[UsageEvent]:
    return await sample_window(last_complete_window(now))
