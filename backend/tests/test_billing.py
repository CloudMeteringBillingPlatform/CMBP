"""Billing tests: rating, late events, close versus in-flight events, invoice immutability."""

import asyncio
import random
import uuid
from datetime import timedelta
from fractions import Fraction

import psycopg
import pytest

from app import db
from app.billing.periods import close_ended_periods, close_period
from app.billing.rating import charge, round_paise, tiers
from app.pipeline.worker import Worker
from tests.helpers import (
    make_event,
    make_org,
    month,
    produce,
    run_worker_until_drained,
    stream_drained,
    wait_for,
)

REQUESTS_CARD = {
    "unit_label": "1k requests",
    "unit_size": 1000,
    "included": 0,
    "tiers": [{"up_to": 10_000, "rate": 0}, {"up_to": 100_000, "rate": 40}, {"up_to": None, "rate": 25}],
}
FLAT_CARD = {"unit_label": "GiB", "unit_size": 1024**3, "included": 1024**3, "rate": 500}


# ---------------------------------------------------------------- rating (pure)
def test_tiers_normalize():
    assert tiers(REQUESTS_CARD) == [(0, 10_000, 0), (10_000, 100_000, 40), (100_000, None, 25)]
    assert tiers(FLAT_CARD) == [(0, 1024**3, 0), (1024**3, None, 500)]


def test_tiered_charge():
    segments = charge("api.requests", REQUESTS_CARD, 0, 150_000)
    assert [s.quantity for s in segments] == [10_000, 90_000, 50_000]
    # 90k at 40 paise/1k = 3600 paise; 50k at 25 paise/1k = 1250 paise
    assert sum(round_paise(s.amount) for s in segments) == 3600 + 1250


def test_marginal_adjustment_rating():
    """A late event is priced at the tier of the closed period's final total, at the margin."""
    whole = charge("api.requests", REQUESTS_CARD, 0, 100_500)
    billed = charge("api.requests", REQUESTS_CARD, 0, 100_000)
    late = charge("api.requests", REQUESTS_CARD, 100_000, 100_500)
    assert [s.rate_paise for s in late] == [25]
    assert sum(s.amount for s in billed) + sum(s.amount for s in late) == sum(s.amount for s in whole)


def test_round_half_up():
    assert round_paise(Fraction(1, 2)) == 1
    assert round_paise(Fraction(149, 100)) == 1
    assert round_paise(Fraction(0)) == 0


# ---------------------------------------------------------------- periods (database)
async def invoice_with_lines(org_id: str, p_start):
    invoice = await db.fetch_one(
        "SELECT * FROM invoices WHERE org_id = %s AND period_start = %s", (org_id, p_start)
    )
    lines = await db.fetch_all(
        "SELECT kind, metric, quantity, rate_paise, amount_paise, usage_period_start, description "
        "FROM invoice_lines WHERE invoice_id = %s ORDER BY id",
        (invoice["id"],),
    )
    return invoice, lines


async def test_late_event_becomes_adjustment(clean):
    """An event for a closed period appears on the next invoice; the issued invoice is unchanged."""
    tenant = await make_org("late")
    aug, sep = month(2026, 8), month(2026, 9)
    await produce([make_event(tenant, "api.requests", 20_000, aug + timedelta(days=3))])
    await run_worker_until_drained()

    aug_invoice_id = await close_period(tenant["org_id"], aug)
    aug_invoice, aug_lines = await invoice_with_lines(tenant["org_id"], aug)
    # 10k free + 10k at 40 paise/1k
    assert aug_invoice["total_paise"] == 400

    # A late August event arrives in September.
    late = make_event(tenant, "api.requests", 5_000, aug + timedelta(days=20))
    await produce([late, late])  # and is duplicated, for good measure
    await run_worker_until_drained()

    adj = await db.fetch_all("SELECT * FROM usage_adjustments")
    assert len(adj) == 1 and adj[0]["quantity"] == 5_000 and adj[0]["applied_invoice_id"] is None
    # August's usage_hourly did not move.
    row = await db.fetch_one(
        "SELECT SUM(quantity) AS q FROM usage_hourly WHERE org_id = %s AND hour_start < %s",
        (tenant["org_id"], sep),
    )
    assert row["q"] == 20_000

    await produce([make_event(tenant, "api.requests", 1_000, sep + timedelta(days=1))])
    await run_worker_until_drained()
    sep_invoice_id = await close_period(tenant["org_id"], sep)
    sep_invoice, sep_lines = await invoice_with_lines(tenant["org_id"], sep)

    adjustments = [l for l in sep_lines if l["kind"] == "adjustment"]
    assert len(adjustments) == 1
    assert adjustments[0]["quantity"] == 5_000
    assert adjustments[0]["usage_period_start"] == aug
    assert adjustments[0]["rate_paise"] == 40  # priced with August's plan, at the margin of 20k
    assert adjustments[0]["amount_paise"] == 200
    assert sep_invoice["total_paise"] == 200  # September's 1k requests are in the free tier

    # The issued August invoice is byte-for-byte unchanged.
    again, again_lines = await invoice_with_lines(tenant["org_id"], aug)
    assert again == aug_invoice and again_lines == aug_lines
    applied = await db.fetch_one("SELECT applied_invoice_id FROM usage_adjustments")
    assert str(applied["applied_invoice_id"]) == sep_invoice_id != aug_invoice_id

    # Closing again is a no-op that returns the same invoice.
    assert await close_period(tenant["org_id"], sep) == sep_invoice_id


async def test_invoice_immutability_enforced_by_database(clean):
    tenant = await make_org("immutable")
    aug = month(2026, 8)
    await produce([make_event(tenant, "api.requests", 50_000, aug + timedelta(days=1))])
    await run_worker_until_drained()
    invoice_id = await close_period(tenant["org_id"], aug)
    with pytest.raises(psycopg.errors.RaiseException):
        await db.execute("UPDATE invoices SET total_paise = 0 WHERE id = %s", (invoice_id,))
    with pytest.raises(psycopg.errors.RaiseException):
        await db.execute("UPDATE invoice_lines SET amount_paise = 0 WHERE invoice_id = %s", (invoice_id,))
    with pytest.raises(psycopg.errors.RaiseException):
        await db.execute("DELETE FROM invoice_lines WHERE invoice_id = %s", (invoice_id,))
    with pytest.raises(psycopg.errors.RaiseException):
        await db.execute("UPDATE plan_versions SET rates = '{}'::jsonb")


async def test_close_versus_in_flight(clean):
    """Close a period while its events are processed: each is in the invoice XOR an adjustment."""
    tenant = await make_org("race")
    sep = month(2026, 9)
    rng = random.Random(11)
    events = [
        make_event(tenant, "api.requests", rng.randint(1, 50), sep + timedelta(minutes=rng.randint(0, 40_000)))
        for _ in range(4_000)
    ]
    expected_total = sum(e.quantity for e in events)
    await produce(events)

    worker = Worker(consumer_name="racer", concurrency=4)
    task = asyncio.create_task(worker.run())
    try:

        async def partly_done():
            row = await db.fetch_one("SELECT COUNT(*) AS n FROM raw_events")
            return row["n"] >= 800

        assert await wait_for(partly_done, timeout=60)
        invoice_id = await close_period(tenant["org_id"], sep)
        assert await wait_for(stream_drained, timeout=120)
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10)

    hourly = await db.fetch_one(
        "SELECT COALESCE(SUM(quantity), 0) AS q FROM usage_hourly WHERE org_id = %s", (tenant["org_id"],)
    )
    adjusted = await db.fetch_one(
        "SELECT COALESCE(SUM(quantity), 0) AS q, COUNT(*) AS n FROM usage_adjustments WHERE org_id = %s",
        (tenant["org_id"],),
    )
    billed = await db.fetch_one(
        "SELECT COALESCE(SUM(quantity), 0) AS q FROM invoice_lines WHERE invoice_id = %s AND kind = 'usage'",
        (invoice_id,),
    )
    # Never neither: everything is accounted for.
    assert hourly["q"] + adjusted["q"] == expected_total
    # Never both: what the invoice billed is exactly what reached usage_hourly before the close.
    assert billed["q"] == hourly["q"]
    assert adjusted["n"] > 0, "the close did not overlap with processing; the race was not exercised"
    # Every raw event went to exactly one place.
    raw = await db.fetch_one("SELECT COUNT(*) AS n FROM raw_events WHERE org_id = %s", (tenant["org_id"],))
    assert raw["n"] == len(events)


async def test_scheduler_closes_only_ended_periods(clean):
    tenant = await make_org("sched")
    aug = month(2026, 8)
    await produce([make_event(tenant, "api.requests", 1, aug + timedelta(days=1))])
    await run_worker_until_drained()
    now = month(2026, 10) + timedelta(days=5)
    await db.execute(
        "INSERT INTO billing_periods (org_id, period_start) VALUES (%s, %s)", (tenant["org_id"], month(2026, 10))
    )
    invoices = await close_ended_periods(now)
    assert len(invoices) == 1
    statuses = await db.fetch_all("SELECT period_start, status FROM billing_periods ORDER BY period_start")
    assert [(r["period_start"], r["status"]) for r in statuses] == [(aug, "closed"), (month(2026, 10), "open")]


async def test_plan_version_applies_per_period(clean):
    """Past periods rate with the plan that applied; a newer version applies only from its date."""
    import json

    # A throwaway plan: published versions are immutable, so tests never touch the seeded ones.
    plan_id = f"test-{uuid.uuid4().hex[:8]}"
    base = await db.fetch_one("SELECT limits, rates FROM plan_versions WHERE plan_id = 'free' AND version = 1")
    expensive = json.loads(json.dumps(base["rates"]))
    expensive["api.requests"]["tiers"][1]["rate"] = 400  # ten times the price from September
    await db.execute("INSERT INTO plans (id, name) VALUES (%s, 'Test')", (plan_id,))
    await db.execute(
        """
        INSERT INTO plan_versions (plan_id, version, effective_from, limits, rates) VALUES
          (%s, 1, '2020-01-01T00:00:00Z', %s, %s),
          (%s, 2, %s, %s, %s)
        """,
        (
            plan_id, json.dumps(base["limits"]), json.dumps(base["rates"]),
            plan_id, month(2026, 9), json.dumps(base["limits"]), json.dumps(expensive),
        ),
    )
    tenant = await make_org("versions", plan=plan_id)
    aug, sep = month(2026, 8), month(2026, 9)
    await produce(
        [
            make_event(tenant, "api.requests", 20_000, aug + timedelta(days=2)),
            make_event(tenant, "api.requests", 20_000, sep + timedelta(days=2)),
        ]
    )
    await run_worker_until_drained()
    await close_period(tenant["org_id"], aug)
    await close_period(tenant["org_id"], sep)
    rows = await db.fetch_all(
        "SELECT period_start, plan_version, total_paise FROM invoices WHERE org_id = %s ORDER BY period_start",
        (tenant["org_id"],),
    )
    assert [(r["plan_version"], r["total_paise"]) for r in rows] == [(1, 400), (2, 4000)]
