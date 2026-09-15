"""HTTP client for the MQTT broker's REST publish API, plus the webhook helpers.
`publish_down` is the one path to `/down` (docs/PROTOCOL.md §14.5)."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
from typing import Any

import httpx

from app import devauth, wirecbor
from app.config import Settings
from app.store import device_secrets as device_secrets_store
from app.store import devices as devices_store

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
            "payload": base64.b64encode(payload).decode("ascii"),
            "qos": qos,
            "retain": retain,
            "payload_encoding": "base64",
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

    def publish_down(self, device_id: str, obj: dict[str, Any]) -> bool:
        """Signs and publishes the down envelope `obj`; `False` (never raises) if it cannot."""
        device = devices_store.get_device(device_id)
        if device is None:
            logger.warning("publish_down: no such device %s", device_id)
            return False
        topic = f"pager/{device_id}/down"
        # Answer in the encoding of the device's last accepted inbound envelope; JSON if none.
        wire_encoding = device.wire or "json"
        if device.authMode == "hmac":
            secret = device_secrets_store.get(device_id)
            if secret is None:
                # Fails closed, as ingest does when a secret is missing.
                logger.error(
                    "publish_down: device %s is authMode=hmac with no "
                    "deviceSecrets row -- dropping (delivery stays queued)",
                    device_id,
                )
                return False
            n = device_secrets_store.next_down_n(device_id)
            signed_obj = {**obj, "n": n}
            if wire_encoding == "cbor":
                payload = devauth.sign_cbor(secret.hmacKey, topic, signed_obj)
            else:
                payload = devauth.sign_json(secret.hmacKey, topic, signed_obj)
        elif wire_encoding == "cbor":
            payload = wirecbor.encode(obj)
        else:
        return self.publish(topic, payload, qos=1, retain=False)

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
        if not isinstance(topic, str):
            return None
        payload_b64 = data.get("payload_b64")
        if payload_b64 is not None:
            # EMQX turns invalid UTF-8 in `payload` into U+FFFD, so `payload_b64` is authoritative;
            # malformed base64 is dropped, not retried on `payload`.
            if not isinstance(payload_b64, str):
                return None
            try:
                payload_bytes = base64.b64decode(payload_b64, validate=True)
            except (binascii.Error, ValueError):
                return None
        else:
            payload = data.get("payload")
            if isinstance(payload, str):
                payload_bytes = payload.encode("utf-8")
            elif isinstance(payload, (bytes, bytearray)):
                payload_bytes = bytes(payload)
            else:
                return None
        if not isinstance(qos, int) or isinstance(qos, bool):
            return None
        return topic, payload_bytes, qos
