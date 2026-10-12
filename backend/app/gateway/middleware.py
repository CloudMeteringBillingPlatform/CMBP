"""The gateway: single entry point to the services and the source of request-level metering.

Pipeline for every gateway request (spec section 6):
  1. authenticate the caller and resolve the tenant (organization, project)
  2. apply the fixed-window rate limit from the plan (429 + Retry-After when over)
  3. forward to the storage or image module, counting request and response bytes while streaming
  4. when the response completes, emit one usage event per metric to the stream

Clients authenticate with `Authorization: Bearer <api key>`; the key's hash resolves via a
30-second Redis cache, Postgres on a miss. The web console is just another API client: its
Files and Image tool pages send a key the user pastes in.

If Redis (or Postgres, for a key lookup) is unreachable in steps 1 or 2 the gateway fails closed
with 503: serving requests that cannot be metered or limited would lose billable usage.

It is pure ASGI middleware so bodies are counted as they stream, never buffered.
"""

import json
import logging
import uuid
from dataclasses import dataclass

import psycopg
import redis.asyncio as redis
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.gateway.keys import resolve_key
from app.pipeline.events import UsageEvent, request_event_id
from app.pipeline.producer import emit
from app.ratelimit import hit
from app.redis_client import get_redis
from app.security import sha256_hex
from app.timeutil import utcnow

log = logging.getLogger("gateway")

GATEWAY_PREFIX = "/v1"
INTERNAL_HEADERS = (b"x-internal-secret", b"x-tenant-org", b"x-tenant-project", b"x-request-id")


class Refusal(Exception):
    """A response the gateway sends itself, before anything reaches a service."""

    def __init__(self, status: int, detail: str, headers: list[tuple[bytes, bytes]] | None = None):
        self.status = status
        self.detail = detail
        self.headers = headers or []


@dataclass(frozen=True)
class Caller:
    org_id: str
    project_id: str
    bucket: str  # rate-limit bucket: one per API key
    rate_limit_per_minute: int
    service_path: str  # the /v1/... path being called


async def _send_json(send: Send, status: int, detail: str, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            text = value.decode("latin-1").strip()
            if text[:7].lower() == "bearer ":
                token = text[7:].strip()
                return token or None
    return None


# ---------------------------------------------------------------- authentication
async def caller_from_api_key(scope: Scope, r: redis.Redis) -> Caller:
    key = _bearer(scope)
    if key is None:
        raise Refusal(401, "missing API key", [(b"www-authenticate", b"Bearer")])
    record = await resolve_key(r, sha256_hex(key))
    if record is None:
        raise Refusal(401, "unknown or revoked API key", [(b"www-authenticate", b"Bearer")])
    return Caller(
        org_id=record.org_id,
        project_id=record.project_id,
        bucket=f"key:{record.key_id}",
        rate_limit_per_minute=record.rate_limit_per_minute,
        service_path=scope["path"],
    )


class GatewayMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == GATEWAY_PREFIX or path.startswith(GATEWAY_PREFIX + "/")):
            await self.app(scope, receive, send)
            return

        # Steps 1 and 2: who is calling, and are they within their rate limit.
        r = get_redis()
        try:
            caller = await caller_from_api_key(scope, r)
            decision = await hit(r, caller.bucket, caller.rate_limit_per_minute)
        except Refusal as refusal:
            await _send_json(send, refusal.status, refusal.detail, refusal.headers)
            return
        except (redis.RedisError, OSError, psycopg.Error) as exc:
            log.error("metering unavailable: %s", exc)
            await _send_json(send, 503, "metering unavailable", [(b"retry-after", b"5")])
            return
        if not decision.allowed:
            await _send_json(
                send,
                429,
                "rate limit exceeded",
                [
                    (b"retry-after", str(decision.retry_after).encode()),
                    (b"x-ratelimit-limit", str(decision.limit).encode()),
                ],
            )
            return

        await self.forward(scope, receive, send, caller, decision)

    async def forward(self, scope: Scope, receive: Receive, send: Send, caller: Caller, decision) -> None:
        """Steps 3 and 4: forward with trusted tenant headers, count bytes, meter on completion."""
        request_id = str(uuid.uuid4())
        headers = [
            (n, v)
            for n, v in scope.get("headers", [])
            if n not in INTERNAL_HEADERS and n not in (b"authorization", b"cookie")
        ]
        headers += [
            (b"x-internal-secret", settings.internal_secret.encode()),
            (b"x-tenant-org", caller.org_id.encode()),
            (b"x-tenant-project", caller.project_id.encode()),
            (b"x-request-id", request_id.encode()),
        ]
        inner_scope = dict(scope, headers=headers)

        counts = {"in": 0, "out": 0, "complete": False}

        async def counting_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                counts["in"] += len(message.get("body", b""))
            return message

        async def counting_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", request_id.encode()),
                    (b"x-ratelimit-limit", str(decision.limit).encode()),
                    (b"x-ratelimit-remaining", str(max(0, decision.limit - decision.count)).encode()),
                ]
            elif message["type"] == "http.response.body":
                counts["out"] += len(message.get("body", b""))
                if not message.get("more_body", False):
                    counts["complete"] = True
            await send(message)

        await self.app(inner_scope, counting_receive, counting_send)

        if counts["complete"]:
            await emit(self.events(caller, request_id, counts))

    @staticmethod
    def events(caller: Caller, request_id: str, counts: dict) -> list[UsageEvent]:
        now = utcnow()

        def ev(metric: str, quantity: int) -> UsageEvent:
            return UsageEvent(
                event_id=request_event_id(request_id, metric),
                org_id=caller.org_id,
                project_id=caller.project_id,
                metric=metric,
                quantity=quantity,
                event_time=now,
                source="gateway",
            )

        events = [ev("api.requests", 1)]
        if caller.service_path.startswith(GATEWAY_PREFIX + "/storage"):
            if counts["in"]:
                events.append(ev("storage.bytes_in", counts["in"]))
            if counts["out"]:
                events.append(ev("storage.bytes_out", counts["out"]))
        return events
