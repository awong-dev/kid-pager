"""`kind` -> `Backend` implementation."""

from __future__ import annotations

from app.backends.base import Backend
from app.backends.bridge import BridgeBackend
from app.backends.gchat import ChatClient, GChatBackend
from app.backends.pager import PagerBackend
from app.backends.sms import SmsBackend
from app.backends.webapp import FCMClient, WebappBackend
from app.broker import BrokerClient


def build_registry(
    broker: BrokerClient,
    *,
    fcm_client: FCMClient | None = None,
    chat_client: ChatClient | None = None,
) -> dict[str, Backend]:
    return {
        "pager": PagerBackend(broker),
        "webapp": WebappBackend(fcm_client),
        # `chat_client` stays `None` (-> `NullChatClient`) in dev/test.
        "gchat": GChatBackend(chat_client),
        # bridge-only; the row lives on an external.
        "sms": SmsBackend(),
        # a subscribed Google Chat conversation; the row lives on an external.
        "bridge": BridgeBackend(),
    }
