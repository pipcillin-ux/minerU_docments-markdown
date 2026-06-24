"""HTTP retry timing helpers."""

from __future__ import annotations

import email.utils
from datetime import datetime, timezone
from typing import Callable


def retry_after_seconds(
    value: str | None,
    *,
    now: Callable[[], datetime] | datetime | None = None,
) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now() if callable(now) else now
    if current is None:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - current).total_seconds())


def retry_delay(
    attempt: int,
    retry_after: str | None = None,
    *,
    base: float = 2.0,
    cap: float = 60.0,
    now: Callable[[], datetime] | datetime | None = None,
) -> float:
    explicit = retry_after_seconds(retry_after, now=now)
    if explicit is not None:
        return min(cap, explicit)
    return min(cap, base**attempt)
