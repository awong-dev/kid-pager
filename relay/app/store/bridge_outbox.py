"""`bridges/{bridgeId}/outbox/{obId}`: items for the phone, `pending` -> `sent` | `failed` only."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter, Transaction
from pydantic import BaseModel, ConfigDict, Field

from app import alerts as alerts_module
from app.db.firestore import get_db, run_transaction
from app.ids import new_id
from app.store import bridges as bridges_store

logger = logging.getLogger("relay.bridge")

OutboxState = Literal["pending", "sent", "failed"]
OutboxKind = Literal["send", "inspect"]

LIST_LIMIT = 20


class OutboxItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    bridgeId: str | None = None
    kind: OutboxKind = "send"
    source: str | None = None
    to: dict = Field(default_factory=dict)
    text: str | None = None
    msgId: str | None = None
    bid: str | None = None
    replyHint: str | None = None
    state: OutboxState = "pending"
    createdAt: datetime | None = None
    ackedAt: datetime | None = None
    tier: int | None = None
    reason: str | None = None

    def for_phone(self) -> dict[str, Any]:
        """What `GET /bridge/outbox` shows the phone."""
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "to": self.to}
        if self.source:
            out["source"] = self.source
        if self.text is not None:
            out["text"] = self.text
        if self.replyHint:
            out["replyHint"] = self.replyHint
        return out


def _outbox(bridge_id: str):
    return get_db().collection("bridges").document(bridge_id).collection("outbox")


def _from_snap(bridge_id: str, snap) -> OutboxItem:
    return OutboxItem.model_validate({"id": snap.id, "bridgeId": bridge_id, **(snap.to_dict() or {})})


def get(bridge_id: str, ob_id: str) -> OutboxItem | None:
    snap = _outbox(bridge_id).document(ob_id).get()
    return _from_snap(bridge_id, snap) if snap.exists else None


def _push_fcm(bridge: bridges_store.Bridge) -> None:
    """A data push wakes the phone's outbox poll. Best-effort."""
    if not bridge.fcmToken:
        return
    try:
        alerts_module.get_fcm_client().send_data(
            [bridge.fcmToken], {"kind": "outbox", "bridgeId": bridge.id}
        )
    except Exception:
        logger.exception("bridge outbox fcm push failed bridge=%s", bridge.id)


def _create(bridge: bridges_store.Bridge, ob_id: str, data: dict[str, Any]) -> OutboxItem:
    """`create()` the row; on `AlreadyExists` return the existing row. FCM only after a new row."""
    try:
        _outbox(bridge.id).document(ob_id).create(
            {**data, "state": "pending", "createdAt": SERVER_TIMESTAMP, "ackedAt": None,
             "tier": None, "reason": None}
        )
    except AlreadyExists:
        existing = get(bridge.id, ob_id)
        assert existing is not None
        return existing
    _push_fcm(bridge)
    created = get(bridge.id, ob_id)
    assert created is not None
    return created


def enqueue_send(
    bridge: bridges_store.Bridge,
    msg_id: str,
    bid: str,
    *,
    source: str,
    to: dict[str, Any],
    text: str,
    reply_hint: str | None = None,
) -> OutboxItem:
    """Id `ob_<msgId>_<bid>`; a redeliver returns the existing row."""
    return _create(
        bridge,
        f"ob_{msg_id}_{bid}",
        {
            "kind": "send",
            "source": source,
            "to": to,
            "text": text,
            "msgId": msg_id,
            "bid": bid,
            "replyHint": reply_hint,
        },
    )


def enqueue_hint(
    bridge: bridges_store.Bridge,
    *,
    source: str,
    phone: str,
    text: str,
    wire_id: str,
    to_extra: dict[str, Any] | None = None,
) -> OutboxItem:
    """The `too_long` reply hint, id `ob_h_<wireId>`, so a retried batch does not text it twice."""
    return _create(
        bridge,
        f"ob_h_{wire_id}",
        {"kind": "send", "source": source, "to": {"phone": phone, **(to_extra or {})}, "text": text,
         "msgId": None, "bid": None, "replyHint": None},
    )


def enqueue_inspect(bridge: bridges_store.Bridge, link: str) -> OutboxItem:
    return _create(
        bridge,
        new_id("ob_"),
        {"kind": "inspect", "source": None, "to": {"link": link}, "text": None,
         "msgId": None, "bid": None, "replyHint": None},
    )


def _pending_snaps(bridge_id: str):
    return _outbox(bridge_id).where(filter=FieldFilter("state", "==", "pending")).stream()


def list_pending(bridge_id: str, limit: int = LIST_LIMIT) -> list[OutboxItem]:
    """Oldest first."""
    items = [_from_snap(bridge_id, s) for s in _pending_snaps(bridge_id)]
    items.sort(key=lambda i: i.createdAt or datetime.min.replace(tzinfo=UTC))
    return items[:limit]


def count_pending(bridge_id: str) -> int:
    return sum(1 for _ in _pending_snaps(bridge_id))


def ack(
    bridge_id: str, ob_id: str, state: Literal["sent", "failed"], reason: str | None, tier: int
) -> tuple[OutboxItem, bool] | None:
    """`pending -> sent|failed` in one transaction; returns the row and whether it moved."""
    ref = _outbox(bridge_id).document(ob_id)

    def _txn(transaction: Transaction) -> bool | None:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return None
        if (snap.to_dict() or {}).get("state") != "pending":
            return False
        transaction.update(
            ref, {"state": state, "reason": reason, "tier": tier, "ackedAt": SERVER_TIMESTAMP}
        )
        return True

    transitioned = run_transaction(_txn)
    if transitioned is None:
        return None
    item = get(bridge_id, ob_id)
    assert item is not None
    return item, transitioned


def fail_pending(bridge_id: str, reason: str) -> list[OutboxItem]:
    """Fails every pending row (unpair). Returns the rows that transitioned."""
    out: list[OutboxItem] = []
    for item in list_pending(bridge_id, limit=10_000):
        acked = ack(bridge_id, item.id, "failed", reason, 0)
        if acked is not None and acked[1]:
            out.append(acked[0])
    return out




def delete_acked_before(cutoff: datetime) -> int:
    deleted = 0
    return deleted
