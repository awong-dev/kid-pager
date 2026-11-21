# ruff: noqa: F811
"""Subscribed link check tests."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import jobs, link_health
from app.db.firestore import get_db
from app.routing import Routing
from app.store import alerts as alerts_store
from app.store import bridge_conversations as conv_store
from app.store import bridge_outbox
from app.store import bridges as bridges_store
from app.store import families as families_store
from tests.bridge_world import (  # noqa: F401
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
from tests.test_chat_subscribe import GROUP, LINK, ROSTER, Env, _ev


@pytest.fixture
def env(client: TestClient, world: World, broker: FakeBrokerClient) -> Env:
    return Env(client, world, broker)


def _subscribed(env: Env, *, conv: str = GROUP) -> conv_store.BridgeConversation:
    env.events(_ev("Devon P", "who drives?"))
    env.sub(conv, {"pagerName": "Soccer", "canReply": True, "roster": ROSTER})
    row = conv_store.get(env.bridge.id, conv)
    assert row is not None and row.status == "subscribed"
    return row


def _ready(env: Env) -> None:
    """The bridge is `ok` with accessibility on, as after a healthy heartbeat."""
    env.client.post(
        "/bridge/heartbeat",
        json={"status": {"accessibility": True, "smsDefault": True, "listenerBound": True}},
        headers=env.headers,
    )


def _health(env: Env, conv: str = GROUP):
    row = conv_store.get(env.bridge.id, conv)
    assert row is not None
    return row.health


def _sys_alert(env: Env, row: conv_store.BridgeConversation):
    return alerts_store.get(
        env.world.family_id, f"sys_link_unreachable_{link_health.alert_key(row)}"
    )


def _chat_item(env: Env, conv: str, state: str, reason: str | None = None, ob: str = "ob_x_1"):
    return bridge_outbox.OutboxItem(
        id=ob,
        bridgeId=env.bridge.id,
        kind="send",
        source="gchat",
        to={"conversationId": conv, "link": LINK},
        state=state,
        reason=reason,
    )


def test_an_inbound_message_marks_a_subscribed_conversation_ok(env: Env):
    _subscribed(env)
    assert _health(env) is None
    env.events(_ev("Logan", "me"))
    h = _health(env)
    assert h is not None and h.state == "ok" and h.okAt is not None
    first = h.okAt
    # Within the hour the row is not rewritten.
    env.events(_ev("Devon P", "again"))
    assert _health(env).okAt == first


def test_a_seen_conversation_gets_no_health(env: Env):
    env.events(_ev("Devon P", "hello"))
    assert _health(env) is None


def test_sent_ack_is_ok_and_link_failures_are_strikes(env: Env):
    row = _subscribed(env)
    now = int(time.time())
    link_health.on_ack(env.bridge, _chat_item(env, GROUP, "sent"), now=now)
    assert _health(env).state == "ok"
    link_health.on_ack(env.bridge, _chat_item(env, GROUP, "failed", "no_link"), now=now + 10)
    h = _health(env)
    assert h.state == "failing" and h.reason == "no_link" and h.failAt == now + 10
    # A second failure inside the hour is not a second strike.
    link_health.on_ack(env.bridge, _chat_item(env, GROUP, "failed", "ui_changed"), now=now + 600)
    assert _health(env).state == "failing" and _health(env).failAt == now + 10
    assert _sys_alert(env, row) is None
    # An hour after the first: unreachable, a web-only alert.
    env.broker.clear()
    link_health.on_ack(env.bridge, _chat_item(env, GROUP, "failed", "no_match"), now=now + 3700)
    h = _health(env)
    assert h.state == "unreachable"
    alert = _sys_alert(env, row)
    assert alert is not None and alert.status == "open"
    assert alert.preview == "Chat 'Soccer carpool' can't be opened on the bridge phone. Re-add it by link."
    assert not [p for p in env.broker.published if p.topic.endswith("/down")]  # pager=False
    # More failures change nothing visible; a send that works clears it all.
    link_health.on_ack(env.bridge, _chat_item(env, GROUP, "failed", "no_link"), now=now + 4000)
    assert _health(env).state == "unreachable"
    link_health.on_ack(env.bridge, _chat_item(env, GROUP, "sent"), now=now + 5000)
    assert _health(env).state == "ok" and _health(env).failAt is None
    assert _sys_alert(env, row).status == "handled"


def test_failures_that_say_nothing_about_the_link_are_ignored(env: Env):
    _subscribed(env)
    for reason in ("no_accessibility", "sim_failed", "sms_not_default", None):
        link_health.on_ack(env.bridge, _chat_item(env, GROUP, "failed", reason))
    assert _health(env) is None


def test_the_real_ack_route_feeds_the_hook(env: Env):
    _subscribed(env)
    item = bridge_outbox.enqueue_send(
        env.bridge, "m_1", "bk", source="gchat", to={"conversationId": GROUP, "link": LINK}, text="hi"
    )
    r = env.client.post(
        f"/bridge/outbox/{item.id}/ack", json={"state": "failed", "reason": "no_link"}, headers=env.headers
    )
    assert r.status_code == 204
    assert _health(env).state == "failing"


def test_a_conversation_that_is_not_subscribed_is_not_tracked(env: Env):
    env.events(_ev("Devon P", "hello"))  # seen only
    link_health.on_ack(env.bridge, _chat_item(env, GROUP, "failed", "no_link"))
    assert _health(env) is None


def test_tick_enqueues_one_probe_and_marks_the_row(env: Env):
    row = _subscribed(env)
    _ready(env)
    assert jobs.tick(Routing(env.broker), broker=env.broker) is not None
    (item,) = bridge_outbox.list_pending(env.bridge.id)
    assert item.id.startswith("ob_hc_" + row.ref + "_") and item.kind == "inspect"
    assert item.to == {"link": LINK}
    h = _health(env)
    assert h.probeId == item.id and h.probeAt is not None
    assert bridges_store.get(env.bridge.id).linkProbeAt is not None
    # Nothing more within the hour (and one is already pending).
    assert link_health.enqueue_probes() == 0


def test_probes_respect_every_gate(env: Env):
    row = _subscribed(env)
    now = int(time.time())
    # Bridge not yet evaluated as ok.
    assert link_health.enqueue_probes(now=now) == 0
    _ready(env)
    # linkChecks off.
    get_db().collection("families").document(env.world.family_id).update({"settings.linkChecks": False})
    assert link_health.enqueue_probes(now=now) == 0
    get_db().collection("families").document(env.world.family_id).update({"settings.linkChecks": True})
    # Accessibility off.
    get_db().collection("bridges").document(env.bridge.id).update({"status.accessibility": False})
    assert link_health.enqueue_probes(now=now) == 0
    get_db().collection("bridges").document(env.bridge.id).update({"status.accessibility": True})
    # Something else pending.
    pending = bridge_outbox.enqueue_send(env.bridge, "m_9", "bk", source="sms", to={"phone": "+1"}, text="x")
    assert link_health.enqueue_probes(now=now) == 0
    bridge_outbox.ack(env.bridge.id, pending.id, "sent", None, 1)
    # A fresh okAt: nothing to check.
    conv_store.record_ok(row.id, now - 3600)
    assert link_health.enqueue_probes(now=now) == 0
    # Stale for a week: probed.
    assert link_health.enqueue_probes(now=now + 8 * 86400) == 1


def test_a_whatsapp_row_and_a_row_without_a_link_are_never_probed(env: Env):
    row = _subscribed(env)
    _ready(env)
    get_db().collection("bridgeConversations").document(row.id).update({"link": None})
    assert link_health.enqueue_probes() == 0
    get_db().collection("bridgeConversations").document(row.id).update(
        {"link": "https://wa.me/1555", "source": "whatsapp"}
    )
    assert link_health.enqueue_probes() == 0


def _probe(env: Env) -> bridge_outbox.OutboxItem:
    _ready(env)
    assert link_health.enqueue_probes() == 1
    (item,) = bridge_outbox.list_pending(env.bridge.id)
    return item


def _inspect(env: Env, title: str | None, link: str = LINK, conv: str = GROUP) -> str:
    event = {
        "id": f"i{time.time_ns()}",
        "source": "gchat",
        "kind": "inspect",
        "conversation": {"id": conv, "title": title, "isGroup": True, "link": link},
        "sender": {"name": "bridge"},
        "ts": 1,
    }
    return env.events(event)[0]


def test_probe_result_with_a_matching_title_is_ok_and_creates_nothing(env: Env):
    row = _subscribed(env)
    _probe(env)
    rows_before = len(conv_store.list_for_bridge(env.bridge.id))
    alerts_before = len(alerts_store.list_alerts(env.world.family_id, "all"))
    assert _inspect(env, "soccer CARPOOL - Chat") == "probe_ok"  # contains, any case
    h = _health(env)
    assert h.state == "ok" and h.okAt is not None and h.probeId is None
    assert len(conv_store.list_for_bridge(env.bridge.id)) == rows_before
    assert len(alerts_store.list_alerts(env.world.family_id, "all")) == alerts_before
    assert conv_store.get(env.bridge.id, GROUP).inspectedAt is None  # not an "add by link"
    assert row.title == "Soccer carpool"


def test_probe_result_with_another_title_is_a_strike_then_two_is_unreachable(env: Env):
    row = _subscribed(env)
    _probe(env)
    assert _inspect(env, "Some other room") == "probe_failing"
    h = _health(env)
    assert h.state == "failing" and h.reason == "no_match"
    # Next day's probe, more than an hour later: the second strike.
    now = int(time.time()) + 86400
    assert link_health.enqueue_probes(now=now) == 0  # the answered probe's outbox row is still pending
    pending = bridge_outbox.list_pending(env.bridge.id)
    for p in pending:
        bridge_outbox.ack(env.bridge.id, p.id, "sent", None, 2)
    get_db().collection("bridges").document(env.bridge.id).update({"linkProbeAt": 0})
    assert link_health.enqueue_probes(now=now) == 1
    assert link_health.apply_probe_result(env.bridge, LINK, "Nope", now=now + 5) == "probe_failing"
    assert _health(env).state == "unreachable"
    assert _sys_alert(env, row).status == "open"


def test_an_inspect_without_a_probe_marker_is_the_web_flow(env: Env):
    _subscribed(env)
    assert _inspect(env, "Soccer carpool") == "inspected"
    assert conv_store.get(env.bridge.id, GROUP).inspectedAt is not None
    # And for a brand new link it still creates a row.
    new = "https://chat.google.com/room/NEW"
    assert _inspect(env, "Brand new", link=new, conv="spaces/NEW|x") == "inspected"
    assert conv_store.get(env.bridge.id, "spaces/NEW|x") is not None


def test_a_stale_probe_marker_is_not_an_answer(env: Env):
    _subscribed(env)
    _probe(env)
    row = conv_store.get(env.bridge.id, GROUP)
    conv_store.set_probe(row.id, row.health.probeId, int(time.time()) - 3 * 3600)
    assert _inspect(env, "Soccer carpool") == "inspected"


def test_probe_ack_failed_with_ui_changed_is_a_strike_but_no_accessibility_is_not(env: Env):
    _subscribed(env)
    item = _probe(env)
    r = env.client.post(
        f"/bridge/outbox/{item.id}/ack",
        json={"state": "failed", "reason": "no_accessibility", "tier": 2},
        headers=env.headers,
    )
    assert r.status_code == 204
    h = _health(env)
    assert h.state == "ok" and h.probeId is None and h.okAt is None
    item2 = _probe_again(env)
    r = env.client.post(
        f"/bridge/outbox/{item2.id}/ack",
        json={"state": "failed", "reason": "ui_changed", "tier": 2},
        headers=env.headers,
    )
    assert r.status_code == 204
    assert _health(env).state == "failing"


def _probe_again(env: Env) -> bridge_outbox.OutboxItem:
    """The same conversation the next day (the id carries the date)."""
    get_db().collection("bridges").document(env.bridge.id).update({"linkProbeAt": 0})
    assert link_health.enqueue_probes(now=int(time.time()) + 86400) == 1
    (item,) = bridge_outbox.list_pending(env.bridge.id)
    return item


def test_probe_ids_are_per_conversation_per_day(env: Env):
    row = _subscribed(env)
    assert link_health.probe_id(row.ref, 0) == f"ob_hc_{row.ref}_19700101"
    assert link_health.is_probe_id("ob_hc_x") and not link_health.is_probe_id("ob_h_x")


def test_dm_title_matching_rules():
    assert link_health.titles_match(None, "anything")
    assert link_health.titles_match("", None)
    assert not link_health.titles_match("Grandma", None)
    assert link_health.titles_match("Grandma", "grandma - Chat")
    assert link_health.titles_match("Grandma Jo", "Jo")
    assert not link_health.titles_match("Grandma", "Soccer")


def test_conversation_health_is_in_the_family_chat_listing(env: Env, world: World):
    _subscribed(env)
    env.events(_ev("Logan", "me"))
    r = env.client.get("/api/family/members/kid/chat", headers=world.admin_headers)
    assert r.status_code == 200
    (sub,) = r.json()["subscribed"]
    assert sub["health"]["state"] == "ok" and sub["health"]["okAt"] > 0
    assert families_store.get_family(world.family_id).settings.linkChecks is True
