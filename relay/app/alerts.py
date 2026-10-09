"""Alert writers: every alert goes through `create` so the push is never forgotten."""

from __future__ import annotations

from app.backends.webapp import FCMClient, NullFCMClient
from app.backends.webapp import push_alert as _push_alert
from app.store import alerts as alerts_store
from app.store import held_chat as held_chat_store
from app.store import held_sms as held_sms_store
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


def sms_held_upsert(family_id: str, target: User, phone: str, body: str) -> str:
    """One open `sms_unknown` alert per `(family, person, number)`; later texts update it."""
    count = held_sms_store.count_held(family_id, phone, target.uid)
    preview = body[:PREVIEW_MAX_CHARS]
    push_body = f"{phone} \u2192 @{target.alias}: {body}"
    existing = alerts_store.find_open(family_id, "sms_unknown", target.uid, phone)
    if existing is not None:
        alerts_store.update_fields(
            family_id, existing.id, {"preview": preview, "heldCount": count}
        )
        alert_id = existing.id
        pushed = {**existing.model_dump(), "preview": preview}
        _push_alert(
            family_id, {**pushed, "id": alert_id, "pushBody": push_body}, fcm_client=_fcm_client
        )
        return alert_id
    alert = _base_alert("sms_unknown")
    alert.update(
        {
            "status": "open",
            "subjectUid": target.uid,
            "subjectAlias": target.alias,
            "peerPhone": phone,
            "preview": preview,
            "heldBody": None,
            "heldCount": count,
            "updatedAt": None,
            "pushBody": push_body,
        }
    )
    # `pushBody` is push-only: stored alerts do not carry it.
    stored = {k: v for k, v in alert.items() if k != "pushBody"}
    alert_id = alerts_store.create(family_id, stored)
    _push_alert(family_id, {**alert, "id": alert_id}, fcm_client=_fcm_client)
    return alert_id


    alert = _base_alert("contact_request")
    alert.update(
        {
            "status": "open",
            "subjectUid": owner.uid,
            "subjectAlias": owner.alias,
        }
    )


def chat_held_upsert(family_id: str, target: User, row, body: str, sender_name: str) -> str:
    """One open `chat_unknown` alert per `(bridge, conversation)`; later texts update it."""
    count = held_chat_store.count_held(row.id)
    preview = body[:PREVIEW_MAX_CHARS]
    title = row.title or "a conversation"
    fields = {
        "preview": preview,
        "heldCount": count,
        "people": list(row.people),
        "convTitle": row.title,
        "isGroup": row.isGroup,
    }
    existing = alerts_store.find_open(
        family_id, "chat_unknown", target.uid, None, bridge_conv=(row.bridgeId, row.conversationId)
    )
    if existing is not None:
        alerts_store.update_fields(family_id, existing.id, fields)
        pushed = {**existing.model_dump(), **fields}
        _push_alert(
            family_id,
            {**pushed, "id": existing.id, "pushBody": push_body},
            fcm_client=_fcm_client,
        )
        return existing.id
    alert = _base_alert("chat_unknown")
    alert.update(
        {
            "status": "open",
            "subjectUid": target.uid,
            "subjectAlias": target.alias,
            **fields,
            "bridgeId": row.bridgeId,
            "conversationId": row.conversationId,
            "convRef": row.ref,
            "source": row.source,
            "updatedAt": None,
        }
    )
    alert_id = alerts_store.create(family_id, alert)
    _push_alert(
        family_id, {**alert, "id": alert_id, "pushBody": push_body}, fcm_client=_fcm_client
    )
    return alert_id
