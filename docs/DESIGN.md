# Design write-up: Mini Cloud Metering & Billing Platform

This document explains how the platform turns a stream of duplicated, delayed and failed
usage events into accurate real-time usage and correct bills, on one 1 CPU / 1 GB VM. It covers
the trade-offs the spec asks to defend (section 17), the decisions taken on its open
questions, and every place the implementation deliberately differs from the spec text.

## 1. Architecture in one paragraph

Two processes share two datastores, and usage flows one way. The **app** process holds the
gateway middleware, the console API, the WebSocket hub, the storage and image modules and the
static React console. The **worker** process holds the stream consumer, the recovery loop, the
quota job, the storage sampler and the billing scheduler. The gateway authenticates each API
request, rate-limits it, forwards it to a service module, and on completion `XADD`s one usage
event per metric to the Redis stream `usage.events`. The worker reads the stream through a
consumer group and counts each event once in a single Postgres transaction. It then publishes
the change on Redis pub/sub `live:<org>`, and every app instance forwards it to that
organization's open dashboards. Postgres is the source of truth; raw events are kept, so
aggregates and invoices can be recomputed.

```
client ──► gateway (auth · rate limit · byte counting) ──► storage / image module
              │ one event per metric
              ▼
        Redis Stream usage.events ──► worker (consumer group) ──► Postgres
                                         │ BEGIN; FOR SHARE period; INSERT raw_events ON CONFLICT DO NOTHING;
                                         │ upsert usage_hourly | insert usage_adjustments; COMMIT; XACK
                                         ▼
                                   Redis pub/sub live:<org> ──► app instances ──► WebSocket dashboards
scheduled (worker): sampler (5 min) · quotas → alerts (60 s) · billing close (60 s check, month end)
```

## 2. Trade-offs to defend

### At-least-once delivery with a constraint-based exactly-once effect
Redis Streams with a consumer group deliver every message at least once. A message stays in
the pending entries list (PEL) until it is acknowledged, and a crashed consumer's messages are
re-delivered. Exactly-once *delivery* is not available, and chasing it would mean distributed
transactions between Redis and Postgres. Instead the worker makes the *effect* exactly-once.
Each event carries an `event_id`, and the first statement that changes anything is
`INSERT INTO raw_events … ON CONFLICT (org_id, event_id) DO NOTHING`. Only if that inserts a row
does the transaction add to `usage_hourly`. The insert and the aggregate commit together.

- **Crash before COMMIT:** nothing happened, and the redelivery does the work once.
- **Crash after COMMIT but before XACK:** the redelivery conflicts and adds nothing.
- **Two workers racing on the same duplicate:** the unique index makes one wait for the
  other's commit, then do nothing.

The tests prove each of these:

- `test_duplicate_suppression`: 10,000 events plus 20% duplicates equal an independent sum.
- `test_crash_recovery`: `kill -9` midway.
- `test_crash_between_commit_and_ack`.

### Redis Streams instead of Kafka
At tens of requests per second on one VM, Kafka's JVM alone would exceed the memory budget.
Redis is already needed for the cache, rate limits and sessions. The concepts map directly:

| Kafka | Redis Streams here |
| --- | --- |
| topic | the stream `usage.events` |
| partition | one stream (a single partition). Scaling out would shard by `org_id` into `usage.events.{n}` with one consumer group per shard, preserving per-org ordering exactly as Kafka keys do |
| consumer group | `XGROUP CREATE … metering-workers`; adding workers shares the load |
| committed offset | the group's `last-delivered-id` plus per-message `XACK` (Redis acks individual messages, not a single offset) |
| rebalance / reassigning a dead consumer's work | `XAUTOCLAIM` of messages idle > 60 s |
| retention | `XTRIM MINID`, only below the oldest pending and unread entry and older than 1 h |
| dead-letter topic | the `dead_letters` table after 5 deliveries |

Two limits are worth stating plainly. A single Redis node is not replicated (Redis has AOF on
here). Ordering within one stream is total, but processing is concurrent (4 in flight per
worker), which is safe because nothing depends on order: aggregates are commutative sums, and
the period lock handles the one order-sensitive case.

### Fail-closed gateway when metering is unavailable
If Redis cannot be reached while resolving the key or applying the rate limit, the gateway
returns **503** and does not serve the request. Serving it would give away usage that can never
be billed, and would let traffic bypass the rate limit. That availability cost is accepted
deliberately. The one case where the request has *already been served* when Redis fails is the
emit at the end. There the gateway appends the event to a local spool file (fsynced), and a
background task drains it into the stream. Those events arrive late and exercise the
late-event path for a real reason. Tests: `test_gateway_fails_closed_when_redis_is_down` and
`test_emit_failure_spools_and_drains`.

### Soft quotas that lag by up to a minute, against exact hard caps
Two kinds of limit are enforced differently on purpose.

**Hard limits** are checked synchronously and exactly:

- the per-key rate limit is a Redis fixed window, with `INCR` and `EXPIRE` in one Lua script so
  a crash cannot leave a counter with no TTL;
- per-object and image size limits are checked while streaming;
- the per-organization storage cap is checked inside the upload transaction.

The upload transaction first takes `SELECT … FROM orgs WHERE id = $org FOR UPDATE`, so
concurrent uploads for one organization serialize. The `SUM(size_bytes)` check is then exact,
and no interleaving can push the total past the cap. Without that lock, two uploads could both
read "3 MB free" and both commit. `test_storage_cap_race` fires 12 concurrent 1 MiB uploads at
an organization with 3 MiB left; exactly 3 succeed.

**Soft quotas** are a job that runs every 60 seconds and compares open-period usage with the
plan's allowance. Crossing 80% or 100% inserts an `alerts` row with
`ON CONFLICT DO NOTHING` on `(org, metric, period, threshold)`, so each alert fires once, and
pushes it to the dashboard. Because they read the aggregates, they lag the pipeline by up to a
minute. That is fine for an early warning; the aggregates remain the accurate record, and
billing never depends on alerts.

### Storage billing that is approximate by design
`storage.byte_hours` comes from a snapshot of each project's stored bytes every 5 minutes,
converted to byte-hours (`bytes × 300 / 3600`). An object uploaded and deleted between two
snapshots is never billed for holding, and one uploaded just after a snapshot is billed from
the next. Exact accounting would need an event on every upload and delete, plus interval
arithmetic. At these prices the error (at most 5 minutes of holding per object) is small, and
the snapshot is simple and cheap. The sampler's event id is a UUIDv5 of
(metric, organization, project, window start), so a retried or duplicated run produces the same
ids and the constraint rejects the repeats (`test_sampler_idempotency`).

### Modules in one process instead of separately deployed services
The gateway, storage, image, console and billing code are separate Python packages that share
no in-memory state. The storage and image modules already behave like remote services:

- they accept a request only with the internal shared-secret header;
- they read the tenant from the `x-tenant-*` headers the gateway sets;
- they never trust client headers, because the gateway strips any `x-internal-*` and
  `x-tenant-*` the client sends (tested).

Moving them to another machine means forwarding over HTTP instead of calling in-process,
which is a configuration change, not a rewrite. The worker is already a separate process; the
only shared state is Postgres and Redis. One process wins on a 1 GB VM: each extra Python
process costs ~40–60 MB of baseline memory.

## 3. Correctness arguments for the two trickiest flows

### Period close versus in-flight events
A worker transaction takes `SELECT status FROM billing_periods … FOR SHARE` on the row of the
period containing `event_time`. A period close takes `FOR UPDATE` on the same row, which
**waits for in-flight worker transactions** to commit. It then sets `status = 'closed'` and rates
`usage_hourly` for the period, all in the same transaction. A worker that arrives during the
close blocks on its `FOR SHARE` until the close commits. It then sees (under Read Committed,
re-reading the locked row) `status = 'closed'` and writes `usage_adjustments` instead. So every
event is either in `usage_hourly` before the close (on this invoice) or in `usage_adjustments`
(on the next one). It is never both, because `raw_events` admits it once, and never neither,
because one of the two branches always runs. `test_close_versus_in_flight` closes a period
while 4,000 events for it are being processed and checks both conditions.

### Late events and invoice immutability
An issued invoice never changes. This is enforced by `BEFORE UPDATE OR DELETE` triggers on
`invoices`, `invoice_lines` and `plan_versions`, not only by convention
(`test_invoice_immutability_enforced_by_database`). An event whose `event_time` falls in a
closed period lands in `usage_adjustments`. The next close of any later period sweeps unapplied
adjustments from earlier periods. It locks them `FOR UPDATE` so two concurrent closes cannot
both apply one, prices them with **the plan of the period they belong to**, and writes them as
`adjustment` lines. So, to the question "what happens to an event for September that arrives
in October?": it is billed as an adjustment line on the October invoice, at September's prices.

### Reconnect without double counting
Each live message carries a per-organization sequence number. On connect, the client subscribes
first, then fetches a REST snapshot that includes the latest sequence number. It applies only
messages with a higher number and drops the rest.
`test_websocket_reconnect_matches_snapshot` drops the socket during traffic and checks that the
client's totals equal a fresh snapshot.

## 4. Decisions on the open questions (spec section 17)

| Question | Decision |
| --- | --- |
| Keep both services, or storage alone? | Both are built. |
| How are tiered adjustments rated? | At the margin of the closed period's final total: the late units are priced as units `[billed, billed + late)` on the tier ladder, so billed + adjustments equal what the period would have cost had the event been on time (`test_marginal_adjustment_rating`). |
| Domain for TLS via Caddy? | Optional `tls` Compose profile; set `DOMAIN`, `COOKIE_SECURE=true`, `TRUST_PROXY=true`. |
| Always Free Ampere instance? | Not required; the stack measured ~180 MB under demo load, well inside the 1 GB VM. |
| Deadline / team size | Built solo. |

## 5. Deliberate differences from the spec text

1. **The live sequence number lives in Postgres, not a Redis counter.** A Redis `INCR` after
   the commit cannot be made consistent with the snapshot. A change can be committed but not
   yet numbered when the snapshot is read (then dropped as old), or numbered after it (then
   counted twice). The worker increments `live_sequences.seq` **in the same transaction** as the
   aggregate. The dashboard reads the sequence and the aggregates in one `REPEATABLE READ`
   transaction, so "≤ seq is in the snapshot, > seq is not" holds exactly.
2. **`image.cpu_seconds` is stored as integer CPU-milliseconds.** Quantities are integers, and
   most jobs take well under a second, so whole seconds would round to zero. Rates are per
   CPU-second (`unit_size` 1000).
3. **One event per metric per request:** `event_id = <request uuid>:<metric>`, so the per-metric
   events of one request are distinct occurrences, each idempotent on its own.
4. **Revocation also deletes the cached key record**, so it normally takes effect at once. The
   30-second TTL remains the upper bound if that delete is lost, and is tested on its own
   (`test_key_revocation_bounded_by_cache_ttl`).
5. **Which plan bills a period:** the plan the organization is on at the end of the period,
   using that plan's newest version with `effective_from` ≤ period start. A price published
   mid-month applies from the next month (no retroactive repricing), and a plan switch changes
   limits immediately.
6. **Storage cap exceeded → 429** (the spec reserves 429 for "a rate or quota limit"; 413 is for
   an oversized body).
7. **Rounding:** each invoice line's exact amount (a fraction of a paisa) is rounded half up
   once. The invoice total is the sum of the rounded lines.
8. **Additions:** `GET /api/subscription`, `GET /api/team`, `PATCH /api/team/members/{id}`,
   `GET /api/admin/status`, `POST /api/admin/periods/close` (operator, for the demo). Operators
   are the emails listed in `OPERATOR_EMAILS`.
9. **Stream trimming** uses `XTRIM MINID` at the minimum of (now − 1 h, oldest pending entry,
   first unread entry). It never trims by length, and never trims what a lagging worker has
   not read (`test_trim_never_drops_unread_events`).

## 6. Resource budget (measured)

`docker stats` during demo traffic from two generators at ~40 req/s each:

| Container | Limit | Used |
| --- | --- | --- |
| postgres | 160 MB | ~61 MB |
| redis | 48 MB | ~7 MB |
| app | 192 MB | ~81 MB |
| worker | 96 MB | ~31 MB |

Memory hazards designed out:

- decoded images are capped at 8 megapixels, checked from the header before decoding;
- image jobs run one at a time on a dedicated thread, with at most 8 waiting;
- argon2 uses 19 MiB per hash instead of the 64 MiB default;
- Redis runs with `noeviction` so the stream and sessions are never silently evicted;
- Docker logs rotate (10 MB × 3).
