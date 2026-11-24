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


def test_sweep_runs_and_reports_zero_deletions_with_nothing_stale(client: TestClient):
    """Route wiring: `POST /internal/sweep` returns all zeros on a fresh emulator."""
    resp = client.post("/internal/sweep")
    assert resp.status_code == 200
    body = resp.json()
    assert body.pop("oldest") == {"messages": None, "locations": None}
    assert body and all(v == 0 for v in body.values())
    assert {"messagesDeleted", "locationsDeleted", "fedOutboxDeleted"} <= body.keys()


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


def _tampered_signature_token() -> bytes:
    token = _sign()
    header_b64, payload_b64, sig_b64 = token.decode("ascii").split(".")
    idx = 5
    tampered_char = "A" if sig_b64[idx] != "A" else "B"
    return f"{header_b64}.{payload_b64}.{sig_b64[:idx]}{tampered_char}{sig_b64[idx + 1:]}".encode("ascii")


@pytest.mark.parametrize("route", ["/internal/tick", "/internal/sweep"])
def test_a_valid_oidc_token_is_accepted(oidc_client: TestClient, route: str):
    resp = oidc_client.post(route, headers=_auth_headers(_sign()))
    assert resp.status_code == 200


@pytest.mark.parametrize("route", ["/internal/tick", "/internal/sweep"])
def test_no_token_is_401_when_dev_mode_is_off(oidc_client: TestClient, route: str):
    resp = oidc_client.post(route)
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "make_token",
    [
        pytest.param(lambda: _sign(audience="https://some-other-service-uc.a.run.app"), id="wrong_audience"),
        pytest.param(lambda: _sign(email="someone-else@demo-pager.iam.gserviceaccount.com"), id="wrong_caller_email"),
        # Expiry is enforced (`clock_skew_in_seconds=30`, so -600 s is past it).
        pytest.param(lambda: _sign(exp_offset=-600), id="expired"),
        pytest.param(_tampered_signature_token, id="tampered_signature"),
    ],
)
def test_a_bad_oidc_token_is_rejected(oidc_client: TestClient, make_token):
    resp = oidc_client.post("/internal/tick", headers=_auth_headers(make_token()))
    assert resp.status_code == 401


def _home_client(**overrides: object) -> TestClient:
    settings = make_settings(dev_mode=False, scheduler_mode="internal", **overrides)
    return TestClient(create_app(settings=settings, broker_client=FakeBrokerClient()))


def test_internal_accepts_the_webhook_key_under_the_internal_scheduler(monkeypatch):
    monkeypatch.setattr("app.scheduler.FIRST_TICK_DELAY_S", 3600.0)
    with _home_client() as c:
        assert c.post("/internal/tick", headers={"X-Relay-Webhook-Key": "test-webhook-key"}).status_code == 200
        assert c.post("/internal/sweep", headers={"X-Relay-Webhook-Key": "test-webhook-key"}).status_code == 200
        assert c.post("/internal/tick", headers={"X-Relay-Webhook-Key": "wrong"}).status_code == 401
        assert c.post("/internal/tick").status_code == 401


def test_internal_ignores_the_webhook_key_in_the_cloud():
    app = create_app(settings=make_settings(dev_mode=False), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        resp = c.post("/internal/tick", headers={"X-Relay-Webhook-Key": "test-webhook-key"})
        assert resp.status_code == 401


def test_internal_refuses_an_empty_webhook_key(monkeypatch):
    monkeypatch.setattr("app.scheduler.FIRST_TICK_DELAY_S", 3600.0)
    with _home_client(webhook_key="") as c:
        assert c.post("/internal/tick", headers={"X-Relay-Webhook-Key": ""}).status_code == 401
