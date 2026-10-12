"""Demo driver for the failure demo (spec section 15). Talks to the running stack.

    cd backend && .venv/bin/python ../scripts/demo.py seed
    .venv/bin/python ../scripts/demo.py traffic --seconds 60
    .venv/bin/python ../scripts/demo.py verify
    .venv/bin/python ../scripts/demo.py duplicates
    .venv/bin/python ../scripts/demo.py late-event
    .venv/bin/python ../scripts/demo.py isolation
    .venv/bin/python ../scripts/demo.py status

The traffic command records what it actually sent (an independent count), so `verify` can
compare the platform's totals with numbers that never went through the pipeline.
Crash demo: run `traffic`, and while it runs `docker compose stop worker` (or kill -9 it),
wait, `docker compose start worker`, then `verify`.
"""

import argparse
import io
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import redis

BASE = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8000")
REDIS_URL = os.environ.get("DEMO_REDIS_URL", "redis://127.0.0.1:6379/0")
STREAM = os.environ.get("STREAM_NAME", "usage.events")
STATE = Path(os.environ.get("DEMO_STATE", Path(__file__).resolve().parent / ".demo-state.json"))
PASSWORD = "demo-password-123"
OPERATOR = os.environ.get("DEMO_OPERATOR_EMAIL", "operator@demo.example")

TENANTS = [
    {"slug": "alpha", "org": "Alpha Analytics", "plan": "pro"},
    {"slug": "bravo", "org": "Bravo Media", "plan": "free"},
    {"slug": "charlie", "org": "Charlie Labs", "plan": "free"},
]


# ---------------------------------------------------------------- helpers
def load_state() -> dict:
    if not STATE.exists():
        sys.exit("no demo state; run `demo.py seed` first")
    return json.loads(STATE.read_text())


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2))


def session(email: str, org: str | None = None) -> httpx.Client:
    client = httpx.Client(base_url=BASE, timeout=30)
    resp = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    if resp.status_code == 401:
        resp = client.post("/api/auth/signup", json={"email": email, "password": PASSWORD, "org_name": org})
    resp.raise_for_status()
    return client


def gateway(key: str) -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=60, headers={"authorization": f"Bearer {key}"})


def totals(client: httpx.Client) -> dict:
    data = client.get("/api/dashboard").raise_for_status().json()
    return {m["metric"]: m["total"] for m in data["metrics"]}


def wait_drained(timeout: float = 120) -> None:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            groups = r.xinfo_groups(STREAM)
            if groups and all(int(g["pending"] or 0) == 0 and int(g["lag"] or 0) == 0 for g in groups):
                return
        except redis.ResponseError:
            pass
        time.sleep(0.5)
    print("  ! stream did not drain within the timeout")


def png(w: int, h: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (random.randint(0, 255), 90, 160)).save(buf, format="PNG")
    return buf.getvalue()


def header(text: str) -> None:
    print(f"\n=== {text}")


def show(table: dict) -> None:
    for k, v in table.items():
        print(f"  {k:<24} {v:>14,}")


# ---------------------------------------------------------------- commands
def cmd_seed(args) -> None:
    """Three tenants sign up, create projects and keys, and pick plans; plus an operator."""
    state = {"tenants": {}, "sent": {}}
    header("seeding tenants")
    for t in TENANTS:
        email = f"owner@{t['slug']}.demo.example"
        client = session(email, t["org"])
        me = client.get("/api/me").json()
        projects = client.get("/api/projects").json()["projects"]
        project = next((p for p in projects if p["name"] == "production"), None)
        if project is None:
            project = client.post("/api/projects", json={"name": "production"}).raise_for_status().json()
        key = client.post(f"/api/projects/{project['id']}/keys", json={"name": "demo"}).raise_for_status().json()
        client.put("/api/subscription", json={"plan_id": t["plan"]}).raise_for_status()
        state["tenants"][t["slug"]] = {
            "email": email,
            "org_id": me["org"]["id"],
            "project_id": project["id"],
            "key": key["key"],
            "key_id": key["id"],
            "plan": t["plan"],
        }
        state["sent"][t["slug"]] = {"requests": 0, "bytes_in": 0, "image_bytes": 0}
        print(f"  {t['org']:<16} plan={t['plan']:<5} key={key['prefix']}…")
    op = session(OPERATOR, "Platform Operations")
    state["operator"] = {"email": OPERATOR, "is_operator": op.get("/api/me").json()["user"]["is_operator"]}
    if not state["operator"]["is_operator"]:
        print(f"  ! {OPERATOR} is not an operator; set OPERATOR_EMAILS={OPERATOR} for the app")
    save_state(state)
    keys_file = STATE.with_name("keys.json")
    keys_file.write_text(json.dumps([t["key"] for t in state["tenants"].values()]))
    print(f"  state saved to {STATE}; keys for k6 in {keys_file}")


def cmd_traffic(args) -> None:
    """Real storage and image requests through the gateway, counted independently."""
    state = load_state()
    clients = {slug: gateway(t["key"]) for slug, t in state["tenants"].items()}
    image = png(640, 480)
    end = time.time() + args.seconds
    header(f"sending traffic for {args.seconds}s at ~{args.rps} req/s")
    i = 0
    while time.time() < end:
        slug = random.choice(list(clients))
        client, sent = clients[slug], state["sent"][slug]
        roll = random.random()
        try:
            if roll < 0.45:
                body = os.urandom(random.randint(1_000, 200_000))
                resp = client.put(f"/v1/storage/objects/obj-{random.randint(0, 20)}", content=body)
            elif roll < 0.75:
                resp = client.get("/v1/storage/objects")
                body = b""
            elif roll < 0.9:
                resp = client.get(f"/v1/storage/objects/obj-{random.randint(0, 20)}")
                body = b""
            else:
                body = image
                resp = client.post("/v1/images/transform", params={"width": 160, "format": "webp"}, content=image)
        except httpx.HTTPError as exc:
            print(f"  request failed: {exc}")
            continue
        # The gateway meters every request it served; it refuses 401/429/503 before metering.
        if resp.status_code not in (401, 429, 503):
            sent["requests"] += 1
            if roll < 0.45:
                sent["bytes_in"] += len(body)
            elif roll >= 0.9 and resp.status_code == 200:
                sent["image_bytes"] += len(body)
        i += 1
        if i % 50 == 0:
            save_state(state)
            print(f"  {i} requests sent")
        time.sleep(max(0.0, 1 / args.rps))
    save_state(state)
    print(f"  done: {i} requests")


def cmd_verify(args) -> None:
    """Totals from the platform vs. the independent count kept by the traffic generator."""
    state = load_state()
    header("waiting for the pipeline to drain")
    wait_drained()
    ok = True
    for slug, t in state["tenants"].items():
        got = totals(session(t["email"]))
        sent = state["sent"][slug]
        rows = [
            ("api.requests", sent["requests"], got["api.requests"]),
            ("storage.bytes_in", sent["bytes_in"], got["storage.bytes_in"]),
            ("image.bytes_processed", sent["image_bytes"], got["image.bytes_processed"]),
        ]
        print(f"\n  {slug}")
        for metric, expected, actual in rows:
            mark = "OK " if expected == actual else "MISMATCH"
            ok &= expected == actual
            print(f"    {mark} {metric:<22} sent {expected:>12,}   platform {actual:>12,}")
    print("\nALL TOTALS MATCH" if ok else "\nTOTALS DIFFER (was traffic sent before this seed, or is the worker still down?)")


def cmd_duplicates(args) -> None:
    """Re-inject copies of recent events straight into the stream: totals do not move."""
    state = load_state()
    r = redis.from_url(REDIS_URL, decode_responses=True)
    slug = next(iter(state["tenants"]))
    client = session(state["tenants"][slug]["email"])
    wait_drained()
    before = totals(client)
    recent = r.xrevrange(STREAM, count=args.count)
    header(f"re-sending {len(recent)} already-counted events (exact duplicates)")
    with r.pipeline(transaction=False) as pipe:
        for _, fields in recent:
            pipe.xadd(STREAM, fields)
        pipe.execute()
    wait_drained()
    after = totals(client)
    print(f"  {slug} before vs after:")
    for metric in before:
        print(f"    {metric:<24} {before[metric]:>14,} -> {after[metric]:>14,}")
    print("  totals unchanged" if before == after else "  ! totals changed (other traffic was running?)")


def inject(r: redis.Redis, tenant: dict, metric: str, quantity: int, when: datetime) -> str:
    event = {
        "event_id": str(uuid.uuid4()),
        "org_id": tenant["org_id"],
        "project_id": tenant["project_id"],
        "metric": metric,
        "quantity": quantity,
        "event_time": when.isoformat().replace("+00:00", "Z"),
        "source": "test",
    }
    r.xadd(STREAM, {"e": json.dumps(event)})
    return event["event_id"]


def month_start(d: datetime, back: int) -> datetime:
    y, m = d.year, d.month - back
    while m <= 0:
        y, m = y - 1, m + 12
    return datetime(y, m, 1, tzinfo=timezone.utc)


def cmd_late_event(args) -> None:
    """Close a period, show its invoice, inject a late event, show it on the NEXT invoice."""
    state = load_state()
    tenant = state["tenants"][args.tenant]
    owner = session(tenant["email"])
    op = session(OPERATOR)
    r = redis.from_url(REDIS_URL, decode_responses=True)
    now = datetime.now(timezone.utc)
    p1, p2 = month_start(now, 2), month_start(now, 1)

    header(f"usage for {p1:%B %Y} arrives, then {p1:%B} is closed")
    inject(r, tenant, "api.requests", 60_000, p1 + timedelta(days=10))
    wait_drained()
    first = op.post("/api/admin/periods/close", json={"org_id": tenant["org_id"], "period_start": p1.isoformat()})
    first.raise_for_status()
    inv1 = owner.get(f"/api/invoices/{first.json()['invoice_id']}").json()
    print(f"  invoice for {p1:%B}: ₹{inv1['total_paise'] / 100:,.2f}  ({len(inv1['lines'])} lines)")

    header(f"a LATE {p1:%B} event arrives after the close")
    inject(r, tenant, "api.requests", 25_000, p1 + timedelta(days=28))
    wait_drained()
    pending = owner.get("/api/dashboard").json()["pending_adjustments"]
    print(f"  pending adjustments: {pending['n']} ({pending['quantity']:,} units), waiting for the next invoice")
    again = owner.get(f"/api/invoices/{inv1['id']}").json()
    print(f"  issued {p1:%B} invoice unchanged: {again == inv1}")

    header(f"{p2:%B %Y} closes; its invoice carries the adjustment")
    second = op.post("/api/admin/periods/close", json={"org_id": tenant["org_id"], "period_start": p2.isoformat()})
    second.raise_for_status()
    inv2 = owner.get(f"/api/invoices/{second.json()['invoice_id']}").json()
    for line in inv2["lines"]:
        print(f"  [{line['kind']:<10}] {line['description']}  ₹{line['amount_paise'] / 100:,.2f}")
    print(f"  total ₹{inv2['total_paise'] / 100:,.2f}")


def cmd_isolation(args) -> None:
    """A cross-organization request is refused and a revoked key is rejected."""
    state = load_state()
    a, b = state["tenants"]["alpha"], state["tenants"]["bravo"]
    alpha = session(a["email"])
    header("alpha's session asks for bravo's resources")
    for method, path in (
        ("GET", f"/api/projects/{b['project_id']}/keys"),
        ("POST", f"/api/projects/{b['project_id']}/keys"),
        ("DELETE", f"/api/keys/{b['key_id']}"),
    ):
        resp = alpha.request(method, path, json={} if method == "POST" else None)
        print(f"  {method:<6} {path:<60} -> {resp.status_code}")
    with gateway(b["key"]) as gb:
        gb.put("/v1/storage/objects/bravo-private.txt", content=b"bravo only")
    with gateway(a["key"]) as ga:
        resp = ga.get("/v1/storage/objects/bravo-private.txt")
        print(f"  alpha's key GET bravo's object -> {resp.status_code}")

    header("revoking a key while it is in use")
    project = a["project_id"]
    temp = alpha.post(f"/api/projects/{project}/keys", json={"name": "to-revoke"}).raise_for_status().json()
    with gateway(temp["key"]) as g:
        print(f"  before revoke: {g.get('/v1/storage/objects').status_code}")
        alpha.delete(f"/api/keys/{temp['id']}").raise_for_status()
        start = time.time()
        while (code := g.get("/v1/storage/objects").status_code) != 401 and time.time() - start < 35:
            time.sleep(0.5)
        print(f"  after revoke:  {code} (after {time.time() - start:.1f}s; the bound is 30s)")


def cmd_status(args) -> None:
    op = session(OPERATOR)
    print(json.dumps(op.get("/api/admin/status").json(), indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed").set_defaults(fn=cmd_seed)
    p = sub.add_parser("traffic")
    p.add_argument("--seconds", type=int, default=60)
    p.add_argument("--rps", type=float, default=15)
    p.set_defaults(fn=cmd_traffic)
    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    p = sub.add_parser("duplicates")
    p.add_argument("--count", type=int, default=500)
    p.set_defaults(fn=cmd_duplicates)
    p = sub.add_parser("late-event")
    p.add_argument("--tenant", default="charlie")
    p.set_defaults(fn=cmd_late_event)
    sub.add_parser("isolation").set_defaults(fn=cmd_isolation)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
