"""Bridge phone relay: reassignment cleanup, heartbeat validation, caps, rate limits, queries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.db.firestore import get_db
from app.routing import Routing
from app.store import alerts as alerts_store
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import bridge_conversations as conv_store
from app.store import bridge_outbox
from app.store import bridges as bridges_store
from app.store import externals as externals_store
from app.store import messages as messages_store
from app.store import rate_limits as rate_limits_store
from app.store import users as users_store
from app.tasks import InlineTaskQueue
from tests.bridge_world import (  # noqa: F401
    MOM,
    SIM,
    VOICE,
    World,
    broker,
    client,
    fcm,
    make_pager_device,
    pair_bridge,
    world,
)
from tests.fake_transport import FakeBrokerClient
from tests.test_chat_subscribe import (  # noqa: F401
    DM,
    GROUP,
    Env,
    _ev,
    _group_subscribe,
    env,
)


def _contact(world: World, member: str = "kid", phone: str = MOM, name: str = "Mom"):
    get_db().collection("users").document(member).update(
        {"policy": {"out": "people_sms", "in": "people_sms"}}
    )
    ext = externals_store.get_or_create(world.family_id, phone, name)
    allow_store.set_edge(member, ext.uid, message=True, locate=False)
    return ext


def _send(broker: FakeBrokerClient, alias: str, body: str = "hi"):
    return Routing(broker).send(
        sender_uid="kid", recipient_alias=alias, kind="text", body=body, origin_backend_kind="pager"
    )


def test_reassign_unsubscribes_moves_rows_fails_outbox_and_clears_channels(env: Env):
    world, client, bridge = env.world, env.client, env.bridge
    users_store.create_user(uid="sis", alias="sis", display_name="Sis", family_id=world.family_id)
    out = _group_subscribe(env)
    # A pending outbox row for the old owner's reply, and a remembered channel.
    env.pager_up(out["alias"], "I can drive", "u_r1")
    (item,) = bridge_outbox.list_pending(bridge.id)
    mom = _contact(world)
    backends_store.update_backend(
        mom.uid, "sms", config={"phone": MOM, "via": {"kid": "gvoice", "sis": "sms"}, "voiceConv": {"kid": "vt"}}
    )
    # A not-yet-subscribed conversation with a held text and an alert.
    env.events(_ev("Pat", "hi", "spaces/CCC", group=False, title="Pat"))
    seen = conv_store.get(bridge.id, "spaces/CCC")
    assert seen.alertId and seen.heldCount == 1

    r = client.patch(
        f"/api/family/bridges/{bridge.id}", json={"ownerUid": "sis"}, headers=world.admin_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["ownerUid"] == "sis" and r.json()["unsubscribed"] == 1

    assert users_store.get_user(out["uid"]) is None
    assert conversations_alias_gone(out["alias"])
    assert bridge_outbox.get(bridge.id, item.id).state == "failed"
    assert bridge_outbox.get(bridge.id, item.id).reason == "reassigned"
    (msg,) = [m for m in get_db().collection("messages").stream() if m.to_dict()["senderUid"] == "kid"]
    assert msg.to_dict()["deliveries"]["bridge"]["state"] == "failed"
    assert users_store.get_user("kid").smsNumber is None
    assert users_store.get_user("sis").smsNumber == SIM
    rows = {r.conversationId: r for r in conv_store.list_for_bridge(bridge.id)}
    assert {r.ownerUid for r in rows.values()} == {"sis"}
    assert rows[GROUP].status == "seen" and rows[GROUP].uid is None
    assert rows["spaces/CCC"].heldCount == 0 and rows["spaces/CCC"].alertId is None
    assert all(a.status != "open" for a in alerts_store.list_alerts(world.family_id, "all"))
    # Channels of the old owner are gone; the new owner's entry stays.
    assert backends_store.get_backend(mom.uid, "sms").config["via"] == {"sis": "sms"}
    assert "voiceConv" not in backends_store.get_backend(mom.uid, "sms").config


def conversations_alias_gone(alias: str) -> bool:
    from app.store import conversations as conversations_store

    return conversations_store.get_by_alias(alias) is None


def test_reassign_away_from_a_disabled_owner_works(env: Env):
    users_store.create_user(uid="sis", alias="sis", display_name="Sis", family_id=env.world.family_id)
    _group_subscribe(env)
    users_store.update_user("kid", disabled=True)
    r = env.client.patch(
        f"/api/family/bridges/{env.bridge.id}", json={"ownerUid": "sis"}, headers=env.world.admin_headers
    )
    assert r.status_code == 200 and r.json()["unsubscribed"] == 1


def _seed_via(world: World, member: str = "kid"):
    mom = _contact(world, member)
    backends_store.update_backend(
        mom.uid, "sms", config={"phone": MOM, "via": {member: "gvoice"}, "voiceConv": {member: "vt"}}
    )
    return mom


@pytest.mark.parametrize(
    "act",
    [
        pytest.param(lambda c, url, h: c.delete(url, headers=h), id="unpair"),
        pytest.param(
            lambda c, url, h: c.patch(url, json={"voiceNumber": ""}, headers=h), id="drop_voice_number"
        ),
    ],
)
def test_unpairing_or_removing_the_voice_number_clears_the_members_channels(
    client: TestClient, world: World, act
):
    bridge, _ = pair_bridge(client, world, sim=SIM, voice=VOICE)
    mom = _seed_via(world)
    r = act(client, f"/api/family/bridges/{bridge.id}", world.admin_headers)
    assert r.status_code == 200
    config = backends_store.get_backend(mom.uid, "sms").config
    assert "via" not in config and "voiceConv" not in config and config["phone"] == MOM


def test_heartbeat_status_is_validated(client: TestClient, world: World):
    bridge, headers = pair_bridge(client, world)
    for bad in (
        {"battery": "full"},
        {"accounts": ["x"] * 11},
        {"version": "v" * 41},
    ):
        r = client.post("/bridge/heartbeat", json={"status": bad}, headers=headers)
        assert r.status_code == 422, bad
    assert bridges_store.get(bridge.id).paired
    # `error` is accepted and ignored (relay-owned); unknown keys are dropped.
    r = client.post(
        "/bridge/heartbeat",
        json={"status": {"error": "phone says", "junk": 1, "battery": 70}},
        headers=headers,
    )
    assert r.status_code == 200
    got = bridges_store.get(bridge.id)
    assert got.status.error is None and got.status.battery == 70


def test_a_corrupt_bridge_row_reads_as_unpaired_instead_of_raising(client: TestClient, world: World):
    bridge, headers = pair_bridge(client, world)
    good, _ = pair_bridge(client, world, sim="+12065550888")
    get_db().collection("bridges").document(bridge.id).update({"status.battery": "oops"})
    got = bridges_store.get(bridge.id)
    assert got is not None and not got.paired and got.ownerUid == "kid"
    assert bridges_store.get_by_sms_number(SIM) is None
    r = client.get("/api/family/bridges", headers=world.admin_headers)
    assert r.status_code == 200 and {b["id"] for b in r.json()} == {bridge.id, good.id}
    assert client.post("/bridge/heartbeat", json={"status": {}}, headers=headers).status_code == 401


def test_number_edits_keep_the_phone_reported_sms_bit(client: TestClient, world: World):
    bridge, headers = pair_bridge(
        client, world, sim=SIM, caps={"sms": False, "gchat": True, "gvoice": True}
    )
    assert bridge.caps.sms is False and bridge.status.smsCapable is False
    url = f"/api/family/bridges/{bridge.id}"
    r = client.patch(url, json={"voiceNumber": VOICE}, headers=world.admin_headers)
    assert r.json()["caps"] == {"sms": False, "gchat": True, "gvoice": True, "whatsapp": False}
    # The phone later gets the default-SMS role: the heartbeat reports it.
    client.post("/bridge/heartbeat", json={"status": {"smsDefault": True}}, headers=headers)
    assert bridges_store.get(bridge.id).caps.sms is True
    client.post("/bridge/heartbeat", json={"status": {"smsDefault": False}}, headers=headers)
    assert bridges_store.get(bridge.id).caps.sms is False
    # accept-sim keeps the reported bit too.
    new_sim = "+12065550777"
    client.post("/bridge/heartbeat", json={"status": {"simNumber": new_sim}}, headers=headers)
    r = client.post(f"{url}/accept-sim", headers=world.admin_headers)
    assert r.json()["simNumber"] == new_sim and r.json()["caps"]["sms"] is False
    client.post("/bridge/heartbeat", json={"status": {"smsDefault": True}}, headers=headers)
    assert bridges_store.get(bridge.id).caps.sms is True


def test_a_bridge_number_with_no_usable_cap_fails_no_bridge(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    bridge, _ = pair_bridge(
        client, world, sim=SIM, caps={"sms": False, "gchat": True, "gvoice": False}
    )
    assert not bridge.caps.sms and not bridge.caps.gvoice
    ext = _contact(world)
    result = _send(broker, ext.alias)
    (msg,) = result.messages
    d = messages_store.get_message(msg.id).deliveries["sms"]
    assert d.state == "failed" and d.error == "no_bridge"


def _inspect(conv: str = "spaces/IN", **over):
    ev = _ev("", "", conv, group=False, title="Grandma")
    ev["kind"] = "inspect"
    ev.update(over)
    return ev


def test_inspect_obeys_owner_caps_and_the_daily_cap(client: TestClient, world: World):
    from tests.test_bridge_chat_events import _post

    bridge, headers = pair_bridge(client, world)
    assert _post(client, headers, _inspect()) == ["inspected"]
    users_store.update_user("kid", disabled=True)
    assert _post(client, headers, _inspect("spaces/IN2")) == ["dropped_owner"]
    assert conv_store.get(bridge.id, "spaces/IN2") is None
    users_store.update_user("kid", disabled=False)
    for i in range(49):
        assert _post(client, headers, _inspect(f"spaces/N{i}")) == ["inspected"]
    assert _post(client, headers, _inspect("spaces/OVER")) == ["dropped_conv_cap"]
    # An inspect of a row that already exists is never capped.
    assert _post(client, headers, _inspect()) == ["inspected"]

    nocap_bridge, nocap = pair_bridge(
        client, world, owner="kid", sim="+12065550888", caps={"sms": True, "gchat": False, "gvoice": False}
    )
    assert _post(client, nocap, _inspect()) == ["dropped_cap"]
    assert conv_store.get(nocap_bridge.id, "spaces/IN") is None


def test_a_subscribed_row_keeps_isgroup_and_a_custom_title(env: Env):
    _group_subscribe(env)
    ref = conv_store.conv_ref(GROUP)
    env.client.patch(
        f"/api/family/bridges/{env.bridge.id}/conversations/{ref}",
        json={"pagerName": "My team"},
        headers=env.world.admin_headers,
    )
    ev = _ev("Logan", "x", group=False, title="Renamed by the phone")
    ev["kind"] = "inspect"
    env.events(ev)
    row = conv_store.get(env.bridge.id, GROUP)
    assert row.isGroup is True and row.title == "Soccer carpool"
    # Without a custom name the title still follows the phone.
    other = "spaces/ZZZ"
    env.events(_ev("Sam", "hi", other, title="Chess"))
    env.sub(other, {"pagerName": "Chess", "roster": []})
    env.events(_ev("Sam", "hi2", other, title="Chess club"))
    assert conv_store.get(env.bridge.id, other).title == "Chess club"


def test_dismissing_a_chat_alert_resets_the_row(env: Env):
    env.events(_ev("Devon P", "one"), _ev("Devon P", "two"))
    row = conv_store.get(env.bridge.id, GROUP)
    assert row.heldCount == 2 and row.alertId
    r = env.client.post(f"/api/family/alerts/{row.alertId}/dismiss", headers=env.world.admin_headers)
    assert r.status_code == 200
    row = conv_store.get(env.bridge.id, GROUP)
    assert row.status == "seen" and row.heldCount == 0 and row.alertId is None


def test_retried_subscribe_with_a_new_name_renames_the_external(env: Env):
    env.events(_ev("Grandma", "hi", DM, group=False, title="Grandma"))
    out = env.sub(DM, {"pagerName": "Grandma"})
    again = env.sub(DM, {"pagerName": "Gran"})
    assert again["uid"] == out["uid"]
    assert users_store.get_user(out["uid"]).displayName == "Gran"
    assert conv_store.get(env.bridge.id, DM).pagerName == "Gran"
    assert externals_store.get_family_contact is not None


def test_rate_limit_count_parameter():
    f = rate_limits_store.check_and_increment
    assert f("k", limit=10, window_s=60, count=6, now=100.0)
    assert not f("k", limit=10, window_s=60, count=5, now=101.0)  # 6 + 5 > 10, not recorded
    assert f("k", limit=10, window_s=60, count=4, now=102.0)
    assert not f("k", limit=10, window_s=60, now=103.0)
    assert not f("big", limit=10, window_s=60, count=11, now=100.0)
    assert f("k", limit=10, window_s=60, count=10, now=200.0)  # new window


def test_tick_and_sweep_make_no_per_bridge_outbox_queries(
    client: TestClient, world: World, broker: FakeBrokerClient, monkeypatch
):
    old = datetime.now(UTC) - timedelta(hours=25)
    made = []
    for i in range(3):
        b, _ = pair_bridge(client, world, sim=f"+1206555090{i}")
        item = bridge_outbox.enqueue_send(b, f"m_{i}", "sms", source="sms", to={}, text="x")
        get_db().collection("bridges").document(b.id).collection("outbox").document(item.id).update(
            {"createdAt": old}
        )
        made.append((b, item))

    real_outbox = bridge_outbox._outbox

    class _NoScan:
        """Per-document access (ack) is fine; a per-bridge `where` scan is not."""

        def __init__(self, inner):
            self._inner = inner

        def document(self, *a, **k):
            return self._inner.document(*a, **k)

        def where(self, *a, **k):
            raise AssertionError("per-bridge outbox query")

    def boom(*a, **k):
        raise AssertionError("per-bridge outbox query")

    monkeypatch.setattr(bridge_outbox, "_outbox", lambda bid: _NoScan(real_outbox(bid)))
    # One `bridges` listing per tick is fine; a per-bridge outbox query is not.
    result = jobs.tick(Routing(broker), task_queue=InlineTaskQueue())
    assert result.bridgeOutboxFailed == 3
    monkeypatch.setattr(bridge_outbox, "_outbox", real_outbox)
    for b, item in made:
        assert bridge_outbox.get(b.id, item.id).reason == "bridge_offline"
    get_db().collection("bridges").document(made[0][0].id).collection("outbox").document(
        made[0][1].id
    ).update({"ackedAt": datetime.now(UTC) - timedelta(days=8)})
    monkeypatch.setattr(bridge_outbox, "_outbox", lambda bid: _NoScan(real_outbox(bid)))
    assert bridge_outbox.delete_acked_before(datetime.now(UTC) - timedelta(days=7)) == 1
