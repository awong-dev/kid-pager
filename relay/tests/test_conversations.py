"""`/api/conversations/*`: send (allow-list enforcement, 403/404 vs 201) and the read-receipt
endpoint."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

from app.config import Settings
from app.main import create_app
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import conversations as conversations_store
from app.store import devices as devices_store
from app.store import families as families_store
from app.store import messages as messages_store
from app.store import users as users_store
from tests.fake_transport import FakeBrokerClient
from tests.firebase_test_utils import auth_header


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


def _make_user(uid: str, alias: str) -> dict[str, str]:
    fb_auth.create_user(uid=uid, email=f"{uid}@example.com")
    users_store.create_user(uid=uid, alias=alias, display_name=alias)
    return auth_header(uid)


def _make_admin(uid: str, alias: str) -> dict[str, str]:
    fb_auth.create_user(uid=uid, email=f"{uid}@example.com")
    users_store.create_user(uid=uid, alias=alias, display_name=alias, role="super")
    fb_auth.set_custom_user_claims(uid, {"role": "super", "fam": ""})
    return auth_header(uid)


def _make_family_admin_in(uid: str, alias: str, family_id: str) -> dict[str, str]:
    """Group creation is `POST /api/family/groups`, gated on `require_family_admin`: these tests
    need a family admin (role `admin`, a `familyId`), not the global `super`."""
    fb_auth.create_user(uid=uid, email=f"{uid}@example.com")
    users_store.create_user(
        uid=uid, alias=alias, display_name=alias, role="admin", family_id=family_id
    )
    fb_auth.set_custom_user_claims(uid, {"role": "admin", "fam": family_id})
    return auth_header(uid)


def _make_family_member_in(uid: str, alias: str, family_id: str) -> dict[str, str]:
    fb_auth.create_user(uid=uid, email=f"{uid}@example.com")
    users_store.create_user(
        uid=uid, alias=alias, display_name=alias, role="member", family_id=family_id
    )
    fb_auth.set_custom_user_claims(uid, {"role": "member", "fam": family_id})
    return auth_header(uid)


def _make_pager_device(device_id: str, owner_uid: str) -> None:
    # A `password`-auth pager device needs no `deviceSecrets` row for `publish_down` to succeed.
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


def test_send_message_success_returns_201_with_id(client: TestClient):
    mom_headers = _make_user("mom", "mom")
    _make_user("kid", "kid")
    allow_store.set_edge("mom", "kid", message=True, locate=True)
    allow_store.set_edge("kid", "mom", message=True, locate=True)

    resp = client.post(
        "/api/conversations/kid/messages", json={"body": "pickup at 3"}, headers=mom_headers
    )
    assert resp.status_code == 201, resp.text
    msg_id = resp.json()["id"]
    msg = messages_store.get_message(msg_id)
    assert msg is not None
    assert msg.senderUid == "mom"
    assert msg.recipientUid == "kid"
    assert msg.body == "pickup at 3"


@pytest.mark.parametrize(
    ("recipient", "edge", "alias", "body", "authed", "status"),
    [
        pytest.param(False, False, "ghost", "hi", True, 404, id="unknown_alias"),
        pytest.param(True, False, "kid3", "hi", True, 403, id="disallowed_recipient"),
        pytest.param(True, True, "kid3", "\x00\x01", True, 400, id="empty_body_after_strip"),
        pytest.param(False, False, "kid", "hi", False, 401, id="requires_auth"),
    ],
)
def test_send_message_refusals(
    client: TestClient, recipient: bool, edge: bool, alias: str, body: str, authed: bool, status: int
):
    headers = _make_user("mom3", "mom3") if authed else {}
    if recipient:
        _make_user("kid3", "kid3")
    if edge:
        allow_store.set_edge("mom3", "kid3", message=True, locate=True)

    resp = client.post(f"/api/conversations/{alias}/messages", json={"body": body}, headers=headers)

    assert resp.status_code == status


def test_mark_read_sets_webapp_delivery_to_read_and_clears_unread_for_the_recipient(client: TestClient):
    mom_headers = _make_user("mom5", "mom5")
    kid_headers = _make_user("kid5", "kid5")
    allow_store.set_edge("mom5", "kid5", message=True, locate=True)
    allow_store.set_edge("kid5", "mom5", message=True, locate=True)

    resp = client.post(
        "/api/conversations/kid5/messages", json={"body": "hi"}, headers=mom_headers
    )
    msg_id = resp.json()["id"]

    conv_key = messages_store.conv_key("mom5", "kid5")
    conv_before = messages_store.get_conversation(conv_key)
    assert conv_before is not None
    assert conv_before.unread["kid5"] == 1

    # {alias} names the other party: kid5 (the recipient) posts to the conversation with mom5.
    read_resp = client.post(
        f"/api/conversations/mom5/messages/{msg_id}/read", headers=kid_headers
    )
    assert read_resp.status_code == 200, read_resp.text

    msg = messages_store.get_message(msg_id)
    webapp_bid = next(bid for bid, d in msg.deliveries.items() if d.kind == "webapp")
    assert msg.deliveries[webapp_bid].state == "read"
    conv_after = messages_store.get_conversation(conv_key)
    assert conv_after is not None
    assert conv_after.unread["kid5"] == 0


@pytest.mark.parametrize(
    ("alias", "reader"),
    [
        pytest.param("other5c", "kid", id="mismatched_alias"),
        pytest.param("kid5c", "stranger", id="non_recipient"),
    ],
)
def test_mark_read_by_the_wrong_alias_or_a_non_recipient_is_404(
    client: TestClient, alias: str, reader: str
):
    """The `{alias}` path segment must name this message's conversation; a read against another
    alias, or by someone who is not the recipient, must not succeed."""
    mom_headers = _make_user("mom5c", "mom5c")
    readers = {"kid": _make_user("kid5c", "kid5c"), "stranger": _make_user("stranger5c", "stranger5c")}
    _make_user("other5c", "other5c")
    allow_store.set_edge("mom5c", "kid5c", message=True, locate=True)
    allow_store.set_edge("kid5c", "mom5c", message=True, locate=True)
    allow_store.set_edge("other5c", "kid5c", message=True, locate=True)
    allow_store.set_edge("kid5c", "other5c", message=True, locate=True)

    resp = client.post(
        "/api/conversations/kid5c/messages", json={"body": "hi"}, headers=mom_headers
    )
    msg_id = resp.json()["id"]

    read_resp = client.post(
        f"/api/conversations/{alias}/messages/{msg_id}/read", headers=readers[reader]
    )
    assert read_resp.status_code == 404

    msg = messages_store.get_message(msg_id)
    webapp_bid = next(bid for bid, d in msg.deliveries.items() if d.kind == "webapp")
    assert msg.deliveries[webapp_bid].state != "read"


def _make_pager_device(device_id: str, owner_uid: str):
    # `auth_mode="password"`: no `deviceSecrets` row; only the loc_req down publish must succeed.
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


def test_locate_success_returns_202_with_request_id(client: TestClient):
    mom_headers = _make_user("mom7", "mom7")
    _make_user("kid7", "kid7")
    allow_store.set_edge("mom7", "kid7", message=True, locate=True)
    _make_pager_device("pgr-conv-1", "kid7")

    resp = client.post("/api/conversations/kid7/locate", headers=mom_headers)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["requestId"]
    assert body["cached"] is False


@pytest.mark.parametrize("case", ["edge_denies_locate", "no_allow_edge_at_all", "other_familys_admin"])
def test_locate_without_permission_is_403(client: TestClient, case: str):
    if case == "other_familys_admin":
        family_a = families_store.create_family(name="loc-fam-b1", created_by="root-uid")
        family_b = families_store.create_family(name="loc-fam-b2", created_by="root-uid")
        headers = _make_family_admin_in("locadm2", "locadm2", family_a.id)
        _make_family_member_in("lockid2", "lockid2", family_b.id)
        _make_pager_device("pgr-conv-fam-2", "lockid2")
        target = "lockid2"
    else:
        headers = _make_user("mom9", "mom9")
        _make_user("kid9", "kid9")
        target = "kid9"
        if case == "edge_denies_locate":
            # message allowed, locate explicitly denied.
            allow_store.set_edge("mom9", "kid9", message=True, locate=False)
            _make_pager_device("pgr-conv-2", "kid9")

    resp = client.post(f"/api/conversations/{target}/locate", headers=headers)

    assert resp.status_code == 403


@pytest.mark.parametrize(
    ("case", "status"),
    [
        pytest.param("unknown_alias", 404, id="unknown_alias"),
        pytest.param("target_without_device", 409, id="target_without_device"),
        pytest.param("owner_without_device_locating_self", 409, id="owner_without_device_locating_self"),
        pytest.param("no_auth", 401, id="requires_auth"),
    ],
)
def test_locate_refusals_other_than_403(client: TestClient, case: str, status: int):
    headers = {} if case == "no_auth" else _make_user("mom11", "mom11")
    target = "nobody"
    if case == "target_without_device":
        _make_user("kid11", "kid11")
        allow_store.set_edge("mom11", "kid11", message=True, locate=True)
        target = "kid11"
    elif case == "owner_without_device_locating_self":
        target = "mom11"
    elif case == "no_auth":
        target = "kid7"

    resp = client.post(f"/api/conversations/{target}/locate", headers=headers)

    assert resp.status_code == status


def test_owner_can_locate_own_device_without_allow_edge(client: TestClient):
    """No `locate` edge is needed for one's own device; docs/PROTOCOL.md §13.3 rules 5-6 still
    apply (a second call coalesces)."""
    kid_headers = _make_user("kid12", "kid12")
    _make_pager_device("pgr-conv-own-1", "kid12")

    resp = client.post("/api/conversations/kid12/locate", headers=kid_headers)
    assert resp.status_code == 202, resp.text
    first = resp.json()
    assert first["requestId"]
    assert first["cached"] is False

    resp2 = client.post("/api/conversations/kid12/locate", headers=kid_headers)
    assert resp2.status_code == 202, resp2.text
    assert resp2.json()["requestId"] == first["requestId"]


def test_admin_without_locate_edge_cannot_locate_someone_elses_device(client: TestClient):
    """`_make_admin` is the global `super` role, not a family admin: super gets `/locate` only
    through an edge."""
    admin_headers = _make_admin("adm13", "adm13")
    _make_user("kid13", "kid13")
    _make_pager_device("pgr-conv-own-2", "kid13")

    resp = client.post("/api/conversations/kid13/locate", headers=admin_headers)
    assert resp.status_code == 403


def test_family_admin_can_locate_family_members_device_without_edge(client: TestClient):
    """A family admin may `/locate` a member of their own family with no `locate` edge."""
    family = families_store.create_family(name="loc-fam-a", created_by="root-uid")
    admin_headers = _make_family_admin_in("locadm1", "locadm1", family.id)
    _make_family_member_in("lockid1", "lockid1", family.id)
    _make_pager_device("pgr-conv-fam-1", "lockid1")

    resp = client.post("/api/conversations/lockid1/locate", headers=admin_headers)
    assert resp.status_code == 202, resp.text


def test_send_group_message_and_mark_read(client: TestClient):
    family = families_store.create_family(name="gapi-fam5-family", created_by="root-uid")
    admin_headers = _make_family_admin_in("gapi-admin5", "gapi-admin5", family.id)
    kid_headers = _make_family_member_in("gapi-kid5", "gapi-kid5", family.id)
    _make_family_member_in("gapi-kid6", "gapi-kid6", family.id)

    created = client.post(
        "/api/family/groups",
        json={
            "name": "Family",
            "alias": "gapi-fam5",
            "memberUids": ["gapi-admin5", "gapi-kid5", "gapi-kid6"],
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text

    send_resp = client.post(
        "/api/conversations/gapi-fam5/messages", json={"body": "hi family"}, headers=admin_headers
    )
    assert send_resp.status_code == 201, send_resp.text
    msg_id = send_resp.json()["id"]

    msg = messages_store.get_message(msg_id)
    assert msg is not None
    assert msg.senderAlias == "gapi-admin5"
    assert msg.groupMsgId is not None

    read_resp = client.post(
        f"/api/conversations/gapi-fam5/messages/{msg_id}/read", headers=kid_headers
    )
    assert read_resp.status_code == 200, read_resp.text


def test_send_group_message_by_non_member_is_403(client: TestClient):
    family = families_store.create_family(name="gapi-fam6-family", created_by="root-uid")
    admin_headers = _make_family_admin_in("gapi-admin6", "gapi-admin6", family.id)
    _make_family_member_in("gapi-kid7", "gapi-kid7", family.id)
    outsider_headers = _make_user("gapi-outsider", "gapi-outsider")

    created = client.post(
        "/api/family/groups",
        json={
            "name": "Family",
            "alias": "gapi-fam6",
            "memberUids": ["gapi-admin6", "gapi-kid7"],
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text

    resp = client.post(
        "/api/conversations/gapi-fam6/messages", json={"body": "hi"}, headers=outsider_headers
    )
    assert resp.status_code == 403


def test_add_member_then_leave(client: TestClient):
    family = families_store.create_family(name="gapi-fam7-family", created_by="root-uid")
    admin_headers = _make_family_admin_in("gapi-admin7", "gapi-admin7", family.id)
    kid_headers = _make_family_member_in("gapi-kid8", "gapi-kid8", family.id)
    _make_user("gapi-newmem", "gapi-newmem")

    created = client.post(
        "/api/family/groups",
        json={
            "name": "Family",
            "alias": "gapi-fam7",
            "memberUids": ["gapi-admin7", "gapi-kid8"],
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    conv_key = created.json()["convKey"]

    add_resp = client.post(
        "/api/conversations/gapi-fam7/members", json={"uid": "gapi-newmem"}, headers=admin_headers
    )
    assert add_resp.status_code == 200, add_resp.text
    assert "gapi-newmem" in add_resp.json()["uids"]
    assert allow_store.is_message_allowed("gapi-newmem", "gapi-admin7")
    assert allow_store.is_message_allowed("gapi-admin7", "gapi-newmem")
    edge = allow_store.get_edge("gapi-newmem", "gapi-admin7")
    assert edge is not None and edge.message is True and edge.locate is False

    leave_resp = client.delete("/api/conversations/gapi-fam7/members/me", headers=kid_headers)
    assert leave_resp.status_code == 200, leave_resp.text
    assert "gapi-kid8" not in leave_resp.json()["uids"]

    conv = messages_store.get_conversation(conv_key)
    assert conv is not None
    assert "gapi-kid8" not in conv.uids
    assert conversations_store.is_member(conv_key, "gapi-newmem")


def test_add_member_by_non_member_is_403(client: TestClient):
    family = families_store.create_family(name="gapi-fam8-family", created_by="root-uid")
    admin_headers = _make_family_admin_in("gapi-admin8", "gapi-admin8", family.id)
    _make_family_member_in("gapi-kid9", "gapi-kid9", family.id)
    outsider_headers = _make_user("gapi-outsider2", "gapi-outsider2")
    _make_user("gapi-target", "gapi-target")

    created = client.post(
        "/api/family/groups",
        json={
            "name": "Family",
            "alias": "gapi-fam8",
            "memberUids": ["gapi-admin8", "gapi-kid9"],
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text

    resp = client.post(
        "/api/conversations/gapi-fam8/members",
        json={"uid": "gapi-target"},
        headers=outsider_headers,
    )
    assert resp.status_code == 403


def test_add_member_pushes_book_to_new_pager_member(client: TestClient):
    family = families_store.create_family(name="gbk-fam3-family", created_by="root-uid")
    admin_headers = _make_family_admin_in("gbk-admin3", "gbk-admin3", family.id)
    _make_family_member_in("gbk-kid3", "gbk-kid3", family.id)
    _make_user("gbk-newmem3", "gbk-newmem3")
    _make_pager_device("gbk-dev3", "gbk-newmem3")

    created = client.post(
        "/api/family/groups",
        json={"name": "Family", "alias": "gbk-fam3", "memberUids": ["gbk-admin3", "gbk-kid3"]},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text

    broker = client.app.state.broker
    broker.published.clear()

    add_resp = client.post(
        "/api/conversations/gbk-fam3/members",
        json={"uid": "gbk-newmem3"},
        headers=admin_headers,
    )
    assert add_resp.status_code == 200, add_resp.text

    pushes = [p for p in broker.published if p.topic == "pager/gbk-dev3/down"]
    assert len(pushes) == 1
    sent = json.loads(pushes[0].payload)
    assert sent["kind"] == "book"
    assert {"a": "gbk-fam3", "n": "Family", "t": "grp"} in sent["c"]


def test_leave_group_pushes_book_to_leaving_pager_member_only(client: TestClient):
    family = families_store.create_family(name="gbk-fam4-family", created_by="root-uid")
    admin_headers = _make_family_admin_in("gbk-admin4", "gbk-admin4", family.id)
    kid_headers = _make_family_member_in("gbk-kid4", "gbk-kid4", family.id)
    _make_pager_device("gbk-dev4-admin", "gbk-admin4")
    _make_pager_device("gbk-dev4-kid", "gbk-kid4")

    created = client.post(
        "/api/family/groups",
        json={"name": "Family", "alias": "gbk-fam4", "memberUids": ["gbk-admin4", "gbk-kid4"]},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text

    broker = client.app.state.broker
    broker.published.clear()

    leave_resp = client.delete("/api/conversations/gbk-fam4/members/me", headers=kid_headers)
    assert leave_resp.status_code == 200, leave_resp.text

    # Only the leaver's device is pushed to; the remaining member's book content did not change.
    topics = {p.topic for p in broker.published}
    assert topics == {"pager/gbk-dev4-kid/down"}
    sent = json.loads(broker.published[0].payload)
    assert sent["kind"] == "book"
    assert all(c["a"] != "gbk-fam4" for c in sent["c"])
