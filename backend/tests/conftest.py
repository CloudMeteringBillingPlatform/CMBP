"""Test configuration: a separate database and Redis DB, fast retry timings.

Tests run against the real Postgres and Redis from docker compose (ports on localhost):
    docker compose up -d postgres redis
    cd backend && .venv/bin/pytest -v
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

TEST_ENV = {
    "DATABASE_URL": os.environ.get(
        "TEST_DATABASE_URL", "postgresql://metering:metering@localhost:5432/metering_test"
    ),
    "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1"),
    "STREAM_NAME": "test.usage.events",
    "CONSUMER_GROUP": "test-workers",
    "CLAIM_MIN_IDLE_MS": "300",
    "RECOVERY_INTERVAL_SECONDS": "0.3",
    "MAX_DELIVERIES": "5",
    "SPOOL_PATH": str(BACKEND / "data" / "test" / "spool.jsonl"),
    "SPOOL_DRAIN_INTERVAL_SECONDS": "0.5",
    "STORAGE_DIR": str(BACKEND / "data" / "test" / "objects"),
    "RUN_SCHEDULED_JOBS": "false",
    "AUTH_RATE_LIMIT_PER_MINUTE": "1000",
    "OPERATOR_EMAILS": "operator@test.example",
    "SAMPLER_WINDOW_SECONDS": "300",
}
os.environ.update(TEST_ENV)

import pytest  # noqa: E402

from app import db  # noqa: E402
from app.redis_client import close_redis, get_redis  # noqa: E402

TABLES = (
    "stored_objects, dead_letters, alerts, live_sequences, usage_adjustments, invoice_lines, "
    "invoices, billing_periods, usage_hourly, raw_events, subscriptions, api_keys, projects, "
    "invites, memberships, users, orgs"
)


def _ensure_test_database() -> None:
    import psycopg

    url = TEST_ENV["DATABASE_URL"]
    base, _, name = url.rpartition("/")
    with psycopg.connect(base + "/postgres", autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{name}"')
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env={**os.environ, **TEST_ENV},
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session", autouse=True)
async def database():
    _ensure_test_database()
    await db.open_pool(min_size=1, max_size=10)
    yield
    await close_redis()
    await db.close_pool()


async def truncate_all() -> None:
    # TRUNCATE bypasses the row-level immutability triggers, which is what a test reset needs.
    await db.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    await get_redis().flushdb()


@pytest.fixture
async def clean():
    await truncate_all()
    yield
