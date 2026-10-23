import re
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_db(dt: datetime) -> str:
    """Serialize an aware datetime as UTC ISO-8601 with second precision and a Z suffix."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime not allowed; use a timezone-aware datetime")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 timestamp with `Z` or an offset; ValueError when malformed or naive."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"timestamp has no timezone: {value!r}")
    return dt


_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_when_lenient(value: str) -> datetime:
    """Parse an agent-written date: ISO with `Z` or an offset, a naive datetime (taken as UTC),
    or a bare `YYYY-MM-DD` (that day at 00:00Z); ValueError for anything else."""
    if _DATE_ONLY.fullmatch(value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def from_db(s: str) -> datetime:
    """Parse a stored UTC timestamp back into an aware datetime."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
