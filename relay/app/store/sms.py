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
    smsTs: int = 0
    # Normal rows always carry these; a `malformed` row carries whatever
    # best-effort extraction found (possibly nothing -> None).
    dir: Direction | None = None
    peer: str | None = None
    st: Status | None = None
    body: str | None = ""
    receivedAt: datetime | None = None
    peerUid: str | None = None
    # Audit row for an `sms_log` upload that could not be stored normally (`create_malformed`).
    # Old rows lack the field: False.
    malformed: bool = False
    reason: str | None = None
    rawHex: str | None = None


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
    peer_uid: str | None = None,
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
                "peerUid": peer_uid,
                "receivedAt": SERVER_TIMESTAMP,
            }
        )
        return True
    except AlreadyExists:
        return False


def create_malformed(
    device_id: str,
    log_id: str,
    *,
    ts: int,
    reason: str,
    raw_hex: str,
    peer: str | None,
    dir_: str | None,
    st: str | None,
    msg_id: str | None,
) -> bool:
    """Audit row for a verified upload that failed decoding or validation; True if created."""
    try:
        _sms_log(device_id).document(log_id).create(
            {
                "ts": ts,
                "smsTs": 0,
                "dir": dir_,
                "peer": peer,
                "st": st,
                "body": None,
                "peerUid": None,
                "receivedAt": SERVER_TIMESTAMP,
                "malformed": True,
                "reason": reason,
                "rawHex": raw_hex,
                "msgId": msg_id,
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
