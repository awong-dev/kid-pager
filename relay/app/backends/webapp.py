"""`webapp` backend: marks the delivery `sent` and best-effort pushes to browsers."""

from __future__ import annotations

import logging
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.backends.base import DeliverResult, LinkStep
from app.store import conversations as conversations_store
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


class WebappBackend:
    kind = "webapp"
    config_schema = WebappConfig

    def __init__(self, fcm_client: FCMClient | None = None) -> None:
        self._fcm = fcm_client or NullFCMClient()

    def deliver(self, msg: Message, delivery: Delivery, backend: BackendRow) -> DeliverResult:
        try:
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
        try:
        except Exception:
            # The alert doc already exists; one admin's failure must not skip the rest.
            logger.exception("FCM alert push failed for user %s", user.uid)
