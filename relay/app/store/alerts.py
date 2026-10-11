"""`families/{fid}/alerts/{id}` documents: create, list, decide and upsert helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter, Transaction
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db, run_transaction

# A decided approval alert stays decided for this long, then a refused send reopens it.
APPROVAL_REOPEN_AFTER = timedelta(hours=24)
ApprovalOutcome = Literal["created", "open", "reopened", "declined"]

AlertStatus = Literal["open", "handled", "dismissed"]


class Alert(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    kind: AlertKind
    status: AlertStatus
    ts: datetime | None = None
    subjectUid: str | None = None
    subjectAlias: str | None = None
    peerUid: str | None = None
    peerAlias: str | None = None
    peerName: str | None = None
    peerPhone: str | None = None
    preview: str = ""
    heldBody: str | None = None
    convKey: str | None = None
    contactRequestKey: str | None = None
    decidedAt: datetime | None = None
    decidedBy: str | None = None
    # A held `sms_unknown` counts the waiting texts; `updatedAt` moves with each one.
    heldCount: int = 0
    updatedAt: datetime | None = None
    # A `chat_unknown` alert names the bridge conversation (`convRef`).
    bridgeId: str | None = None
    conversationId: str | None = None
    convRef: str | None = None
    convTitle: str | None = None
    isGroup: bool | None = None
    people: list[str] = []
    source: str | None = None


def _alerts(family_id: str):
    return get_db().collection("families").document(family_id).collection("alerts")


def create(family_id: str, alert: dict) -> str:
    """Writes a new alert (Firestore-assigned id, server `ts`) and returns its id."""
    ref = _alerts(family_id).document()
    ref.set({**alert, "ts": SERVER_TIMESTAMP})
    return ref.id


def find_open(
    family_id: str,
    kind: AlertKind,
    subject_uid: str | None,
    peer_phone: str | None,
    bridge_conv: tuple[str, str] | None = None,
) -> Alert | None:
    """The newest open alert of `kind` for `(subject_uid, peer_phone)`, or None."""
    query = _alerts(family_id).where(filter=FieldFilter("status", "==", "open"))
    found = [
        Alert.model_validate({"id": snap.id, **(snap.to_dict() or {})}) for snap in query.stream()
    ]
    found = [
        a
        for a in found
        if a.kind == kind
        and a.subjectUid == subject_uid
        and a.peerPhone == peer_phone
        and (bridge_conv is None or (a.bridgeId, a.conversationId) == bridge_conv)
    ]
    found.sort(key=lambda a: a.ts or datetime.min.replace(tzinfo=UTC), reverse=True)
    return found[0] if found else None


def update_fields(family_id: str, alert_id: str, fields: dict) -> None:
    """Merges `fields` plus a server `updatedAt` into an existing alert."""
    _alerts(family_id).document(alert_id).update({**fields, "updatedAt": SERVER_TIMESTAMP})


def get(family_id: str, alert_id: str) -> Alert | None:
    snap = _alerts(family_id).document(alert_id).get()
    if not snap.exists:
        return None
    return Alert.model_validate({"id": alert_id, **(snap.to_dict() or {})})


def list_alerts(family_id: str, status: Literal["open", "all"] = "open") -> list[Alert]:
    """`status="open"` lists open alerts only, `"all"` every alert; newest first."""
    query = _alerts(family_id)
    if status == "open":
        query = query.where(filter=FieldFilter("status", "==", "open"))
    alerts = [
        Alert.model_validate({"id": snap.id, **(snap.to_dict() or {})}) for snap in query.stream()
    ]
    alerts.sort(key=lambda a: a.ts or datetime.min.replace(tzinfo=UTC), reverse=True)
    return alerts


def decide(family_id: str, alert_id: str, status: Literal["handled", "dismissed"], by: str) -> Alert:
    """Sets status to handled/dismissed with `decidedAt`/`decidedBy`; `KeyError` if missing."""
    ref = _alerts(family_id).document(alert_id)
    if not ref.get().exists:
        raise KeyError(f"no such alert: {family_id!r}/{alert_id!r}")
    ref.update({"status": status, "decidedAt": SERVER_TIMESTAMP, "decidedBy": by})
    fetched = get(family_id, alert_id)
    assert fetched is not None
    return fetched


def approval_alert_id(owner_uid: str, peer_uid: str) -> str:
    return f"cr_{owner_uid}_{peer_uid}"


def upsert_approval(
    family_id: str, owner_uid: str, peer_uid: str, alert: dict, *, now: datetime | None = None
) -> ApprovalOutcome:
    """Upserts the `contact_request` alert `cr_{owner}_{peer}`; reopens after 24 h."""
    now = now or datetime.now(UTC)
    ref = _alerts(family_id).document(approval_alert_id(owner_uid, peer_uid))

    def _txn(transaction: Transaction) -> ApprovalOutcome:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            transaction.create(ref, {**alert, "ts": SERVER_TIMESTAMP})
            return "created"
        data = snap.to_dict() or {}
        if data.get("status") == "open":
            return "open"
        decided_at = data.get("decidedAt")
        if isinstance(decided_at, datetime) and now - decided_at < APPROVAL_REOPEN_AFTER:
            return "declined"
        transaction.set(ref, {**alert, "ts": SERVER_TIMESTAMP})
        return "reopened"

    return run_transaction(_txn)
