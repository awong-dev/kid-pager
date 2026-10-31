"""`users/{uid}/pushSubscriptions/{id}`: Web Push subscriptions, doc id = sha256(endpoint) hex.
The endpoint is a capability to notify that browser, so it stays out of ids and logs."""

from __future__ import annotations

import hashlib
from datetime import datetime

from google.cloud.firestore import SERVER_TIMESTAMP
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db


class PushSubscription(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    endpoint: str
    p256dh: str
    auth: str
    ua: str = ""
    createdAt: datetime | None = None


def subscription_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()


def _subs(uid: str):
    return get_db().collection("users").document(uid).collection("pushSubscriptions")


def upsert(uid: str, endpoint: str, p256dh: str, auth: str, ua: str = "") -> str:
    """Idempotent per endpoint: replaces the keys, keeps `createdAt`; returns the doc id."""
    sid = subscription_id(endpoint)
    ref = _subs(uid).document(sid)
    fields = {"endpoint": endpoint, "p256dh": p256dh, "auth": auth, "ua": ua}
    if not ref.get().exists:
        fields["createdAt"] = SERVER_TIMESTAMP
    ref.set(fields, merge=True)
    return sid


def remove(uid: str, sid: str) -> None:
    _subs(uid).document(sid).delete()


def remove_endpoint(uid: str, endpoint: str) -> None:
    remove(uid, subscription_id(endpoint))


def list_subscriptions(uid: str) -> list[PushSubscription]:
    return [
        PushSubscription.model_validate({"id": snap.id, **(snap.to_dict() or {})})
        for snap in _subs(uid).stream()
    ]
