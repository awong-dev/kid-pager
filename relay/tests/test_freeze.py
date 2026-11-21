"""Freezing a family for a move."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

from app.ingest import Ingest
from app.main import create_app
from app.routing import Routing
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import devices as devices_store
from app.store import families as families_store
from app.store import users as users_store
from tests.bridge_world import SIM, make_pager_device, pair_bridge
from tests.bridge_world import World as _World
from tests.conftest import up_topic
from tests.fake_transport import FakeBrokerClient
from tests.firebase_test_utils import auth_header
from tests.test_admin import make_settings


@pytest.fixture
def broker() -> FakeBrokerClient:
    return FakeBrokerClient()


@pytest.fixture
def client(broker: FakeBrokerClient) -> Iterator[TestClient]:
    app = create_app(settings=make_settings(), broker_client=broker)
    with TestClient(app) as c:
        yield c


def _account(uid: str, role: str, fam: str | None) -> dict[str, str]:
    fb_auth.create_user(uid=uid, email=f"{uid}@example.com")
    users_store.create_user(
        uid=uid, alias=uid, display_name=uid, role=role, family_id=fam
    )  # type: ignore[arg-type]
    fb_auth.set_custom_user_claims(uid, {"role": role, **({"fam": fam} if fam else {})})
    return auth_header(uid)


@pytest.fixture
def fam() -> str:
    return families_store.create_family(name="Moving", created_by="root").id


def test_freeze_routes_super_only_and_toggle(client: TestClient, fam: str):
    super_h = _account("fz-super", "super", "default")
    admin_h = _account("fz-admin", "admin", fam)
    assert (
        client.post(f"/api/admin/families/{fam}/freeze", headers=admin_h).status_code
        == 403
    )
    assert families_store.is_frozen(fam) is False

    r = client.post(f"/api/admin/families/{fam}/freeze", headers=super_h)
    assert r.status_code == 200 and r.json()["frozen"] is True
    assert families_store.is_frozen(fam) is True
    r = client.post(f"/api/admin/families/{fam}/unfreeze", headers=super_h)
    assert r.status_code == 200 and r.json()["frozen"] is False
    assert families_store.is_frozen(fam) is False
    assert (
        client.post("/api/admin/families/nope/freeze", headers=super_h).status_code
        == 404
    )


def _pager_world(fam: str) -> None:
    users_store.create_user(uid="kid", alias="kid", display_name="Kid", family_id=fam)
    users_store.create_user(uid="mom", alias="mom", display_name="Mom", family_id=fam)
    allow_store.set_edge("kid", "mom", message=True, locate=False)
    devices_store.create_device(
        device_id="pgr-fz",
        owner_uid="kid",
        label="d",
        mqtt_username="pgr-fz",
        mqtt_password_hash="x",
        auth_mode="password",
        family_id=fam,
    )
    backends_store.create_backend(
        "kid", kind="pager", config={"deviceId": "pgr-fz"}, enabled=True
    )


def _up(ingest: Ingest, mid: str, body: str = "hi") -> None:
    ingest.handle_up(
        up_topic("pgr-fz"),
        json.dumps(
            {
                "v": 1,
                "id": mid,
                "ts": int(time.time()),
                "from": "kid",
                "to": "mom",
                "body": body,
                "ack": None,
            }
        ).encode(),
    )


def _inbox_count() -> int:
    from app.db.firestore import get_db

    return len(list(get_db().collection("messages").stream()))


def test_pager_up_refused_while_frozen_then_normal(fam: str):
    _pager_world(fam)
    broker = FakeBrokerClient()
    ingest = Ingest(broker, Routing(broker))

    families_store.set_frozen(fam, True)
    _up(ingest, "u_frozen01")
    assert _inbox_count() == 0
    assert len(broker.published) == 1
    sent = json.loads(broker.published[0].payload)
    assert sent["from"] == "system" and sent["body"] == families_store.FROZEN_REPLY

    families_store.set_frozen(fam, False)
    before = len(broker.published)
    _up(ingest, "u_thawed01")
    assert _inbox_count() == 1
    assert all(
        json.loads(p.payload)["body"] != families_store.FROZEN_REPLY
        for p in broker.published[before:]
    )


def test_routing_send_raises_while_frozen(fam: str):
    _pager_world(fam)
    routing = Routing(FakeBrokerClient())
    families_store.set_frozen(fam, True)
    with pytest.raises(families_store.FamilyFrozen):
        routing.send(
            sender_uid="kid",
            recipient_alias="mom",
            kind="text",
            body="x",
            origin_backend_kind="webapp",
        )
    families_store.set_frozen(fam, False)
    res = routing.send(
        sender_uid="kid",
        recipient_alias="mom",
        kind="text",
        body="x",
        origin_backend_kind="webapp",
    )
    assert res.ok


def test_web_send_503_while_frozen(client: TestClient, fam: str):
    _pager_world(fam)
    fb_auth.create_user(uid="kid", email="kid@example.com")
    fb_auth.set_custom_user_claims("kid", {"role": "member", "fam": fam})
    h = auth_header("kid")
    families_store.set_frozen(fam, True)
    r = client.post(
        "/api/conversations/mom/messages", json={"body": "hello"}, headers=h
    )
    assert r.status_code == 503
    families_store.set_frozen(fam, False)
    r = client.post(
        "/api/conversations/mom/messages", json={"body": "hello"}, headers=h
    )
    assert r.status_code == 201, r.text


def test_bridge_events_error_while_frozen(client: TestClient, broker: FakeBrokerClient):
    fam_id = families_store.create_family(name="BF", created_by="root").id
    fb_auth.create_user(uid="adm", email="adm@example.com")
    users_store.create_user(
        uid="adm", alias="adm", display_name="adm", role="admin", family_id=fam_id
    )
    users_store.create_user(
        uid="kid", alias="kid", display_name="Kid", family_id=fam_id
    )
    w = _World(family_id=fam_id, admin_headers={})
    make_pager_device("pgr-br", "kid")
    _bridge, headers = pair_bridge(client, w, sim=SIM)

    def post() -> str:
        ev = {
            "id": f"e{time.time_ns()}",
            "source": "sms",
            "conversation": {"id": "c1", "isGroup": False},
            "sender": {"name": "Mom", "phone": "+12065550111"},
            "text": "hi",
            "ts": 1,
        }
        r = client.post("/bridge/events", json={"events": [ev]}, headers=headers)
        assert r.status_code == 200, r.text
        return r.json()["results"][0]["outcome"]

    families_store.set_frozen(fam_id, True)
    assert post() == "error"
    families_store.set_frozen(fam_id, False)
    assert post() != "error"


def test_send_to_a_frozen_family_is_a_per_recipient_reject(fam: str):
    """Traffic for a frozen family is refused per recipient; the rest of the send delivers."""
    _pager_world(fam)
    other = families_store.create_family(name="Other", created_by="root").id
    users_store.create_user(uid="zed", alias="zed", display_name="Zed", family_id=other)
    for a, b in (("kid", "zed"), ("zed", "kid"), ("zed", "mom"), ("mom", "zed")):
        allow_store.set_edge(a, b, message=True, locate=False)
    routing = Routing(FakeBrokerClient())
    families_store.set_frozen(other, True)

    res = routing.send(
        sender_uid="kid", recipient_alias="zed", kind="text", body="x", origin_backend_kind="webapp"
    )
    assert [(r.uid, r.reason) for r in res.rejected] == [("zed", "family_frozen")]
    assert res.messages == []

    # a broadcast: mom delivers, zed is rejected
    res = routing.send(
        sender_uid="kid", recipient_alias=None, kind="text", body="all", origin_backend_kind="webapp"
    )
    assert {m.recipientUid for m in res.messages} == {"mom"}
    assert [(r.uid, r.reason) for r in res.rejected] == [("zed", "family_frozen")]

    # a group: the frozen member is skipped, the others get it
    from app.store import conversations as conversations_store

    group = conversations_store.create_group(
        alias="grp", name="Grp", member_uids=["kid", "mom", "zed"], created_by="kid"
    )
    res = routing.send(
        sender_uid="kid", recipient_alias=group.alias, kind="text", body="g",
        origin_backend_kind="webapp",
    )
    assert {m.recipientUid for m in res.messages} == {"mom"}
    assert [(r.uid, r.reason) for r in res.rejected] == [("zed", "family_frozen")]

    # the frozen family's own member is the whole-request FamilyFrozen
    with pytest.raises(families_store.FamilyFrozen):
        routing.send(
            sender_uid="zed", recipient_alias="kid", kind="text", body="y",
            origin_backend_kind="webapp",
        )
    families_store.set_frozen(other, False)
    res = routing.send(
        sender_uid="kid", recipient_alias="zed", kind="text", body="x", origin_backend_kind="webapp"
    )
    assert res.ok
