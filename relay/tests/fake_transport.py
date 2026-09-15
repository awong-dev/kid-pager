"""A fake `app.broker.BrokerClient` that records publishes and never touches a socket."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from app.broker import BrokerClient

WEBHOOK_KEY = "test-webhook-key"


@dataclass
class PublishedMessage:
    topic: str
    payload: bytes
    qos: int
    retain: bool


class FakeBrokerClient:
    """Stand-in for `BrokerClient`; `fail_publish` makes `publish()` return False."""

    def __init__(self, *, webhook_key: str = WEBHOOK_KEY) -> None:
        self.published: list[PublishedMessage] = []
        self.fail_publish = False
        self.healthy = True
        self._webhook_key = webhook_key

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> bool:
        if self.fail_publish:
            return False
        self.published.append(PublishedMessage(topic, payload, qos, retain))
        return True

    def publish_down(self, device_id: str, obj: dict[str, Any]) -> bool:
        return BrokerClient.publish_down(self, device_id, obj)  # type: ignore[arg-type]

    def healthcheck(self) -> bool:
        return self.healthy

    def verify_webhook(self, request: Any) -> bool:
        import hmac

        provided = request.headers.get("X-Relay-Webhook-Key", "")
        return hmac.compare_digest(provided, self._webhook_key)

    @staticmethod
    def parse_webhook(body: bytes) -> tuple[str, bytes, int] | None:
        return BrokerClient.parse_webhook(body)


    def clear(self) -> None:
        self.published.clear()


def webhook_event(topic: str, payload: bytes, qos: int = 1) -> dict:
    """The JSON the EMQX HTTP action POSTs: `payload_b64` is exact, `payload` is EMQX's lossy
    rendering (invalid UTF-8 becomes U+FFFD)."""
    return {
        "topic": topic,
        "payload": payload.decode("utf-8", "replace"),
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "qos": qos,
    }


def webhook_body(topic: str, payload: bytes, qos: int = 1) -> bytes:
    return json.dumps(webhook_event(topic, payload, qos)).encode("utf-8")


def webhook_headers(webhook_key: str = WEBHOOK_KEY) -> dict[str, str]:
    return {"X-Relay-Webhook-Key": webhook_key}
