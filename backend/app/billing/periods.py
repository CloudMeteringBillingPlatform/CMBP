"""Closing a billing period and issuing its immutable invoice (spec section 9).

close_period runs as ONE transaction:
  1. lock the organization's billing_periods row FOR UPDATE — this waits for in-flight worker
     transactions, which hold FOR SHARE on the same row;
  2. set status = 'closed';
  3. rate every usage_hourly row of the period with the plan version that applied;
  4. sweep unapplied usage_adjustments from earlier periods into adjustment lines;
  5. commit. The invoice is now immutable (enforced by a trigger, not just by convention).

Every event is therefore either counted before the lock (in the invoice) or waits for the
close and then lands in usage_adjustments (on the next invoice) — never both, never neither.
"""

import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime

from psycopg import AsyncConnection

from app import db
from app.billing.plans import plan_for_period
from app.billing.rating import Segment, charge, rate_totals, round_paise
from app.pipeline.worker import live_channel
from app.redis_client import get_redis
from app.timeutil import iso, next_period_start, period_start, utcnow

log = logging.getLogger("billing")


async def _period_totals(conn: AsyncConnection, org_id: str, p_start: datetime, p_end: datetime) -> dict[str, int]:
    cur = await conn.execute(
        """
        SELECT metric, SUM(quantity)::bigint AS total FROM usage_hourly
        WHERE org_id = %s AND hour_start >= %s AND hour_start < %s
        GROUP BY metric
        """,
        (org_id, p_start, p_end),
    )
    return {r["metric"]: int(r["total"]) for r in await cur.fetchall()}


async def _adjustment_segments(conn: AsyncConnection, org_id: str, adjustments: list[dict]) -> list[tuple[Segment, datetime]]:
    """Price late usage per (period, metric) at the margin of what that period already billed."""
    grouped: dict[tuple[datetime, str], int] = defaultdict(int)
    for adj in adjustments:
        grouped[(adj["period_start"], adj["metric"])] += int(adj["quantity"])

    result: list[tuple[Segment, datetime]] = []
    plans: dict[datetime, dict] = {}
    for (adj_period, metric), late_qty in sorted(grouped.items()):
        adj_end = next_period_start(adj_period)
        if adj_period not in plans:
            plans[adj_period] = await plan_for_period(conn, org_id, adj_period, adj_end)
        card = plans[adj_period]["rates"].get(metric)
        if not card:
            continue
        cur = await conn.execute(
            """
            SELECT
              (SELECT COALESCE(SUM(quantity), 0) FROM usage_hourly
                WHERE org_id = %(org)s AND metric = %(metric)s
                  AND hour_start >= %(start)s AND hour_start < %(end)s) AS billed,
              (SELECT COALESCE(SUM(quantity), 0) FROM usage_adjustments
                WHERE org_id = %(org)s AND metric = %(metric)s AND period_start = %(start)s
                  AND applied_invoice_id IS NOT NULL) AS adjusted
            """,
            {"org": org_id, "metric": metric, "start": adj_period, "end": adj_end},
        )
        row = await cur.fetchone()
        already = int(row["billed"]) + int(row["adjusted"])
        for seg in charge(metric, card, already, already + late_qty):
            seg.description = f"Late usage for {adj_period:%B %Y} — {seg.description}"
            result.append((seg, adj_period))
    return result


async def close_period(org_id: str, p_start: datetime) -> str | None:
    """Close one period and issue its invoice. Returns the invoice id (existing if already closed)."""
    p_start = period_start(p_start)
    p_end = next_period_start(p_start)
    invoice_id = str(uuid.uuid4())
    async with db.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO billing_periods (org_id, period_start) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (org_id, p_start),
            )
            cur = await conn.execute(
                "SELECT status FROM billing_periods WHERE org_id = %s AND period_start = %s FOR UPDATE",
                (org_id, p_start),
            )
            if (await cur.fetchone())["status"] == "closed":
                cur = await conn.execute(
                    "SELECT id FROM invoices WHERE org_id = %s AND period_start = %s", (org_id, p_start)
                )
                row = await cur.fetchone()
                return str(row["id"]) if row else None

            await conn.execute(
                "UPDATE billing_periods SET status = 'closed', closed_at = now() WHERE org_id = %s AND period_start = %s",
                (org_id, p_start),
            )
            plan = await plan_for_period(conn, org_id, p_start, p_end)
            totals = await _period_totals(conn, org_id, p_start, p_end)
            usage_lines = [(seg, p_start) for seg in rate_totals(plan["rates"], totals)]

            # Lock unapplied adjustments from earlier periods so concurrent closes cannot both apply them.
            cur = await conn.execute(
                """
                SELECT id, metric, quantity, period_start FROM usage_adjustments
                WHERE org_id = %s AND applied_invoice_id IS NULL AND period_start < %s
                ORDER BY id FOR UPDATE
                """,
                (org_id, p_start),
            )
            adjustments = await cur.fetchall()
            adjustment_lines = await _adjustment_segments(conn, org_id, adjustments)

            lines = [("usage", seg, ups) for seg, ups in usage_lines] + [
                ("adjustment", seg, ups) for seg, ups in adjustment_lines
            ]
            total = sum(round_paise(seg.amount) for _, seg, _ in lines)
            await conn.execute(
                """
                INSERT INTO invoices (id, org_id, period_start, period_end, plan_id, plan_version, total_paise)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (invoice_id, org_id, p_start, p_end, plan["plan_id"], plan["version"], total),
            )
            async with conn.cursor() as c:
                await c.executemany(
                    """
                    INSERT INTO invoice_lines
                      (invoice_id, kind, metric, description, quantity, unit_label, unit_size,
                       rate_paise, amount_paise, usage_period_start)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            invoice_id,
                            kind,
                            seg.metric,
                            seg.description,
                            seg.quantity,
                            seg.unit_label,
                            seg.unit_size,
                            seg.rate_paise,
                            round_paise(seg.amount),
                            ups,
                        )
                        for kind, seg, ups in lines
                    ],
                )
            if adjustments:
                await conn.execute(
                    "UPDATE usage_adjustments SET applied_invoice_id = %s WHERE id = ANY(%s)",
                    (invoice_id, [a["id"] for a in adjustments]),
                )

    log.info("closed %s for org %s: invoice %s total %s paise", iso(p_start), org_id, invoice_id, total)
    try:
        await get_redis().publish(
            live_channel(org_id),
            json.dumps({"type": "invoice", "invoice_id": invoice_id, "period_start": iso(p_start), "total_paise": total}),
        )
    except Exception:  # noqa: BLE001 - notification only
        pass
    return invoice_id


async def ensure_current_periods(now: datetime | None = None) -> None:
    current = period_start(now or utcnow())
    await db.execute(
        """
        INSERT INTO billing_periods (org_id, period_start)
        SELECT id, %s FROM orgs ON CONFLICT DO NOTHING
        """,
        (current,),
    )


async def close_ended_periods(now: datetime | None = None) -> list[str]:
    """Close every open period that has ended, oldest first."""
    current = period_start(now or utcnow())
    rows = await db.fetch_all(
        """
        SELECT org_id, period_start FROM billing_periods
        WHERE status = 'open' AND period_start < %s
        ORDER BY period_start, org_id
        """,
        (current,),
    )
    invoices = []
    for row in rows:
        invoice_id = await close_period(str(row["org_id"]), row["period_start"])
        if invoice_id:
            invoices.append(invoice_id)
    return invoices
