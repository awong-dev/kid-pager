"""`families/{fid}/alerts/{id}` documents: create, list, decide and upsert helpers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


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


def _alerts(family_id: str):
    return get_db().collection("families").document(family_id).collection("alerts")


def create(family_id: str, alert: dict) -> str:
    """Writes a new alert (Firestore-assigned id, server `ts`) and returns its id."""
    ref = _alerts(family_id).document()
    ref.set({**alert, "ts": SERVER_TIMESTAMP})
    return ref.id


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
