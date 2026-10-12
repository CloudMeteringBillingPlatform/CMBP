"""Team: members, invites (viewers by default) and role changes. Owner only."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import db
from app.console.deps import Principal, rate_limited, require_owner
from app.security import generate_token, sha256_hex

router = APIRouter(prefix="/api/team", tags=["team"])


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: str = Field(default="viewer", pattern=r"^(viewer|owner)$")


class RoleIn(BaseModel):
    role: str = Field(pattern=r"^(viewer|owner)$")


@router.get("")
async def team(p: Principal = Depends(require_owner)) -> dict:
    members = await db.fetch_all(
        """
        SELECT u.id AS user_id, u.email, m.role, m.created_at FROM memberships m
        JOIN users u ON u.id = m.user_id WHERE m.org_id = %s ORDER BY m.created_at
        """,
        (p.org_id,),
    )
    invites = await db.fetch_all(
        """
        SELECT id, email, role, created_at, accepted_at FROM invites
        WHERE org_id = %s ORDER BY created_at DESC
        """,
        (p.org_id,),
    )
    return {"members": members, "invites": invites}


@router.post("/invites", status_code=201, dependencies=[Depends(rate_limited("invite"))])
async def invite(body: InviteIn, p: Principal = Depends(require_owner)) -> dict:
    token = generate_token()
    row = await db.fetch_one(
        """
        INSERT INTO invites (org_id, email, role, token_hash, invited_by)
        VALUES (%s, %s, %s, %s, %s) RETURNING id, email, role, created_at
        """,
        (p.org_id, body.email.strip().lower(), body.role, sha256_hex(token), p.user_id),
    )
    # The token is shown once; the invitee signs up with it to join this organization.
    return {**row, "invite_token": token, "signup_path": f"/signup?invite={token}"}


@router.patch("/members/{user_id}")
async def change_role(user_id: str, body: RoleIn, p: Principal = Depends(require_owner)) -> dict:
    try:
        user_id = str(uuid.UUID(user_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="member not found") from exc
    async with db.connection() as conn:
        async with conn.transaction():
            # Lock the org's memberships so two owners cannot demote each other at once.
            cur = await conn.execute(
                "SELECT user_id, role FROM memberships WHERE org_id = %s FOR UPDATE", (p.org_id,)
            )
            members = {str(r["user_id"]): r["role"] for r in await cur.fetchall()}
            if user_id not in members:
                raise HTTPException(status_code=404, detail="member not found")
            owners = [u for u, role in members.items() if role == "owner"]
            if body.role != "owner" and owners == [user_id]:
                raise HTTPException(status_code=409, detail="an organization needs at least one owner")
            await conn.execute(
                "UPDATE memberships SET role = %s WHERE user_id = %s AND org_id = %s",
                (body.role, user_id, p.org_id),
            )
    return {"user_id": user_id, "role": body.role}
