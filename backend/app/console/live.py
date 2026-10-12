"""Live updates: Redis pub/sub fanned out to this instance's WebSockets.

The worker publishes a small message per aggregate change to `live:<org_id>`. Each app
instance runs ONE pattern subscription and forwards each message only to the sockets of that
organization's open dashboards, so updates reach users whichever instance they are on.
The socket authenticates with the session cookie on connect and only ever receives its own
organization's messages.
"""

import asyncio
import json
import logging
from collections import defaultdict

import redis.asyncio as redis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.console.deps import websocket_principal
from app.redis_client import get_redis

log = logging.getLogger("live")

router = APIRouter(tags=["live"])


class LiveHub:
    def __init__(self) -> None:
        self.sockets: dict[str, set[WebSocket]] = defaultdict(set)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def add(self, org_id: str, ws: WebSocket) -> None:
        self.sockets[org_id].add(ws)

    def remove(self, org_id: str, ws: WebSocket) -> None:
        self.sockets[org_id].discard(ws)
        if not self.sockets[org_id]:
            self.sockets.pop(org_id, None)

    async def _run(self) -> None:
        while True:
            pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.psubscribe("live:*")
                async for message in pubsub.listen():
                    if message.get("type") != "pmessage":
                        continue
                    org_id = str(message["channel"]).split(":", 1)[1]
                    await self._fanout(org_id, message["data"])
            except asyncio.CancelledError:
                await pubsub.aclose()
                raise
            except (redis.RedisError, OSError) as exc:
                log.warning("live subscription lost (%s); reconnecting", exc)
                await asyncio.sleep(1)
            finally:
                try:
                    await pubsub.aclose()
                except Exception:  # noqa: BLE001
                    pass

    async def _fanout(self, org_id: str, data: str) -> None:
        for ws in list(self.sockets.get(org_id, ())):
            try:
                await ws.send_text(data)
            except Exception:  # noqa: BLE001 - a dead socket is dropped, not fatal
                self.remove(org_id, ws)


hub = LiveHub()


@router.websocket("/ws")
async def live_socket(websocket: WebSocket) -> None:
    principal = await websocket_principal(websocket)
    if principal is None:
        await websocket.close(code=4401, reason="not signed in")
        return
    await websocket.accept()
    hub.add(principal.org_id, websocket)
    try:
        await websocket.send_text(json.dumps({"type": "hello", "org_id": principal.org_id}))
        while True:
            # Clients may send pings; anything received just keeps the connection alive.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove(principal.org_id, websocket)
