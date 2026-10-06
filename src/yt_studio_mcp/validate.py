"""Argument validation shared by the mutating tools.

Validation happens BEFORE any API call: a typo'd privacy value or a schedule
time without a timezone should cost zero quota and change nothing, instead of
reaching YouTube and being interpreted in some way nobody intended.
"""

from __future__ import annotations

from datetime import UTC, datetime

PRIVACY_VALUES = ("private", "unlisted", "public")


def check_privacy(value: str) -> str | None:
    """Return an error string, or None when value is a valid privacyStatus."""
    if value not in PRIVACY_VALUES:
        return f"privacy must be one of {'|'.join(PRIVACY_VALUES)} (got {value!r})"
    return None


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC3339 timestamp that carries an explicit offset.

    A naive time ("2026-10-06T17:00:00") is rejected: YouTube would read it as
    UTC, which for a Central-time schedule is five or six hours early.
    """
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"not an RFC3339 timestamp: {value!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"timestamp {value!r} has no timezone; append Z or an offset like -05:00"
        )
    return dt


def check_future_schedule(value: str, now: datetime | None = None) -> str | None:
    """Error string unless value is a tz-aware RFC3339 time in the future."""
    try:
        dt = parse_rfc3339(value)
    except ValueError as exc:
        return str(exc)
    now = now or datetime.now(UTC)
    if dt <= now:
        return (
            f"publish_at {value!r} is not in the future; a past schedule either "
            "fails (invalidPublishAt) or publishes immediately"
        )
    return None


def is_past(value: str | None, now: datetime | None = None) -> bool:
    """True when value is a parseable timestamp at or before now."""
    if not value:
        return False
    try:
        dt = parse_rfc3339(value)
    except ValueError:
        return False
    return dt <= (now or datetime.now(UTC))
