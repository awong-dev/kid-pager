"""`bridges/{bridgeId}/carrierLog/{id}`: raw texts to and from a carrier, trimmed to `KEEP` rows.
Server-only; retention class `content`."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore import SERVER_TIMESTAMP
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db

logger = logging.getLogger("relay.carrier_log")

KEEP = 50
TEXT_MAX = 1600


class CarrierLogEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    at: datetime | None = None
    # `in`: a reply from the carrier; `out`: a command the relay sent.
    dir: Literal["in", "out"] = "in"
    party: str = ""
    text: str = ""
    # `balance`, `topup_ok`, `topup_failed`, `low`, `unknown` for replies; `check`,
    # `topup` for commands.
    kind: str = ""
    detail: str | None = None


def _col(bridge_id: str):
    return get_db().collection("bridges").document(bridge_id).collection("carrierLog")


def _entry(snap) -> CarrierLogEntry:
    return CarrierLogEntry.model_validate({"id": snap.id, **(snap.to_dict() or {})})


def append(
    bridge_id: str,
    entry_id: str,
    *,
    dir: Literal["in", "out"],
    party: str,
    text: str,
    kind: str,
    detail: str | None = None,
) -> bool:
    """Writes the row (False if `entry_id` exists) and trims the log to the last `KEEP`."""
    data: dict[str, Any] = {
        "at": SERVER_TIMESTAMP,
        "dir": dir,
        "party": party,
        "text": text[:TEXT_MAX],
        "kind": kind,
        "detail": detail,
    }
    try:
        _col(bridge_id).document(entry_id).create(data)
    except AlreadyExists:
        return False
    trim(bridge_id)
    return True


def list_recent(bridge_id: str, limit: int = KEEP) -> list[CarrierLogEntry]:
    """Newest first."""
    rows = [_entry(s) for s in _col(bridge_id).stream()]
    rows.sort(key=lambda r: r.at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows[:limit]


def trim(bridge_id: str, keep: int = KEEP) -> int:
    rows = list_recent(bridge_id, limit=10_000)
    extra = rows[keep:]
    for row in extra:
        _col(bridge_id).document(row.id).delete()
    return len(extra)
