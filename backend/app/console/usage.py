"""Dashboard snapshot, hourly usage, invoices and alerts (read-only, any role)."""

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from app import db
from app.billing.plans import current_plan
from app.billing.projection import projection
from app.console.deps import Principal, current_principal
from app.pipeline.events import METRICS, METRIC_UNITS
from app.timeutil import as_utc, hour_start, next_period_start, period_start, utcnow

router = APIRouter(prefix="/api", tags=["usage"])


@router.get("/dashboard")
async def dashboard(p: Principal = Depends(current_principal)) -> dict:
    """A consistent snapshot plus its sequence number.

    One REPEATABLE READ transaction reads the sequence and the aggregates from the same
    snapshot. The worker advances the sequence in the same transaction as each aggregate
    change, so every change numbered <= seq is in this snapshot and every change numbered
    > seq is not. The client applies only live messages with a higher number.
    """
    now = utcnow()
    p_start = period_start(now)
    p_end = next_period_start(p_start)
    trend_start = hour_start(now) - timedelta(hours=47)
    async with db.connection() as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur = await conn.execute("SELECT seq FROM live_sequences WHERE org_id = %s", (p.org_id,))
        row = await cur.fetchone()
        seq = int(row["seq"]) if row else 0

        cur = await conn.execute(
            """
            SELECT metric, SUM(quantity)::bigint AS total FROM usage_hourly
            WHERE org_id = %s AND hour_start >= %s AND hour_start < %s GROUP BY metric
            """,
            (p.org_id, p_start, p_end),
        )
        totals = {r["metric"]: int(r["total"]) for r in await cur.fetchall()}

        cur = await conn.execute(
            """
            SELECT hour_start, metric, SUM(quantity)::bigint AS quantity FROM usage_hourly
            WHERE org_id = %s AND hour_start >= %s GROUP BY hour_start, metric ORDER BY hour_start
            """,
            (p.org_id, trend_start),
        )
        trend = await cur.fetchall()

        plan = await current_plan(conn, p.org_id, now)
        proj = await projection(conn, p.org_id)

        cur = await conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0)::bigint AS stored FROM stored_objects WHERE org_id = %s",
            (p.org_id,),
        )
        stored = int((await cur.fetchone())["stored"])

        cur = await conn.execute(
            """
            SELECT id, metric, threshold, usage, allowance, period_start, fired_at FROM alerts
            WHERE org_id = %s ORDER BY fired_at DESC LIMIT 10
            """,
            (p.org_id,),
        )
        alerts = await cur.fetchall()

        cur = await conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(quantity), 0)::bigint AS quantity FROM usage_adjustments
            WHERE org_id = %s AND applied_invoice_id IS NULL
            """,
            (p.org_id,),
        )
        pending_adjustments = await cur.fetchone()

    quotas = plan["limits"].get("quotas", {})
    return {
        "seq": seq,
        "as_of": now,
        "period": {"start": p_start, "end": p_end},
        "plan": {"id": plan["plan_id"], "name": plan["plan_name"], "version": plan["version"], "limits": plan["limits"]},
        "metrics": [
            {
                "metric": m,
                "unit": METRIC_UNITS[m],
                "total": totals.get(m, 0),
                "allowance": int(quotas.get(m, 0)),
                "percent": round(100 * totals.get(m, 0) / quotas[m], 1) if quotas.get(m) else None,
            }
            for m in METRICS
        ],
        "trend": trend,
        "projection": proj,
        "storage": {"stored_bytes": stored, "cap_bytes": int(plan["limits"]["storage_bytes_cap"])},
        "alerts": alerts,
        "pending_adjustments": pending_adjustments,
    }


@router.get("/usage")
async def usage(
    p: Principal = Depends(current_principal),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    metric: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
) -> dict:
    end = as_utc(end) if end else utcnow()
    start = as_utc(start) if start else end - timedelta(hours=48)
    if end <= start or end - start > timedelta(days=93):
        raise HTTPException(status_code=400, detail="range must be positive and at most 93 days")
    if metric is not None and metric not in METRICS:
        raise HTTPException(status_code=400, detail="unknown metric")
    clauses = ["org_id = %(org)s", "hour_start >= %(start)s", "hour_start < %(end)s"]
    params: dict = {"org": p.org_id, "start": hour_start(start), "end": end}
    if metric:
        clauses.append("metric = %(metric)s")
        params["metric"] = metric
    if project_id:
        try:
            params["project"] = str(uuid.UUID(project_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="bad project id") from exc
        clauses.append("project_id = %(project)s")
    rows = await db.fetch_all(
        f"""
        SELECT hour_start, project_id, metric, quantity FROM usage_hourly
        WHERE {' AND '.join(clauses)} ORDER BY hour_start, metric
        """,
        params,
    )
    return {"start": start, "end": end, "rows": rows}


@router.get("/invoices")
async def list_invoices(p: Principal = Depends(current_principal)) -> dict:
    rows = await db.fetch_all(
        """
        SELECT i.id, i.period_start, i.period_end, i.plan_id, i.plan_version, i.issued_at, i.total_paise,
               COUNT(l.id) FILTER (WHERE l.kind = 'adjustment') AS adjustment_lines
        FROM invoices i LEFT JOIN invoice_lines l ON l.invoice_id = i.id
        WHERE i.org_id = %s GROUP BY i.id ORDER BY i.period_start DESC
        """,
        (p.org_id,),
    )
    return {"invoices": rows}


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, p: Principal = Depends(current_principal)) -> dict:
    try:
        invoice_id = str(uuid.UUID(invoice_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="invoice not found") from exc
    invoice = await db.fetch_one(
        """
        SELECT id, period_start, period_end, plan_id, plan_version, issued_at, total_paise
        FROM invoices WHERE id = %s AND org_id = %s
        """,
        (invoice_id, p.org_id),
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    lines = await db.fetch_all(
        """
        SELECT id, kind, metric, description, quantity, unit_label, unit_size, rate_paise,
               amount_paise, usage_period_start
        FROM invoice_lines WHERE invoice_id = %s ORDER BY kind DESC, id
        """,
        (invoice_id,),
    )
    return {**invoice, "lines": lines}


@router.get("/alerts")
async def list_alerts(p: Principal = Depends(current_principal)) -> dict:
    rows = await db.fetch_all(
        """
        SELECT id, metric, threshold, usage, allowance, period_start, fired_at FROM alerts
        WHERE org_id = %s ORDER BY fired_at DESC LIMIT 500
        """,
        (p.org_id,),
    )
    return {"alerts": rows}
