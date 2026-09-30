of a `contact_request` alert."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

from app import devauth
from app.config import Settings
from app.db.firestore import get_db
from app.main import create_app
from app.store import alerts as alerts_store
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import contacts as contacts_store
from app.store import device_secrets as device_secrets_store
from app.store import devices as devices_store
from app.store import families as families_store
from app.store import users as users_store
from tests.conftest import up_topic
from tests.fake_transport import FakeBrokerClient
from tests.firebase_test_utils import auth_header

_HMAC_KEY = b"k" * 32


def _make_user(uid: str, alias: str, *, family_id: str | None = None) -> None:
    users_store.create_user(uid=uid, alias=alias, display_name=alias, family_id=family_id)


def _make_pager_device(device_id: str, owner_uid: str) -> None:
    devices_store.create_device(
        device_id=device_id,
        owner_uid=owner_uid,
        label="d",
        mqtt_username=device_id,
        mqtt_password_hash="x",
        auth_mode="password",
    )
    backends_store.create_backend(
        owner_uid, kind="pager", config={"deviceId": device_id}, enabled=True
    )


def _make_hmac_pager_device(device_id: str, owner_uid: str, *, key: bytes = _HMAC_KEY) -> bytes:
    devices_store.create_device(
        device_id=device_id,
        owner_uid=owner_uid,
        label="d",
        mqtt_username=device_id,
        mqtt_password_hash="x",
        auth_mode="hmac",
    )
    backends_store.create_backend(
        owner_uid, kind="pager", config={"deviceId": device_id}, enabled=True
    )
    device_secrets_store.create(device_id, hmac_key=key, mqtt_password_hash="y")
    return key


def _ingest() -> tuple[Ingest, FakeBrokerClient]:
    broker = FakeBrokerClient()
    return Ingest(broker), broker


def contact_req_payload(
    req_id: str, name: str, *, ph: str | None = None, ts: int | None = None
) -> bytes:
    ts = ts if ts is not None else int(time.time())
    obj: dict[str, object] = {
        "v": 1,
        "id": req_id,
        "ts": ts,
        "kind": "contact_req",
        "name": name,
        "ack": None,
    }
    if ph is not None:
        obj["ph"] = ph
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def _book_version(device_id: str) -> int:
    # `bookVersion` is written directly on `devices/{d}`; read it raw, since `Device` drops it.
    raw = get_db().collection("devices").document(device_id).get().to_dict() or {}
    return int(raw.get("bookVersion", 0))


    ingest, broker = _ingest()

    ingest.handle_up(up_topic("pgr-c-1"), contact_req_payload("u_c1", "Grandma", ph="+15551234567"))

    req = contacts_store.get_by_device_and_req("pgr-c-1", "u_c1")
    assert req is not None
    assert req.ownerUid == "student"














    ingest, _broker = _ingest()





    ingest, broker = _ingest()

        ingest.handle_up(
        )




def test_contact_req_from_unregistered_device_dropped():
    ingest, broker = _ingest()
    ingest.handle_up(up_topic("pgr-c-missing"), contact_req_payload("u_c7", "Nobody"))
    assert contacts_store.get_by_device_and_req("pgr-c-missing", "u_c7") is None
    assert broker.published == []


def test_contact_req_from_revoked_device_dropped():
    _make_user("student", "student")
    ingest, broker = _ingest()

    assert broker.published == []


def test_contact_req_name_too_long_is_dropped_as_malformed():
    _make_user("student", "student")
    ingest, _broker = _ingest()






def test_hmac_signed_contact_req_is_verified_then_stored():
    ingest, _broker = _ingest()

    payload = devauth.sign_json(
        key,
        topic,
        {
            "v": 1,
            "id": "u_c11",
            "ts": int(time.time()),
            "kind": "contact_req",
            "name": "Aunt",
            "ph": "+15553330000",
            "ack": None,
            "n": 1,
        },
    )
    ingest.handle_up(topic, payload)

    assert req is not None


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


def _make_family_admin(uid: str, alias: str, family_id: str) -> dict[str, str]:
    fb_auth.create_user(uid=uid, email=f"{uid}@example.com")
    users_store.create_user(
        uid=uid, alias=alias, display_name=alias, role="admin", family_id=family_id
    )
    fb_auth.set_custom_user_claims(uid, {"role": "admin", "fam": family_id})
    return auth_header(uid)


    ingest, _broker = _ingest()


def test_block_contact_request_alert_adds_blocked_number_and_marks_handled(client: TestClient):
    family = _make_family("Block1")
    headers = _make_family_admin("cadmin5", "cadmin5", family.id)

    resp = client.post(f"/api/family/alerts/{alert.id}/block", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "handled"
    updated_family = families_store.get_family(family.id)
    assert updated_family is not None
    assert "+15557770000" in updated_family.blockedNumbers


    family = _make_family("Dismiss1")
    headers = _make_family_admin("cadmin8", "cadmin8", family.id)

    resp = client.post(f"/api/family/alerts/{alert.id}/dismiss", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "dismissed"


    )
    )
    assert resp.status_code == 200, resp.text
