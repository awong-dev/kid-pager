"""Alert writers: every alert goes through `create` so the push is never forgotten."""

from __future__ import annotations

from app.backends.webapp import FCMClient, NullFCMClient
from app.backends.webapp import push_alert as _push_alert
from app.store import alerts as alerts_store
from app.store import users as users_store
from app.store.users import User

# The alert `preview` cap.
PREVIEW_MAX_CHARS = 120

_fcm_client: FCMClient = NullFCMClient()


def set_fcm_client(client: FCMClient) -> None:
    """Called once from `app/main.py`'s startup; tests pass a fake."""
    global _fcm_client
    _fcm_client = client


def get_fcm_client() -> FCMClient:
    return _fcm_client


def create(family_id: str, alert: dict) -> str:
    """`app/store/alerts.py`'s `create`, plus the push every alert must get."""
    alert_id = alerts_store.create(family_id, alert)
    _push_alert(family_id, {**alert, "id": alert_id}, fcm_client=_fcm_client)
    return alert_id


def _base_alert(kind: str) -> dict:
    return {
        "kind": kind,
        "subjectUid": None,
        "subjectAlias": None,
        "peerUid": None,
        "peerAlias": None,
        "peerName": None,
        "peerPhone": None,
        "preview": "",
        "heldBody": None,
        "convKey": None,
        "contactRequestKey": None,
        "decidedAt": None,
        "decidedBy": None,
    }


    """Fired when `routing.send()` creates a DM with no approving edge; `None` if no family."""
    if sender.familyId is None:
        return None
    peer_phone = recipient.phone if recipient.kind == "external" else None
    alert = _base_alert("new_conversation")
    alert.update(
        {
            "status": "open",
            "subjectUid": sender.uid,
            "subjectAlias": sender.alias,
            "peerUid": recipient.uid,
            "peerAlias": recipient.alias,
            "peerPhone": peer_phone,
            "convKey": conv_key,
        }
    )
    return create(sender.familyId, alert)


def sms_unknown(family_id: str, phone: str, target_uid: str | None, body: str) -> str:
    """An open alert for a device-direct text from a number the family has no contact for."""
    subject = users_store.get_user(target_uid) if target_uid is not None else None
    alert = _base_alert("sms_unknown")
    alert.update(
        {
            "status": "open",
            "subjectUid": subject.uid if subject is not None else None,
            "subjectAlias": subject.alias if subject is not None else None,
            "peerUid": None,
            "peerAlias": None,
            "peerName": None,
            "peerPhone": phone,
            "preview": body[:PREVIEW_MAX_CHARS],
            "heldBody": None,
        }
    )
    return create(family_id, alert)


    alert = _base_alert("contact_request")
    alert.update(
        {
            "status": "open",
            "subjectUid": owner.uid,
            "subjectAlias": owner.alias,
        }
    )
