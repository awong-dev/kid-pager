"""`devices/{deviceId}/smsLog/{logId}`: audit log of the pager's direct SMS; not a thread entry.
Keyed by the envelope `id`, so an at-least-once redelivery is a no-op (docs/PROTOCOL.md §2)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db

Direction = Literal["out", "in"]
Status = Literal["sent", "failed", "recv", "blocked"]


class SmsLogEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    ts: int
    receivedAt: datetime | None = None


def _sms_log(device_id: str):
    return get_db().collection("devices").document(device_id).collection("smsLog")


def create_log(
    device_id: str,
    log_id: str,
    *,
    ts: int,
    sms_ts: int,
    dir_: Direction,
    peer: str,
    st: Status,
    body: str,
) -> bool:
    """Writes `smsLog/{log_id}` with `create()`; True if created, False if already stored."""
    ref = _sms_log(device_id).document(log_id)
    try:
        ref.create(
            {
                "ts": ts,
                "smsTs": sms_ts,
                "dir": dir_,
                "peer": peer,
                "st": st,
                "body": body,
                "receivedAt": SERVER_TIMESTAMP,
            }
        )
        return True
    except AlreadyExists:
        return False


def list_log(device_id: str, *, limit: int = 100, before: int | None = None) -> list[SmsLogEntry]:
    """Newest first by the envelope's `ts`, which `before=<epoch seconds>` pagination keys on."""
    query = _sms_log(device_id)
    if before is not None:
        query = query.where(filter=FieldFilter("ts", "<", before))
    query = query.order_by("ts", direction="DESCENDING").limit(limit)
    return [
        SmsLogEntry.model_validate({"id": snap.id, **(snap.to_dict() or {})})
        for snap in query.stream()
    ]
