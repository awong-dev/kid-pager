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

# Cap on `added` markers per owner.
MAX_ADDED_PER_OWNER = 32

# "pending" only exists on legacy rows; nothing creates one.
Status = Literal["pending", "approved", "rejected", "added"]


class ContactRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str  # "{deviceId}_{reqId}" -- the Firestore document id.
    deviceId: str
    reqId: str
    ownerUid: str
    name: str
    phone: str | None = None
    alias: str | None = None
    peerUid: str | None = None
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


def add_entry(
    *,
    device_id: str,
    owner_uid: str,
    family_id: str | None,
    req_id: str,
    name: str,
    peer_uid: str,
    nick: str | None,
    phone: str | None = None,
    alias: str | None = None,
) -> Literal["added", "full", "dup"]:
    """Creates the `added` request, merges the owner's book marker and bumps `bookVersion`."""
    db = get_db()
    row_ref = _contact_requests().document(key(device_id, req_id))
    book_col = db.collection("users").document(owner_uid).collection("book")
    marker_ref = book_col.document(peer_uid)
    markers_query = book_col.where(filter=FieldFilter("added", "==", True))
    devices_query = db.collection("devices").where(filter=FieldFilter("ownerUid", "==", owner_uid))

    def _txn(transaction: Transaction) -> Literal["added", "full", "dup"]:
        # All reads before any write.
        if row_ref.get(transaction=transaction).exists:
            return "dup"
        marked = {s.id for s in markers_query.stream(transaction=transaction)}
        if peer_uid not in marked and len(marked) >= MAX_ADDED_PER_OWNER:
            return "full"
        device_snaps = list(devices_query.stream(transaction=transaction))
        transaction.create(
            row_ref,
            {
                "deviceId": device_id,
                "reqId": req_id,
                "ownerUid": owner_uid,
                "name": name,
                "phone": phone,
                "alias": alias,
                "peerUid": peer_uid,
                "status": "added",
                "reason": None,
                "createdAt": SERVER_TIMESTAMP,
                "decidedAt": SERVER_TIMESTAMP,
                "decidedBy": "relay",
            },
        )
        marker: dict = {
            "added": True,
            "addedAt": SERVER_TIMESTAMP,
            "addedBy": device_id,
            "familyId": family_id,
        }
        if nick is not None:
            marker["nick"] = nick
        transaction.set(marker_ref, marker, merge=True)
        for snap in device_snaps:
            bv = int((snap.to_dict() or {}).get("bookVersion", 0) or 0) + 1
            transaction.update(snap.reference, {"bookVersion": bv})
        return "added"

    return run_transaction(_txn)


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
