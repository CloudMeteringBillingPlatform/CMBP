"""Plan versions and the organization's subscription."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app.billing.plans import current_plan
from app.console.deps import Principal, current_principal, require_owner
from app.gateway.keys import invalidate
from app.redis_client import get_redis
from app.timeutil import utcnow

router = APIRouter(prefix="/api", tags=["plans"])


class SubscriptionIn(BaseModel):
    plan_id: str


@router.get("/plans")
async def list_plans(p: Principal = Depends(current_principal)) -> dict:
    rows = await db.fetch_all(
        """
        SELECT pv.plan_id, pl.name, pv.version, pv.effective_from, pv.limits, pv.rates,
               pv.effective_from <= now() AS in_effect
        FROM plan_versions pv JOIN plans pl ON pl.id = pv.plan_id
        ORDER BY pv.plan_id, pv.version
        """
    )
    return {"plan_versions": rows}


@router.get("/subscription")
async def get_subscription(p: Principal = Depends(current_principal)) -> dict:
    async with db.connection() as conn:
        plan = await current_plan(conn, p.org_id)
        cur = await conn.execute(
            "SELECT plan_id, effective_from FROM subscriptions WHERE org_id = %s ORDER BY effective_from DESC",
            (p.org_id,),
        )
        history = await cur.fetchall()
    return {"current": plan, "history": history}


@router.put("/subscription")
async def switch_plan(body: SubscriptionIn, p: Principal = Depends(require_owner)) -> dict:
    exists = await db.fetch_one("SELECT 1 FROM plans WHERE id = %s", (body.plan_id,))
    if exists is None:
        raise HTTPException(status_code=404, detail="plan not found")
    await db.execute(
        "INSERT INTO subscriptions (org_id, effective_from, plan_id) VALUES (%s, %s, %s)",
        (p.org_id, utcnow(), body.plan_id),
    )
    # Cached key records carry the plan's rate limit; drop them so the new limit applies now.
    keys = await db.fetch_all("SELECT key_hash FROM api_keys WHERE org_id = %s", (p.org_id,))
    try:
        await invalidate(get_redis(), [k["key_hash"] for k in keys])
    except Exception:  # noqa: BLE001
        pass
    async with db.connection() as conn:
        return {"current": await current_plan(conn, p.org_id)}
