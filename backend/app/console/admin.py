"""Operator tools: dead letters (inspect, retry), manual period close, system status."""

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app.billing.periods import close_period
from app.config import settings
from app.console.deps import Principal, require_operator
from app.pipeline.worker import EVENT_FIELD
from app.redis_client import get_redis

router = APIRouter(prefix="/api/admin", tags=["admin"])


class CloseIn(BaseModel):
    org_id: str
    period_start: datetime


@router.get("/dead-letters")
async def dead_letters(p: Principal = Depends(require_operator)) -> dict:
    rows = await db.fetch_all(
        """
        SELECT id, stream_id, payload, error, delivery_count, created_at, retried_at
        FROM dead_letters ORDER BY id DESC LIMIT 200
        """
    )
    return {"dead_letters": rows}


@router.post("/dead-letters/{dl_id}/retry")
async def retry_dead_letter(dl_id: int, p: Principal = Depends(require_operator)) -> dict:
    """Put the payload back on the stream. Idempotency makes a retry of a counted event harmless."""
    row = await db.fetch_one("SELECT payload FROM dead_letters WHERE id = %s", (dl_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="dead letter not found")
    payload = row["payload"]
    if isinstance(payload, dict) and "raw" in payload and set(payload) <= {"raw", "fields"}:
        raw = payload["raw"] or ""  # it was not valid JSON; requeue it exactly as received
    else:
        raw = json.dumps(payload)
    stream_id = await get_redis().xadd(settings.stream_name, {EVENT_FIELD: raw})
    await db.execute("UPDATE dead_letters SET retried_at = now() WHERE id = %s", (dl_id,))
    return {"id": dl_id, "requeued_as": stream_id}


@router.post("/periods/close")
async def close(body: CloseIn, p: Principal = Depends(require_operator)) -> dict:
    """Close a period on demand (the scheduler does this automatically at month end)."""
    try:
        org_id = str(uuid.UUID(body.org_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bad org id") from exc
    if await db.fetch_one("SELECT 1 FROM orgs WHERE id = %s", (org_id,)) is None:
        raise HTTPException(status_code=404, detail="org not found")
    invoice_id = await close_period(org_id, body.period_start)
    return {"invoice_id": invoice_id}


@router.get("/status")
async def status(p: Principal = Depends(require_operator)) -> dict:
    r = get_redis()
    stream = {"length": 0, "pending": 0, "groups": []}
    try:
        stream["length"] = await r.xlen(settings.stream_name)
        for group in await r.xinfo_groups(settings.stream_name):
            stream["groups"].append(
                {
                    "name": group.get("name"),
                    "consumers": group.get("consumers"),
                    "pending": group.get("pending"),
                    "lag": group.get("lag"),
                    "last_delivered_id": group.get("last-delivered-id"),
                }
            )
            stream["pending"] += int(group.get("pending") or 0)
    except Exception as exc:  # noqa: BLE001
        stream["error"] = str(exc)
    counts = await db.fetch_one(
        """
        SELECT (SELECT COUNT(*) FROM raw_events) AS raw_events,
               (SELECT COUNT(*) FROM dead_letters WHERE retried_at IS NULL) AS dead_letters,
               (SELECT COUNT(*) FROM usage_adjustments WHERE applied_invoice_id IS NULL) AS unapplied_adjustments,
               (SELECT COUNT(*) FROM orgs) AS orgs,
               (SELECT COUNT(*) FROM invoices) AS invoices
        """
    )
    orgs = await db.fetch_all("SELECT id, name, created_at FROM orgs ORDER BY created_at")
    return {"stream": stream, "counts": counts, "orgs": orgs}
