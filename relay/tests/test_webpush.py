"""Standard Web Push for browsers alongside FCM for the bridge, against a fake `requests.Session`
push service; tests decrypt (RFC 8291, `http_ece`) what the relay sent."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import http_ece
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient
from py_vapid import Vapid02

from app import alerts as alerts_module
from app import homeinit, push_webpush
from app.backends.webapp import WebappBackend, push_alert
from app.main import create_app
from app.store import bridge_outbox
from app.store import bridges as bridges_store
from app.store import push_subscriptions as subs_store
from app.store import push_tokens as push_tokens_store
from app.store import users as users_store
from app.store.backends import Backend as BackendRow
from app.store.messages import Delivery, Message
from tests.bridge_world import (  # noqa: F401
    SIM,
    RecordingFCM,
    World,
    broker,
    fcm,
    make_settings,
    pair_bridge,
    world,
)

PRIVATE, PUBLIC = homeinit.vapid_keypair()
ENDPOINT = "https://push.example.test/send/abc123"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class Browser:
    """The browser half of a subscription: its ECDH key and auth secret."""

    endpoint: str = ENDPOINT
    key: ec.EllipticCurvePrivateKey = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))
    auth: bytes = field(default_factory=lambda: os.urandom(16))

    @property
    def p256dh(self) -> str:
        return _b64(
            self.key.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        )

    def subscribe(self, uid: str) -> str:
        return subs_store.upsert(uid, self.endpoint, self.p256dh, _b64(self.auth), "test-ua")

    def decrypt(self, body: bytes) -> dict:
        plain = http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm")
        return json.loads(plain)


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.reason = "x"
        self.text = ""
        self.headers: dict[str, str] = {}


class FakePushService:
    """Stands in for `requests.Session`: records POSTs, answers `status`."""

    def __init__(self, status: int = 201) -> None:
        self.status = status
        self.posts: list[tuple[str, bytes, dict[str, str]]] = []

    def post(self, url: str, data: bytes = b"", headers: dict | None = None, timeout=None, **_):
        # HTTP header names are case-insensitive; pywebpush sends `ttl`.
        self.posts.append((url, data, {k.lower(): v for k, v in (headers or {}).items()}))
        return _Resp(self.status)


class RecordingWebPush:
    configured = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []

    def send(self, uid, subs, data) -> None:
        self.calls.append((uid, [s.endpoint for s in subs], data))


PUBLIC_ADDR = "142.250.80.10"


@pytest.fixture(autouse=True)
def resolver() -> Iterator[dict[str, list[str]]]:
    """`push.example.test` stands in for a push service: `check_endpoint` resolves it to a public
    address unless a test sets another answer; hosts not in the map do not resolve."""
    answers: dict[str, list[str]] = {"push.example.test": [PUBLIC_ADDR]}

    def resolve(host: str) -> list[str]:
        if host not in answers:
            raise OSError(f"no such host {host}")
        return answers[host]

    push_webpush.set_resolver(resolve)
    yield answers
    push_webpush.set_resolver(None)


@pytest.fixture
def webpush() -> Iterator[RecordingWebPush]:
    fake = RecordingWebPush()
    previous = push_webpush.get_sender()
    push_webpush.set_sender(fake)
    yield fake
    push_webpush.set_sender(previous)


@pytest.fixture
def client(broker, fcm: RecordingFCM) -> Iterator[TestClient]:
    """VAPID configured."""
    settings = dataclasses.replace(
        make_settings(), webpush_vapid_private_key=PRIVATE, webpush_vapid_public_key=PUBLIC
    )
    with TestClient(create_app(settings=settings, broker_client=broker)) as c:
        alerts_module.set_fcm_client(fcm)
        yield c


def _sender(service: FakePushService) -> push_webpush.WebPushSender:
    return push_webpush.WebPushSender(PRIVATE, "mailto:ops@example.test", session=service)  # type: ignore[arg-type]


def test_vapid_public_key_route(client: TestClient):
    resp = client.get("/api/push/vapid-public-key")
    assert resp.status_code == 200
    assert resp.json() == {"publicKey": PUBLIC}


def test_vapid_public_key_is_503_when_not_configured(broker):
    with TestClient(create_app(settings=make_settings(), broker_client=broker)) as c:
        assert c.get("/api/push/vapid-public-key").status_code == 503
        assert isinstance(push_webpush.get_sender(), push_webpush.NullWebPushSender)


def test_put_upserts_by_endpoint_hash_and_delete_removes(client: TestClient, world: World):
    sub = {"endpoint": ENDPOINT, "keys": {"p256dh": "p1", "auth": "a1"}, "ua": "Chrome"}
    first = client.put("/api/me/push-subscriptions", json=sub, headers=world.admin_headers)
    assert first.status_code == 200, first.text
    sid = first.json()["id"]
    assert sid == hashlib.sha256(ENDPOINT.encode()).hexdigest()

    sub["keys"] = {"p256dh": "p2", "auth": "a2"}
    again = client.put("/api/me/push-subscriptions", json=sub, headers=world.admin_headers)
    assert again.json()["id"] == sid
    (row,) = subs_store.list_subscriptions("adm")
    assert (row.id, row.endpoint, row.p256dh, row.auth, row.ua) == (sid, ENDPOINT, "p2", "a2", "Chrome")
    assert row.createdAt is not None

    gone = client.delete(f"/api/me/push-subscriptions/{sid}", headers=world.admin_headers)
    assert gone.status_code == 200
    assert subs_store.list_subscriptions("adm") == []


def test_put_requires_auth_and_an_https_endpoint(client: TestClient, world: World):
    sub = {"endpoint": ENDPOINT, "keys": {"p256dh": "p", "auth": "a"}}
    assert client.put("/api/me/push-subscriptions", json=sub).status_code == 401
    sub["endpoint"] = "http://169.254.169.254/latest"
    resp = client.put("/api/me/push-subscriptions", json=sub, headers=world.admin_headers)
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "answer",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["192.168.1.10"],
        ["172.16.0.9"],
        ["169.254.169.254"],
        ["::1"],
        ["fd00::1"],
        ["fe80::1%eth0"],
        ["::ffff:10.0.0.1"],
        [PUBLIC_ADDR, "10.0.0.1"],  # one private answer is enough
    ],
)
def test_put_refuses_an_endpoint_that_resolves_private(
    client: TestClient, world: World, resolver: dict, answer: list[str]
):
    resolver["push.example.test"] = answer
    sub = {"endpoint": ENDPOINT, "keys": {"p256dh": "p", "auth": "a"}}
    resp = client.put("/api/me/push-subscriptions", json=sub, headers=world.admin_headers)
    assert resp.status_code == 422, answer
    assert subs_store.list_subscriptions("adm") == []


@pytest.mark.parametrize(
    "endpoint",
    ["https://127.0.0.1/x", "https://[::1]/x", "https://169.254.169.254/latest", "https://nowhere.test/x"],
)
def test_put_refuses_private_literals_and_unresolvable_hosts(client: TestClient, world: World, endpoint: str):
    sub = {"endpoint": endpoint, "keys": {"p256dh": "p", "auth": "a"}}
    resp = client.put("/api/me/push-subscriptions", json=sub, headers=world.admin_headers)
    assert resp.status_code == 422, endpoint


def test_put_accepts_a_public_answer(client: TestClient, world: World, resolver: dict):
    resolver["push.example.test"] = [PUBLIC_ADDR, "2607:f8b0:4004:c1b::5f"]
    sub = {"endpoint": ENDPOINT, "keys": {"p256dh": "p", "auth": "a"}}
    resp = client.put("/api/me/push-subscriptions", json=sub, headers=world.admin_headers)
    assert resp.status_code == 200


def test_send_rechecks_the_endpoint_and_skips_a_private_one(resolver: dict):
    """The address can change after PUT: the send checks again, skips the
    row and keeps it (DNS can change back)."""
    Browser().subscribe("bob")
    resolver["push.example.test"] = ["10.0.0.7"]
    service = FakePushService(201)
    _sender(service).send("bob", subs_store.list_subscriptions("bob"), {"kind": "message"})
    assert service.posts == []
    assert len(subs_store.list_subscriptions("bob")) == 1


def test_getaddrinfo_is_the_default_resolver(monkeypatch: pytest.MonkeyPatch):
    push_webpush.set_resolver(None)
    seen: list[str] = []

    def fake_getaddrinfo(host, port, *a, **kw):
        seen.append(host)
        return [(2, 1, 6, "", ("10.9.9.9", port))]

    monkeypatch.setattr(push_webpush.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(push_webpush.EndpointRefused):
        push_webpush.check_endpoint(ENDPOINT)
    assert seen == ["push.example.test"]


def test_send_success_encrypts_the_payload_contract():
    browser = Browser()
    browser.subscribe("bob")
    service = FakePushService(201)
    data = {"kind": "message", "title": "@al", "body": "hi", "url": "/chat/al"}
    _sender(service).send("bob", subs_store.list_subscriptions("bob"), data)

    ((url, body, headers),) = service.posts
    assert url == ENDPOINT
    assert headers["ttl"] == "14400"
    assert headers["urgency"] == "high"
    assert headers["content-encoding"] == "aes128gcm"
    scheme, _, rest = headers["authorization"].partition(" ")
    assert scheme == "vapid"
    params = dict(p.strip().split("=", 1) for p in rest.split(","))
    assert params["k"] == PUBLIC
    head, jwt_body, sig = params["t"].split(".")
    vapid_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _unb64(PUBLIC))
    raw_sig = _unb64(sig)
    vapid_pub.verify(
        encode_dss_signature(int.from_bytes(raw_sig[:32]), int.from_bytes(raw_sig[32:])),
        f"{head}.{jwt_body}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )
    payload = json.loads(_unb64(jwt_body))
    assert payload["aud"] == "https://push.example.test"
    assert payload["sub"] == "mailto:ops@example.test"
    assert browser.decrypt(body) == data
    assert len(subs_store.list_subscriptions("bob")) == 1


@pytest.mark.parametrize("status", [404, 410])
def test_gone_subscription_is_removed(status: int):
    Browser().subscribe("bob")
    keep = Browser(endpoint="https://push.example.test/send/other")
    keep.subscribe("bob")
    service = FakePushService(status)
    _sender(service).send("bob", subs_store.list_subscriptions("bob")[:1], {"kind": "message"})
    assert len(service.posts) == 1
    (left,) = subs_store.list_subscriptions("bob")
    assert left.endpoint != service.posts[0][0]


def test_other_failures_keep_the_subscription():
    Browser().subscribe("bob")
    service = FakePushService(500)
    _sender(service).send("bob", subs_store.list_subscriptions("bob"), {"kind": "message"})
    assert len(service.posts) == 1
    assert len(subs_store.list_subscriptions("bob")) == 1


def _message() -> Message:
    return Message.model_validate(
        {
            "id": "msg1",
            "seq": 1,
            "convKey": "alice_bob",
            "uids": ["alice", "bob"],
            "senderUid": "alice",
            "recipientUid": "bob",
            "kind": "text",
            "body": "hello",
            "ts": 1,
            "deliveries": {"be1": Delivery(kind="webapp", state="queued")},
        }
    )


def test_message_fans_out_to_both_stores(webpush: RecordingWebPush):
    users_store.create_user(uid="alice", alias="alice", display_name="Alice")
    push_tokens_store.add_token("bob", "tok_bob")
    Browser().subscribe("bob")
    fcm_fake = RecordingFCM()
    msg = _message()
    WebappBackend(fcm_client=fcm_fake).deliver(msg, msg.deliveries["be1"], BackendRow(id="be1", kind="webapp"))

    ((tokens, fcm_data),) = fcm_fake.calls
    ((uid, endpoints, wp_data),) = webpush.calls
    assert tokens == ["tok_bob"] and uid == "bob" and endpoints == [ENDPOINT]
    assert fcm_data == wp_data
    assert wp_data["kind"] == "message" and wp_data["url"] == "/chat/alice"


def test_message_with_only_a_subscription_reaches_web_push(webpush: RecordingWebPush):
    users_store.create_user(uid="alice", alias="alice", display_name="Alice")
    Browser().subscribe("bob")
    fcm_fake = RecordingFCM()
    msg = _message()
    WebappBackend(fcm_client=fcm_fake).deliver(msg, msg.deliveries["be1"], BackendRow(id="be1", kind="webapp"))
    assert fcm_fake.calls == []
    assert len(webpush.calls) == 1


def test_unconfigured_web_push_never_lists_subscriptions(monkeypatch: pytest.MonkeyPatch):
    push_webpush.set_sender(push_webpush.NullWebPushSender())

    def _boom(uid: str):
        raise AssertionError("listed subscriptions with Web Push unconfigured")

    monkeypatch.setattr(subs_store, "list_subscriptions", _boom)
    users_store.create_user(uid="alice", alias="alice", display_name="Alice")
    push_tokens_store.add_token("bob", "tok_bob")
    fcm_fake = RecordingFCM()
    msg = _message()
    WebappBackend(fcm_client=fcm_fake).deliver(msg, msg.deliveries["be1"], BackendRow(id="be1", kind="webapp"))
    assert len(fcm_fake.calls) == 1


def test_fcm_failure_does_not_cost_web_push(webpush: RecordingWebPush):
    class _Broken:
        def send_data(self, tokens, data):
            raise RuntimeError("fcm down")

    users_store.create_user(uid="alice", alias="alice", display_name="Alice")
    push_tokens_store.add_token("bob", "tok_bob")
    Browser().subscribe("bob")
    msg = _message()
    WebappBackend(fcm_client=_Broken()).deliver(msg, msg.deliveries["be1"], BackendRow(id="be1", kind="webapp"))
    assert len(webpush.calls) == 1


def test_alert_fans_out_to_both_stores(webpush: RecordingWebPush):
    users_store.create_user(uid="adm", alias="adm", display_name="adm", role="admin", family_id="fam1")
    push_tokens_store.add_token("adm", "tok-adm")
    Browser().subscribe("adm")
    fcm_fake = RecordingFCM()
    alert = {"kind": "sms_unknown", "id": "al1", "subjectAlias": "kid", "preview": "yo"}
    push_alert("fam1", alert, fcm_client=fcm_fake)
    ((tokens, fcm_data),) = fcm_fake.calls
    ((uid, _, wp_data),) = webpush.calls
    assert tokens == ["tok-adm"] and uid == "adm"
    assert fcm_data == wp_data and wp_data["kind"] == "alert" and wp_data["url"] == "/family/alerts"


def test_bridge_outbox_and_alerts_still_reach_the_fcm_client(
    client: TestClient, world: World, fcm: RecordingFCM, webpush: RecordingWebPush
):
    """The bridge phone's outbox wake is a native FCM push, never Web Push; an alert reaches the
    admin's FCM token and Web Push subscription."""
    Browser().subscribe("adm")
    bridge, _ = pair_bridge(client, world)
    bridges_store.touch(bridge.id, {}, "bridge-fcm-tok")
    bridge = bridges_store.get(bridge.id)
    assert bridge is not None
    fcm.calls.clear()
    bridge_outbox.enqueue_send(bridge, "m_1", "sms", source="sms", to={"phone": SIM}, text="x")
    assert fcm.calls == [(["bridge-fcm-tok"], {"kind": "outbox", "bridgeId": bridge.id})]
    assert webpush.calls == []

    alerts_module.sms_unknown(world.family_id, "+15550001111", None, "who is this")
    assert fcm.calls[-1][0] == ["tok-adm"] and fcm.calls[-1][1]["kind"] == "alert"
    ((uid, endpoints, data),) = webpush.calls
    assert uid == "adm" and endpoints == [ENDPOINT] and data == fcm.calls[-1][1]


def test_end_to_end_message_reaches_the_push_service(client: TestClient, world: World):
    """A real sender over the fake push service: a PUT subscription, then an alert, arrives
    decryptable."""
    browser = Browser()
    resp = client.put(
        "/api/me/push-subscriptions",
        json={"endpoint": ENDPOINT, "keys": {"p256dh": browser.p256dh, "auth": _b64(browser.auth)}},
        headers=world.admin_headers,
    )
    assert resp.status_code == 200
    sender = push_webpush.get_sender()
    assert isinstance(sender, push_webpush.WebPushSender)
    service = FakePushService(201)
    sender._session = service  # type: ignore[assignment]
    alerts_module.sms_unknown(world.family_id, "+15550001111", None, "who is this")
    ((url, body, _),) = service.posts
    assert url == ENDPOINT
    got = browser.decrypt(body)
    assert got["kind"] == "alert" and got["url"] == "/family/alerts" and got["body"] == "who is this"


def test_homeinit_generates_a_usable_vapid_pair_once(tmp_path: Path):
    path = tmp_path / "secrets.env"
    first = homeinit.ensure_secrets(path)
    private, public = first[homeinit.VAPID_PRIVATE], first[homeinit.VAPID_PUBLIC]
    raw_pub = _unb64(public)
    assert len(raw_pub) == 65 and raw_pub[0] == 4
    derived = Vapid02.from_string(private).public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    assert derived == raw_pub
    assert homeinit.ensure_secrets(path) == first
    assert path.read_text().count(homeinit.VAPID_PRIVATE) == 1
