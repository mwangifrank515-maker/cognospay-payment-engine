"""One place for "now in UTC".

Returns a *naive* UTC datetime so values compare cleanly with the naive UTC timestamps
stored in the database (an aware datetime would break those comparisons).
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
