from __future__ import annotations

from datetime import datetime, timedelta, timezone

HOLD_TTL = timedelta(minutes=15)


def utc_now() -> datetime:
    """Return timezone-naive UTC for consistent storage across SQLite and PostgreSQL."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
