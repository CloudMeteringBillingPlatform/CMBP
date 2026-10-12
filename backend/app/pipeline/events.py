"""The usage event: schema, validation and id rules (spec section 7)."""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.timeutil import iso, parse_time

METRICS = (
    "api.requests",
    "storage.bytes_in",
    "storage.bytes_out",
    "storage.byte_hours",
    "image.bytes_processed",
    "image.cpu_seconds",
)

METRIC_UNITS = {
    "api.requests": "count",
    "storage.bytes_in": "bytes",
    "storage.bytes_out": "bytes",
    "storage.byte_hours": "byte-hours",
    "image.bytes_processed": "bytes",
    # Stored as integer CPU-milliseconds so short jobs are not rounded to zero.
    "image.cpu_seconds": "cpu-ms",
}

SOURCES = ("gateway", "image-service", "sampler", "test")

# Namespace for deterministic sampler ids.
SAMPLER_NAMESPACE = uuid.UUID("6f1d2c1e-8a8e-4c55-9f7e-3b5a1a0c9d10")


class InvalidEvent(ValueError):
    """A message that can never be processed; it goes to dead letters after its retries."""


@dataclass(frozen=True)
class UsageEvent:
    event_id: str
    org_id: str
    project_id: str
    metric: str
    quantity: int
    event_time: datetime
    source: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "event_id": self.event_id,
                "org_id": self.org_id,
                "project_id": self.project_id,
                "metric": self.metric,
                "quantity": self.quantity,
                "event_time": iso(self.event_time),
                "source": self.source,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "UsageEvent":
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise InvalidEvent(f"payload is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise InvalidEvent("payload is not an object")
        try:
            event = cls(
                event_id=str(data["event_id"]),
                org_id=str(uuid.UUID(str(data["org_id"]))),
                project_id=str(uuid.UUID(str(data["project_id"]))),
                metric=str(data["metric"]),
                quantity=data["quantity"],
                event_time=parse_time(str(data["event_time"])),
                source=str(data.get("source", "")),
            )
        except (KeyError, ValueError) as exc:
            raise InvalidEvent(f"bad field: {exc!r}") from exc
        if not event.event_id or len(event.event_id) > 200:
            raise InvalidEvent("event_id missing or too long")
        if event.metric not in METRICS:
            raise InvalidEvent(f"unknown metric {event.metric!r}")
        if not isinstance(event.quantity, int) or isinstance(event.quantity, bool) or event.quantity < 0:
            raise InvalidEvent("quantity must be a non-negative integer")
        if event.source not in SOURCES:
            raise InvalidEvent(f"unknown source {event.source!r}")
        return event


def request_event_id(request_id: str, metric: str) -> str:
    """One event per metric per request or job: the occurrence id plus the metric."""
    return f"{request_id}:{metric}"


def sampler_event_id(metric: str, org_id: str, project_id: str, window: datetime) -> str:
    """Deterministic, so a retried or duplicated sampler run yields the same id."""
    return str(uuid.uuid5(SAMPLER_NAMESPACE, f"{metric}|{org_id}|{project_id}|{iso(window)}"))
