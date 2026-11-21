"""`fedOutbox/{id}`: events waiting to go to a linked family, `pending` -> `acked` | `failed`.
`enqueue` is create-only, so a retried `deliver()` writes one row; rows are `content` retention."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Literal

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter
from pydantic import BaseModel, ConfigDict, Field

from app.db.firestore import get_db

OutboxState = Literal["pending", "acked", "failed"]
BATCH_MAX = 20


class OutboxRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    linkId: str
    familyId: str = ""
    event: dict = Field(default_factory=dict)
    msgId: str | None = None
    state: OutboxState = "pending"
    attempts: int = 0
    nextAt: float = 0
    createdAt: datetime | None = None
    ackedAt: datetime | None = None
    error: str | None = None


def _rows():
    return get_db().collection("fedOutbox")


def _row(snap) -> OutboxRow:
    return OutboxRow.model_validate({"id": snap.id, **(snap.to_dict() or {})})


def enqueue(
    row_id: str,
    link_id: str,
    family_id: str,
    event: dict,
    *,
    msg_id: str | None = None,
    now: float | None = None,
) -> OutboxRow:
    """Creates the pending row (`nextAt` now); an existing row with this id is returned as is."""
    now = time.time() if now is None else now
    try:
        _rows().document(row_id).create(
            {
                "linkId": link_id,
                "familyId": family_id,
                "event": event,
                "msgId": msg_id,
                "state": "pending",
                "attempts": 0,
                "nextAt": now,
                "createdAt": SERVER_TIMESTAMP,
                "ackedAt": None,
                "error": None,
            }
        )
    except AlreadyExists:
        pass
    row = get(row_id)
    assert row is not None
    return row


def get(row_id: str) -> OutboxRow | None:
    snap = _rows().document(row_id).get()
    return _row(snap) if snap.exists else None


def list_pending(link_id: str | None = None) -> list[OutboxRow]:
    """Pending rows, oldest first (`createdAt`, then id); all links when `link_id` is None."""
    query = _rows().where(filter=FieldFilter("state", "==", "pending"))
    if link_id is not None:
        query = query.where(filter=FieldFilter("linkId", "==", link_id))
    rows = [_row(s) for s in query.stream()]
    rows.sort(key=lambda r: (r.createdAt or datetime.min.replace(tzinfo=UTC), r.id))
    return rows


def due_batch(
    link_id: str, *, now: float | None = None, limit: int = BATCH_MAX
) -> list[OutboxRow]:
    """Next batch for `link_id`: only while the oldest pending row is due, to keep order."""
    now = time.time() if now is None else now
    pending = list_pending(link_id)
    if not pending or pending[0].nextAt > now:
        return []
    return pending[:limit]


def mark_acked(row_id: str) -> None:
    _rows().document(row_id).update(
        {"state": "acked", "ackedAt": SERVER_TIMESTAMP, "error": None}
    )


def mark_failed(row_id: str, error: str) -> None:
    _rows().document(row_id).update(
        {"state": "failed", "ackedAt": SERVER_TIMESTAMP, "error": error}
    )


def record_attempt(row_id: str, next_at: float, error: str | None = None) -> None:
    ref = _rows().document(row_id)
    snap = ref.get()
    attempts = int((snap.to_dict() or {}).get("attempts") or 0) if snap.exists else 0
    ref.update({"attempts": attempts + 1, "nextAt": next_at, "error": error})


def fail_pending_for_link(link_id: str, error: str) -> list[OutboxRow]:
    """Fails every pending row of `link_id` (`unpeered`); returns the rows as they were."""
    rows = list_pending(link_id)
    for row in rows:
        mark_failed(row.id, error)
    return rows
