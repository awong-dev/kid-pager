"""`app.devcfg` book/cfg envelope builders and push/ack/republish (PROTOCOL.md §3.1, §3.2)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

from app.config import Settings
from app.db.firestore import get_db
from app.ingest import Ingest
from app.main import create_app
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import contacts as contacts_store
from app.store import devices as devices_store
from app.store import users as users_store
from tests.conftest import (
    ack_payload,
    offline_status_payload,
    online_status_payload,
    status_topic,
    up_topic,
)
from tests.fake_transport import FakeBrokerClient


def _make_user(uid: str, alias: str, display_name: str | None = None) -> None:
    users_store.create_user(uid=uid, alias=alias, display_name=display_name or alias)


def _make_pager_device(
    device_id: str, owner_uid: str, *, default_to_uid: str | None = None
) -> None:
    devices_store.create_device(
        device_id=device_id,
        owner_uid=owner_uid,
        label="d",
        mqtt_username=device_id,
        mqtt_password_hash="x",
        default_to_uid=default_to_uid,
        auth_mode="password",
    )
    backends_store.create_backend(
        owner_uid, kind="pager", config={"deviceId": device_id}, enabled=True
    )


def _approve(owner_uid: str, contact_uid: str) -> None:
    allow_store.set_edge(owner_uid, contact_uid, message=True, locate=False)
    allow_store.set_edge(contact_uid, owner_uid, message=True, locate=False)


def _raw_device(device_id: str) -> dict:
    return get_db().collection("devices").document(device_id).get().to_dict() or {}


def _bump_book_version(device_id: str) -> int:
    return contacts_store.bump_book_version(device_id)


def test_build_book_basic_shape():
    _make_user("student1", "student1")
    _make_user("mom1", "mom1", "Mom")
    _make_pager_device("pgr-b-1", "student1", default_to_uid="mom1")
    _approve("student1", "mom1")
    _bump_book_version("pgr-b-1")

    obj = devcfg.build_book("pgr-b-1")

    assert obj["v"] == 1
    assert obj["kind"] == "book"
    assert obj["bv"] == 1
    assert obj["d"] == "mom1"
    assert obj["c"] == [{"a": "mom1", "n": "Mom", "t": "web"}]
    assert obj["ack"] is None
    assert obj["id"].startswith("m_")


def test_build_book_omits_d_when_no_default_recipient():
    _make_user("student2", "student2")
    _make_pager_device("pgr-b-2", "student2")

    obj = devcfg.build_book("pgr-b-2")

    assert "d" not in obj


def test_build_book_unregistered_device_raises():
    with pytest.raises(ValueError):
        devcfg.build_book("no-such-device")


    )
    _make_pager_device("pgr-b-3", "student3")

    obj = devcfg.build_book("pgr-b-3")



def test_build_book_caps_approved_contacts_at_ten():
    _make_user("student4", "student4")
    _make_pager_device("pgr-b-4", "student4")
    for i in range(12):
        uid = f"contact4_{i}"
        _make_user(uid, f"c4{i:02d}")
        _approve("student4", uid)

    obj = devcfg.build_book("pgr-b-4")

    assert len(obj["c"]) == 10


    _make_user("student5", "student5")
    _make_pager_device("pgr-b-5", "student5")



def test_push_book_publishes_and_stores_pending():
    _make_user("student8", "student8")
    _make_pager_device("pgr-b-8", "student8")
    broker = FakeBrokerClient()

    ok = devcfg.push_book("pgr-b-8", broker)

    assert ok is True
    assert len(broker.published) == 1
    sent = json.loads(broker.published[0].payload)
    assert sent["kind"] == "book"

    raw = _raw_device("pgr-b-8")
    assert raw["pendingBook"]["id"] == sent["id"]
    assert raw["pendingBook"]["acked"] is False


def test_push_book_unregistered_device_returns_false_without_publishing():
    broker = FakeBrokerClient()
    assert devcfg.push_book("no-such-device", broker) is False
    assert broker.published == []


def test_push_book_supersedes_older_unacked_book():
    _make_user("student9", "student9")
    _make_pager_device("pgr-b-9", "student9")
    broker = FakeBrokerClient()

    devcfg.push_book("pgr-b-9", broker)
    first_id = _raw_device("pgr-b-9")["pendingBook"]["id"]

    devcfg.push_book("pgr-b-9", broker)
    second_id = _raw_device("pgr-b-9")["pendingBook"]["id"]

    assert first_id != second_id
    assert devcfg.ack("pgr-b-9", first_id) is False
    assert devcfg.ack("pgr-b-9", second_id) is True


def test_ack_shown_marks_book_acked_idempotently():
    _make_user("student10", "student10")
    _make_pager_device("pgr-b-10", "student10")
    broker = FakeBrokerClient()
    devcfg.push_book("pgr-b-10", broker)
    msg_id = _raw_device("pgr-b-10")["pendingBook"]["id"]

    assert devcfg.ack("pgr-b-10", msg_id) is True
    assert _raw_device("pgr-b-10")["pendingBook"]["acked"] is True
    assert devcfg.ack("pgr-b-10", msg_id) is True


def test_ack_unknown_id_returns_false():
    _make_user("student11", "student11")
    _make_pager_device("pgr-b-11", "student11")
    assert devcfg.ack("pgr-b-11", "m_doesnotexist") is False


def test_republish_pending_skips_acked_book():
    _make_user("student12", "student12")
    _make_pager_device("pgr-b-12", "student12")
    broker = FakeBrokerClient()
    devcfg.push_book("pgr-b-12", broker)
    msg_id = _raw_device("pgr-b-12")["pendingBook"]["id"]
    devcfg.ack("pgr-b-12", msg_id)
    broker.clear()

    devcfg.republish_pending("pgr-b-12", broker)

    assert broker.published == []


def test_republish_pending_resends_unacked_book_and_cfg_with_same_id():
    _make_user("student13", "student13")
    _make_pager_device("pgr-b-13", "student13")
    broker = FakeBrokerClient()
    devcfg.push_book("pgr-b-13", broker)
    devcfg.push_cfg("pgr-b-13", {"auto": 10}, broker)
    book_id = _raw_device("pgr-b-13")["pendingBook"]["id"]
    cfg_id = _raw_device("pgr-b-13")["pendingCfg"]["id"]
    broker.clear()

    devcfg.republish_pending("pgr-b-13", broker)

    assert len(broker.published) == 2
    sent_ids = {json.loads(p.payload)["id"] for p in broker.published}
    assert sent_ids == {book_id, cfg_id}


def test_push_cfg_publishes_lock_and_stores_pending():
    _make_user("student14", "student14")
    _make_pager_device("pgr-b-14", "student14")
    broker = FakeBrokerClient()

    ok = devcfg.push_cfg("pgr-b-14", {"clear": True}, broker)

    assert ok is True
    sent = json.loads(broker.published[0].payload)
    assert sent["kind"] == "cfg"
    assert sent["cfg"] == {"lock": {"clear": True}}
    assert sent["ack"] is None

    raw = _raw_device("pgr-b-14")
    assert raw["pendingCfg"]["id"] == sent["id"]
    assert raw["pendingCfg"]["acked"] is False


def test_push_cfg_unregistered_device_returns_false():
    broker = FakeBrokerClient()
    assert devcfg.push_cfg("no-such-device", {"auto": 5}, broker) is False
    assert broker.published == []

def test_status_bv_behind_book_version_triggers_push_book():
    _make_user("student15", "student15")
    _make_pager_device("pgr-b-15", "student15")
    _bump_book_version("pgr-b-15")
    _bump_book_version("pgr-b-15")
    broker = FakeBrokerClient()
    ingest = Ingest(broker)

    ingest.handle_status(
        status_topic("pgr-b-15"), online_status_payload("s_00000001", bv=1)
    )

    books = [
        json.loads(p.payload) for p in broker.published if json.loads(p.payload).get("kind") == "book"
    ]
    assert len(books) == 1
    assert books[0]["bv"] == 2


def test_status_bv_caught_up_does_not_push_book():
    _make_user("student16", "student16")
    _make_pager_device("pgr-b-16", "student16")
    _bump_book_version("pgr-b-16")
    broker = FakeBrokerClient()
    ingest = Ingest(broker)

    ingest.handle_status(
        status_topic("pgr-b-16"), online_status_payload("s_00000002", bv=1)
    )

    assert broker.published == []


def test_up_ack_shown_for_book_id_is_handled_by_devcfg_not_dropped():
    _make_user("student17", "student17")
    _make_pager_device("pgr-b-17", "student17")
    broker = FakeBrokerClient()
    devcfg.push_book("pgr-b-17", broker)
    book_id = _raw_device("pgr-b-17")["pendingBook"]["id"]
    ingest = Ingest(broker)

    ingest.handle_up(up_topic("pgr-b-17"), ack_payload(book_id, "shown"))

    assert _raw_device("pgr-b-17")["pendingBook"]["acked"] is True


def test_online_edge_republishes_unacked_book():
    _make_user("student18", "student18")
    _make_pager_device("pgr-b-18", "student18")
    broker = FakeBrokerClient()
    devcfg.push_book("pgr-b-18", broker)
    book_id = _raw_device("pgr-b-18")["pendingBook"]["id"]
    ingest = Ingest(broker)

    ingest.handle_status(status_topic("pgr-b-18"), offline_status_payload("s_x"))
    broker.clear()
    ingest.handle_status(status_topic("pgr-b-18"), online_status_payload("s_00000003"))

    books = [
        json.loads(p.payload) for p in broker.published if json.loads(p.payload).get("kind") == "book"
    ]
    assert len(books) == 1
    assert books[0]["id"] == book_id


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


@pytest.fixture
def admin_headers() -> dict[str, str]:
    auth_user = fb_auth.create_user(email="devcfg-admin@example.com")
    users_store.create_user(
    )
    return auth_header(auth_user.uid)


def test_admin_push_cfg_endpoint(
    client: TestClient, admin_headers: dict[str, str], broker: FakeBrokerClient
):
    _make_user("student19", "student19")
    _make_pager_device("pgr-b-19", "student19")

    resp = client.post(
        "/api/admin/devices/pgr-b-19/cfg",
        json={"lock": {"clear": True, "auto": 5}},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    sent = json.loads(broker.published[0].payload)
    assert sent["kind"] == "cfg"
    assert sent["cfg"] == {"lock": {"clear": True, "auto": 5}}


def test_admin_push_cfg_unknown_device_404(client: TestClient, admin_headers: dict[str, str]):
    resp = client.post(
        "/api/admin/devices/no-such-device/cfg",
        json={"lock": {"clear": True}},
        headers=admin_headers,
    )
    assert resp.status_code == 404


def test_admin_push_cfg_requires_admin(client: TestClient):
    fb_auth.create_user(uid="nonadmin2", email="nonadmin2@example.com")
    users_store.create_user(uid="nonadmin2", alias="nonadmin2", display_name="NA")
    resp = client.post(
        "/api/admin/devices/pgr-b-x/cfg",
        json={"lock": {"clear": True}},
        headers=auth_header("nonadmin2"),
    )
    assert resp.status_code == 403


def test_approve_contact_publishes_book(
    client: TestClient, admin_headers: dict[str, str], broker: FakeBrokerClient
):
    _make_pager_device("pgr-b-20", "student20")
    ingest = Ingest(broker)
    ingest.handle_up(
        up_topic("pgr-b-20"),
        json.dumps(
            {
                "v": 1,
                "id": "u_b20",
                "ts": int(time.time()),
                "kind": "contact_req",
                "name": "Grandma",
                "ph": "+15550009999",
                "ack": None,
            },
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    broker.clear()

    resp = client.post(
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text

    books = [
        json.loads(p.payload) for p in broker.published if json.loads(p.payload).get("kind") == "book"
    ]
    assert len(books) == 1
