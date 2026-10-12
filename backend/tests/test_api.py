"""API tests against a real server and worker: tenancy, keys, limits, metering, live updates."""

import asyncio
import io
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import timedelta

import httpx
import pytest
import websockets
from PIL import Image

from app import db
from app.billing.periods import close_period
from tests.conftest import BACKEND, TEST_ENV
from tests.helpers import month

PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"
API_STREAM = "test.api.events"
API_GROUP = "test-api-workers"
STACK_ENV = {**TEST_ENV, "STREAM_NAME": API_STREAM, "CONSUMER_GROUP": API_GROUP, "KEY_CACHE_TTL_SECONDS": "30"}


# ---------------------------------------------------------------- the running stack
@pytest.fixture(scope="module")
async def stack():
    env = {**os.environ, **STACK_ENV}
    app_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=BACKEND,
        env=env,
    )
    worker_proc = subprocess.Popen(
        [sys.executable, "-m", "app.worker_main"],
        cwd=BACKEND,
        env={**env, "CONSUMER_NAME": "api-worker"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    if (await client.get(f"{BASE}/healthz")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("app did not start")
        yield
    finally:
        for proc in (app_proc, worker_proc):
            proc.terminate()
        for proc in (app_proc, worker_proc):
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


async def api_drained(timeout: float = 30) -> None:
    from app.redis_client import get_redis

    r = get_redis()
    deadline = time.monotonic() + timeout
    await asyncio.sleep(0.3)
    while time.monotonic() < deadline:
        try:
            groups = await r.xinfo_groups(API_STREAM)
            group = next((g for g in groups if g["name"] == API_GROUP), None)
            if group and int(group["pending"] or 0) == 0 and int(group["lag"] or 0) == 0:
                return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.2)
    raise AssertionError("API stream did not drain")


def email(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}@test.example"


class Tenant:
    """An organization driven through the public APIs, the way a real user would."""

    def __init__(self, client: httpx.AsyncClient, me: dict):
        self.client = client
        self.me = me
        self.org_id = me["org"]["id"]
        self.project_id: str | None = None
        self.key: str | None = None
        self.key_id: str | None = None

    @classmethod
    async def signup(cls, tag: str, org_name: str | None = None, mail: str | None = None) -> "Tenant":
        client = httpx.AsyncClient(base_url=BASE, timeout=30)
        resp = await client.post(
            "/api/auth/signup", json={"email": mail or email(tag), "password": "password123", "org_name": org_name}
        )
        assert resp.status_code == 201, resp.text
        return cls(client, resp.json())

    async def setup_key(self, project: str = "main") -> "Tenant":
        resp = await self.client.post("/api/projects", json={"name": project})
        assert resp.status_code == 201, resp.text
        self.project_id = resp.json()["id"]
        resp = await self.client.post(f"/api/projects/{self.project_id}/keys", json={"name": "k"})
        assert resp.status_code == 201, resp.text
        self.key, self.key_id = resp.json()["key"], resp.json()["id"]
        return self

    def gw(self, key: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE, timeout=30, headers={"authorization": f"Bearer {key or self.key}"}
        )

    async def dashboard(self) -> dict:
        resp = await self.client.get("/api/dashboard")
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def close(self) -> None:
        await self.client.aclose()


def totals(dashboard: dict) -> dict:
    return {m["metric"]: m["total"] for m in dashboard["metrics"]}


def png_bytes(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- tests
async def test_metering_end_to_end(stack):
    """A real request yields the right events."""
    t = await (await Tenant.signup("meter")).setup_key()
    data = os.urandom(123_456)
    async with t.gw() as gw:
        assert (await gw.put("/v1/storage/objects/blob.bin", content=data)).status_code == 201
        got = await gw.get("/v1/storage/objects/blob.bin")
        assert got.status_code == 200 and got.content == data
        assert (await gw.get("/v1/storage/objects")).status_code == 200
        img = await gw.post("/v1/images/transform", params={"width": 64, "format": "jpeg"}, content=png_bytes(320, 200))
        assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
        assert Image.open(io.BytesIO(img.content)).size == (64, 40)
    await api_drained()
    d = totals(await t.dashboard())
    assert d["api.requests"] == 4
    assert d["storage.bytes_in"] == 123_456
    assert d["storage.bytes_out"] >= 123_456
    assert d["image.bytes_processed"] == len(png_bytes(320, 200))
    assert d["image.cpu_seconds"] >= 1
    await t.close()


async def test_roles_and_invites(stack):
    owner = await (await Tenant.signup("owner")).setup_key()
    viewer_email = email("viewer")
    resp = await owner.client.post("/api/team/invites", json={"email": viewer_email})
    assert resp.status_code == 201
    token = resp.json()["invite_token"]

    viewer_client = httpx.AsyncClient(base_url=BASE)
    bad = await viewer_client.post(
        "/api/auth/signup", json={"email": email("wrong"), "password": "password123", "invite_token": token}
    )
    assert bad.status_code == 400
    resp = await viewer_client.post(
        "/api/auth/signup", json={"email": viewer_email, "password": "password123", "invite_token": token}
    )
    assert resp.status_code == 201 and resp.json()["role"] == "viewer"
    assert resp.json()["org"]["id"] == owner.org_id
    viewer = Tenant(viewer_client, resp.json())

    # Viewers read; they never write.
    assert (await viewer.client.get("/api/dashboard")).status_code == 200
    assert (await viewer.client.get("/api/invoices")).status_code == 200
    assert (await viewer.client.get("/api/alerts")).status_code == 200
    assert (await viewer.client.post("/api/projects", json={"name": "nope"})).status_code == 403
    assert (await viewer.client.get(f"/api/projects/{owner.project_id}/keys")).status_code == 403
    assert (await viewer.client.post(f"/api/projects/{owner.project_id}/keys", json={})).status_code == 403
    assert (await viewer.client.delete(f"/api/keys/{owner.key_id}")).status_code == 403
    assert (await viewer.client.put("/api/subscription", json={"plan_id": "pro"})).status_code == 403
    assert (await viewer.client.get("/api/team")).status_code == 403
    assert (await viewer.client.get("/api/admin/dead-letters")).status_code == 403

    # The last owner cannot be demoted; promoting the viewer works and applies at once.
    owner_id = owner.me["user"]["id"]
    assert (await owner.client.patch(f"/api/team/members/{owner_id}", json={"role": "viewer"})).status_code == 409
    viewer_id = viewer.me["user"]["id"]
    assert (await owner.client.patch(f"/api/team/members/{viewer_id}", json={"role": "owner"})).status_code == 200
    assert (await viewer.client.post("/api/projects", json={"name": "now-allowed"})).status_code == 201

    # Login / logout.
    assert (await viewer.client.post("/api/auth/logout")).status_code == 204
    assert (await viewer.client.get("/api/me")).status_code == 401
    wrong = await viewer.client.post("/api/auth/login", json={"email": viewer_email, "password": "bad-password"})
    assert wrong.status_code == 401
    ok = await viewer.client.post("/api/auth/login", json={"email": viewer_email, "password": "password123"})
    assert ok.status_code == 200 and (await viewer.client.get("/api/me")).status_code == 200
    await owner.close()
    await viewer.close()


async def test_tenant_isolation_every_endpoint(stack):
    """Org A's credentials against every endpoint, targeting org B's ids: every request is refused."""
    from app.main import app

    a = await (await Tenant.signup("alpha", "Alpha Org")).setup_key("alpha-proj")
    b = await (await Tenant.signup("bravo", "Bravo Secret Org")).setup_key("bravo-secret-proj")
    async with b.gw() as gw:
        assert (await gw.put("/v1/storage/objects/bravo-secret.txt", content=b"top secret")).status_code == 201
    await api_drained()
    # Give B an invoice and an alert.
    aug = month(2026, 8)
    await db.execute(
        "INSERT INTO usage_hourly (org_id, project_id, metric, hour_start, quantity) VALUES (%s, %s, 'api.requests', %s, 50000)",
        (b.org_id, b.project_id, aug + timedelta(days=2)),
    )
    b_invoice = await close_period(b.org_id, aug)
    await db.execute(
        "INSERT INTO alerts (org_id, metric, period_start, threshold, usage, allowance) VALUES (%s, 'api.requests', %s, 80, 8, 10)",
        (b.org_id, aug),
    )
    b_user = b.me["user"]["id"]
    resp = await b.client.post("/api/team/invites", json={"email": email("bravo-invitee")})
    b_invite_email = resp.json()["email"]

    secrets = {
        b.org_id,
        b.project_id,
        b.key_id,
        b.key.split("_")[1],
        b_invoice,
        b_user,
        b.me["user"]["email"],
        b_invite_email,
        "bravo-secret.txt",
        "Bravo Secret Org",
        "bravo-secret-proj",
    }
    params = {"project_id": b.project_id, "key_id": b.key_id, "invoice_id": b_invoice, "user_id": b_user, "dl_id": "1"}
    bodies = {
        ("POST", "/api/projects"): {"name": "alpha-two"},
        ("POST", "/api/projects/{project_id}/keys"): {"name": "steal"},
        ("PUT", "/api/subscription"): {"plan_id": "free"},
        ("POST", "/api/team/invites"): {"email": email("alpha-invitee")},
        ("PATCH", "/api/team/members/{user_id}"): {"role": "viewer"},
        ("POST", "/api/admin/periods/close"): {"org_id": b.org_id, "period_start": "2026-08-01T00:00:00Z"},
    }
    skip = {"/api/auth/signup", "/api/auth/login", "/api/auth/logout"}
    checked = 0
    # Every endpoint the app exposes, from its OpenAPI schema.
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api") or path in skip:
            continue
        for method in operations:
            method = method.upper()
            url = path.format(**{k: v for k, v in params.items() if "{" + k + "}" in path})
            query = {"project_id": b.project_id} if path == "/api/usage" else None
            resp = await a.client.request(method, url, json=bodies.get((method, path)), params=query)
            checked += 1
            if "{" in path:
                assert resp.status_code in (403, 404), f"{method} {path} -> {resp.status_code}"
            leaked = [s for s in secrets if s in resp.text]
            assert not leaked, f"{method} {path} leaked {leaked}"
    assert checked >= 20

    # Nothing of B's changed.
    b_keys = (await b.client.get(f"/api/projects/{b.project_id}/keys")).json()["keys"]
    assert b_keys[0]["status"] == "active"
    assert (await b.client.get("/api/me")).json()["role"] == "owner"

    # Gateway: A's key cannot reach B's objects, and forged tenant headers are ignored.
    async with a.gw() as gw:
        assert (await gw.get("/v1/storage/objects/bravo-secret.txt")).status_code == 404
        assert (await gw.delete("/v1/storage/objects/bravo-secret.txt")).status_code == 404
        listing = await gw.get("/v1/storage/objects")
        assert "bravo-secret" not in listing.text
        forged = await gw.put(
            "/v1/storage/objects/forged.txt",
            content=b"x",
            headers={
                "x-tenant-org": b.org_id,
                "x-tenant-project": b.project_id,
                "x-internal-secret": "guess",
            },
        )
        assert forged.status_code == 201
    async with b.gw() as gw:
        names = [o["name"] for o in (await gw.get("/v1/storage/objects")).json()["objects"]]
        assert names == ["bravo-secret.txt"]

    # WebSocket: A's socket never sees B's traffic.
    cookie = "; ".join(f"{k}={v}" for k, v in a.client.cookies.items())
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws", additional_headers={"Cookie": cookie}) as ws:
        hello = json.loads(await ws.recv())
        assert hello == {"type": "hello", "org_id": a.org_id}
        async with b.gw() as gw:
            for _ in range(5):
                await gw.get("/v1/storage/objects")
        await api_drained()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=1.0)
    await a.close()
    await b.close()


async def test_unauthenticated_requests_refused(stack):
    async with httpx.AsyncClient(base_url=BASE) as client:
        for path in ("/api/me", "/api/dashboard", "/api/projects", "/api/invoices", "/api/team"):
            assert (await client.get(path)).status_code == 401
        assert (await client.get("/v1/storage/objects")).status_code == 401
        assert (await client.get("/v1/storage/objects", headers={"authorization": "Bearer mk_x_y"})).status_code == 401
        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws"):
                pass


async def test_key_revocation_stops_traffic(stack):
    """Revoking through the API stops the key at once (the cache entry is deleted)."""
    t = await (await Tenant.signup("revoke")).setup_key()
    async with t.gw() as gw:
        assert (await gw.get("/v1/storage/objects")).status_code == 200
        revoked_at = time.monotonic()
        assert (await t.client.delete(f"/api/keys/{t.key_id}")).status_code == 204
        while (await gw.get("/v1/storage/objects")).status_code != 401:
            assert time.monotonic() - revoked_at < 30
            await asyncio.sleep(0.2)
    keys = (await t.client.get(f"/api/projects/{t.project_id}/keys")).json()["keys"]
    assert keys[0]["status"] == "revoked"
    await t.close()


async def test_key_revocation_bounded_by_cache_ttl(stack):
    """Even if the cache entry is NOT deleted, a revoked key stops within the 30-second TTL."""
    t = await (await Tenant.signup("ttl")).setup_key()
    async with t.gw() as gw:
        assert (await gw.get("/v1/storage/objects")).status_code == 200  # now cached
        await db.execute("UPDATE api_keys SET revoked_at = now() WHERE id = %s", (t.key_id,))
        revoked_at = time.monotonic()
        while (await gw.get("/v1/storage/objects")).status_code == 200:
            assert time.monotonic() - revoked_at < 31, "revoked key still served after the TTL"
            await asyncio.sleep(0.5)
        assert (await gw.get("/v1/storage/objects")).status_code == 401
    await t.close()


async def test_rate_limit_and_plan_switch(stack):
    t = await (await Tenant.signup("rate")).setup_key()
    async with t.gw() as gw:
        statuses = []
        for _ in range(130):
            resp = await gw.get("/v1/storage/objects")
            statuses.append(resp.status_code)
            if resp.status_code == 429:
                assert int(resp.headers["retry-after"]) >= 1
                break
        if 429 not in statuses:
            pytest.skip("crossed a minute boundary; the window reset")
        assert statuses.count(200) == 120
        assert (await t.client.put("/api/subscription", json={"plan_id": "pro"})).status_code == 200
        assert (await gw.get("/v1/storage/objects")).status_code == 200  # pro limit applies at once
    sub = (await t.client.get("/api/subscription")).json()
    assert sub["current"]["plan_id"] == "pro"
    await t.close()


async def test_storage_cap_race(stack):
    """Concurrent uploads against a nearly full organization: stored bytes never exceed the cap."""
    t = await (await Tenant.signup("cap")).setup_key()
    cap = 100 * 1024 * 1024
    mib = 1024 * 1024
    await db.execute(
        "INSERT INTO stored_objects (org_id, project_id, name, size_bytes, path) VALUES (%s, %s, 'filler', %s, '/dev/null')",
        (t.org_id, t.project_id, cap - 3 * mib - 10),
    )
    payload = os.urandom(mib)
    async with t.gw() as gw:
        results = await asyncio.gather(
            *(gw.put(f"/v1/storage/objects/part-{i}", content=payload) for i in range(12))
        )
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 3, codes
    assert codes.count(429) == 9
    total = await db.fetch_one("SELECT SUM(size_bytes) AS s FROM stored_objects WHERE org_id = %s", (t.org_id,))
    assert total["s"] <= cap
    await t.close()


async def test_hard_limits(stack):
    t = await (await Tenant.signup("limits")).setup_key()
    async with t.gw() as gw:
        too_big = await gw.put("/v1/storage/objects/big", content=b"x" * (10 * 1024 * 1024 + 1))
        assert too_big.status_code == 413
        assert (await gw.put("/v1/storage/objects/..%2Fetc", content=b"x")).status_code in (400, 404)
        assert (await gw.put("/v1/storage/objects/.hidden", content=b"x")).status_code == 400
        not_image = await gw.post("/v1/images/transform", content=b"hello")
        assert not_image.status_code == 415
        big_input = await gw.post("/v1/images/transform", content=os.urandom(2 * 1024 * 1024 + 1))
        assert big_input.status_code == 413
        # A decompression bomb: tiny file, huge declared dimensions.
        bomb = io.BytesIO()
        Image.new("1", (5000, 5000)).save(bomb, format="PNG")
        assert len(bomb.getvalue()) < 2 * 1024 * 1024
        assert (await gw.post("/v1/images/transform", content=bomb.getvalue())).status_code == 413
        assert (await gw.post("/v1/images/transform", params={"format": "gif"}, content=png_bytes(8, 8))).status_code == 400
    # No stray files from refused uploads.
    rows = await db.fetch_one("SELECT COUNT(*) AS n FROM stored_objects WHERE org_id = %s", (t.org_id,))
    assert rows["n"] == 0
    await t.close()


async def test_websocket_reconnect_matches_snapshot(stack):
    """Drop the socket during traffic; after reconnect the client's totals match the REST snapshot."""
    t = await (await Tenant.signup("ws")).setup_key()
    cookie = "; ".join(f"{k}={v}" for k, v in t.client.cookies.items())
    url = f"ws://127.0.0.1:{PORT}/ws"

    class Client:
        """The dashboard's client logic: snapshot first, then apply only newer sequence numbers."""

        def __init__(self):
            self.seq = 0
            self.totals: dict[str, int] = {}

        async def connect(self):
            ws = await websockets.connect(url, additional_headers={"Cookie": cookie})
            await ws.recv()  # hello
            snapshot = await t.dashboard()  # subscribe first, then snapshot, then apply newer
            self.seq = snapshot["seq"]
            self.totals = totals(snapshot)
            return ws

        def apply(self, message: dict):
            if message.get("type") == "usage" and message["seq"] > self.seq:
                self.totals[message["metric"]] = self.totals.get(message["metric"], 0) + message["delta"]
                self.seq = message["seq"]

    client = Client()
    stop = asyncio.Event()

    async def traffic():
        async with t.gw() as gw:
            i = 0
            while not stop.is_set():
                await gw.put(f"/v1/storage/objects/o{i % 5}", content=os.urandom(1000 + i))
                i += 1

    async def listen(ws, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.monotonic()))
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                continue
            client.apply(json.loads(raw))

    traffic_task = asyncio.create_task(traffic())
    ws = await client.connect()
    await listen(ws, 1.5)
    await ws.close()  # drop the socket mid-traffic
    await asyncio.sleep(1.0)  # messages published now are missed
    ws = await client.connect()
    await listen(ws, 1.5)
    stop.set()
    await traffic_task
    await api_drained()
    await listen(ws, 1.0)
    await ws.close()

    final = totals(await t.dashboard())
    assert client.totals == final
    assert final["api.requests"] > 10
    await t.close()


async def test_alerts_fire_once_and_push(stack):
    """Crossing 80% and 100% of an allowance fires each alert once and pushes it over the socket."""
    from app.jobs.quotas import check_quotas
    from app.timeutil import hour_start, utcnow

    t = await (await Tenant.signup("alerts")).setup_key()
    cookie = "; ".join(f"{k}={v}" for k, v in t.client.cookies.items())
    await db.execute(
        "INSERT INTO usage_hourly (org_id, project_id, metric, hour_start, quantity) VALUES (%s, %s, 'api.requests', %s, 8500)",
        (t.org_id, t.project_id, hour_start(utcnow())),
    )
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws", additional_headers={"Cookie": cookie}) as ws:
        await ws.recv()
        fired = [a for a in await check_quotas() if a["org_id"] == t.org_id]
        assert [(a["metric"], a["threshold"]) for a in fired] == [("api.requests", 80)]
        pushed = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert pushed["type"] == "alert" and pushed["threshold"] == 80
    assert [a for a in await check_quotas() if a["org_id"] == t.org_id] == []  # fires once
    await db.execute(
        "UPDATE usage_hourly SET quantity = 10000 WHERE org_id = %s AND metric = 'api.requests'", (t.org_id,)
    )
    fired = [a for a in await check_quotas() if a["org_id"] == t.org_id]
    assert [a["threshold"] for a in fired] == [100]
    alerts = (await t.client.get("/api/alerts")).json()["alerts"]
    assert sorted(a["threshold"] for a in alerts) == [80, 100]
    await t.close()


async def test_operator_dead_letter_retry_and_close(stack):
    op_email = "operator@test.example"
    await db.execute("DELETE FROM users WHERE email = %s", (op_email,))
    op = await (await Tenant.signup("op", mail=op_email)).setup_key()
    assert op.me["user"]["is_operator"] is True
    event = {
        "event_id": str(uuid.uuid4()),
        "org_id": op.org_id,
        "project_id": op.project_id,
        "metric": "api.requests",
        "quantity": 3,
        "event_time": "2026-10-01T00:00:00Z",
        "source": "test",
    }
    row = await db.fetch_one(
        "INSERT INTO dead_letters (stream_id, payload, error, delivery_count) VALUES ('1-0', %s, 'boom', 5) RETURNING id",
        (json.dumps(event),),
    )
    listing = (await op.client.get("/api/admin/dead-letters")).json()["dead_letters"]
    assert any(d["id"] == row["id"] for d in listing)
    assert (await op.client.post(f"/api/admin/dead-letters/{row['id']}/retry")).status_code == 200
    await api_drained()
    counted = await db.fetch_one("SELECT quantity FROM raw_events WHERE event_id = %s", (event["event_id"],))
    assert counted["quantity"] == 3
    # Retrying again is harmless: the event is already counted once.
    assert (await op.client.post(f"/api/admin/dead-letters/{row['id']}/retry")).status_code == 200
    await api_drained()
    total = await db.fetch_one(
        "SELECT SUM(quantity) AS q FROM usage_hourly WHERE org_id = %s AND metric = 'api.requests'", (op.org_id,)
    )
    assert total["q"] >= 3
    status = (await op.client.get("/api/admin/status")).json()
    assert "stream" in status and status["counts"]["orgs"] >= 1
    resp = await op.client.post("/api/admin/periods/close", json={"org_id": op.org_id, "period_start": "2026-09-01T00:00:00Z"})
    assert resp.status_code == 200 and resp.json()["invoice_id"]
    invoices = (await op.client.get("/api/invoices")).json()["invoices"]
    detail = (await op.client.get(f"/api/invoices/{invoices[0]['id']}")).json()
    assert "lines" in detail
    await op.close()


# ---------------------------------------------------------------- in-process failure modes
async def test_gateway_fails_closed_when_redis_is_down(stack, monkeypatch):
    import redis.asyncio as redis

    from app.gateway import middleware
    from app.main import app

    dead = redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    monkeypatch.setattr(middleware, "get_redis", lambda: dead)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/storage/objects", headers={"authorization": "Bearer mk_abc_def"})
    assert resp.status_code == 503
    await dead.aclose()


async def test_emit_failure_spools_and_drains(stack, monkeypatch):
    import redis.asyncio as redis

    from app.pipeline import producer
    from app.pipeline.events import UsageEvent
    from app.timeutil import utcnow
    from pathlib import Path

    Path(producer.settings.spool_path).unlink(missing_ok=True)
    real = producer.get_redis
    dead = redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    monkeypatch.setattr(producer, "get_redis", lambda: dead)
    org = await db.fetch_one("INSERT INTO orgs (name) VALUES ('spool') RETURNING id")
    proj = await db.fetch_one("INSERT INTO projects (org_id, name) VALUES (%s, 'p') RETURNING id", (org["id"],))
    events = [
        UsageEvent(str(uuid.uuid4()), str(org["id"]), str(proj["id"]), "api.requests", 1, utcnow(), "gateway")
        for _ in range(5)
    ]
    assert await producer.emit(events) is False
    assert len(Path(producer.settings.spool_path).read_text().splitlines()) == 5
    monkeypatch.setattr(producer, "get_redis", real)
    assert await producer.drain_spool_once() == 5
    assert Path(producer.settings.spool_path).read_text() == ""
    await dead.aclose()

