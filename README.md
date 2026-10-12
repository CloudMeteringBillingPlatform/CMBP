# Mini Cloud Metering & Billing Platform

A small multi-tenant cloud. It hosts two real services, object storage and image processing,
meters what each tenant actually uses, and turns that into live dashboards, quotas and
immutable invoices. It fits on one 1 CPU / 1 GB VM.

The project's main problem: count every usage event exactly once, even when events arrive
duplicated, delayed or after failures, and bill correctly. See [docs/DESIGN.md](docs/DESIGN.md)
for the architecture, the trade-offs and the correctness arguments.

**Stack:** FastAPI (Python 3.12) · PostgreSQL 16 · Redis 7 (Streams, cache, rate limits,
sessions, pub/sub) · React 18 + Vite · Docker Compose · k6.

## Run it

```bash
cp .env.example .env                 # optional; defaults work on a laptop
cd frontend && npm install && npm run build && cd ..
docker compose up -d --build         # postgres, redis, app (:8000), worker
open http://localhost:8000
```

Migrations run automatically when the app starts. Sign up to create an organization (you
become its owner). Then create a project and an API key, and call the gateway:

```bash
KEY=mk_...   # shown once when you create it
curl -X PUT  localhost:8000/v1/storage/objects/hello.txt -H "Authorization: Bearer $KEY" --data-binary @hello.txt
curl         localhost:8000/v1/storage/objects            -H "Authorization: Bearer $KEY"
curl -X POST "localhost:8000/v1/images/transform?width=200&format=webp" \
     -H "Authorization: Bearer $KEY" --data-binary @photo.png -o out.webp
```

Interactive API docs are at http://localhost:8000/docs.

### Local development (without rebuilding images)

```bash
docker compose up -d postgres redis
cd backend
uv venv .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --port 8000       # terminal 1
.venv/bin/python -m app.worker_main                        # terminal 2
cd ../frontend && npm run dev                              # terminal 3 → http://localhost:5173
```

## Tests

The tests run against the real Postgres and Redis, using a separate `metering_test` database
and Redis DB 1:

```bash
docker compose up -d postgres redis
cd backend && .venv/bin/pytest -v          # 29 tests, ~2 minutes
```

| Spec test (section 15) | Test |
| --- | --- |
| Duplicate suppression: 10,000 events, 20% duplicates | `test_pipeline.py::test_duplicate_suppression` |
| Crash recovery: `kill -9` the worker midway | `test_pipeline.py::test_crash_recovery`, `test_crash_between_commit_and_ack` |
| Poison message → dead letters after 5 deliveries | `test_pipeline.py::test_poison_message` |
| Sampler idempotency | `test_pipeline.py::test_sampler_idempotency` |
| Tenant isolation on every endpoint | `test_api.py::test_tenant_isolation_every_endpoint` (walks the OpenAPI schema) |
| Key revocation within 30 s | `test_api.py::test_key_revocation_stops_traffic`, `test_key_revocation_bounded_by_cache_ttl` |
| Late event → adjustment on the next invoice | `test_billing.py::test_late_event_becomes_adjustment` |
| Close versus in-flight | `test_billing.py::test_close_versus_in_flight` |
| Storage cap race | `test_api.py::test_storage_cap_race` |
| WebSocket reconnect | `test_api.py::test_websocket_reconnect_matches_snapshot` |
| Load | `loadtest/k6.js` (below) |

Other tests cover rating, invoice immutability, plan versions, roles, hard limits, fail-closed
503, the spool, alerts and operator tools.

## Demo script (spec section 15)

With the stack running (`docker compose up -d`):

```bash
cd backend
.venv/bin/python ../scripts/demo.py seed                  # 3 tenants sign up, projects, keys, plans
.venv/bin/python ../scripts/demo.py traffic --seconds 60  # real storage + image requests; watch the dashboard
.venv/bin/python ../scripts/demo.py duplicates            # re-inject 500 counted events: totals do not move
# crash: run traffic, and meanwhile:  docker kill -s KILL fsd_project-worker-1 ; sleep 10 ; docker start fsd_project-worker-1
.venv/bin/python ../scripts/demo.py verify                # platform totals == independent count from the generator
.venv/bin/python ../scripts/demo.py late-event            # close a period, late event, adjustment on the next invoice
.venv/bin/python ../scripts/demo.py isolation             # cross-org requests refused; revoked key rejected
```

Demo logins (password `demo-password-123`) are `owner@alpha.demo.example`,
`owner@bravo.demo.example`, `owner@charlie.demo.example`, and the operator
`operator@demo.example`. The operator sees the dead-letter and system-status pages.

**Load test** (from the laptop, never on the VM):

```bash
k6 run -e BASE_URL=http://127.0.0.1:8000 -e RATE=30 loadtest/k6.js   # while it runs: docker stats
```

## Layout

```
backend/app/
  gateway/      key lookup (30 s cache), rate limit, metering middleware (fail-closed)
  services/     storage (files + exact cap), images (one job at a time, CPU-timed)
  pipeline/     event schema, producer (+ spool), idempotent worker, recovery, trimming
  billing/      plans, rating (tiers, integer paise), period close, projection
  jobs/         storage sampler, quotas → alerts
  console/      auth, projects/keys, plans, dashboard/usage/invoices/alerts, team, admin, WebSocket hub
  main.py       app process        worker_main.py   worker process
backend/migrations/   Alembic (schema, constraints, immutability triggers, seeded plans)
backend/tests/        29 tests
frontend/src/         React console (live dashboard, projects & keys, plan, invoices, alerts, team, operator)
scripts/demo.py       demo driver        loadtest/k6.js    load test
docker-compose.yml    memory limits per spec section 14; `--profile tls` adds Caddy
```
