"""`kind:"contact_req"` ingest handling as an add (docs/PROTOCOL.md §3.2, §4.2), plus block/dismiss
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
from app.ingest import Ingest, classify_ph
from app.main import create_app
from app.store import alerts as alerts_store
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import contacts as contacts_store
from app.store import device_secrets as device_secrets_store
from app.store import devices as devices_store
from app.store import externals as externals_store
from app.store import families as families_store
from app.store import users as users_store
from tests.conftest import up_topic
from tests.fake_transport import FakeBrokerClient
from tests.firebase_test_utils import auth_header

_HMAC_KEY = b"k" * 32


def _make_user(uid: str, alias: str, *, family_id: str | None = None) -> None:
    """`family_id` is needed by every add: an owner without a family is dropped."""
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


def _make_family(name: str) -> families_store.Family:
    return families_store.create_family(name=name, created_by="root-contacts")


def _kid(
    family_id: str,
    uid: str = "student",
    *,
    policy: tuple[str, str] = ("people_sms", "people_sms"),
    sms: bool = True,
    devices: tuple[str, ...] = (),
) -> None:
    """A family member; `sms` gives it a relay number, otherwise it texts through the modem
    (`cfg.sms`)."""
    _make_user(uid, uid, family_id=family_id)
    get_db().collection("users").document(uid).update(
        {"policy": {"out": policy[0], "in": policy[1]}}
    )
    if sms:
        users_store.set_sms_number(uid, "+12065550999")
    for device_id in devices:
        _make_pager_device(device_id, uid)


def _marker(owner: str, peer: str) -> dict | None:
    snap = get_db().collection("users").document(owner).collection("book").document(peer).get()
    return snap.to_dict() if snap.exists else None


def _system_bodies(broker: FakeBrokerClient) -> list[str]:
    out = []
    for m in broker.published:
        d = json.loads(m.payload)
        if d.get("from") == "system":
            out.append(d["body"])
    return out


def _book_pushes(broker: FakeBrokerClient) -> int:
    return sum(1 for m in broker.published if json.loads(m.payload).get("kind") == "book")


def test_add_phone_creates_contact_and_marker():
    family = _make_family("Add1")
    _kid(family.id, devices=("pgr-c-1", "pgr-c-1b"))
    ingest, broker = _ingest()

    ingest.handle_up(up_topic("pgr-c-1"), contact_req_payload("u_c1", "Grandma", ph="+15551234567"))

    req = contacts_store.get_by_device_and_req("pgr-c-1", "u_c1")
    assert req is not None
    assert (req.status, req.name, req.phone, req.alias) == ("added", "Grandma", "+15551234567", None)
    assert req.ownerUid == "student"
    ext = externals_store.get_family_contact(family.id, "+15551234567")
    assert ext is not None and ext.displayName == "Grandma"
    assert req.peerUid == ext.uid
    marker = _marker("student", ext.uid)
    assert marker is not None and marker["added"] is True and marker["addedBy"] == "pgr-c-1"
    assert "nick" not in marker
    assert _book_version("pgr-c-1") == 1 and _book_version("pgr-c-1b") == 1
    assert alerts_store.list_alerts(family.id, "all") == []
    assert _system_bodies(broker) == []
    assert _book_pushes(broker) == 2
    # No edge: the add grants nothing.
    assert allow_store.get_edge("student", ext.uid) is None


def test_add_redelivery_is_noop():
    family = _make_family("Add2")
    _kid(family.id, devices=("pgr-c-3",))
    ingest, broker = _ingest()

    payload = contact_req_payload("u_c3", "Uncle Bob", ph="+15559990000")
    ingest.handle_up(up_topic("pgr-c-3"), payload)
    published = len(broker.published)
    ingest.handle_up(up_topic("pgr-c-3"), payload)

    assert len(contacts_store.list_requests(device_id="pgr-c-3")) == 1
    assert _book_version("pgr-c-3") == 1
    assert len(broker.published) == published


def test_add_lost_race_on_the_row_is_a_dup_with_no_second_bump():
    """The row's `create()` inside the transaction is the dedup on the wire id."""
    family = _make_family("Add2b")
    _kid(family.id, devices=("pgr-c-3b",))
    ext = externals_store.get_or_create(family.id, "+15559990001", "Bob")
    kwargs = {
        "device_id": "pgr-c-3b",
        "owner_uid": "student",
        "family_id": family.id,
        "req_id": "u_c3b",
        "name": "Bob",
        "peer_uid": ext.uid,
        "nick": None,
    }
    assert contacts_store.add_entry(**kwargs) == "added"
    assert contacts_store.add_entry(**kwargs) == "dup"
    assert _book_version("pgr-c-3b") == 1


@pytest.mark.parametrize(
    ("existing", "adds", "display_names"),
    [
        pytest.param(
            [("+15551110000", "Gran")],
            [("+15551110000", "Grandma")],
            ["Gran"],
            id="existing_number_other_name_sets_nick",
        ),
        pytest.param(
            [("+15552220000", "Grandma")],
            [("+15553330100", "Grandma")],
            ["Grandma 0100"],
            id="name_taken_suffixes_last4",
        ),
        pytest.param(
            [("+15552220000", "Grandma"), ("+15554440100", "Grandma 0100")],
            [("+15553440100", "Grandma"), ("+15557440100", "Grandma")],
            ["Grandma 440100", "+15557440100"],
            id="name_taken_falls_through_to_last6_then_e164",
        ),
    ],
)
def test_add_name_collisions_get_a_distinct_display_name_and_keep_the_requested_nick(
    existing: list[tuple[str, str]], adds: list[tuple[str, str]], display_names: list[str]
):
    family = _make_family("Add3")
    _kid(family.id, devices=("pgr-c-4",))
    for phone, name in existing:
        externals_store.get_or_create(family.id, phone, name)
    ingest, broker = _ingest()

    for n, (phone, name) in enumerate(adds):
        ingest.handle_up(up_topic("pgr-c-4"), contact_req_payload(f"u_c4{n}", name, ph=phone))

    assert len(externals_store.list_family_contacts(family.id)) == len({p for p, _ in existing + adds})
    for (phone, name), display_name in zip(adds, display_names, strict=True):
        ext = externals_store.get_family_contact(family.id, phone)
        assert ext is not None and ext.displayName == display_name
        marker = _marker("student", ext.uid)
        assert marker is not None and marker["nick"] == name and marker["added"] is True
    assert _system_bodies(broker) == []


def test_add_alias_inbound_edge_marks():
    fam_a, fam_b = _make_family("AddA"), _make_family("AddB")
    _kid(fam_a.id, devices=("pgr-c-6",))
    _make_user("gma", "gma", family_id=fam_b.id)
    allow_store.set_edge("gma", "student", message=True, locate=False)
    ingest, broker = _ingest()

    ingest.handle_up(up_topic("pgr-c-6"), contact_req_payload("u_c6", "Gma", ph="gma"))

    req = contacts_store.get_by_device_and_req("pgr-c-6", "u_c6")
    assert req is not None and (req.status, req.alias, req.peerUid) == ("added", "gma", "gma")
    marker = _marker("student", "gma")
    assert marker is not None and marker["added"] is True
    assert alerts_store.list_alerts(fam_a.id, "all") == [] and _system_bodies(broker) == []
    assert allow_store.get_edge("student", "gma") is None


def test_add_alias_foreign_no_edge_shared_reply():
    fam_a, fam_b = _make_family("AddC"), _make_family("AddD")
    _kid(fam_a.id, devices=("pgr-c-7",))
    _make_user("stranger", "stranger", family_id=fam_b.id)
    ingest, broker = _ingest()

    ingest.handle_up(up_topic("pgr-c-7"), contact_req_payload("u_c7a", "X", ph="stranger"))
    ingest.handle_up(up_topic("pgr-c-7"), contact_req_payload("u_c7b", "X", ph="nobody"))

    assert _marker("student", "stranger") is None
    assert _system_bodies(broker) == ["X: no contact @stranger", "X: no contact @nobody"]
    assert _system_bodies(broker)[0].replace("stranger", "nobody") == _system_bodies(broker)[1]


def test_add_cap_32_full():
    family = _make_family("Add5")
    _kid(family.id, devices=("pgr-c-8",))
    for i in range(32):
        get_db().collection("users").document("student").collection("book").document(
            f"peer{i}"
        ).set({"added": True})
    ingest, broker = _ingest()

    ingest.handle_up(up_topic("pgr-c-8"), contact_req_payload("u_c8", "Over", ph="+15556660001"))

    assert _system_bodies(broker) == ["Over: address book full"]
    req = contacts_store.get_by_device_and_req("pgr-c-8", "u_c8")
    assert req is not None and (req.status, req.reason) == ("rejected", "full")
    assert _book_version("pgr-c-8") == 0
    # An already-marked peer is not a 33rd entry.
    ext = externals_store.get_or_create(family.id, "+15556660002", "Again")
    get_db().collection("users").document("student").collection("book").document("peer0").delete()
    get_db().collection("users").document("student").collection("book").document(ext.uid).set(
        {"added": True}
    )
    kwargs = {
        "device_id": "pgr-c-8",
        "owner_uid": "student",
        "family_id": family.id,
        "req_id": "u_c8b",
        "name": "Again",
        "peer_uid": ext.uid,
        "nick": None,
    }
    assert contacts_store.add_entry(**kwargs) == "added"
    assert contacts_store.add_entry(**{**kwargs, "req_id": "u_c8c", "peer_uid": "newpeer"}) == "full"


def test_add_rate_10_per_hour():
    family = _make_family("Add6")
    _kid(family.id, devices=("pgr-c-9",))
    ingest, broker = _ingest()

    for i in range(10):
        ingest.handle_up(
            up_topic("pgr-c-9"),
            contact_req_payload(f"u_c9_{i}", f"C{i}", ph=f"+1555000000{i}"),
        )
    assert _system_bodies(broker) == []
    ingest.handle_up(up_topic("pgr-c-9"), contact_req_payload("u_c9_x", "Late", ph="+15559999999"))

    assert _system_bodies(broker) == ["too many adds; try later"]
    assert contacts_store.get_by_device_and_req("pgr-c-9", "u_c9_x") is None
    assert externals_store.get_family_contact(family.id, "+15559999999") is None
    # A redelivery of an earlier add is a dedup, not a rate hit.
    ingest.handle_up(up_topic("pgr-c-9"), contact_req_payload("u_c9_0", "C0", ph="+15550000000"))
    assert _system_bodies(broker) == ["too many adds; try later"]


def test_policy_hidden_sibling_not_in_book():
    """A sibling hidden by policy is not "already in your book"; the marker lists it."""
    family = _make_family("Add7")
    _kid(family.id, policy=("sms", "people_sms"), devices=("pgr-c-10",))
    _make_user("sis", "sis", family_id=family.id)
    ingest, broker = _ingest()

    ingest.handle_up(up_topic("pgr-c-10"), contact_req_payload("u_c10", "Sis", ph="sis"))

    marker = _marker("student", "sis")
    assert marker is not None and marker["added"] is True
    assert _system_bodies(broker) == []
    # Now it is on the pager: a second add says so.
    ingest.handle_up(up_topic("pgr-c-10"), contact_req_payload("u_c10b", "Sis", ph="sis"))
    assert _system_bodies(broker) == ["Sis: already in your book"]


@pytest.mark.parametrize("via", ["same_family_alias", "phone_on_the_pager"])
def test_an_add_already_in_your_book_replies_and_stores_no_row(via: str):
    family = _make_family("Add8")
    _kid(family.id, devices=("pgr-c-11",))
    if via == "same_family_alias":
        _make_user("sis", "sis", family_id=family.id)
        ph, name, peer = "sis", "X", "sis"
    else:
        ext = externals_store.get_or_create(family.id, "+12065550123", "Gma")
        allow_store.set_edge("student", ext.uid, message=True, locate=False)
        ph, name, peer = "2065550123", "Gma", ext.uid
    ingest, broker = _ingest()

    ingest.handle_up(up_topic("pgr-c-11"), contact_req_payload("u_c11", name, ph=ph))

    assert contacts_store.get_by_device_and_req("pgr-c-11", "u_c11") is None
    assert _system_bodies(broker) == [f"{name}: already in your book"]
    assert _marker("student", peer) is None


@pytest.mark.parametrize(
    ("kid_kwargs", "bodies", "sms_phones", "alerts"),
    [
        pytest.param(
            {"sms": False},
            ["Gma: added; needs a parent's OK to text"],
            [],
            True,
            id="refused_fixable_alerts_and_replies",
        ),
        pytest.param(
            {"policy": ("people", "people"), "sms": False},
            ["Gma: added; settings don't allow texting"],
            [],
            False,
            id="unfixable_replies_without_alert",
        ),
        pytest.param(
            {"policy": ("any_sms", "people"), "sms": False},
            [],
            ["+15557770001"],
            False,
            id="allowed_reaches_cfg_sms",
        ),
    ],
)
def test_modem_owner_add_is_gated_by_cfg_sms(
    kid_kwargs: dict, bodies: list[str], sms_phones: list[str], alerts: bool
):
    """A modem owner's `cfg.sms` is the gate; a fixable refusal raises the alert and the number
    stays off `cfg.sms`."""
    family = _make_family("Add10")
    _kid(family.id, devices=("pgr-c-13",), **kid_kwargs)
    ingest, broker = _ingest()

    ingest.handle_up(up_topic("pgr-c-13"), contact_req_payload("u_c13", "Gma", ph="+15557770001"))

    assert _system_bodies(broker) == bodies
    assert [c.phone for c in devices_store.get_device("pgr-c-13").smsContacts] == sms_phones
    if not alerts:
        assert alerts_store.list_alerts(family.id, "all") == []
        return
    ext = externals_store.get_family_contact(family.id, "+15557770001")
    assert ext is not None
    (alert,) = alerts_store.list_alerts(family.id, "all")
    assert alert.id == f"cr_student_{ext.uid}" and alert.kind == "contact_request"
    assert (alert.subjectUid, alert.peerUid, alert.peerPhone) == ("student", ext.uid, ext.phone)
    assert alert.contactRequestKey is None
    assert _marker("student", ext.uid)["added"] is True

    # A second add of another number to the same alert id is a different alert;
    # the same peer again does not duplicate.
    ingest.handle_up(up_topic("pgr-c-13"), contact_req_payload("u_c13b", "Gma", ph="+15557770001"))
    assert len(alerts_store.list_alerts(family.id, "all")) == 1


def test_rejection_no_bump():
    family = _make_family("Add13")
    families_store.add_blocked_number(family.id, "+12065550999")
    _kid(family.id, devices=("pgr-c-16",))
    ingest, broker = _ingest()

    ingest.handle_up(up_topic("pgr-c-16"), contact_req_payload("u_c16a", "Bad", ph="5550100"))
    ingest.handle_up(up_topic("pgr-c-16"), contact_req_payload("u_c16b", "Blk", ph="2065550999"))
    ingest.handle_up(up_topic("pgr-c-16"), contact_req_payload("u_c16c", "X", ph="nobody"))

    a = contacts_store.get_by_device_and_req("pgr-c-16", "u_c16a")
    b = contacts_store.get_by_device_and_req("pgr-c-16", "u_c16b")
    c = contacts_store.get_by_device_and_req("pgr-c-16", "u_c16c")
    assert (a.status, a.reason) == ("rejected", "bad_number")
    assert (b.status, b.reason) == ("rejected", "blocked")
    assert (c.status, c.reason, c.decidedBy) == ("rejected", "no_contact", "relay")
    assert _system_bodies(broker) == [
        "Bad: number must be 10 digits or start with +",
        "Blk: number not allowed",
        "X: no contact @nobody",
    ]
    assert _book_version("pgr-c-16") == 0 and _book_pushes(broker) == 0
    assert alerts_store.list_alerts(family.id, "all") == []


def test_added_marker_grants_nothing():
    """Routing, policy and `sms_contacts_for` never read the marker."""
    from app import book as book_module
    from app.routing import Routing

    family = _make_family("Add14")
    _kid(family.id, devices=("pgr-c-17",))
    ingest, broker = _ingest()
    ingest.handle_up(up_topic("pgr-c-17"), contact_req_payload("u_c17", "Gma", ph="+15558880001"))
    ext = externals_store.get_family_contact(family.id, "+15558880001")
    assert ext is not None and _marker("student", ext.uid) is not None

    owner = users_store.get_user("student")
    assert book_module.sms_contacts_for(owner) == []
    result = Routing(broker).send(
        sender_uid="student",
        recipient_alias=ext.alias,
        kind="text",
        body="hi",
        origin_backend_kind="pager",
    )
    assert result.messages == [] and [r.reason for r in result.rejected] == ["not_allowed"]
    assert allow_store.get_edge("student", ext.uid) is None


@pytest.mark.parametrize("revoked", [False, True], ids=["unregistered", "revoked"])
def test_contact_req_from_an_unusable_device_is_dropped(revoked: bool):
    if revoked:
        _make_user("student", "student")
        _make_pager_device("pgr-c-8r", "student")
        devices_store.revoke_device("pgr-c-8r")
    ingest, broker = _ingest()

    ingest.handle_up(up_topic("pgr-c-8r"), contact_req_payload("u_c8", "Nope"))

    assert contacts_store.get_by_device_and_req("pgr-c-8r", "u_c8") is None
    assert broker.published == []


def test_contact_req_name_too_long_is_dropped_as_malformed():
    _make_user("student", "student")
    _make_pager_device("pgr-c-9m", "student")
    ingest, _broker = _ingest()

    ingest.handle_up(up_topic("pgr-c-9m"), contact_req_payload("u_c9", "N" * 17))
    assert contacts_store.get_by_device_and_req("pgr-c-9m", "u_c9") is None


def test_hmac_signed_contact_req_is_verified_then_stored():
    family = _make_family("Hmac1")
    _make_user("student", "student", family_id=family.id)
    key = _make_hmac_pager_device("pgr-c-11h", "student")
    ingest, _broker = _ingest()

    topic = up_topic("pgr-c-11h")
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

    req = contacts_store.get_by_device_and_req("pgr-c-11h", "u_c11")
    assert req is not None
    assert (req.status, req.phone) == ("added", "+15553330000")


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


def _raise_approval_alert(family_id: str, phone: str = "+15557770000"):
    """A modem owner's refused add raises the one `contact_request` alert."""
    ingest, _broker = _ingest()
    ingest.handle_up(up_topic("pgr-a-1"), contact_req_payload("u_a1", "Stranger", ph=phone))
    (alert,) = alerts_store.list_alerts(family_id, "all")
    assert alert.kind == "contact_request"
    return alert


def test_block_contact_request_alert_adds_blocked_number_and_marks_handled(client: TestClient):
    family = _make_family("Block1")
    headers = _make_family_admin("cadmin5", "cadmin5", family.id)
    _kid(family.id, sms=False, devices=("pgr-a-1",))
    alert = _raise_approval_alert(family.id)

    resp = client.post(f"/api/family/alerts/{alert.id}/block", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "handled"
    updated_family = families_store.get_family(family.id)
    assert updated_family is not None
    assert "+15557770000" in updated_family.blockedNumbers


def test_dismiss_contact_request_alert_marks_it_dismissed(client: TestClient):
    family = _make_family("Dismiss1")
    headers = _make_family_admin("cadmin8", "cadmin8", family.id)
    _kid(family.id, sms=False, devices=("pgr-a-1",))
    alert = _raise_approval_alert(family.id)

    resp = client.post(f"/api/family/alerts/{alert.id}/dismiss", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "dismissed"


def test_block_legacy_pending_request_still_rejects_it(client: TestClient):
    """An alert from before the add flow wraps a pending `contactRequests` row; blocking it still
    rejects the row."""
    family = _make_family("Legacy1")
    headers = _make_family_admin("cadmin6", "cadmin6", family.id)
    _kid(family.id, devices=("pgr-a-6",))
    doc_key = contacts_store.key("pgr-a-6", "u_old")
    get_db().collection("contactRequests").document(doc_key).set(
        {
            "deviceId": "pgr-a-6",
            "reqId": "u_old",
            "ownerUid": "student",
            "name": "Old",
            "phone": "+15557770009",
            "alias": None,
            "status": "pending",
        }
    )
    alert_id = alerts_store.create(
        family.id,
        {
            "kind": "contact_request",
            "status": "open",
            "subjectUid": "student",
            "subjectAlias": "student",
            "peerPhone": "+15557770009",
            "preview": "Old",
            "contactRequestKey": doc_key,
        },
    )

    resp = client.post(f"/api/family/alerts/{alert_id}/block", headers=headers)
    assert resp.status_code == 200, resp.text
    request = contacts_store.get_request(doc_key)
    assert request is not None and (request.status, request.reason) == ("rejected", "blocked")
    assert _book_version("pgr-a-6") == 1


@pytest.mark.parametrize(
    ("ph", "expected"),
    [
        ("2065550100", ("phone", "+12065550100")),
        ("12065550100", ("phone", "+12065550100")),
        ("+442071838750", ("phone", "+442071838750")),
        ("+12065550100", ("phone", "+12065550100")),
        ("+1206", ("bad", "+1206")),
        ("5550100", ("bad", "5550100")),
        ("123456789012", ("bad", "123456789012")),
        ("grandma", ("alias", "grandma")),
        ("Grandma", ("bad", "Grandma")),
        ("+abc", ("bad", "+abc")),
    ],
)
def test_classify_ph(ph: str, expected: tuple[str, str]):
    assert classify_ph(ph) == expected
