"""Alert writers: every alert goes through `create` so the push is never forgotten."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

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


def new_conversation(
    sender: User, recipient: User, conv_key: str, body: str | None = None
) -> str | None:
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
            "peerName": recipient.displayName,
            "peerPhone": peer_phone,
            "preview": (
                body or f"@{sender.alias} started a chat with @{recipient.alias}"
            )[:PREVIEW_MAX_CHARS],
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


def approval_upsert(owner: User, peer: User) -> alerts_store.ApprovalOutcome:
    """The one `contact_request` alert per `(owner, peer)`; pushed on `created`/`reopened`."""
    if owner.familyId is None:
        return "open"
    alert = _base_alert("contact_request")
    alert.update(
        {
            "status": "open",
            "subjectUid": owner.uid,
            "subjectAlias": owner.alias,
            "peerUid": peer.uid,
            "peerAlias": peer.alias,
            "peerName": peer.displayName,
            "peerPhone": peer.phone if peer.kind == "external" else None,
            "preview": f"wants to text {peer.displayName}"[:PREVIEW_MAX_CHARS],
        }
    )
    outcome = alerts_store.upsert_approval(owner.familyId, owner.uid, peer.uid, alert)
    if outcome in ("created", "reopened"):
        _push_alert(
            owner.familyId,
            {**alert, "id": alerts_store.approval_alert_id(owner.uid, peer.uid)},
            fcm_client=_fcm_client,
        )
    return outcome


def system_notice(family_id: str, code: str, key: str, text: str, *, closed: bool = False) -> str:
    """The family's `system` alert `sys_<code>_<key>`, pushed to admins when new or changed."""
    alert_id = alerts_store.system_alert_id(code, key)
    alert = _base_alert("system")
    alert.update(
        {
            "status": "handled" if closed else "open",
            "preview": text[:PREVIEW_MAX_CHARS],
            "code": code,
            "key": key,
        }
    )
    outcome = alerts_store.upsert_system(
        family_id, alert_id, alert, status="handled" if closed else "open"
    )
    if outcome != "unchanged":
        _push_alert(family_id, {**alert, "id": alert_id}, fcm_client=_fcm_client)
    return alert_id


def resolve_system(family_id: str, code: str, key: str) -> bool:
    """Closes the open `sys_<code>_<key>` alert, if any."""
    return alerts_store.resolve_system(family_id, alerts_store.system_alert_id(code, key))


_SOURCE_LABELS = {"whatsapp": "WhatsApp", "gchat": "Google Chat", "gvoice": "Google Voice"}


def chat_held_upsert(family_id: str, target: User, row, body: str, sender_name: str) -> str:
    """One open `chat_unknown` alert per `(bridge, conversation)`; later texts update it."""
    count = held_chat_store.count_held(row.id)
    preview = body[:PREVIEW_MAX_CHARS]
    title = row.title or "a conversation"
    if row.isGroup:
        push_body = f"{title} ({sender_name}) \u2192 @{target.alias}: {body}"
    else:
        # A DM push names the app, not the sender twice.
        label = _SOURCE_LABELS.get(row.source, "Chat")
        push_body = f"{label}: {row.title or sender_name} \u2192 @{target.alias}: {body}"
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


def fed_contact(
    link, target: User, rid: str, alias: str, name: str
) -> alerts_store.ApprovalOutcome:
    """`<family>: <name> added <member> as a contact. Add them back?`; reopened after 24 h."""
    if target.familyId is None:
        return "open"
    remote = link.remote
    digest = hashlib.sha256(f"{remote.fedId}|{rid}|{target.uid}".encode()).hexdigest()[:24]
    alert_id = f"fc_{digest}"
    alert = _base_alert("fed_contact")
    alert.update(
        {
            "status": "open",
            "subjectUid": target.uid,
            "subjectAlias": target.alias,
            "peerAlias": alias,
            "peerName": name,
            "fedLinkId": link.id,
            "fedName": remote.name,
            "fedHost": remote.where,
            "fedRid": rid,
            "preview": f"{remote.name}: {name} added {target.displayName} as a contact. Add them back?"[
                :PREVIEW_MAX_CHARS
            ],
        }
    )
    outcome = alerts_store.upsert_keyed(
        target.familyId, alert_id, alert, reopen_after=alerts_store.APPROVAL_REOPEN_AFTER
    )
    if outcome in ("created", "reopened"):
        _push_alert(target.familyId, {**alert, "id": alert_id}, fcm_client=_fcm_client)
    return outcome


def fed_unknown(
    link, target: User, rid: str, *, now: datetime | None = None
) -> alerts_store.ApprovalOutcome:
    """A refused message from a person in a linked family with no contact; one alert per UTC day."""
    if target.familyId is None:
        return "open"
    now = now or datetime.now(UTC)
    remote = link.remote
    digest = hashlib.sha256(f"{remote.fedId}|{rid}|{target.uid}".encode()).hexdigest()[:24]
    alert_id = f"fu_{digest}_{now:%Y%m%d}"
    alert = _base_alert("fed_unknown")
    alert.update(
        {
            "status": "open",
            "subjectUid": target.uid,
            "subjectAlias": target.alias,
            "fedLinkId": link.id,
            "fedName": remote.name,
            "fedHost": remote.where,
            "fedRid": rid,
            "preview": f"A person in {remote.name} wrote to {target.displayName}; they are not a contact."[
                :PREVIEW_MAX_CHARS
            ],
        }
    )
    outcome = alerts_store.upsert_keyed(target.familyId, alert_id, alert, reopen_after=None, now=now)
    if outcome == "created":
        _push_alert(target.familyId, {**alert, "id": alert_id}, fcm_client=_fcm_client)
    return outcome


def fed_moved(
    family_id: str,
    *,
    link_id: str,
    name: str,
    old_host: str,
    new_host: str,
    epoch: int,
) -> alerts_store.ApprovalOutcome:
    """A linked family announced a new address and key; one alert per `(link, epoch)`."""
    alert_id = f"fm_{hashlib.sha256(f'{link_id}|{epoch}'.encode()).hexdigest()[:24]}"
    alert = _base_alert("fed_moved")
    alert.update(
        {
            "status": "open",
            "fedLinkId": link_id,
            "fedName": name,
            "fedHost": new_host,
            "fedOldHost": old_host,
            "preview": (
                f"{name} moved from {old_host} to {new_host}. "
                "Suspend the link if you did not expect this."
            )[:PREVIEW_MAX_CHARS],
        }
    )
    outcome = alerts_store.upsert_keyed(family_id, alert_id, alert, reopen_after=None)
    if outcome == "created":
        _push_alert(family_id, {**alert, "id": alert_id}, fcm_client=_fcm_client)
    return outcome


def fed_reauth(
    family_id: str, *, link_id: str, name: str, host: str, epoch: int
) -> alerts_store.ApprovalOutcome:
    """A linked family restored from a backup was re-linked; one alert per `(link, epoch)`."""
    alert_id = f"fr_{hashlib.sha256(f'{link_id}|{epoch}'.encode()).hexdigest()[:24]}"
    alert = _base_alert("fed_reauth")
    alert.update(
        {
            "status": "open",
            "fedLinkId": link_id,
            "fedName": name,
            "fedHost": host,
            "preview": f"{name} was restored from a backup and re-linked from {host}."[
                :PREVIEW_MAX_CHARS
            ],
        }
    )
    outcome = alerts_store.upsert_keyed(family_id, alert_id, alert, reopen_after=None)
    if outcome == "created":
        _push_alert(family_id, {**alert, "id": alert_id}, fcm_client=_fcm_client)
    return outcome
