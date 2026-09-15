"""`app/backends/gchat.py` and `POST /webhooks/gchat`, with a locally signed RS256 JWT and a faked
cert source; signature, audience, issuer and expiry checks run in `google-auth`."""

from __future__ import annotations

import time
from collections.abc import Iterator

import google.auth.crypt as google_crypt
import google.auth.jwt as google_jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.backends import gchat
from app.config import Settings
from app.main import create_app
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import messages as messages_store
from app.store import users as users_store
from tests.fake_transport import FakeBrokerClient

WEBHOOK_KEY = "test-webhook-key"
AUDIENCE = "123456789012"
KEY_ID = "test-key-1"


def _keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv_pem, pub_pem


PRIVATE_KEY_PEM, PUBLIC_KEY_PEM = _keypair()
CERTS = {KEY_ID: PUBLIC_KEY_PEM}


def _sign(
    *,
    issuer: str = gchat.CHAT_ISSUER,
    audience: str = AUDIENCE,
    key: str = PRIVATE_KEY_PEM,
    key_id: str | None = KEY_ID,
    exp_offset: int = 300,
) -> bytes:
    now = int(time.time())
    payload = {"iss": issuer, "aud": audience, "iat": now, "exp": now + exp_offset, "sub": "users/1"}
    signer = google_crypt.RSASigner.from_string(key, key_id=key_id)
    return google_jwt.encode(signer, payload)


def test_verify_chat_bearer_token_accepts_a_validly_signed_jwt():
    token = _sign()
    claims = gchat.verify_chat_bearer_token(token, audience=AUDIENCE, certs=CERTS)
    assert claims["iss"] == gchat.CHAT_ISSUER
    assert claims["aud"] == AUDIENCE


def test_verify_chat_bearer_token_rejects_tampered_signature():
    token = _sign()
    # Flip a character inside the signature segment, not the last one: the last base64 character
    # can carry unused padding bits and change nothing.
    header_b64, payload_b64, sig_b64 = token.decode("ascii").split(".")
    idx = 5
    tampered_char = "A" if sig_b64[idx] != "A" else "B"
    tampered = f"{header_b64}.{payload_b64}.{sig_b64[:idx]}{tampered_char}{sig_b64[idx + 1:]}"
    with pytest.raises(ValueError):
        gchat.verify_chat_bearer_token(tampered, audience=AUDIENCE, certs=CERTS)


def test_verify_chat_bearer_token_rejects_wrong_audience():
    token = _sign(audience="some-other-project")
    with pytest.raises(ValueError):
        gchat.verify_chat_bearer_token(token, audience=AUDIENCE, certs=CERTS)


def test_verify_chat_bearer_token_rejects_wrong_issuer():
    token = _sign(issuer="someone-else@example.com")
    with pytest.raises(ValueError):
        gchat.verify_chat_bearer_token(token, audience=AUDIENCE, certs=CERTS)


def test_verify_chat_bearer_token_rejects_expired_token():
    # Verification tolerates 30 s of clock skew; expire well past it.
    token = _sign(exp_offset=-120)
    with pytest.raises(ValueError):
        gchat.verify_chat_bearer_token(token, audience=AUDIENCE, certs=CERTS)


def test_verify_chat_bearer_token_rejects_key_signed_by_untrusted_key():
    other_priv, _ = _keypair()
    token = _sign(key=other_priv)  # signed by a key not in `CERTS`
    with pytest.raises(ValueError):
        gchat.verify_chat_bearer_token(token, audience=AUDIENCE, certs=CERTS)


def test_verify_chat_bearer_token_rejects_blank_audience_config():
    token = _sign()
    with pytest.raises(ValueError):
        gchat.verify_chat_bearer_token(token, audience="", certs=CERTS)


def make_settings(**overrides: object) -> Settings:
    defaults = {
        "broker_api_url": "http://unused.invalid/api/v5",
        "broker_api_key": None,
        "broker_api_secret": None,
        "webhook_key": WEBHOOK_KEY,
        "dev_mode": False,
        "google_cloud_project": None,
        "firestore_emulator_host": None,
        "firebase_auth_emulator_host": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def _gchat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCHAT_AUDIENCE", AUDIENCE)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Use the local `CERTS` instead of a network fetch.
    import app.routers.webhooks as webhooks_module

    real = gchat.verify_chat_bearer_token

    def _verify_with_test_certs(token, *, audience):
        return real(token, audience=audience, certs=CERTS)

    monkeypatch.setattr(webhooks_module.gchat_backend, "verify_chat_bearer_token", _verify_with_test_certs)

    settings = make_settings()
    app = create_app(settings=settings, broker_client=FakeBrokerClient(webhook_key=WEBHOOK_KEY))
    with TestClient(app) as c:
        yield c


def _auth_headers(token: bytes | None) -> dict[str, str]:
    if token is None:
        return {}
    return {"Authorization": f"Bearer {token.decode('ascii')}"}


DEFAULT_SENDER = "users/111111111111111111111"


def _chat_event(
    space: str, text: str, *, space_type: str = "DM", sender: str | None = DEFAULT_SENDER
) -> dict:
    message: dict = {"text": text, "space": {"name": space, "type": space_type}}
    if sender is not None:
        message["sender"] = {"name": sender}
    return {
        "type": "MESSAGE",
        "message": message,
        "space": {"name": space, "type": space_type},
    }


def _link_gchat(uid: str, alias: str) -> str:
    users_store.create_user(uid=uid, alias=alias, display_name=alias)
    backend = backends_store.create_backend(uid, kind="gchat", config={}, enabled=True)
    return backend.id


def test_webhook_missing_bearer_is_401(client: TestClient):
    resp = client.post("/webhooks/gchat", json=_chat_event("spaces/AAA", "hi"))
    assert resp.status_code == 401


def test_webhook_invalid_jwt_is_401(client: TestClient):
    resp = client.post(
        "/webhooks/gchat",
        json=_chat_event("spaces/AAA", "hi"),
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )
    assert resp.status_code == 401


def test_webhook_link_code_links_the_space(client: TestClient):
    bid = _link_gchat("mom", "mom")
    code = "424242"
    backends_store.set_gchat_link_code(code, "mom", bid, int(time.time()) + 600)
    backends_store.update_backend(
        "mom", bid, config={"linkCode": code, "linkCodeExpiresAt": int(time.time()) + 600}
    )

    resp = client.post(
        "/webhooks/gchat",
        json=_chat_event("spaces/AAAABBBB", f"/link {code}"),
        headers=_auth_headers(_sign()),
    )
    assert resp.status_code == 200

    row = backends_store.get_backend("mom", bid)
    assert row is not None
    assert row.config.get("space") == "spaces/AAAABBBB"
    assert row.verifiedAt is not None
    # The one-time link code is cleared out of `config` once used.
    assert "linkCode" not in row.config
    assert backends_store.get_by_gchat_space("AAAABBBB") == ("mom", bid, DEFAULT_SENDER)


def test_start_link_stashes_a_user_visible_link_code(client: TestClient):
    """`start_link()` writes the user-visible link code to `config`."""
    from app.backends.gchat import GChatBackend
    from app.store import users as users_store

    users_store.create_user(uid="dad", alias="dad", display_name="Dad")
    backend = backends_store.create_backend("dad", kind="gchat", config={}, enabled=True)
    user = users_store.get_user("dad")
    assert user is not None

    GChatBackend().start_link(user, backend)

    row = backends_store.get_backend("dad", backend.id)
    assert row is not None
    assert row.config.get("linkCode") is not None
    assert len(row.config["linkCode"]) == 6


def test_webhook_link_code_wrong_code_does_not_link(client: TestClient):
    bid = _link_gchat("mom", "mom")
    backends_store.set_gchat_link_code("111111", "mom", bid, int(time.time()) + 600)

    resp = client.post(
        "/webhooks/gchat",
        json=_chat_event("spaces/AAAABBBB", "/link 999999"),
        headers=_auth_headers(_sign()),
    )
    assert resp.status_code == 200
    row = backends_store.get_backend("mom", bid)
    assert row is not None
    assert row.config.get("space") is None


def test_webhook_link_rejects_group_space(client: TestClient):
    """`/link` in a group space is rejected; otherwise any member could send as the linked user."""
    bid = _link_gchat("mom", "mom")
    code = "555555"
    backends_store.set_gchat_link_code(code, "mom", bid, int(time.time()) + 600)
    backends_store.update_backend(
        "mom", bid, config={"linkCode": code, "linkCodeExpiresAt": int(time.time()) + 600}
    )

    resp = client.post(
        "/webhooks/gchat",
        json=_chat_event("spaces/GROUPSPACE", f"/link {code}", space_type="ROOM"),
        headers=_auth_headers(_sign()),
    )
    assert resp.status_code == 200
    assert "direct message" in (resp.json().get("text") or "")

    row = backends_store.get_backend("mom", bid)
    assert row is not None
    assert row.config.get("space") is None
    assert row.verifiedAt is None
    assert backends_store.get_by_gchat_space("GROUPSPACE") is None


def test_webhook_message_from_wrong_sender_in_linked_space_is_ignored(client: TestClient):
    """A message from a different sender in a linked space is not treated as the linked user."""
    users_store.create_user(uid="student", alias="student", display_name="Student")
    bid = _link_gchat("mom", "mom")
    code = "666666"
    backends_store.set_gchat_link_code(code, "mom", bid, int(time.time()) + 600)
    backends_store.update_backend(
        "mom", bid, config={"linkCode": code, "linkCodeExpiresAt": int(time.time()) + 600}
    )
    link_resp = client.post(
        "/webhooks/gchat",
        json=_chat_event("spaces/PINNEDSPACE", f"/link {code}", sender="users/AAA_owner"),
        headers=_auth_headers(_sign()),
    )
    assert link_resp.status_code == 200
    assert backends_store.get_by_gchat_space("PINNEDSPACE") == ("mom", bid, "users/AAA_owner")
    allow_store.set_edge("mom", "student", message=True, locate=True)

    resp = client.post(
        "/webhooks/gchat",
        json=_chat_event("spaces/PINNEDSPACE", "hello from an intruder", sender="users/BBB_intruder"),
        headers=_auth_headers(_sign()),
    )
    assert resp.status_code == 200
    assert messages_store.list_thread(messages_store.conv_key("mom", "student")) == []


def test_webhook_message_from_linked_space_routes_via_single_peer(client: TestClient):
    users_store.create_user(uid="student", alias="student", display_name="Student")
    bid = _link_gchat("mom", "mom")
    backends_store.set_gchat_space("SPACE1", "mom", bid)
    allow_store.set_edge("mom", "student", message=True, locate=True)

    resp = client.post(
        "/webhooks/gchat",
        json=_chat_event("spaces/SPACE1", "hello from chat"),
        headers=_auth_headers(_sign()),
    )
    assert resp.status_code == 200

    convo = messages_store.list_thread(messages_store.conv_key("mom", "student"))
    assert len(convo) == 1
    assert convo[0].body == "hello from chat"
    assert convo[0].originBackendKind == "gchat"
    assert convo[0].originBackendId == bid


def test_webhook_message_from_unlinked_space_is_ignored(client: TestClient):
    resp = client.post(
        "/webhooks/gchat",
        json=_chat_event("spaces/NEVERLINKED", "hello"),
        headers=_auth_headers(_sign()),
    )
    assert resp.status_code == 200
    assert resp.json().get("text")  # a "not linked" hint, not silently dropped


def test_webhook_self_addressed_reply_excludes_gchat_origin_backend(client: TestClient):
    bid = _link_gchat("mom", "mom")
    backends_store.set_gchat_space("SPACE2", "mom", bid)
    allow_store.set_edge("mom", "mom", message=True, locate=True)

    resp = client.post(
        "/webhooks/gchat",
        json=_chat_event("spaces/SPACE2", "@mom note to self"),
        headers=_auth_headers(_sign()),
    )
    assert resp.status_code == 200

    convo = messages_store.list_thread(messages_store.conv_key("mom", "mom"))
    assert len(convo) == 1
    kinds = {d.kind for d in convo[0].deliveries.values()}
    assert "gchat" not in kinds
    assert "webapp" in kinds
