"""Sign up, log in, log out, current user (sessions in Redis, HTTP-only SameSite cookie)."""

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import db
from app.config import settings
from fastapi.concurrency import run_in_threadpool

from app.console.deps import Principal, current_principal, principal_from_token, rate_limited
from app.security import (
    SESSION_COOKIE,
    create_session,
    delete_session,
    hash_password,
    sha256_hex,
    verify_password,
)
from app.timeutil import period_start, utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])
me_router = APIRouter(prefix="/api", tags=["auth"])

# Verified against when the email is unknown, so both paths cost one argon2 verification.
_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")


class SignupIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=200)
    org_name: str | None = Field(default=None, max_length=100)
    invite_token: str | None = Field(default=None, max_length=200)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def principal_json(p: Principal) -> dict:
    return {
        "user": {"id": p.user_id, "email": p.email, "is_operator": p.is_operator},
        "org": {"id": p.org_id, "name": p.org_name},
        "role": p.role,
    }


@router.post("/signup", status_code=201, dependencies=[Depends(rate_limited("signup"))])
async def signup(body: SignupIn, response: Response) -> dict:
    email = body.email.strip().lower()
    password_hash = await run_in_threadpool(hash_password, body.password)
    is_operator = email in settings.operator_emails
    now = utcnow()
    try:
        async with db.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "INSERT INTO users (email, password_hash, is_operator) VALUES (%s, %s, %s) RETURNING id",
                    (email, password_hash, is_operator),
                )
                user_id = str((await cur.fetchone())["id"])
                if body.invite_token:
                    cur = await conn.execute(
                        """
                        UPDATE invites SET accepted_at = now()
                        WHERE token_hash = %s AND accepted_at IS NULL AND lower(email) = %s
                        RETURNING org_id, role
                        """,
                        (sha256_hex(body.invite_token), email),
                    )
                    invite = await cur.fetchone()
                    if invite is None:
                        raise HTTPException(status_code=400, detail="invite is invalid, used, or for another email")
                    await conn.execute(
                        "INSERT INTO memberships (user_id, org_id, role) VALUES (%s, %s, %s)",
                        (user_id, invite["org_id"], invite["role"]),
                    )
                else:
                    org_name = (body.org_name or "").strip() or f"{email.split('@')[0]}'s organization"
                    cur = await conn.execute("INSERT INTO orgs (name) VALUES (%s) RETURNING id", (org_name,))
                    org_id = str((await cur.fetchone())["id"])
                    await conn.execute(
                        "INSERT INTO memberships (user_id, org_id, role) VALUES (%s, %s, 'owner')",
                        (user_id, org_id),
                    )
                    await conn.execute(
                        "INSERT INTO subscriptions (org_id, effective_from, plan_id) VALUES (%s, %s, 'free')",
                        (org_id, now),
                    )
                    await conn.execute(
                        "INSERT INTO billing_periods (org_id, period_start) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (org_id, period_start(now)),
                    )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="an account with this email already exists") from exc

    token = await create_session(user_id)
    _set_cookie(response, token)
    principal = await principal_from_token(token)
    return principal_json(principal)


@router.post("/login", dependencies=[Depends(rate_limited("login"))])
async def login(body: LoginIn, response: Response) -> dict:
    email = body.email.strip().lower()
    row = await db.fetch_one("SELECT id, password_hash FROM users WHERE lower(email) = %s", (email,))
    if row is None:
        await run_in_threadpool(verify_password, _DUMMY_HASH, body.password)
        raise HTTPException(status_code=401, detail="wrong email or password")
    if not await run_in_threadpool(verify_password, row["password_hash"], body.password):
        raise HTTPException(status_code=401, detail="wrong email or password")
    token = await create_session(str(row["id"]))
    _set_cookie(response, token)
    principal = await principal_from_token(token)
    if principal is None:
        raise HTTPException(status_code=403, detail="account has no organization")
    return principal_json(principal)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await delete_session(token)
    response.status_code = 204
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@me_router.get("/me")
async def me(principal: Principal = Depends(current_principal)) -> dict:
    return principal_json(principal)
