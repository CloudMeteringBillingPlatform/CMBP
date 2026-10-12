"""Projected bill for the open period: usage so far plus the trailing 24-hour run rate."""

from datetime import timedelta

from psycopg import AsyncConnection

from app.billing.plans import current_plan
from app.billing.rating import cost_paise
from app.timeutil import hours_between, next_period_start, period_start, utcnow


async def period_usage(conn: AsyncConnection, org_id: str, start, end) -> dict[str, int]:
    cur = await conn.execute(
        """
        SELECT metric, SUM(quantity)::bigint AS total FROM usage_hourly
        WHERE org_id = %s AND hour_start >= %s AND hour_start < %s GROUP BY metric
        """,
        (org_id, start, end),
    )
    return {r["metric"]: int(r["total"]) for r in await cur.fetchall()}


async def projection(conn: AsyncConnection, org_id: str) -> dict:
    now = utcnow()
    p_start = period_start(now)
    p_end = next_period_start(p_start)
    plan = await current_plan(conn, org_id, now)
    so_far = await period_usage(conn, org_id, p_start, p_end)

    window_start = max(p_start, now - timedelta(hours=24))
    trailing = await period_usage(conn, org_id, window_start, p_end)
    window_hours = max(1.0, hours_between(window_start, now))
    remaining_hours = hours_between(now, p_end)
    projected = {
        metric: int(so_far.get(metric, 0) + trailing.get(metric, 0) / window_hours * remaining_hours)
        for metric in set(so_far) | set(trailing)
    }
    return {
        "period_start": p_start,
        "period_end": p_end,
        "plan": {"id": plan["plan_id"], "name": plan["plan_name"], "version": plan["version"]},
        "usage_so_far": so_far,
        "projected_usage": projected,
        "cost_so_far_paise": cost_paise(plan["rates"], so_far),
        "projected_paise": cost_paise(plan["rates"], projected),
        "is_estimate": True,
        "basis": "usage so far plus the trailing 24-hour run rate over the remaining hours",
    }
