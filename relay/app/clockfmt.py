"""Short wall-clock text for pager copy (`5:21p`) in the relay-wide `RELAY_TIMEZONE`."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("relay.clockfmt")

DEFAULT_TIMEZONE = "America/Los_Angeles"


def zone_for(family_id: str | None = None) -> tzinfo:
    """An unknown `RELAY_TIMEZONE` is logged and falls back to UTC."""
    name = os.environ.get("RELAY_TIMEZONE") or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("RELAY_TIMEZONE %r is not a known zone; using UTC", name)
        return UTC


def clock(ts: float, family_id: str | None = None) -> str:
    """`ts` (epoch seconds) as `h:mm` plus `a` or `p`: 12 hour, no leading zero."""
    local = datetime.fromtimestamp(ts, tz=zone_for(family_id))
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d}{'a' if local.hour < 12 else 'p'}"
