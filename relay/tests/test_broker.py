"""`app/broker.py` binary safety in both directions: MQTT payloads (CBOR, HMAC signatures,
AES-GCM blobs; docs/PROTOCOL.md §3.1, §14) are not text."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from app.broker import BrokerClient
from app.config import Settings

# Not valid UTF-8; the other bytes are ordinary CBOR bytes.
BINARY_PAYLOAD = bytes([0xA3, 0xDD, 0x00, 0xFF, 0x7B, 0x22]) + "héllo".encode()
JSON_PAYLOAD = b'{"v":1,"id":"m_00000001"}'


def make_broker(**overrides: Any) -> BrokerClient:
    defaults: dict[str, Any] = {
        "broker_api_url": "http://broker.invalid/api/v5",
        "broker_api_key": "key",
        "broker_api_secret": "secret",
        "webhook_key": "test-webhook-key",
        "dev_mode": True,
        "google_cloud_project": None,
        "firestore_emulator_host": None,
        "firebase_auth_emulator_host": None,
    }
    defaults.update(overrides)
    return BrokerClient(Settings(**defaults))


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Captures the JSON body of every `httpx.post` the broker client makes."""
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        calls.append({"url": url, **kwargs})
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


@pytest.mark.parametrize("payload", [BINARY_PAYLOAD, JSON_PAYLOAD, b""])
def test_publish_sends_base64_for_every_payload(sent: list[dict[str, Any]], payload: bytes):
    # One code path: base64 is always used, not only for non-text.
    assert make_broker().publish("pager/pgr-0001/down", payload, qos=1, retain=False) is True
    body = sent[0]["json"]
    assert body["payload_encoding"] == "base64"
    assert base64.b64decode(body["payload"], validate=True) == payload
    assert body["topic"] == "pager/pgr-0001/down"
    assert body["qos"] == 1 and body["retain"] is False


def _transport_error(url: str, **kwargs: Any) -> httpx.Response:
    raise httpx.ConnectError("nope", request=httpx.Request("POST", url))


def _rejection(url: str, **kwargs: Any) -> httpx.Response:
    return httpx.Response(400, text="bad", request=httpx.Request("POST", url))


@pytest.mark.parametrize("fake_post", [_transport_error, _rejection], ids=["transport_error", "rejection"])
def test_publish_returns_false_on_failure(monkeypatch: pytest.MonkeyPatch, fake_post: Any):
    monkeypatch.setattr(httpx, "post", fake_post)
    assert make_broker().publish("pager/pgr-0001/down", BINARY_PAYLOAD, 1, False) is False


def emqx_body(topic: str, payload: bytes, qos: int = 1) -> bytes:
    """What EMQX POSTs for `"body": "${.}"` with the `base64_encode(payload) as payload_b64` rule
    SQL: the lossy `payload` and the exact `payload_b64`."""
    return json.dumps(
        {
            "topic": topic,
            "payload": payload.decode("utf-8", "replace"),
            "payload_b64": base64.b64encode(payload).decode("ascii"),
            "qos": qos,
            "clientid": "pgr-0001",
        }
    ).encode("utf-8")


@pytest.mark.parametrize("payload", [BINARY_PAYLOAD, JSON_PAYLOAD, b""])
def test_parse_webhook_recovers_exact_bytes(payload: bytes):
    parsed = BrokerClient.parse_webhook(emqx_body("pager/pgr-0001/up", payload))
    assert parsed == ("pager/pgr-0001/up", payload, 1)


def test_parse_webhook_falls_back_to_plain_payload():
    # A broker whose rule SQL lacks payload_b64 keeps working for text envelopes.
    body = json.dumps({"topic": "pager/pgr-0001/up", "payload": '{"v":1}', "qos": 1}).encode()
    assert BrokerClient.parse_webhook(body) == ("pager/pgr-0001/up", b'{"v":1}', 1)


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        json.dumps({"payload_b64": "AAA="}).encode(),  # no topic
        json.dumps({"topic": "pager/pgr-0001/up"}).encode(),  # no payload of any kind
        json.dumps({"topic": "t", "payload_b64": "!!!not base64!!!"}).encode(),
        json.dumps({"topic": "t", "payload_b64": "AAA=", "qos": "one"}).encode(),
    ],
)
def test_parse_webhook_rejects_malformed_bodies(body: bytes):
    # None means "2xx but dispatch nothing" (docs/PROTOCOL.md §3.4); never an exception.
    assert BrokerClient.parse_webhook(body) is None


def test_parse_webhook_does_not_fall_back_when_b64_is_broken():
    # The lossy `payload` is no substitute: wrong bytes would fail signature verification.
    body = json.dumps(
        {"topic": "pager/pgr-0001/up", "payload": '{"v":1}', "payload_b64": "%%%"}
    ).encode()
    assert BrokerClient.parse_webhook(body) is None
