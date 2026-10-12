"""The one shared dependency that derives the tenant for every console route.

The organization always comes from the session (looked up fresh, so role changes apply
at once), never from a request parameter. Handlers scope every query by principal.org_id.
"""

from dataclasses import dataclass

import redis.asyncio as redis
from fastapi import Depends, HTTPException, Request, WebSocket

from app import db
from app.config import settings
from app.ratelimit import hit
from app.redis_client import get_redis
from app.security import SESSION_COOKIE, read_session


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    org_id: str
    org_name: str
    role: str
    is_operator: bool

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


async def principal_from_token(token: str | None) -> Principal | None:
    if not token:
        return None
    session = await read_session(token)
    if not session:
        return None
    row = await db.fetch_one(
        """
        SELECT u.id, u.email, u.is_operator, m.org_id, m.role, o.name AS org_name
        FROM users u
        JOIN memberships m ON m.user_id = u.id
        JOIN orgs o ON o.id = m.org_id
        WHERE u.id = %s
        """,
        (session["user_id"],),
    )
    if row is None:
        return None
    return Principal(
        user_id=str(row["id"]),
        email=row["email"],
        org_id=str(row["org_id"]),
        org_name=row["org_name"],
        role=row["role"],
        is_operator=bool(row["is_operator"]),
    )


async def current_principal(request: Request) -> Principal:
    try:
        principal = await principal_from_token(request.cookies.get(SESSION_COOKIE))
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="session store unavailable") from exc
    if principal is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return principal


async def websocket_principal(websocket: WebSocket) -> Principal | None:
    return await principal_from_token(websocket.cookies.get(SESSION_COOKIE))


async def require_owner(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_owner:
        raise HTTPException(status_code=403, detail="owner role required")
    return principal


async def require_operator(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_operator:
        raise HTTPException(status_code=403, detail="operator access required")
    return principal


def client_ip(request: Request) -> str:
    # Behind Caddy the peer is the proxy, so trust X-Forwarded-For only when configured to.
    forwarded = request.headers.get("x-forwarded-for")
    if settings.trust_proxy and forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limited(bucket: str, limit: int | None = None):
    """Dependency factory: fixed-window limit per client IP for sensitive endpoints."""

    async def dependency(request: Request) -> None:
        try:
            decision = await hit(get_redis(), f"{bucket}:{client_ip(request)}", limit or settings.auth_rate_limit_per_minute)
        except redis.RedisError as exc:
            raise HTTPException(status_code=503, detail="rate limiter unavailable") from exc
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail="too many attempts, try again shortly",
                headers={"Retry-After": str(decision.retry_after)},
            )

    return dependency
