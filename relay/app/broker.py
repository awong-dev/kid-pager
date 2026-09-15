"""HTTP client for the MQTT broker's REST publish API, plus the webhook helpers.

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger("relay.broker")

PUBLISH_TIMEOUT_S = 5.0
HEALTHCHECK_TIMEOUT_S = 3.0
WEBHOOK_KEY_HEADER = "X-Relay-Webhook-Key"


class BrokerClient:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.broker_api_url.rstrip("/")
        self._api_key = settings.broker_api_key
        self._api_secret = settings.broker_api_secret
        self._webhook_key = settings.webhook_key

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> bool:
        """POST to the broker's REST publish API; True on 2xx, never raises."""
        auth = (self._api_key, self._api_secret or "") if self._api_key else None
        body = {
            "topic": topic,
            "qos": qos,
            "retain": retain,
        }
        try:
            resp = httpx.post(
                f"{self._base_url}/publish", json=body, auth=auth, timeout=PUBLISH_TIMEOUT_S
            )
        except httpx.HTTPError:
            logger.warning("broker publish request failed (topic=%s)", topic, exc_info=True)
            return False
        if 200 <= resp.status_code < 300:
            return True
        logger.warning(
            "broker publish rejected (topic=%s status=%s body=%r)",
            topic,
            resp.status_code,
            resp.text[:200],
        )
        return False

    def healthcheck(self) -> bool:
        """`GET {base}/status`; a network failure or non-2xx means "unreachable"."""
        try:
            resp = httpx.get(f"{self._base_url}/status", timeout=HEALTHCHECK_TIMEOUT_S)
        except httpx.HTTPError:
            return False
        return 200 <= resp.status_code < 300

    def verify_webhook(self, request: Any) -> bool:
        """Constant-time check of the shared-secret header."""
        provided = request.headers.get(WEBHOOK_KEY_HEADER, "")
        expected = self._webhook_key or ""
        # A blank configured key must never match a blank header: fail closed.
        if not expected:
            return False
        return hmac.compare_digest(provided, expected)

    @staticmethod
    def parse_webhook(body: bytes) -> tuple[str, bytes, int] | None:
        """Parse the broker rule-engine POST body into `(topic, payload, qos)`, or None."""
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        topic = data.get("topic")
        qos = data.get("qos", 0)
            return None
        else:
        if not isinstance(qos, int) or isinstance(qos, bool):
            return None
        return topic, payload_bytes, qos
