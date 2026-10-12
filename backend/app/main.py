"""The app process: gateway, console API, WebSocket, storage and image modules, static frontend."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db
from app.config import settings
from app.console import admin, auth, live, plans, projects, team, usage
from app.gateway.middleware import GatewayMiddleware
from app.pipeline.producer import spool_drainer
from app.redis_client import close_redis
from app.services import images, storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool(min_size=1, max_size=8)
    stop = asyncio.Event()
    drainer = asyncio.create_task(spool_drainer(stop))
    live.hub.start()
    try:
        yield
    finally:
        stop.set()
        await live.hub.stop()
        drainer.cancel()
        await asyncio.gather(drainer, return_exceptions=True)
        await close_redis()
        await db.close_pool()


app = FastAPI(title="Mini Cloud Metering & Billing", version="1.0.0", lifespan=lifespan)
app.add_middleware(GatewayMiddleware)

for router in (
    auth.router,
    auth.me_router,
    projects.router,
    plans.router,
    usage.router,
    team.router,
    admin.router,
    live.router,
    storage.router,
    images.router,
):
    app.include_router(router)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    await db.fetch_one("SELECT 1 AS ok")
    return {"ok": True}


# ---------------------------------------------------------------- static single-page app
STATIC_DIR = Path(settings.static_dir).resolve()


@app.exception_handler(StarletteHTTPException)
async def spa_fallback(request, exc: StarletteHTTPException):
    """Serve index.html for unknown non-API GET paths so client-side routes work on reload."""
    path = request.url.path
    is_api = path.startswith(("/api", "/v1", "/ws", "/docs", "/openapi.json", "/healthz"))
    if exc.status_code == 404 and request.method == "GET" and not is_api and (STATIC_DIR / "index.html").exists():
        candidate = (STATIC_DIR / path.lstrip("/")).resolve()
        if candidate.is_file() and STATIC_DIR in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))
