"""`GET /healthz`: 200 when Firestore and the broker API are reachable."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.fake_transport import FakeBrokerClient


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
def broker() -> FakeBrokerClient:
    return FakeBrokerClient()


@pytest.fixture
def client(broker: FakeBrokerClient) -> Iterator[TestClient]:
    app = create_app(settings=make_settings(), broker_client=broker)
    with TestClient(app) as c:
        yield c


def test_healthz_ok_when_firestore_and_broker_both_reachable(client: TestClient):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "firestore": True, "broker": True}


@pytest.mark.parametrize("down", ["broker", "firestore"])
def test_healthz_503_when_a_dependency_is_unreachable(
    client: TestClient, broker: FakeBrokerClient, monkeypatch: pytest.MonkeyPatch, down: str
):
    if down == "broker":
        broker.healthy = False
    else:
        import app.main as main_module

        def _raise_firestore_error():
            raise RuntimeError("firestore unreachable (simulated)")

        monkeypatch.setattr(main_module, "get_db", _raise_firestore_error)
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["broker"] is (down != "broker")
    assert body["firestore"] is (down != "firestore")


def test_healthz_response_carries_a_request_id_header_and_echoes_the_callers(client: TestClient):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-Id")
    resp = client.get("/healthz", headers={"X-Request-Id": "caller-supplied-id-123"})
    assert resp.headers.get("X-Request-Id") == "caller-supplied-id-123"
