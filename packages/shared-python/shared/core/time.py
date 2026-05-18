"""Shared UTC timestamp helper for naive database columns."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now_naive() -> datetime:
    """Return the current UTC timestamp without timezone info."""
    return datetime.now(UTC).replace(tzinfo=None)
