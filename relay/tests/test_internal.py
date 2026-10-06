"""`/internal/tick`, `/internal/sweep` with a locally signed RS256 JWT and a faked cert source."""

from __future__ import annotations

import time
from collections.abc import Iterator

import google.auth.crypt as google_crypt
import google.auth.jwt as google_jwt
import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

from app import auth as auth_module
from app.config import Settings
from app.main import create_app
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import devices as devices_store
from app.store import users as users_store
from tests.fake_transport import FakeBrokerClient

AUDIENCE = "https://pager-relay-xxxxx-uc.a.run.app"
ALLOWED_EMAIL = "pager-scheduler@demo-pager.iam.gserviceaccount.com"
KEY_ID = "test-internal-key-1"


def _keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

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
    audience: str = AUDIENCE,
    email: str = ALLOWED_EMAIL,
    key: str = PRIVATE_KEY_PEM,
    key_id: str | None = KEY_ID,
    exp_offset: int = 300,
) -> bytes:
    now = int(time.time())
    payload = {
        "iss": "https://accounts.google.com",
        "aud": audience,
        "email": email,
        "iat": now,
        "exp": now + exp_offset,
        "sub": "1234567890",
    }
    signer = google_crypt.RSASigner.from_string(key, key_id=key_id)
    return google_jwt.encode(signer, payload)


def make_settings(**overrides: object) -> Settings:
    defaults = {
        "broker_api_url": "http://unused.invalid/api/v5",
        "broker_api_key": None,
        "broker_api_secret": None,
        "webhook_key": "test-webhook-key",
        "dev_mode": True,
        "google_cloud_project": None,
        "firestore_emulator_host": None,
        "firebase_auth_emulator_host": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(settings=make_settings(), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        yield c


def test_tick_not_found_replaced_by_401_when_dev_mode_off_and_no_token():
    app = create_app(settings=make_settings(dev_mode=False), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        resp = c.post("/internal/tick")
        assert resp.status_code == 401


def test_sweep_401_when_dev_mode_off_and_no_token():
    app = create_app(settings=make_settings(dev_mode=False), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        resp = c.post("/internal/sweep")
        assert resp.status_code == 401


def test_sweep_runs_and_reports_zero_deletions_with_nothing_stale(client: TestClient):
    """Route wiring: `POST /internal/sweep` returns all zeros on a fresh emulator."""
    resp = client.post("/internal/sweep")
    assert resp.status_code == 200
        "messagesDeleted": 0,
        "wireIdsDeleted": 0,
        "locationsDeleted": 0,
        "locWireIdsDeleted": 0,
        "locReqsDeleted": 0,
        "orphanedWireIdsDeleted": 0,
        "conversationsDeleted": 0,
        "gchatLinkCodesDeleted": 0,
        "alertsDeleted": 0,
        "batteryDeleted": 0,
    }


def test_tick_retries_queued_pager_deliveries_end_to_end():
    from tests.firebase_test_utils import auth_header

    fb_auth.create_user(uid="mom", email="mom-tick@example.com")
    users_store.create_user(uid="mom", alias="mom", display_name="mom")
    users_store.create_user(uid="kid", alias="kid", display_name="kid")
    allow_store.set_edge("mom", "kid", message=True, locate=True)
    allow_store.set_edge("kid", "mom", message=True, locate=True)
    devices_store.create_device(
        device_id="pgr-tick-int",
        owner_uid="kid",
        label="d",
        mqtt_username="pgr-tick-int",
        mqtt_password_hash="x",
        auth_mode="password",
    )
    backends_store.create_backend(
        "kid", kind="pager", config={"deviceId": "pgr-tick-int"}, enabled=True
    )

    # Builds its own app to keep a reference to the FakeBrokerClient;
    # TestClient does not expose `app.state`.
    broker = FakeBrokerClient()
    broker.fail_publish = True
    app = create_app(settings=make_settings(), broker_client=broker)
    with TestClient(app) as c:
        send_resp = c.post(
            "/api/conversations/kid/messages",
            json={"body": "hi"},
            headers=auth_header("mom"),
        )
        assert send_resp.status_code == 201

        broker.fail_publish = False
        tick_resp = c.post("/internal/tick")
        assert tick_resp.status_code == 200
        data = tick_resp.json()
        assert data["retriesAttempted"] == 1
        assert len(broker.published) == 1


@pytest.fixture(autouse=True)
def _oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("OIDC_ALLOWED_EMAILS", ALLOWED_EMAIL)


@pytest.fixture
def oidc_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Use the local `CERTS` instead of a network fetch.
    import app.routers.internal as internal_module

    real = auth_module.verify_internal_oidc_token

    def _verify_with_test_certs(token, *, audience, allowed_emails):
        return real(token, audience=audience, allowed_emails=allowed_emails, certs=CERTS)

    monkeypatch.setattr(internal_module, "verify_internal_oidc_token", _verify_with_test_certs)

    app = create_app(settings=make_settings(dev_mode=False), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        yield c


def _auth_headers(token: bytes) -> dict[str, str]:
    return {"Authorization": f"Bearer {token.decode('ascii')}"}


def test_tick_accepts_a_valid_oidc_token(oidc_client: TestClient):
    resp = oidc_client.post("/internal/tick", headers=_auth_headers(_sign()))
    assert resp.status_code == 200


def test_sweep_accepts_a_valid_oidc_token(oidc_client: TestClient):
    resp = oidc_client.post("/internal/sweep", headers=_auth_headers(_sign()))
    assert resp.status_code == 200


def test_internal_rejects_missing_bearer_token(oidc_client: TestClient):
    resp = oidc_client.post("/internal/tick")
    assert resp.status_code == 401


def test_internal_rejects_wrong_audience(oidc_client: TestClient):
    token = _sign(audience="https://some-other-service-uc.a.run.app")
    resp = oidc_client.post("/internal/tick", headers=_auth_headers(token))
    assert resp.status_code == 401


def test_internal_rejects_wrong_caller_email(oidc_client: TestClient):
    token = _sign(email="someone-else@demo-pager.iam.gserviceaccount.com")
    resp = oidc_client.post("/internal/tick", headers=_auth_headers(token))
    assert resp.status_code == 401


def test_internal_rejects_an_expired_token(oidc_client: TestClient):
    """Expiry is enforced (`clock_skew_in_seconds=30`, so -600 s is past it)."""
    token = _sign(exp_offset=-600)
    resp = oidc_client.post("/internal/tick", headers=_auth_headers(token))
    assert resp.status_code == 401


def test_internal_rejects_tampered_signature(oidc_client: TestClient):
    token = _sign()
    header_b64, payload_b64, sig_b64 = token.decode("ascii").split(".")
    idx = 5
    tampered_char = "A" if sig_b64[idx] != "A" else "B"
    tampered = f"{header_b64}.{payload_b64}.{sig_b64[:idx]}{tampered_char}{sig_b64[idx + 1:]}"
    resp = oidc_client.post(
        "/internal/tick", headers={"Authorization": f"Bearer {tampered}"}
    )
    assert resp.status_code == 401


def test_dev_mode_bypass_still_works_locally(client: TestClient):
    """`client` (dev_mode=True) needs no Authorization header."""
    resp = client.post("/internal/tick")
    assert resp.status_code == 200
