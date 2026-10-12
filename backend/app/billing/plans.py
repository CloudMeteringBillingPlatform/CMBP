"""Which plan version applies to an organization, now or for a past billing period.

Rules (stated in the design write-up):
  * A plan switch takes effect immediately for limits (rate limit, storage cap, quotas).
  * A billing period is rated with the plan the organization was on at the END of the period,
    using that plan's newest version whose effective_from is on or before the period START.
    So a price published mid-month applies from the next month (customers are not repriced
    retroactively), and a plan switched mid-month bills that month on the new plan.
"""

from datetime import datetime

from psycopg import AsyncConnection

from app.timeutil import utcnow

DEFAULT_PLAN = "free"


async def _version(conn: AsyncConnection, plan_id: str, at: datetime, fallback_before: datetime | None = None) -> dict:
    cur = await conn.execute(
        """
        SELECT pv.plan_id, p.name AS plan_name, pv.version, pv.effective_from, pv.limits, pv.rates
        FROM plan_versions pv JOIN plans p ON p.id = pv.plan_id
        WHERE pv.plan_id = %s AND pv.effective_from <= %s
        ORDER BY pv.version DESC LIMIT 1
        """,
        (plan_id, at),
    )
    row = await cur.fetchone()
    if row is None and fallback_before is not None:
        cur = await conn.execute(
            """
            SELECT pv.plan_id, p.name AS plan_name, pv.version, pv.effective_from, pv.limits, pv.rates
            FROM plan_versions pv JOIN plans p ON p.id = pv.plan_id
            WHERE pv.plan_id = %s AND pv.effective_from < %s
            ORDER BY pv.version ASC LIMIT 1
            """,
            (plan_id, fallback_before),
        )
        row = await cur.fetchone()
    if row is None:
        raise LookupError(f"no published version of plan {plan_id!r} at {at}")
    return row


async def subscribed_plan_id(conn: AsyncConnection, org_id: str, before: datetime, inclusive: bool) -> str:
    op = "<=" if inclusive else "<"
    cur = await conn.execute(
        f"""
        SELECT plan_id FROM subscriptions
        WHERE org_id = %s AND effective_from {op} %s
        ORDER BY effective_from DESC LIMIT 1
        """,
        (org_id, before),
    )
    row = await cur.fetchone()
    if row is None:
        cur = await conn.execute(
            "SELECT plan_id FROM subscriptions WHERE org_id = %s ORDER BY effective_from ASC LIMIT 1",
            (org_id,),
        )
        row = await cur.fetchone()
    return row["plan_id"] if row else DEFAULT_PLAN


async def current_plan(conn: AsyncConnection, org_id: str, at: datetime | None = None) -> dict:
    at = at or utcnow()
    plan_id = await subscribed_plan_id(conn, org_id, at, inclusive=True)
    return await _version(conn, plan_id, at)


async def plan_for_period(conn: AsyncConnection, org_id: str, p_start: datetime, p_end: datetime) -> dict:
    plan_id = await subscribed_plan_id(conn, org_id, p_end, inclusive=False)
    return await _version(conn, plan_id, p_start, fallback_before=p_end)
