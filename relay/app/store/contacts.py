"""`contactRequests/{deviceId}_{reqId}`: device contact requests (docs/PROTOCOL.md §3.2).
`bump_book_version` writes `devices/{d}.bookVersion` directly, not through the devices store."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter, Transaction
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db, run_transaction

logger = logging.getLogger("relay.contacts")




class ContactRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str  # "{deviceId}_{reqId}" -- the Firestore document id.
    deviceId: str
    reqId: str
    ownerUid: str
    name: str
    phone: str | None = None
    alias: str | None = None
    status: Status = "pending"
    reason: str | None = None
    createdAt: datetime | None = None
    decidedAt: datetime | None = None
    decidedBy: str | None = None


def key(device_id: str, req_id: str) -> str:
    return f"{device_id}_{req_id}"


def _contact_requests():
    return get_db().collection("contactRequests")


def _decode(doc_key: str, data: dict) -> ContactRequest:
    return ContactRequest.model_validate({"key": doc_key, **data})


def get_request(doc_key: str) -> ContactRequest | None:
    snap = _contact_requests().document(doc_key).get()
    if not snap.exists:
        return None
    return _decode(doc_key, snap.to_dict() or {})


def get_by_device_and_req(device_id: str, req_id: str) -> ContactRequest | None:
    return get_request(key(device_id, req_id))


def list_requests(
    *, status: Status | None = None, device_id: str | None = None
) -> list[ContactRequest]:
    query = _contact_requests()
    if device_id is not None:
        query = query.where(filter=FieldFilter("deviceId", "==", device_id))
    if status is not None:
        query = query.where(filter=FieldFilter("status", "==", status))
    return [_decode(snap.id, snap.to_dict() or {}) for snap in query.stream()]


    *,
    device_id: str,
    owner_uid: str,
    req_id: str,
    name: str,
    phone: str | None = None,
    alias: str | None = None,
            {
                "deviceId": device_id,
                "reqId": req_id,
                "ownerUid": owner_uid,
                "name": name,
                "phone": phone,
                "alias": alias,
                "reason": None,
                "createdAt": SERVER_TIMESTAMP,
        )


def create_rejected(
    *,
    device_id: str,
    owner_uid: str,
    req_id: str,
    name: str,
    phone: str | None = None,
    alias: str | None = None,
    reason: str,
) -> ContactRequest:
    """A request rejected at ingest: `status:"rejected"`, `decidedBy:"relay"`, no alert."""
    doc_key = key(device_id, req_id)
    existing = get_request(doc_key)
    if existing is not None:
        return existing
    try:
        _contact_requests().document(doc_key).create(
            {
                "deviceId": device_id,
                "reqId": req_id,
                "ownerUid": owner_uid,
                "name": name,
                "phone": phone,
                "alias": alias,
                "status": "rejected",
                "reason": reason,
                "createdAt": SERVER_TIMESTAMP,
                "decidedAt": SERVER_TIMESTAMP,
                "decidedBy": "relay",
            }
        )
    except AlreadyExists:
        pass
    fetched = get_request(doc_key)
    assert fetched is not None
    return fetched


def approve(doc_key: str, *, decided_by: str) -> ContactRequest:
    ref = _contact_requests().document(doc_key)
    if not ref.get().exists:
        raise KeyError(f"no such contact request: {doc_key!r}")
    ref.update({"status": "approved", "decidedAt": SERVER_TIMESTAMP, "decidedBy": decided_by})
    fetched = get_request(doc_key)
    assert fetched is not None
    return fetched


def reject(doc_key: str, *, reason: str, decided_by: str) -> ContactRequest:
    ref = _contact_requests().document(doc_key)
    if not ref.get().exists:
        raise KeyError(f"no such contact request: {doc_key!r}")
    ref.update(
        {
            "status": "rejected",
            "reason": reason,
            "decidedAt": SERVER_TIMESTAMP,
            "decidedBy": decided_by,
        }
    )
    fetched = get_request(doc_key)
    assert fetched is not None
    return fetched


def bump_book_version(device_id: str) -> int:
    """Bumps `devices/{device_id}.bookVersion`, written directly rather than via devices.py."""
    ref = get_db().collection("devices").document(device_id)

    def _txn(transaction: Transaction) -> int:
        snap = ref.get(transaction=transaction)
        data = (snap.to_dict() or {}) if snap.exists else {}
        bv = int(data.get("bookVersion", 0)) + 1
        if snap.exists:
            transaction.update(ref, {"bookVersion": bv})
        else:
            transaction.set(ref, {"bookVersion": bv}, merge=True)
        return bv

    return run_transaction(_txn)


def bootstrap_book_version(device_id: str) -> bool:
    """Bumps `bookVersion` 0 -> 1 in a transaction (docs/PROTOCOL.md §5.3); True only if bumped."""
    ref = get_db().collection("devices").document(device_id)

    def _txn(transaction: Transaction) -> bool:
        snap = ref.get(transaction=transaction)
        data = (snap.to_dict() or {}) if snap.exists else {}
        bv = int(data.get("bookVersion", 0) or 0)
        if bv != 0:
            return False
        if snap.exists:
            transaction.update(ref, {"bookVersion": 1})
        else:
            transaction.set(ref, {"bookVersion": 1}, merge=True)
        return True

    return run_transaction(_txn)
