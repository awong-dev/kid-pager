"""`bridgeConversations/{bridgeId}_{ref}`, `ref = sha256(conversationId)[:16]`.
Android shortcut ids and notification keys may contain `/` and `|`, which break document ids."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter, Transaction
from pydantic import BaseModel, ConfigDict, Field

from app.db.firestore import get_db, run_transaction

ConvStatus = Literal["seen", "subscribed", "ignored", "paused"]

PEOPLE_MAX = 64
PREVIEW_MAX = 160


class ConvHealth(BaseModel):
    """Link health: whether the phone can still reach a subscribed conversation; epoch seconds."""

    model_config = ConfigDict(extra="ignore")

    state: Literal["ok", "failing", "unreachable"] = "ok"
    okAt: int | None = None
    # The first strike (a second one at least an hour later makes it `unreachable`).
    failAt: int | None = None
    reason: str | None = None
    # The active probe in flight (outbox id `ob_hc_...`) and when it was enqueued.
    probeId: str | None = None
    probeAt: int | None = None


class BridgeConversation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    ref: str
    bridgeId: str
    familyId: str
    ownerUid: str
    source: str
    conversationId: str
    title: str | None = None
    isGroup: bool = False
    link: str | None = None
    people: list[str] = Field(default_factory=list)
    lastPreview: str = ""
    lastAt: datetime | None = None
    firstSeenAt: datetime | None = None
    inspectedAt: datetime | None = None
    heldCount: int = 0
    status: ConvStatus = "seen"
    uid: str | None = None
    convKey: str | None = None
    pagerName: str | None = None
    customName: bool = False
    alertId: str | None = None
    health: ConvHealth | None = None


def conv_ref(conversation_id: str) -> str:
    return hashlib.sha256(conversation_id.encode()).hexdigest()[:16]


def row_id(bridge_id: str, conversation_id: str) -> str:
    return f"{bridge_id}_{conv_ref(conversation_id)}"


def _col():
    return get_db().collection("bridgeConversations")


def _from_snap(snap) -> BridgeConversation:
    data = snap.to_dict() or {}
    return BridgeConversation.model_validate(
        {"id": snap.id, "ref": conv_ref(data.get("conversationId", "")), **data}
    )


def get(bridge_id: str, conversation_id: str) -> BridgeConversation | None:
    snap = _col().document(row_id(bridge_id, conversation_id)).get()
    return _from_snap(snap) if snap.exists else None


def get_by_ref(bridge_id: str, ref: str) -> BridgeConversation | None:
    snap = _col().document(f"{bridge_id}_{ref}").get()
    return _from_snap(snap) if snap.exists else None


def count_created_since(bridge_id: str, since: datetime) -> int:
    """New rows of `bridge_id` first seen after `since`, filtered in Python (no composite index)."""
    query = _col().where(filter=FieldFilter("firstSeenAt", ">", since))
    return sum(1 for s in query.stream() if (s.to_dict() or {}).get("bridgeId") == bridge_id)


def upsert_seen(
    bridge: Any,
    *,
    source: str,
    conversation_id: str,
    title: str | None,
    is_group: bool,
    link: str | None,
    speaker: str | None,
    people: list[str],
    preview: str | None,
    inspected: bool = False,
) -> BridgeConversation:
    """Creates or updates the row in one transaction; never changes `status`."""
    rid = row_id(bridge.id, conversation_id)
    ref = _col().document(rid)

    def _txn(transaction: Transaction) -> None:
        snap = ref.get(transaction=transaction)
        data = (snap.to_dict() or {}) if snap.exists else {}
        merged = list(data.get("people") or [])
        for name in [speaker, *people]:
            if name and name not in merged and len(merged) < PEOPLE_MAX:
                merged.append(name)
        updates: dict[str, Any] = {"people": merged}
        # A subscribed or paused row keeps `isGroup`; `title` follows the phone only until a
        # parent renames it.
        locked = snap.exists and data.get("status") in ("subscribed", "paused")
        if title and not (locked and data.get("customName")):
            updates["title"] = title
        if link:
            updates["link"] = link
        if inspected:
            updates["inspectedAt"] = SERVER_TIMESTAMP
        if preview is not None:
            updates["lastPreview"] = preview[:PREVIEW_MAX]
            updates["lastAt"] = SERVER_TIMESTAMP
        if not locked:
            updates["isGroup"] = is_group
        if snap.exists:
            transaction.update(ref, updates)
        else:
            transaction.create(
                ref,
                {
                    "bridgeId": bridge.id,
                    "familyId": bridge.familyId,
                    "ownerUid": bridge.ownerUid,
                    "source": source,
                    "conversationId": conversation_id,
                    "title": title,
                    "link": link,
                    "lastPreview": "",
                    "lastAt": None,
                    "firstSeenAt": SERVER_TIMESTAMP,
                    "inspectedAt": None,
                    "heldCount": 0,
                    "status": "seen",
                    "uid": None,
                    "convKey": None,
                    "pagerName": None,
                    "customName": False,
                    "alertId": None,
                    **updates,
                },
            )

    run_transaction(_txn)
    fetched = get(bridge.id, conversation_id)
    assert fetched is not None
    return fetched


def list_for_owner(owner_uid: str, statuses: tuple[str, ...] | None = None) -> list[BridgeConversation]:
    query = _col().where(filter=FieldFilter("ownerUid", "==", owner_uid))
    rows = [_from_snap(s) for s in query.stream()]
    if statuses is not None:
        rows = [r for r in rows if r.status in statuses]
    rows.sort(key=lambda r: (r.lastAt or r.firstSeenAt or datetime.min.replace(tzinfo=r.firstSeenAt.tzinfo if r.firstSeenAt else None)), reverse=True)
    return rows


def list_for_bridge(bridge_id: str) -> list[BridgeConversation]:
    query = _col().where(filter=FieldFilter("bridgeId", "==", bridge_id))
    return [_from_snap(s) for s in query.stream()]


def set_status(row_id_: str, status: ConvStatus) -> None:
    _col().document(row_id_).update({"status": status})


def set_fields(row_id_: str, **fields: Any) -> None:
    _col().document(row_id_).update(fields)


STRIKE_GAP_S = 3600


def _health_txn(row_id_: str, fn) -> tuple[ConvHealth, ConvHealth] | None:
    """Transactional `health` update by `fn(old) -> new | None`; returns `(old, new)` or None."""
    ref = _col().document(row_id_)

    def _txn(transaction: Transaction):
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return None
        old = ConvHealth.model_validate((snap.to_dict() or {}).get("health") or {})
        new = fn(old)
        if new is None or new == old:
            return None
        transaction.update(ref, {"health": new.model_dump()})
        return old, new

    return run_transaction(_txn)


def record_ok(
    row_id_: str, now: int, *, min_interval_s: int = 0
) -> tuple[ConvHealth, ConvHealth] | None:
    """Marks the conversation reachable and clears strikes; `min_interval_s` skips recent writes."""

    def fn(old: ConvHealth) -> ConvHealth | None:
        if old.state == "ok" and old.okAt is not None and now - old.okAt < min_interval_s:
            return None
        return old.model_copy(
            update={"state": "ok", "okAt": now, "failAt": None, "reason": None}
        )

    return _health_txn(row_id_, fn)


def record_failing(
    row_id_: str, reason: str, now: int
) -> tuple[ConvHealth, ConvHealth] | None:
    """First failure: `failing`; a second at least `STRIKE_GAP_S` later: `unreachable`."""

    def fn(old: ConvHealth) -> ConvHealth | None:
        if old.state == "ok":
            return old.model_copy(update={"state": "failing", "failAt": now, "reason": reason})
        if old.state == "failing":
            if old.failAt is not None and now - old.failAt >= STRIKE_GAP_S:
                return old.model_copy(
                    update={"state": "unreachable", "failAt": now, "reason": reason}
                )
            return None
        return old.model_copy(update={"failAt": now, "reason": reason})

    return _health_txn(row_id_, fn)


def set_probe(row_id_: str, probe_id: str | None, now: int) -> None:
    """Marks (or with `None` clears) the active probe on the row."""
    _health_txn(
        row_id_,
        lambda old: old.model_copy(update={"probeId": probe_id, "probeAt": now if probe_id else None}),
    )


def list_subscribed() -> list[BridgeConversation]:
    """Every subscribed row on every bridge (one single-field query, no index)."""
    query = _col().where(filter=FieldFilter("status", "==", "subscribed"))
    return [_from_snap(s) for s in query.stream()]
