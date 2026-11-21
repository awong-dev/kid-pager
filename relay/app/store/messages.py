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
    # A `fed` delivery: the other family's name and server host, for the web chip.
    server: str | None = None
    serverHost: str | None = None
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
    # The pager a pager-origin message came from (receipts go to it only); absent otherwise.
    originDeviceId: str | None = None
    ts: int
    createdAt: datetime | None = None
    deliveries: dict[str, Delivery] = {}
    pendingDeviceIds: list[str] = []
    # One id shared by every copy of a group message (dedupe / push collapse key) and the author's
    # alias, denormalised to avoid a `users` read per delivery. Both `None` on a DM copy.
    groupMsgId: str | None = None
    senderAlias: str | None = None
    # `hooks.{hookId} {action, labels, reason, at}` from a filter verdict or callback; `hookRef`
    # is the `message.ref` the hooks were given. Absent unless a hook ran.
    hooks: dict[str, dict] = {}
    hookRef: str | None = None
    # Sorted unique non-null `familyId` over this message's `uids`; an admin-read rule lookup field.
    familyIds: list[str] = []


class Participant(BaseModel):
    """One `conversations/{k}.participants` entry, denormalised to avoid a `users` read per row."""

    model_config = ConfigDict(extra="ignore")

    alias: str
    displayName: str
    kind: str = "person"


class Conversation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    convKey: str
    uids: list[str]
    # Absent MUST be read as 'dm'.
    kind: Literal["dm", "group"] = "dm"
    name: str | None = None
    alias: str | None = None
    createdBy: str | None = None
    lastMessageAt: datetime | None = None
    lastPreview: str = ""
    unread: dict[str, int] = {}
    # Written on conversation create; rewritten on group join/leave and a `displayName` change.
    familyIds: list[str] = []
    participants: dict[str, Participant] = {}
    # A group bridged to a Google Chat space: `{bridgeId, conversationId, source, link}`, plus the
    # `{nick: person name}` roster that becomes the wire's `sndr`.
    bridge: dict | None = None
    roster: dict[str, str] = {}


def conv_key(uid_a: str, uid_b: str) -> str:
    return "_".join(sorted([uid_a, uid_b]))


# `create_message`'s `conv_key` argument shadows `conv_key`; this alias derives the default key.
_derive_conv_key = conv_key


def _messages():
    return get_db().collection("messages")


def _wire_ids():
    return get_db().collection("wireIds")


def wire_id_exists(wire_id: str, recipient_uid: str) -> bool:
    """Whether `wireIds/{wire_id}_{recipient_uid}` exists."""
    return _wire_ids().document(f"{wire_id}_{recipient_uid}").get().exists


def _conversations():
    return get_db().collection("conversations")


def _users():
    return get_db().collection("users")


def _meta_ref():
    return get_db().collection("settings").document("meta")


def build_participants_and_family_ids(
    uids: list[str], *, transaction: Transaction | None = None
) -> tuple[dict[str, dict[str, str | None]], list[str]]:
    """`participants` and sorted `familyIds` for member uids; `transaction` carries the reads."""
    users_ref = _users()
    participants: dict[str, dict[str, str | None]] = {}
    family_ids: set[str] = set()
    for uid in uids:
        ref = users_ref.document(uid)
        snap = ref.get(transaction=transaction) if transaction is not None else ref.get()
        if not snap.exists:
            continue
        data = snap.to_dict() or {}
        participants[uid] = {
            "alias": data.get("alias", ""),
            "displayName": data.get("displayName", ""),
            "kind": data.get("kind", "person"),
        }
        if data.get("kind") == "external":
            # Externals carry their phone so the web does not read it out of the alias.
            participants[uid]["phone"] = data.get("phone")
        family_id = data.get("familyId")
        if family_id:
            family_ids.add(family_id)
    return participants, sorted(family_ids)


def rewrite_participants_for_user(uid: str) -> None:
    """Re-derives `participants`/`familyIds` in every conversation listing `uid`; best-effort."""
    query = _conversations().where(filter=FieldFilter("uids", "array_contains", uid))
    for snap in query.stream():
        data = snap.to_dict() or {}
        members = data.get("uids") or []
        participants, family_ids = build_participants_and_family_ids(members)
        snap.reference.update({"participants": participants, "familyIds": family_ids})


def allocate_seq() -> int:
    """Mints one `seq` in its own transaction, shared by every copy of a group message."""
    meta_ref = _meta_ref()

    def _txn(transaction: Transaction) -> int:
        meta_snap = meta_ref.get(transaction=transaction)
        seq = ((meta_snap.get("seqCounter") if meta_snap.exists else 0) or 0) + 1
        if meta_snap.exists:
            transaction.update(meta_ref, {"seqCounter": seq})
        else:
            transaction.set(
                meta_ref, {"schemaVersion": 2, "lastSweepAt": None, "seqCounter": seq}
            )
        return seq

    return run_transaction(_txn)


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
    conv_key: str | None = None,
    uids: list[str] | None = None,
    seq: int | None = None,
    group_msg_id: str | None = None,
    sender_alias: str | None = None,
    hooks: dict[str, dict] | None = None,
    hook_ref: str | None = None,
    origin_device_id: str | None = None,
) -> Message | None:
    """One transaction: `wireId` dedup (None if seen), `seq`, message create, conversation."""
    msg_id = new_id("m_")
    key = conv_key if conv_key is not None else _derive_conv_key(sender_uid, recipient_uid)
    pair = uids if uids is not None else sorted([sender_uid, recipient_uid])
    msg_ref = _messages().document(msg_id)
    conv_ref = _conversations().document(key)
    meta_ref = _meta_ref()
    wire_ref = _wire_ids().document(f"{wire_id}_{recipient_uid}") if wire_id else None
    preview = (body or "")[:PREVIEW_MAX_CHARS]

    def _txn(transaction: Transaction) -> None:
        # Reads precede writes (Firestore transaction rule); the dedup `create()` is staged after.
        meta_snap = None if seq is not None else meta_ref.get(transaction=transaction)
        conv_snap = conv_ref.get(transaction=transaction)
        # `familyIds` over this message's `uids` on every write; `participants` only when this
        # send creates the conversation.
        participants, family_ids = build_participants_and_family_ids(pair, transaction=transaction)

        if wire_ref is not None:
            # Dedup: `create()` raises AlreadyExists (not retried) for a seen (wireId, recipient).
            # `createdAt` lets `sweep()` reclaim the doc independently of its message.
            transaction.create(wire_ref, {"messageId": msg_id, "createdAt": SERVER_TIMESTAMP})

        if seq is not None:
            new_seq = seq
        else:
            new_seq = ((meta_snap.get("seqCounter") if meta_snap.exists else 0) or 0) + 1
            if meta_snap.exists:
                transaction.update(meta_ref, {"seqCounter": new_seq})
            else:
                transaction.set(
                    meta_ref, {"schemaVersion": 2, "lastSweepAt": None, "seqCounter": new_seq}
                )

        extra: dict[str, object] = {}
        if hooks:
            extra["hooks"] = hooks
        if hook_ref is not None:
            extra["hookRef"] = hook_ref
        transaction.set(
            msg_ref,
            {
                **extra,
                "seq": new_seq,
                "convKey": key,
                "uids": pair,
                "senderUid": sender_uid,
                "recipientUid": recipient_uid,
                "kind": kind,
                "body": body,
                "loc": loc,
                "wireId": wire_id,
                "originBackendKind": origin_backend_kind,
                "originBackendId": origin_backend_id,
                "originDeviceId": origin_device_id,
                "ts": ts,
                "createdAt": SERVER_TIMESTAMP,
                "deliveries": deliveries or {},
                "pendingDeviceIds": pending_device_ids or [],
                "groupMsgId": group_msg_id,
                "senderAlias": sender_alias,
                "familyIds": family_ids,
            },
        )

        unread = dict(conv_snap.get("unread") or {}) if conv_snap.exists else {}
        unread[recipient_uid] = unread.get(recipient_uid, 0) + 1
        conv_data: dict[str, object] = {
            "lastMessageAt": SERVER_TIMESTAMP,
            "lastPreview": preview,
            "unread": unread,
        }
        if conv_snap.exists:
            # Not "uids": a group conversation's `uids` is the member list set by `create_group`;
            # writing the pair would drop members.
            transaction.update(conv_ref, conv_data)
        else:
            # Only DMs create their conversation lazily; a group is created up front.
            conv_data["uids"] = pair
            # Written only when creating the conversation.
            conv_data["participants"] = participants
            conv_data["familyIds"] = family_ids
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


def list_split_siblings(msg: Message) -> list[Message]:
    """All stored parts of split message `msg` (wire id `<base>` or `<base>~<i>`)."""
    if not msg.wireId:
        return [msg]
    base = msg.wireId.split("~", 1)[0]
    query = (
        _messages()
        .where(filter=FieldFilter("wireId", ">=", base))
        .where(filter=FieldFilter("wireId", "<", base + "\uffff"))
    )
    out: list[Message] = []
    for snap in query.stream():
        m = Message.model_validate({"id": snap.id, **(snap.to_dict() or {})})
        suffix = (m.wireId or "")[len(base) :]
        if m.wireId != base and not (suffix.startswith("~") and suffix[1:].isdigit()):
            continue
        if m.senderUid == msg.senderUid and m.recipientUid == msg.recipientUid:
            out.append(m)
    return out


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


def mark_delivery_sent_if_queued(msg_id: str, backend_id: str) -> bool:
    """`queued` -> `sent` only, in a transaction (PROTOCOL.md §4.1 rule 2); True if written."""
    ref = _messages().document(msg_id)

    def _txn(transaction: Transaction) -> bool:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return False
        delivery = ((snap.to_dict() or {}).get("deliveries") or {}).get(backend_id)
        if delivery and delivery.get("state") == "queued":
            transaction.update(ref, {f"deliveries.{backend_id}.state": "sent"})
            return True
        return False

    return run_transaction(_txn)


def mark_delivery_failed_if_queued(
    msg_id: str, backend_id: str, *, error: str | None = None
) -> bool:
    """`queued` -> `failed` for a delivery that can never succeed; stores `error`; True if moved."""
    ref = _messages().document(msg_id)

    def _txn(transaction: Transaction) -> bool:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return False
        delivery = ((snap.to_dict() or {}).get("deliveries") or {}).get(backend_id)
        if delivery and delivery.get("state") == "queued":
            updates: dict[str, object] = {f"deliveries.{backend_id}.state": "failed"}
            if error is not None:
                updates[f"deliveries.{backend_id}.error"] = error
            transaction.update(ref, updates)
            return True
        return False

    return run_transaction(_txn)


def set_delivery_external_id(msg_id: str, backend_id: str, external_id: str) -> None:
    """Records the provider-side id of a delivery without touching its state."""
    _messages().document(msg_id).update({f"deliveries.{backend_id}.externalId": external_id})


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
) -> bool:
    """Counts a delivery attempt; at `max_attempts` marks it `failed`. True if it failed."""
    ref = _messages().document(msg_id)

    def _txn(transaction: Transaction) -> bool:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return False
        data = snap.to_dict() or {}
        delivery = (data.get("deliveries") or {}).get(backend_id)
        if delivery is None:
            return False
        attempts = (delivery.get("attempts") or 0) + 1
        state = delivery.get("state")
        updates: dict[str, object] = {f"deliveries.{backend_id}.attempts": attempts}
        if error is not None or state != "failed":
            # A delivery that already failed keeps the reason it was given.
            updates[f"deliveries.{backend_id}.error"] = error
        reached_success = state not in ("queued", "failed")
        flipped = False
        if not ok and not reached_success and attempts >= max_attempts:
            updates[f"deliveries.{backend_id}.state"] = "failed"
            flipped = state == "queued"
            if device_id is not None:
                updates["pendingDeviceIds"] = ArrayRemove([device_id])
        transaction.update(ref, updates)
        return flipped

    return run_transaction(_txn)


def set_pending_device_ids(msg_id: str, device_ids: list[str]) -> None:
    _messages().document(msg_id).update({"pendingDeviceIds": device_ids})


def get_conversation(key: str) -> Conversation | None:
    snap = _conversations().document(key).get()
    if not snap.exists:
        return None
    return Conversation.model_validate({"convKey": key, **(snap.to_dict() or {})})
