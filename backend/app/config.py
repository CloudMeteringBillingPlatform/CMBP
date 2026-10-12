"""All runtime settings, read once from environment variables."""

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql://metering:metering@localhost:5432/metering"
    )
    redis_url: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    # Secret the gateway puts on every forwarded request; services reject requests without it.
    internal_secret: str = os.environ.get("INTERNAL_SECRET", "dev-internal-secret-change-me")
    cookie_secure: bool = _bool("COOKIE_SECURE", False)
    session_ttl_seconds: int = _int("SESSION_TTL_SECONDS", 7 * 24 * 3600)
    operator_emails: frozenset[str] = field(
        default_factory=lambda: frozenset(
            e.strip().lower() for e in os.environ.get("OPERATOR_EMAILS", "").split(",") if e.strip()
        )
    )

    # Gateway
    key_cache_ttl_seconds: int = _int("KEY_CACHE_TTL_SECONDS", 30)
    spool_path: str = os.environ.get("SPOOL_PATH", "./data/spool/usage-events.jsonl")
    spool_drain_interval_seconds: float = _float("SPOOL_DRAIN_INTERVAL_SECONDS", 5)

    # Event pipeline
    stream_name: str = os.environ.get("STREAM_NAME", "usage.events")
    consumer_group: str = os.environ.get("CONSUMER_GROUP", "metering-workers")
    consumer_name: str = os.environ.get("CONSUMER_NAME", "")
    claim_min_idle_ms: int = _int("CLAIM_MIN_IDLE_MS", 60_000)
    recovery_interval_seconds: float = _float("RECOVERY_INTERVAL_SECONDS", 10)
    max_deliveries: int = _int("MAX_DELIVERIES", 5)
    stream_retention_ms: int = _int("STREAM_RETENTION_MS", 3_600_000)
    trim_interval_seconds: float = _float("TRIM_INTERVAL_SECONDS", 300)

    # Services
    storage_dir: str = os.environ.get("STORAGE_DIR", "./data/objects")
    max_object_bytes: int = _int("MAX_OBJECT_BYTES", 10 * 1024 * 1024)
    max_image_bytes: int = _int("MAX_IMAGE_BYTES", 2 * 1024 * 1024)
    max_image_pixels: int = _int("MAX_IMAGE_PIXELS", 8_000_000)
    max_output_pixels: int = _int("MAX_OUTPUT_PIXELS", 8_000_000)

    # Scheduled jobs (worker process)
    sampler_window_seconds: int = _int("SAMPLER_WINDOW_SECONDS", 300)
    quota_interval_seconds: float = _float("QUOTA_INTERVAL_SECONDS", 60)
    billing_interval_seconds: float = _float("BILLING_INTERVAL_SECONDS", 60)
    run_scheduled_jobs: bool = _bool("RUN_SCHEDULED_JOBS", True)

    # Web
    static_dir: str = os.environ.get("STATIC_DIR", "../frontend/dist")
    auth_rate_limit_per_minute: int = _int("AUTH_RATE_LIMIT_PER_MINUTE", 20)
    trust_proxy: bool = _bool("TRUST_PROXY", False)


settings = Settings()
