"""`webapp` backend: marks the delivery `sent` and best-effort pushes to browsers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.backends.base import DeliverResult, LinkStep
from app.store import conversations as conversations_store
from app.store import push_subscriptions as subs_store
from app.store import push_tokens as push_tokens_store
from app.store import users as users_store
from app.store.backends import Backend as BackendRow
from app.store.messages import Delivery, Message
from app.store.users import User

logger = logging.getLogger("relay.backends.webapp")

PREVIEW_MAX_CHARS = 120


class WebappConfig(BaseModel):
    """Every user gets an implicit `webapp` backend; there is nothing to configure."""

    model_config = ConfigDict(extra="ignore")


class FCMClient(Protocol):
    """Stubbable stand-in for `firebase_admin.messaging`."""

    def send_data(self, tokens: list[str], data: dict[str, str]) -> None: ...


class NullFCMClient:
    """Default FCM client: does nothing."""

    def send_data(self, tokens: list[str], data: dict[str, str]) -> None:
        return None


class BrowserPush:
    """One send to every browser a user registered (FCM and Web Push); each half best-effort."""

    def __init__(
        self,
        fcm_client: FCMClient | None = None,
        webpush_sender: push_webpush.BrowserPushSender | None = None,
    ) -> None:
        self._fcm = fcm_client or NullFCMClient()
        self._webpush = webpush_sender

    def send(self, uid: str, payload: Callable[[], dict[str, str]]) -> None:
        """`payload` is built only when the user has somewhere to send it."""
        webpush = self._webpush or push_webpush.get_sender()
        tokens = push_tokens_store.list_tokens(uid)
        subs = subs_store.list_subscriptions(uid) if webpush.configured else []
        if not tokens and not subs:
            return
        data = payload()
        if tokens:
            try:
                self._fcm.send_data(tokens, data)
            except Exception:
                logger.exception("FCM push failed for user %s", uid)
        if subs:
            try:
                webpush.send(uid, subs, data)
            except Exception:
                logger.exception("web push failed for user %s", uid)


class WebappBackend:
    kind = "webapp"
    config_schema = WebappConfig

    def __init__(self, fcm_client: FCMClient | None = None) -> None:
        self._fcm = fcm_client or NullFCMClient()
        self._push = BrowserPush(self._fcm)

    def _payload(self, msg: Message) -> dict[str, str]:
        # FCM data maps are string-only.
        # Group copies carry `senderAlias` (no user read); a lookup miss falls back to the uid.
        is_group = msg.groupMsgId is not None
        if is_group and msg.senderAlias is not None:
            sender_alias = msg.senderAlias
        else:
            sender = users_store.get_user(msg.senderUid)
            sender_alias = sender.alias if sender is not None else msg.senderUid

        # For a group copy, `title` is the group name and `url` names the group's alias.
        if is_group:
            conv = conversations_store.get_conversation(msg.convKey)
            title = conv.name if conv is not None and conv.name else sender_alias
            chat_alias = conv.alias if conv is not None and conv.alias else sender_alias
        else:
            title = sender_alias
            chat_alias = sender_alias

        data = {
            "kind": "message",
            "convKey": msg.convKey,
            "id": msg.id,
            "senderUid": msg.senderUid,
            "senderAlias": sender_alias,
            "title": title,
            "body": (msg.body or "")[:PREVIEW_MAX_CHARS],
            "url": f"/chat/{chat_alias}",
        }
        if msg.groupMsgId is not None:
            # Collapse key: travels as a data field the service worker keys its
            # notification tag on. Absent on a DM push.
            data["groupMsgId"] = msg.groupMsgId
        return data

    def deliver(self, msg: Message, delivery: Delivery, backend: BackendRow) -> DeliverResult:
        try:
            self._push.send(msg.recipientUid, lambda: self._payload(msg))
        except Exception:
            # The message is already visible to the browser's Firestore listener, so a
            # push failure must not fail the delivery.
            logger.exception("FCM push failed for message %s", msg.id)
        return DeliverResult(ok=True, state="sent")

    def start_link(self, user: User, backend: BackendRow) -> LinkStep | None:
        return None

    def complete_link(self, backend: BackendRow, proof: str) -> bool:
        return True

    def render_state(self, delivery: Delivery) -> str:
        return delivery.state


ALERT_URL = "/family/alerts"
PUSH_BODY_MAX_CHARS = 300

_ALERT_TITLES = {
    "new_conversation": lambda a: f"New chat: @{a['subjectAlias']} ↔ @{a.get('peerAlias')}",
    "sms_unknown": lambda a: f"Text from an unknown number for @{a['subjectAlias']}",
    "contact_request": lambda a: f"Contact request from @{a['subjectAlias']}'s pager",
    "chat_unknown": lambda a: f"Google Chat for @{a['subjectAlias']}: {a.get('convTitle') or 'a conversation'}",
}


def push_alert(family_id: str, alert: dict, fcm_client: FCMClient | None = None) -> None:
    """Pushes an alert to the family's admins (FCM per token, Web Push per subscription)."""
    fcm = fcm_client or NullFCMClient()
    title_fn = _ALERT_TITLES.get(alert["kind"])
    if title_fn is None:
        raise ValueError(f"unknown alert kind: {alert['kind']!r}")
    data = {
        "kind": "alert",
        "alertKind": str(alert["kind"]),
        "id": str(alert["id"]),
        "title": title_fn(alert),
        # `pushBody` (held texts) carries the sender and the text; push-only, never stored.
        "body": (
            alert["pushBody"][:PUSH_BODY_MAX_CHARS]
            if alert.get("pushBody")
            else (alert.get("preview") or "")[:PREVIEW_MAX_CHARS]
        ),
        "url": ALERT_URL,
    }
    recipients = [
        u
        for u in users_store.list_users()
        if u.familyId == family_id and u.role in ("admin", "super") and u.notify.alerts is not False
    ]
    logger.info(
        "alert push kind=%s family=%s recipients=%d", alert["kind"], family_id, len(recipients)
    )
    push = BrowserPush(fcm)
    for user in recipients:
        try:
            push.send(user.uid, lambda: data)
        except Exception:
            # The alert doc already exists; one admin's failure must not skip the rest.
            logger.exception("FCM alert push failed for user %s", user.uid)
