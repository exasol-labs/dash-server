"""Shared ISO-8601 timestamp parsing.

All three of (idle sweep, env-GC retention, diagnostics rate-limit dedup) need to
parse the ``"...Z"``-suffixed UTC timestamps that the rest of the server emits.
This helper centralizes the ``Z`` → ``+00:00`` substitution and the lenient
fall-through behavior so they all agree on what "unparseable" means.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def to_iso(value: datetime) -> str:
    """Render a datetime in the server's canonical ``"...Z"`` UTC format."""

    return value.isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    """The canonical stored-timestamp format: UTC now as ``"...Z"``."""

    return to_iso(datetime.now(timezone.utc))


def parse_iso8601(value: Any) -> datetime | None:
    """Parse a stored ISO-8601 timestamp. Returns ``None`` on missing/malformed input.

    Accepts ``"...Z"`` UTC strings (the format `_now_iso` emits) as well as anything
    `datetime.fromisoformat` natively understands. Non-string inputs and parse errors
    return ``None`` rather than raising.
    """

    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def seconds_since(value: Any, now: datetime) -> float:
    """Age in seconds since ``value``. Returns ``inf`` when ``value`` can't be parsed.

    Useful as the eligibility key in retention / idle-stop sweeps: an unparseable
    timestamp is treated as "infinitely old," which makes the record a candidate for
    cleanup rather than silently retained.
    """

    parsed = parse_iso8601(value)
    if parsed is None:
        return float("inf")
    return (now - parsed).total_seconds()
