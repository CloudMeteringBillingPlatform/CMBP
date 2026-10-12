"""Initial schema: tenancy, keys, plans, usage pipeline, billing, alerts.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-11
"""

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


SCHEMA = r"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- tenancy
CREATE TABLE orgs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email          text NOT NULL,
    password_hash  text NOT NULL,
    is_operator    boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_key ON users (lower(email));

-- One organization per user in the core build: user_id is the primary key.
CREATE TABLE memberships (
    user_id     uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    role        text NOT NULL CHECK (role IN ('owner', 'viewer')),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memberships_org_idx ON memberships (org_id);

CREATE TABLE invites (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    email        text NOT NULL,
    role         text NOT NULL DEFAULT 'viewer' CHECK (role IN ('owner', 'viewer')),
    token_hash   text NOT NULL UNIQUE,
    invited_by   uuid REFERENCES users (id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    accepted_at  timestamptz
);
CREATE INDEX invites_org_idx ON invites (org_id);

CREATE TABLE projects (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

-- Only the SHA-256 hash of a key is stored; the full key is shown once.
CREATE TABLE api_keys (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    project_id  uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    name        text NOT NULL DEFAULT '',
    prefix      text NOT NULL,
    key_hash    text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);
CREATE INDEX api_keys_project_idx ON api_keys (project_id);

-- ---------------------------------------------------------------- plans
CREATE TABLE plans (
    id    text PRIMARY KEY,
    name  text NOT NULL
);

-- A published plan version never changes; a price change is a new version.
CREATE TABLE plan_versions (
    plan_id         text NOT NULL REFERENCES plans (id),
    version         integer NOT NULL,
    effective_from  timestamptz NOT NULL,
    limits          jsonb NOT NULL,
    rates           jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, version)
);

CREATE TABLE subscriptions (
    org_id          uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    effective_from  timestamptz NOT NULL,
    plan_id         text NOT NULL REFERENCES plans (id),
    PRIMARY KEY (org_id, effective_from)
);

-- ---------------------------------------------------------------- usage pipeline
-- The primary key is the idempotency guard: a redelivered event conflicts and adds nothing.
CREATE TABLE raw_events (
    org_id       uuid NOT NULL,
    event_id     text NOT NULL,
    project_id   uuid,
    metric       text NOT NULL,
    quantity     bigint NOT NULL CHECK (quantity >= 0),
    event_time   timestamptz NOT NULL,
    source       text NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, event_id)
);
CREATE INDEX raw_events_org_time_idx ON raw_events (org_id, event_time);

-- Only ever incremented by the worker.
CREATE TABLE usage_hourly (
    org_id      uuid NOT NULL,
    project_id  uuid NOT NULL,
    metric      text NOT NULL,
    hour_start  timestamptz NOT NULL,
    quantity    bigint NOT NULL CHECK (quantity >= 0),
    PRIMARY KEY (org_id, project_id, metric, hour_start)
);
CREATE INDEX usage_hourly_org_hour_idx ON usage_hourly (org_id, hour_start);

CREATE TABLE billing_periods (
    org_id        uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    period_start  timestamptz NOT NULL,
    status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    closed_at     timestamptz,
    PRIMARY KEY (org_id, period_start)
);

CREATE TABLE invoices (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    period_start    timestamptz NOT NULL,
    period_end      timestamptz NOT NULL,
    plan_id         text NOT NULL,
    plan_version    integer NOT NULL,
    issued_at       timestamptz NOT NULL DEFAULT now(),
    total_paise     bigint NOT NULL,
    UNIQUE (org_id, period_start)
);

CREATE TABLE invoice_lines (
    id                  bigserial PRIMARY KEY,
    invoice_id          uuid NOT NULL REFERENCES invoices (id) ON DELETE CASCADE,
    kind                text NOT NULL CHECK (kind IN ('usage', 'adjustment')),
    metric              text NOT NULL,
    description         text NOT NULL,
    quantity            bigint NOT NULL,
    unit_label          text NOT NULL,
    unit_size           bigint NOT NULL,
    rate_paise          bigint NOT NULL,
    amount_paise        bigint NOT NULL,
    usage_period_start  timestamptz NOT NULL
);
CREATE INDEX invoice_lines_invoice_idx ON invoice_lines (invoice_id);

-- Late events for closed periods. Unapplied rows are swept into the next invoice.
CREATE TABLE usage_adjustments (
    id                  bigserial PRIMARY KEY,
    org_id              uuid NOT NULL,
    project_id          uuid NOT NULL,
    metric              text NOT NULL,
    hour_start          timestamptz NOT NULL,
    period_start        timestamptz NOT NULL,
    quantity            bigint NOT NULL CHECK (quantity >= 0),
    event_id            text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    applied_invoice_id  uuid REFERENCES invoices (id)
);
CREATE INDEX usage_adjustments_unapplied_idx
    ON usage_adjustments (org_id, period_start) WHERE applied_invoice_id IS NULL;

-- Per-organization sequence for live dashboard messages, advanced in the same
-- transaction as the aggregate change it describes.
CREATE TABLE live_sequences (
    org_id  uuid PRIMARY KEY,
    seq     bigint NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------- alerts, dead letters, storage
-- Each threshold fires once per metric per period.
CREATE TABLE alerts (
    id            bigserial PRIMARY KEY,
    org_id        uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    metric        text NOT NULL,
    period_start  timestamptz NOT NULL,
    threshold     integer NOT NULL,
    usage         bigint NOT NULL,
    allowance     bigint NOT NULL,
    fired_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, metric, period_start, threshold)
);

CREATE TABLE dead_letters (
    id              bigserial PRIMARY KEY,
    stream_id       text NOT NULL,
    payload         jsonb NOT NULL,
    error           text NOT NULL,
    delivery_count  integer NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    retried_at      timestamptz
);

CREATE TABLE stored_objects (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    project_id    uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    name          text NOT NULL,
    size_bytes    bigint NOT NULL CHECK (size_bytes >= 0),
    path          text NOT NULL,
    content_type  text NOT NULL DEFAULT 'application/octet-stream',
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);
CREATE INDEX stored_objects_org_idx ON stored_objects (org_id);

-- ---------------------------------------------------------------- immutability
CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER plan_versions_immutable BEFORE UPDATE OR DELETE ON plan_versions
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER invoices_immutable BEFORE UPDATE OR DELETE ON invoices
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER invoice_lines_immutable BEFORE UPDATE OR DELETE ON invoice_lines
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
"""

GIB = 1024**3
MIB = 1024**2

PLANS = """
INSERT INTO plans (id, name) VALUES ('free', 'Free'), ('pro', 'Pro');
"""


def _plan_version_sql() -> str:
    import json

    free_limits = {
        "rate_limit_per_minute": 120,
        "storage_bytes_cap": 100 * MIB,
        "quotas": {
            "api.requests": 10_000,
            "storage.bytes_in": 1 * GIB,
            "storage.bytes_out": 1 * GIB,
            "storage.byte_hours": 50 * GIB,
            "image.bytes_processed": 50 * MIB,
            "image.cpu_seconds": 60_000,
        },
    }
    # Rates are paise per unit_size raw units. api.requests is tiered; every other metric
    # is flat with an included allowance.
    free_rates = {
        "api.requests": {
            "unit_label": "1k requests",
            "unit_size": 1000,
            "included": 0,
            "tiers": [
                {"up_to": 10_000, "rate": 0},
                {"up_to": 100_000, "rate": 40},
                {"up_to": None, "rate": 25},
            ],
        },
        "storage.bytes_in": {"unit_label": "GiB", "unit_size": GIB, "included": 1 * GIB, "rate": 500},
        "storage.bytes_out": {"unit_label": "GiB", "unit_size": GIB, "included": 1 * GIB, "rate": 800},
        "storage.byte_hours": {
            "unit_label": "GiB-hour",
            "unit_size": GIB,
            "included": 50 * GIB,
            "rate": 2,
        },
        "image.bytes_processed": {
            "unit_label": "MiB",
            "unit_size": MIB,
            "included": 50 * MIB,
            "rate": 10,
        },
        "image.cpu_seconds": {
            "unit_label": "CPU-second",
            "unit_size": 1000,
            "included": 60_000,
            "rate": 20,
        },
    }
    pro_limits = {
        "rate_limit_per_minute": 1200,
        "storage_bytes_cap": 2 * GIB,
        "quotas": {
            "api.requests": 200_000,
            "storage.bytes_in": 20 * GIB,
            "storage.bytes_out": 20 * GIB,
            "storage.byte_hours": 1000 * GIB,
            "image.bytes_processed": 1 * GIB,
            "image.cpu_seconds": 1_800_000,
        },
    }
    pro_rates = {
        "api.requests": {
            "unit_label": "1k requests",
            "unit_size": 1000,
            "included": 0,
            "tiers": [
                {"up_to": 200_000, "rate": 0},
                {"up_to": 2_000_000, "rate": 20},
                {"up_to": None, "rate": 10},
            ],
        },
        "storage.bytes_in": {"unit_label": "GiB", "unit_size": GIB, "included": 20 * GIB, "rate": 300},
        "storage.bytes_out": {"unit_label": "GiB", "unit_size": GIB, "included": 20 * GIB, "rate": 500},
        "storage.byte_hours": {
            "unit_label": "GiB-hour",
            "unit_size": GIB,
            "included": 1000 * GIB,
            "rate": 1,
        },
        "image.bytes_processed": {
            "unit_label": "MiB",
            "unit_size": MIB,
            "included": 1024 * MIB,
            "rate": 5,
        },
        "image.cpu_seconds": {
            "unit_label": "CPU-second",
            "unit_size": 1000,
            "included": 1_800_000,
            "rate": 10,
        },
    }
    rows = [
        ("free", 1, "2020-01-01T00:00:00Z", free_limits, free_rates),
        ("pro", 1, "2020-01-01T00:00:00Z", pro_limits, pro_rates),
    ]
    values = ",\n".join(
        "('{}', {}, '{}', '{}'::jsonb, '{}'::jsonb)".format(
            plan, version, eff, json.dumps(limits), json.dumps(rates)
        )
        for plan, version, eff, limits, rates in rows
    )
    return (
        "INSERT INTO plan_versions (plan_id, version, effective_from, limits, rates) VALUES\n"
        + values
        + ";"
    )


def upgrade() -> None:
    op.execute(SCHEMA)
    op.execute(PLANS)
    op.execute(_plan_version_sql())


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS stored_objects, dead_letters, alerts, live_sequences,
            usage_adjustments, invoice_lines, invoices, billing_periods, usage_hourly,
            raw_events, subscriptions, plan_versions, plans, api_keys, projects, invites,
            memberships, users, orgs CASCADE;
        DROP FUNCTION IF EXISTS forbid_change();
        """
    )
