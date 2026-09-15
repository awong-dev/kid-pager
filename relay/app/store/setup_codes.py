"""`setupCodes/{bid}`: holds only `deviceId` and `expiresAt`, never the token, `bpw` or `bkey`.
Server-only; `bid` is the bootstrap credential's HKDF id (`app/devsetup.derive()`), not `device_id`."""

from __future__ import annotations

from datetime import datetime

from google.cloud.firestore import FieldFilter
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db


class SetupCode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bid: str
    deviceId: str
    expiresAt: datetime


def _codes():
    return get_db().collection("setupCodes")


def create(bid: str, device_id: str, expires_at: datetime) -> SetupCode:
    _codes().document(bid).set({"deviceId": device_id, "expiresAt": expires_at})
    fetched = get(bid)
    assert fetched is not None
    return fetched


def get(bid: str) -> SetupCode | None:
    snap = _codes().document(bid).get()
    if not snap.exists:
        return None
    return SetupCode.model_validate({"bid": bid, **(snap.to_dict() or {})})


def delete(bid: str) -> None:
    _codes().document(bid).delete()


def list_expired(now: datetime) -> list[SetupCode]:
    """Every `setupCodes/{bid}` whose `expiresAt` is in the past."""
    query = _codes().where(filter=FieldFilter("expiresAt", "<", now))
    return [
        SetupCode.model_validate({"bid": snap.id, **(snap.to_dict() or {})})
        for snap in query.stream()
    ]
