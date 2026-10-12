"""UTC time helpers: hourly buckets and calendar-month billing periods."""

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def hour_start(value: datetime) -> datetime:
    return as_utc(value).replace(minute=0, second=0, microsecond=0)


def period_start(value: datetime) -> datetime:
    """Billing periods are calendar months in UTC."""
    return as_utc(value).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_period_start(start: datetime) -> datetime:
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def previous_period_start(start: datetime) -> datetime:
    if start.month == 1:
        return start.replace(year=start.year - 1, month=12)
    return start.replace(month=start.month - 1)


def window_start(value: datetime, window_seconds: int) -> datetime:
    epoch = int(as_utc(value).timestamp())
    return datetime.fromtimestamp(epoch - epoch % window_seconds, tz=timezone.utc)


def parse_time(value: str) -> datetime:
    return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def iso(value: datetime) -> str:
    return as_utc(value).isoformat().replace("+00:00", "Z")


def hours_between(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start) / timedelta(hours=1))
