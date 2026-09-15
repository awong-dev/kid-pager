"""`messages/{id}`, `wireIds/{wireId}_{recipientUid}`, `conversations/{convKey}`; `create_message`
is one transaction: `wireIds` dedup plus the `seqCounter` increment (docs/PROTOCOL.md §3.5)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Literal

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore import (
    SERVER_TIMESTAMP,
    ArrayRemove,
    FieldFilter,
    Transaction,
)
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db, run_transaction
from app.ids import new_id

MessageKind = Literal["text", "loc_req", "loc"]
DeliveryState = Literal[
    "queued", "sent", "shown", "read", "fulfilled", "failed", "expired"
]
AckResult = Literal["updated", "noop", "unknown"]

PREVIEW_MAX_CHARS = 120
MAX_DELIVERY_ATTEMPTS = 5
# PROTOCOL.md §4: a down message still 'queued'/'sent' 24 h after creation is `expired`, derived
# at read time; this only bounds the republish/tick queries.
EXPIRY_SECONDS = 24 * 60 * 60


class Delivery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str
    state: DeliveryState = "queued"
    attempts: int = 0
    externalId: str | None = None
    error: str | None = None
    sentTs: int | None = None
    shownTs: int | None = None
    readTs: int | None = None


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    seq: int
    convKey: str
    uids: list[str]
    senderUid: str
    recipientUid: str
    kind: MessageKind
    body: str | None = None
    loc: dict | None = None
    wireId: str | None = None
    originBackendKind: str | None = None
    originBackendId: str | None = None
    ts: int
    createdAt: datetime | None = None
    deliveries: dict[str, Delivery] = {}
    pendingDeviceIds: list[str] = []


class Conversation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    convKey: str
    uids: list[str]
    lastMessageAt: datetime | None = None
    lastPreview: str = ""
    unread: dict[str, int] = {}


def conv_key(uid_a: str, uid_b: str) -> str:
    return "_".join(sorted([uid_a, uid_b]))


def _messages():
    return get_db().collection("messages")


def _wire_ids():
    return get_db().collection("wireIds")


def _conversations():
    return get_db().collection("conversations")


def _meta_ref():
    return get_db().collection("settings").document("meta")


def create_message(
    *,
    sender_uid: str,
    recipient_uid: str,
    kind: MessageKind,
    ts: int,
    body: str | None = None,
    loc: dict | None = None,
    wire_id: str | None = None,
    origin_backend_kind: str | None = None,
    origin_backend_id: str | None = None,
    deliveries: dict[str, dict] | None = None,
    pending_device_ids: list[str] | None = None,
) -> Message | None:
    """One transaction: `wireId` dedup (None if seen), `seq`, message create, conversation."""
    msg_id = new_id("m_")
    msg_ref = _messages().document(msg_id)
    conv_ref = _conversations().document(key)
    meta_ref = _meta_ref()
    wire_ref = _wire_ids().document(f"{wire_id}_{recipient_uid}") if wire_id else None
    preview = (body or "")[:PREVIEW_MAX_CHARS]

    def _txn(transaction: Transaction) -> None:
        # Reads precede writes (Firestore transaction rule); the dedup `create()` is staged after.
        conv_snap = conv_ref.get(transaction=transaction)

        if wire_ref is not None:
            # Dedup: `create()` raises AlreadyExists (not retried) for a seen (wireId, recipient).
            # `createdAt` lets `sweep()` reclaim the doc independently of its message.
            transaction.create(wire_ref, {"messageId": msg_id, "createdAt": SERVER_TIMESTAMP})

        else:

        transaction.set(
            msg_ref,
            {
                "convKey": key,
                "senderUid": sender_uid,
                "recipientUid": recipient_uid,
                "kind": kind,
                "body": body,
                "loc": loc,
                "wireId": wire_id,
                "originBackendKind": origin_backend_kind,
                "originBackendId": origin_backend_id,
                "ts": ts,
                "createdAt": SERVER_TIMESTAMP,
                "deliveries": deliveries or {},
                "pendingDeviceIds": pending_device_ids or [],
            },
        )

        unread = dict(conv_snap.get("unread") or {}) if conv_snap.exists else {}
        unread[recipient_uid] = unread.get(recipient_uid, 0) + 1
            "lastMessageAt": SERVER_TIMESTAMP,
            "lastPreview": preview,
            "unread": unread,
        }
        if conv_snap.exists:
            transaction.update(conv_ref, conv_data)
        else:
            transaction.set(conv_ref, conv_data)

    try:
        run_transaction(_txn)
    except AlreadyExists:
        return None

    fetched = get_message(msg_id)
    assert fetched is not None
    return fetched


def messages_ref(msg_id: str):
    """Public: `app/location.py` builds its own multi-document transactions on this collection."""
    return _messages().document(msg_id)


def conversation_ref(key: str):
    return _conversations().document(key)


def meta_ref():
    return _meta_ref()


def get_message(msg_id: str) -> Message | None:
    snap = _messages().document(msg_id).get()
    if not snap.exists:
        return None
    return Message.model_validate({"id": msg_id, **(snap.to_dict() or {})})


def list_thread(key: str, *, limit: int = 50) -> list[Message]:
    # `limit_to_last` queries can't be streamed (the client buffers them); use `.get()`.
    query = (
        _messages()
        .where(filter=FieldFilter("convKey", "==", key))
        .order_by("seq")
        .limit_to_last(limit)
    )
    return [
        Message.model_validate({"id": snap.id, **(snap.to_dict() or {})}) for snap in query.get()
    ]


def list_pending_for_device(
    device_id: str, *, limit: int = 10, max_age_s: int | None = EXPIRY_SECONDS
) -> list[Message]:
    """`pendingDeviceIds` contains `device_id`, oldest first, capped; `max_age_s` bounds it."""
    query = _messages().where(filter=FieldFilter("pendingDeviceIds", "array_contains", device_id))
    if max_age_s is not None:
        cutoff = datetime.fromtimestamp(time.time() - max_age_s, tz=UTC)
        query = query.where(filter=FieldFilter("createdAt", ">", cutoff))
    query = query.order_by("createdAt").limit(limit)
    return [
        Message.model_validate({"id": snap.id, **(snap.to_dict() or {})})
        for snap in query.stream()
    ]


def clear_pending_device(msg_id: str, device_id: str) -> None:
    """Removes `device_id` from `pendingDeviceIds` once its pager delivery is no longer pending."""
    _messages().document(msg_id).update({"pendingDeviceIds": ArrayRemove([device_id])})


def find_pager_delivery(msg: Message, device_id: str) -> str | None:
    """The `bid` of `msg`'s pager delivery for `device_id` (stored in `externalId`), or None."""
    for bid, delivery in msg.deliveries.items():
        if delivery.kind == "pager" and delivery.externalId == device_id:
            return bid
    return None


def find_delivery_by_kind(msg: Message, kind: str) -> str | None:
    """The first delivery of `kind` in `msg.deliveries` (a message has at most one per kind)."""
    for bid, delivery in msg.deliveries.items():
        if delivery.kind == kind:
            return bid
    return None


    """`queued` -> `sent` only, in a transaction (PROTOCOL.md §4.1 rule 2); True if written."""
    ref = _messages().document(msg_id)

        snap = ref.get(transaction=transaction)
        if not snap.exists:
        delivery = ((snap.to_dict() or {}).get("deliveries") or {}).get(backend_id)
        if delivery and delivery.get("state") == "queued":
            transaction.update(ref, {f"deliveries.{backend_id}.state": "sent"})



def apply_delivery_ack(
    msg_id: str,
    backend_id: str,
    device_id: str | None,
    ack: Literal["shown", "read"],
    ack_ts: int,
    *,
    clear_unread_uid: str | None = None,
) -> AckResult:
    """Monotonic ack machine (PROTOCOL.md §4.1 rules 1-3); `read` clears `clear_unread_uid`."""
    ref = _messages().document(msg_id)

    def _txn(transaction: Transaction) -> AckResult:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return "unknown"
        data = snap.to_dict() or {}
        delivery = (data.get("deliveries") or {}).get(backend_id)
        if delivery is None:
            return "unknown"
        state = delivery.get("state")
        updates: dict[str, object] = {}
        if ack == "shown":
            if state in ("shown", "read"):
                return "noop"
            updates[f"deliveries.{backend_id}.state"] = "shown"
            updates[f"deliveries.{backend_id}.shownTs"] = ack_ts
        elif ack == "read":
            if state == "read":
                return "noop"
            shown_ts = delivery.get("shownTs") if delivery.get("shownTs") is not None else ack_ts
            updates[f"deliveries.{backend_id}.state"] = "read"
            updates[f"deliveries.{backend_id}.readTs"] = ack_ts
            updates[f"deliveries.{backend_id}.shownTs"] = shown_ts
        else:
            return "unknown"
        if device_id is not None:
            updates["pendingDeviceIds"] = ArrayRemove([device_id])

        # All reads come before the `transaction.update` below.
        conv_ref = None
        conv_snap = None
        if clear_unread_uid is not None and ack == "read":
            conv_key = data.get("convKey")
            if conv_key:
                conv_ref = _conversations().document(conv_key)
                conv_snap = conv_ref.get(transaction=transaction)

        transaction.update(ref, updates)
        if conv_ref is not None and conv_snap is not None and conv_snap.exists:
            unread = dict(conv_snap.get("unread") or {})
            unread[clear_unread_uid] = 0
            transaction.update(conv_ref, {"unread": unread})
        return "updated"

    return run_transaction(_txn)


def record_delivery_attempt(
    msg_id: str,
    backend_id: str,
    *,
    ok: bool,
    error: str | None,
    device_id: str | None = None,
    max_attempts: int = MAX_DELIVERY_ATTEMPTS,
    """Counts a delivery attempt; at `max_attempts` marks it `failed`. True if it failed."""
    ref = _messages().document(msg_id)

        snap = ref.get(transaction=transaction)
        if not snap.exists:
        data = snap.to_dict() or {}
        delivery = (data.get("deliveries") or {}).get(backend_id)
        if delivery is None:
        attempts = (delivery.get("attempts") or 0) + 1
        state = delivery.get("state")
        reached_success = state not in ("queued", "failed")
        if not ok and not reached_success and attempts >= max_attempts:
            updates[f"deliveries.{backend_id}.state"] = "failed"
            if device_id is not None:
                updates["pendingDeviceIds"] = ArrayRemove([device_id])
        transaction.update(ref, updates)



def set_pending_device_ids(msg_id: str, device_ids: list[str]) -> None:
    _messages().document(msg_id).update({"pendingDeviceIds": device_ids})


def get_conversation(key: str) -> Conversation | None:
    snap = _conversations().document(key).get()
    if not snap.exists:
        return None
    return Conversation.model_validate({"convKey": key, **(snap.to_dict() or {})})
