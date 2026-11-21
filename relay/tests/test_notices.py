# ruff: noqa: F811
"""Tests for `app/notices.py` and `families.settings`."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import notices, sysmsg
from app.db.firestore import get_db
from app.store import alerts as alerts_store
from app.store import allow as allow_store
from app.store import devices as devices_store
from app.store import externals as externals_store
from app.store import families as families_store
from tests.bridge_world import (  # noqa: F401
    MOM,
    RecordingFCM,
    World,
    broker,
    client,
    fcm,
    make_pager_device,
    pair_bridge,
    world,
)
from tests.fake_transport import FakeBrokerClient


def _kid_with_bridged_pager(
    client: TestClient, world: World, device_id: str = "d_kid"
) -> str:
    pair_bridge(client, world)
    get_db().collection("users").document("kid").update(
        {"policy": {"out": "people_sms", "in": "people_sms"}}
    )
    ext = externals_store.get_or_create(world.family_id, MOM, "Mom")
    allow_store.set_edge("kid", ext.uid, message=True, locate=False)
    make_pager_device(device_id, "kid")
    get_db().collection("devices").document(device_id).update(
        {"familyId": world.family_id}
    )
    return device_id


def _system_texts(broker: FakeBrokerClient) -> list[dict]:
    out = [json.loads(p.payload) for p in broker.published if p.topic.endswith("/down")]
    return [d for d in out if d.get("from") == "system"]


def test_notify_creates_alert_pushes_admins_and_texts_the_pager(
    client: TestClient, world: World, broker: FakeBrokerClient, fcm: RecordingFCM
):
    dev = _kid_with_bridged_pager(client, world)
    broker.clear()
    res = notices.notify(
        world.family_id,
        "bridge_offline",
        "Bridge phone offline since 5:21p. SMS and chat pages will wait.",
        key="b_1",
        since=1000,
    )
    assert (
        res.alertId == "sys_bridge_offline_b_1"
        and res.pagers == [dev]
        and not res.pagerFailed
    )
    alert = alerts_store.get(world.family_id, res.alertId)
    assert alert is not None and alert.kind == "system" and alert.status == "open"
    assert alert.code == "bridge_offline" and alert.key == "b_1"
    assert alert.preview.startswith("Bridge phone offline")
    assert any(
        d["kind"] == "alert" and d["alertKind"] == "system" and d["id"] == res.alertId
        for _, d in fcm.calls
    )
    (down,) = _system_texts(broker)
    assert down["id"] == sysmsg.derived_id(
        "notice", dev, "bridge_offline", "b_1", "1000"
    )
    assert down["body"].startswith("Bridge phone offline since 5:21p")
    assert broker.published[-1].qos == 1


def test_same_epoch_resends_the_same_id_and_same_text_does_not_repush(
    client: TestClient, world: World, broker: FakeBrokerClient, fcm: RecordingFCM
):
    _kid_with_bridged_pager(client, world)
    broker.clear()
    for _ in range(2):
        notices.notify(world.family_id, "sms_out", "Out of texts.", key="b_1", since=5)
    a, b = _system_texts(broker)
    assert a["id"] == b["id"]
    pushes = [c for c in fcm.calls if c[1].get("alertKind") == "system"]
    assert len(pushes) == 1  # the second upsert changed nothing
    notices.notify(
        world.family_id, "sms_out", "Out of texts, again.", key="b_1", since=5
    )
    assert len([c for c in fcm.calls if c[1].get("alertKind") == "system"]) == 2
    assert (
        alerts_store.get(world.family_id, "sys_sms_out_b_1").preview
        == "Out of texts, again."
    )


def test_resolves_closes_the_down_alert_and_writes_a_closed_restored_row(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    notices.notify(world.family_id, "bridge_offline", "down", key="b_1", pager=False)
    assert [a.id for a in alerts_store.list_alerts(world.family_id)] == [
        "sys_bridge_offline_b_1"
    ]
    notices.notify(
        world.family_id,
        "bridge_online",
        "Bridge phone is back.",
        key="b_1",
        resolves="bridge_offline",
        pager=False,
    )
    assert alerts_store.list_alerts(world.family_id) == []
    down = alerts_store.get(world.family_id, "sys_bridge_offline_b_1")
    up = alerts_store.get(world.family_id, "sys_bridge_online_b_1")
    assert down.status == "handled" and down.decidedBy == "system"
    assert up.status == "handled"
    # a later outage reopens the very same alert
    notices.notify(
        world.family_id, "bridge_offline", "down again", key="b_1", pager=False
    )
    assert alerts_store.get(world.family_id, "sys_bridge_offline_b_1").status == "open"
    assert (
        alerts_store.get(world.family_id, "sys_bridge_offline_b_1").preview
        == "down again"
    )


def test_family_switch_turns_the_pager_copy_off_but_not_the_web_alert(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    _kid_with_bridged_pager(client, world)
    broker.clear()
    r = client.patch(
        "/api/family",
        json={"settings": {"notices": {"pager": False}}},
        headers=world.admin_headers,
    )
    assert r.status_code == 200, r.text
    res = notices.notify(
        world.family_id, "sms_low", "12 texts left.", key="b_1", since=1
    )
    assert res.pagers == [] and _system_texts(broker) == []
    assert alerts_store.get(world.family_id, res.alertId) is not None


def test_default_targets_need_a_bridged_contact_and_a_live_device(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    # A pager of the family with no bridged contact gets nothing.
    make_pager_device("d_plain", "adm")
    get_db().collection("devices").document("d_plain").update(
        {"familyId": world.family_id}
    )
    broker.clear()
    assert (
        notices.notify(world.family_id, "sms_low", "x", key="k", since=1).pagers == []
    )
    dev = _kid_with_bridged_pager(client, world)
    assert notices.notify(world.family_id, "sms_low", "x", key="k", since=2).pagers == [
        dev
    ]
    devices_store.revoke_device(dev)
    assert (
        notices.notify(world.family_id, "sms_low", "x", key="k", since=3).pagers == []
    )
    # an explicit list is honoured as given
    assert notices.notify(
        world.family_id, "sms_low", "x", key="k", since=4, device_ids=["d_plain"]
    ).pagers == ["d_plain"]


def test_text_is_clipped_to_90_bytes_and_cleaned(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    dev = _kid_with_bridged_pager(client, world)
    broker.clear()
    long = "Bridge\nphone " + "電話" * 40
    res = notices.notify(world.family_id, "bridge_degraded", long, key="k", since=1)
    (down,) = _system_texts(broker)
    assert res.pagers == [dev]
    assert len(down["body"].encode()) <= notices.NOTICE_MAX_BYTES
    assert "\n" not in down["body"] and down["body"].startswith("Bridge phone ")


def test_refused_publish_is_reported(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    dev = _kid_with_bridged_pager(client, world)
    broker.fail_publish = True
    res = notices.notify(world.family_id, "sms_out", "x", key="k", since=1)
    assert res.pagers == [] and res.pagerFailed == [dev]


def test_hold_off_rules():
    h = notices.Health()
    h, announce = notices.enter_bad(h, "offline", 1000)
    assert announce and h.since == 1000 and h.notifiedAt == 1000
    # the same state again: nothing
    assert notices.enter_bad(h, "offline", 1100) == (h, False)
    # restored after an announced down: announce
    h, announce = notices.enter_ok(h, 1200)
    assert announce and h.state == "ok" and h.notifiedAt == 1000
    # flaps again 10 minutes after the first announcement: held off, and so is its restore
    h, announce = notices.enter_bad(h, "offline", 1600)
    assert not announce and h.notifiedAt == 1000 and h.since == 1600
    h, announce = notices.enter_ok(h, 1700)
    assert not announce
    # 60 minutes after the last announced down: announced again
    h, announce = notices.enter_bad(h, "offline", 1000 + notices.HOLD_OFF_S)
    assert announce
    # bad -> bad keeps the outage start; its restore follows the announced down
    h, _ = notices.enter_bad(h, "degraded", 1000 + notices.HOLD_OFF_S + 5)
    assert h.since == 1000 + notices.HOLD_OFF_S
    assert notices.enter_ok(h, 99999)[1] is True


def test_family_settings_defaults_patch_and_validation(
    client: TestClient, world: World
):
    r = client.get("/api/family", headers=world.admin_headers)
    assert r.status_code == 200
    assert r.json()["settings"] == {
        "notices": {"pager": True},
        "linkChecks": True,
        "topup": {"mode": "off", "perMonth": 1},
    }
    r = client.patch(
        "/api/family",
        json={
            "settings": {
                "topup": {"mode": "limited", "perMonth": 3},
                "linkChecks": False,
            }
        },
        headers=world.admin_headers,
    )
    assert r.status_code == 200, r.text
    # partial: the earlier edit survives a later one
    r = client.patch(
        "/api/family",
        json={"settings": {"notices": {"pager": False}}},
        headers=world.admin_headers,
    )
    s = r.json()["settings"]
    assert s == {
        "notices": {"pager": False},
        "linkChecks": False,
        "topup": {"mode": "limited", "perMonth": 3},
    }
    assert families_store.get_family(world.family_id).settings.topup.perMonth == 3
    # name still patches alone and leaves settings alone
    r = client.patch("/api/family", json={"name": "Examples"}, headers=world.admin_headers)
    assert r.json()["settings"] == s and r.json()["name"] == "Examples"


def test_family_settings_validation_and_scope(client: TestClient, world: World):
    h = world.admin_headers
    for body in (
        {},
        {"settings": {}},
        {"settings": {"topup": {"mode": "always"}}},
        {"settings": {"topup": {"perMonth": 0}}},
        {"settings": {"topup": {"perMonth": 11}}},
        {"settings": {"linkChecks": None}},
        {"settings": {"notices": {"pager": "maybe"}}},
        {"settings": {"nope": True}},
    ):
        r = client.patch("/api/family", json=body, headers=h)
        assert r.status_code == 422 or (
            body == {"settings": {}} and r.status_code == 200
        ), (body, r.text)
    assert (
        families_store.get_family(world.family_id).settings
        == families_store.FamilySettings()
    )
    # a member is not a family admin
    from firebase_admin import auth as fb_auth

    from tests.firebase_test_utils import auth_header

    fb_auth.create_user(uid="kid", email="kid@example.com")
    r = client.patch(
        "/api/family",
        json={"settings": {"linkChecks": False}},
        headers=auth_header("kid"),
    )
    assert r.status_code in (401, 403)
