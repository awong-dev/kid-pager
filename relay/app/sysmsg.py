"""Relay-originated `system` down text for a pager (PROTOCOL.md §4.2 case 3); not stored.
Ids are derived, never minted, so a retry re-publishes the same id (PROTOCOL.md §4.1 rule 7)."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from app import wire
from app.broker import BrokerClient

_broker: BrokerClient | None = None


def set_broker(client: BrokerClient | None) -> None:
    """Called at startup; the delivery wrapper and the tick have no request to carry the broker."""
    global _broker
    _broker = client


def get_broker() -> BrokerClient | None:
    return _broker


def derived_id(*parts: str) -> str:
    """`m_` + sha256(`:`-joined parts)[:8]; same shape as `ids.new_id("m_")`."""
    return "m_" + hashlib.sha256(":".join(parts).encode()).hexdigest()[:8]


def clip_bytes(text: str, max_bytes: int) -> str:
    """`text` cut at a code point boundary to at most `max_bytes` UTF-8 bytes."""
    while len(text.encode("utf-8")) > max_bytes:
        text = text[:-1]
    return text


def send_system_text(
    broker: BrokerClient, device_id: str, body: str, *, msg_id: str
) -> bool:
    """Publishes `body` as a `system` down text via `BrokerClient.publish_down`; False if not."""
    obj: dict[str, Any] = {
        "v": 1,
        "id": msg_id,
        "ts": int(time.time()),
        "from": wire.SYSTEM_ALIAS,
        "body": body,
        "ack": None,
    }
    return broker.publish_down(device_id, obj)
