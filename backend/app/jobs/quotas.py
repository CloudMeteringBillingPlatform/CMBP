"""Soft quotas: every minute, compare open-period usage with the plan's allowance per metric.

Crossing 80% or 100% inserts an alert with ON CONFLICT DO NOTHING, so each threshold fires
once per metric per period, and a new alert is pushed to connected dashboards. Soft quotas
lag the pipeline by up to a minute: the aggregates are the accurate record, alerts are an
early warning, not a guarantee. (Hard limits are enforced synchronously elsewhere.)
"""

import json
import logging

from app import db
from app.billing.plans import current_plan
from app.pipeline.worker import live_channel
from app.redis_client import get_redis
from app.timeutil import iso, next_period_start, period_start, utcnow

log = logging.getLogger("quotas")

THRESHOLDS = (80, 100)


async def check_quotas() -> list[dict]:
    now = utcnow()
    p_start = period_start(now)
    p_end = next_period_start(p_start)
    fired: list[dict] = []
    async with db.connection() as conn:
        cur = await conn.execute(
            """
            SELECT org_id, metric, SUM(quantity)::bigint AS used FROM usage_hourly
            WHERE hour_start >= %s AND hour_start < %s GROUP BY org_id, metric
            """,
            (p_start, p_end),
        )
        usage: dict[str, dict[str, int]] = {}
        for row in await cur.fetchall():
            usage.setdefault(str(row["org_id"]), {})[row["metric"]] = int(row["used"])

        for org_id, metrics in usage.items():
            plan = await current_plan(conn, org_id, now)
            quotas = plan["limits"].get("quotas", {})
            for metric, used in metrics.items():
                allowance = int(quotas.get(metric, 0))
                if allowance <= 0:
                    continue
                for threshold in THRESHOLDS:
                    if used * 100 < allowance * threshold:
                        continue
                    cur = await conn.execute(
                        """
                        INSERT INTO alerts (org_id, metric, period_start, threshold, usage, allowance)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (org_id, metric, period_start, threshold) DO NOTHING
                        RETURNING id, fired_at
                        """,
                        (org_id, metric, p_start, threshold, used, allowance),
                    )
                    row = await cur.fetchone()
                    if row:
                        fired.append(
                            {
                                "type": "alert",
                                "org_id": org_id,
                                "id": row["id"],
                                "metric": metric,
                                "threshold": threshold,
                                "usage": used,
                                "allowance": allowance,
                                "period_start": iso(p_start),
                                "fired_at": iso(row["fired_at"]),
                            }
                        )
        await conn.commit()

    r = get_redis()
    for alert in fired:
        log.info("alert: org %s %s at %s%%", alert["org_id"], alert["metric"], alert["threshold"])
        try:
            await r.publish(live_channel(alert["org_id"]), json.dumps(alert))
        except Exception:  # noqa: BLE001 - notification only
            pass
    return fired
