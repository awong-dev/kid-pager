"""Tests for `app/store/rate_limits.py` and its call sites: /api/me/backends, /api/admin/* writes
and the per-IP cap on /webhooks/gchat."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

from app.config import Settings
from app.main import create_app
from app.store import rate_limits as rate_limits_store
from app.store import users as users_store
from tests.fake_transport import FakeBrokerClient
from tests.firebase_test_utils import auth_header


@pytest.mark.parametrize(
    ("limit", "calls"),
    [
        pytest.param(3, [(0, True), (0, True), (0, True), (0, False)], id="allows-up-to-the-limit-then-trips"),
        pytest.param(1, [(0, True), (0, False), (0, False)], id="rejected-call-is-not-counted"),
        pytest.param(1, [(0, True), (0, False), (61, True)], id="resets-after-the-window-elapses"),
    ],
)
def test_check_and_increment_sequence(limit: int, calls: list[tuple[int, bool]]):
    key = "unit-test-limit"
    now = 1_000_000.0
    for offset_s, allowed in calls:
        result = rate_limits_store.check_and_increment(
            key, limit=limit, window_s=60, now=now + offset_s
        )
        assert result is allowed


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


@pytest.fixture(autouse=True)
def _tight_backend_create_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_BACKEND_CREATE_LIMIT", "2")
    monkeypatch.setenv("RATE_LIMIT_BACKEND_CREATE_WINDOW_S", "3600")


def _make_user(uid: str, alias: str) -> dict[str, str]:
    fb_auth.create_user(uid=uid, email=f"{uid}@example.com")
    users_store.create_user(uid=uid, alias=alias, display_name=alias)
    return auth_header(uid)


def test_backend_create_trips_429_after_the_configured_limit(client: TestClient):
    headers = _make_user("rl1", "rl1")
    for i in range(2):
        resp = client.post(
            "/api/me/backends",
            json={"kind": "gchat", "config": {}, "enabled": False},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
    resp = client.post(
        "/api/me/backends",
        json={"kind": "gchat", "config": {}, "enabled": False},
        headers=headers,
    )
    assert resp.status_code == 429


def test_backend_create_rate_limit_is_per_user(client: TestClient):
    headers1 = _make_user("rl2", "rl2")
    headers2 = _make_user("rl3", "rl3")
    for i in range(2):
        resp = client.post(
            "/api/me/backends",
            json={"kind": "gchat", "config": {}, "enabled": False},
            headers=headers1,
        )
        assert resp.status_code == 200
    # A different user's own quota is untouched by the first user's calls.
    resp = client.post(
        "/api/me/backends",
        json={"kind": "gchat", "config": {}, "enabled": False},
        headers=headers2,
    )
    assert resp.status_code == 200


@pytest.fixture(autouse=True)
def _tight_admin_write_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ADMIN_WRITE_LIMIT", "2")
    monkeypatch.setenv("RATE_LIMIT_ADMIN_WRITE_WINDOW_S", "3600")


@pytest.fixture
def admin_headers() -> dict[str, str]:
    auth_user = fb_auth.create_user(email="rl-admin@example.com")
    users_store.create_user(
        uid=auth_user.uid, alias="rladmin", display_name="RL Admin", role="super"
    )
    fb_auth.set_custom_user_claims(auth_user.uid, {"role": "super", "fam": ""})
    return auth_header(auth_user.uid)


def test_admin_reads_are_not_rate_limited(client: TestClient, admin_headers: dict[str, str]):
    for _ in range(5):
        resp = client.get("/api/admin/settings", headers=admin_headers)
        assert resp.status_code == 200


def test_admin_write_limit_shared_across_routes_for_the_same_admin(
    client: TestClient, admin_headers: dict[str, str]
):
    current = client.get("/api/admin/settings", headers=admin_headers).json()
    resp1 = client.put("/api/admin/settings", json=current, headers=admin_headers)
    assert resp1.status_code == 200
    resp2 = client.put("/api/admin/allowlist", json={"entries": []}, headers=admin_headers)
    assert resp2.status_code == 200
    resp3 = client.put("/api/admin/settings", json=current, headers=admin_headers)
    assert resp3.status_code == 429


@pytest.fixture(autouse=True)
def _tight_webhook_ip_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_WEBHOOK_IP_LIMIT", "2")
    monkeypatch.setenv("RATE_LIMIT_WEBHOOK_IP_WINDOW_S", "3600")


def make_webhook_settings(**overrides: object) -> Settings:
    defaults = {
        "broker_api_url": "http://unused.invalid/api/v5",
        "broker_api_key": None,
        "broker_api_secret": None,
        "webhook_key": "test-webhook-key",
        "dev_mode": False,
        "google_cloud_project": None,
        "firestore_emulator_host": None,
        "firebase_auth_emulator_host": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def webhook_client() -> Iterator[TestClient]:
    app = create_app(settings=make_webhook_settings(), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        yield c


def test_gchat_webhook_ip_cap_trips_429_before_jwt_check(webhook_client: TestClient):
    for _ in range(2):
        resp = webhook_client.post("/webhooks/gchat", json={"type": "MESSAGE"})
        assert resp.status_code == 401
    resp = webhook_client.post("/webhooks/gchat", json={"type": "MESSAGE"})
    assert resp.status_code == 429


def test_webhook_ip_cap_keys_on_x_forwarded_for_not_the_proxy_peer(webhook_client: TestClient):
    """`request.client.host` is a shared Google front-end address; `_client_ip` keys on
    `X-Forwarded-For`'s originating entry instead."""
    body = {"type": "MESSAGE"}
    for _ in range(2):
        resp = webhook_client.post(
            "/webhooks/gchat", json=body, headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
        )
        assert resp.status_code == 401
    resp = webhook_client.post(
        "/webhooks/gchat", json=body, headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
    )
    assert resp.status_code == 429
    # A different originating IP behind the same proxy still has its budget.
    resp = webhook_client.post(
        "/webhooks/gchat", json=body, headers={"X-Forwarded-For": "198.51.100.4, 10.0.0.1"}
    )
    assert resp.status_code == 401
