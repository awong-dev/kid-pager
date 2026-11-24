# ruff: noqa: F811
"""Carrier balance bookkeeping, interception and routes."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.carriers import service as svc
from app.carriers.usmobile import SERVICE_NUMBER
from app.db.firestore import get_db
from app.routing import Routing
from app.store import alerts as alerts_store
from app.store import bridge_conversations, bridge_outbox, carrier_log
from app.store import bridges as bridges_store
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
from tests.test_bridge_health import _heartbeat, _setup, _texts

CARRIER_INFO = {"simOperator": "310260", "simName": "US Mobile", "netName": "T-Mobile"}


class Env:
    def __init__(self, client: TestClient, world: World, broker: FakeBrokerClient) -> None:
        self.client = client
        self.world = world
        self.broker = broker
        self.bridge, self.headers = _setup(client, world)
        _heartbeat(client, self.headers, carrier=CARRIER_INFO)
        self.broker.clear()

    def fresh(self) -> bridges_store.Bridge:
        b = bridges_store.get(self.bridge.id)
        assert b is not None
        return b

    def carrier(self):
        return svc.state_of(self.fresh())

    def sms_event(self, text: str, *, sender: str = SERVICE_NUMBER, event_id: str | None = None) -> str:
        event = {
            "id": event_id or f"e{time.time_ns()}",
            "source": "sms",
            "conversation": {"id": "c1", "isGroup": False},
            "sender": {"name": "", "phone": sender},
            "text": text,
            "ts": 1,
        }
        r = self.client.post("/bridge/events", json={"events": [event]}, headers=self.headers)
        assert r.status_code == 200, r.text
        return r.json()["results"][0]["outcome"]

    def ack_sms(self, text: str = "hello", *, parts: int | None = None, state: str = "sent",
                reason: str | None = None, n: int = 0) -> bridge_outbox.OutboxItem:
        item = bridge_outbox.enqueue_send(
            self.fresh(), f"m_{time.time_ns()}", f"bk{n}", source="sms", to={"phone": "+12065550111"}, text=text
        )
        body: dict = {"state": state, "reason": reason, "tier": 1}
        if parts is not None:
            body["parts"] = parts
        r = self.client.post(f"/bridge/outbox/{item.id}/ack", json=body, headers=self.headers)
        assert r.status_code == 204, r.text
        return item

    def tick(self, *, now: int | None = None) -> int:
        return svc.tick(now=now, broker=self.broker)

    def left(self) -> int | None:
        return svc.left_of(self.carrier())

    def pending_checks(self) -> list[bridge_outbox.OutboxItem]:
        return [i for i in bridge_outbox.list_pending(self.bridge.id) if i.id.startswith("ob_cr_bal_")]


@pytest.fixture
def env(client: TestClient, world: World, broker: FakeBrokerClient) -> Env:
    return Env(client, world, broker)


def test_heartbeat_auto_matches_the_carrier_and_a_manual_choice_sticks(env: Env):
    assert env.carrier().id == "usmobile" and env.carrier().manual is False
    r = env.client.put(
        f"/api/family/bridges/{env.bridge.id}/carrier", json={"id": "generic"}, headers=env.world.admin_headers
    )
    assert r.status_code == 200 and r.json()["carrier"]["id"] == "generic" and r.json()["carrier"]["manual"]
    _heartbeat(env.client, env.headers, carrier=CARRIER_INFO)
    assert env.carrier().id == "generic"
    r = env.client.put(
        f"/api/family/bridges/{env.bridge.id}/carrier", json={"id": None}, headers=env.world.admin_headers
    )
    assert r.json()["carrier"]["id"] == "usmobile" and not r.json()["carrier"]["manual"]
    r = env.client.put(
        f"/api/family/bridges/{env.bridge.id}/carrier", json={"id": "nope"}, headers=env.world.admin_headers
    )
    assert r.status_code == 422


def test_a_service_number_text_never_reaches_inbound_text(env: Env, monkeypatch):
    from app.routers import bridge as bridge_router

    def boom(*a, **k):
        raise AssertionError("carrier text reached inbound_text")

    monkeypatch.setattr(bridge_router, "handle_text", boom)
    assert env.sms_event("You have 45 texts remaining.") == "carrier_balance"
    assert env.carrier().balance.texts == 45 and env.carrier().balance.source == "carrier"
    assert all(a.kind == "system" for a in alerts_store.list_alerts(env.world.family_id, "all"))
    assert list(get_db().collection("heldSms").stream()) == []
    assert bridge_conversations.list_for_bridge(env.bridge.id) == []
    (row,) = carrier_log.list_recent(env.bridge.id)
    assert row.dir == "in" and row.text == "You have 45 texts remaining." and row.kind == "balance"
    assert row.party == SERVICE_NUMBER


def test_the_same_event_twice_is_applied_once(env: Env):
    env.sms_event("Top-up successful! 100 texts have been added.", event_id="dup1")
    after_first = env.left()
    assert env.sms_event("Top-up successful! 100 texts have been added.", event_id="dup1") == "carrier_duplicate"
    assert env.left() == after_first
    assert len(carrier_log.list_recent(env.bridge.id)) == 1


@pytest.mark.parametrize(
    ("carrier", "sender", "text"),
    [
        pytest.param(None, "+12065550123", "hello", id="other_sender"),
        pytest.param("generic", SERVICE_NUMBER, "You have 45 texts remaining.", id="generic_carrier"),
    ],
)
def test_texts_not_from_the_carriers_service_number_take_the_ordinary_path(
    env: Env, carrier: str | None, sender: str, text: str
):
    if carrier:
        svc.set_carrier(env.fresh(), carrier)
    assert env.sms_event(text, sender=sender) == "held"
    assert env.carrier().balance is None


def test_unknown_replies_are_kept_raw_and_counted(env: Env):
    assert env.sms_event("Welcome to US Mobile!") == "carrier_unknown"
    assert env.carrier().unknownReplies == 1 and env.carrier().balance is None
    assert carrier_log.list_recent(env.bridge.id)[0].text == "Welcome to US Mobile!"


def test_the_log_keeps_the_last_fifty(env: Env):
    for i in range(55):
        env.sms_event(f"note {i}", event_id=f"n{i}")
    rows = carrier_log.list_recent(env.bridge.id, limit=500)
    assert len(rows) == 50
    assert "note 54" in {r.text for r in rows} and "note 0" not in {r.text for r in rows}


def test_sent_acks_decrement_the_estimate_by_parts(env: Env):
    svc.set_balance(env.fresh(), 100, broker=env.broker)
    env.ack_sms(parts=2)
    assert env.left() == 98
    long_text = "x" * 200  # no `parts`: the relay counts 2 segments itself
    env.ack_sms(long_text, n=1)
    assert env.left() == 96
    env.ack_sms(parts=3, state="failed", reason="sim_failed", n=2)  # failed: not counted
    assert env.left() == 96
    view = svc.view(env.fresh())
    assert view.texts == 96 and view.source == "estimate" and view.sentSince == 4


def test_a_parsed_balance_resets_and_a_manual_one_is_marked(env: Env):
    svc.set_balance(env.fresh(), 100, broker=env.broker)
    assert svc.view(env.fresh()).source == "manual"
    env.ack_sms(parts=1)
    env.sms_event("You have 40 texts left.")
    assert env.left() == 40 and env.carrier().sentSince == 0
    assert svc.view(env.fresh()).source == "carrier"


def test_carrier_commands_are_not_counted_and_skip_delivery_bookkeeping(env: Env):
    svc.set_balance(env.fresh(), 50, broker=env.broker)
    item = svc.enqueue_check(env.fresh(), now=int(time.time()), manual=True)
    assert item is not None and item.id.startswith("ob_cr_bal_")
    assert item.kind == "send" and item.source == "sms" and item.to == {"phone": SERVICE_NUMBER}
    assert item.text == "BALANCE" and item.msgId is None and item.bid is None
    r = env.client.post(
        f"/bridge/outbox/{item.id}/ack", json={"state": "sent", "tier": 1, "parts": 1}, headers=env.headers
    )
    assert r.status_code == 204
    assert env.left() == 50 and env.carrier().sentSince == 0
    kinds = [(r.dir, r.kind) for r in carrier_log.list_recent(env.bridge.id)]
    assert ("out", "check") in kinds and ("out", "ack") in kinds


def test_a_bad_parts_value_never_fails_the_ack(env: Env):
    item = bridge_outbox.enqueue_send(
        env.fresh(), "m_9", "bk", source="sms", to={"phone": "+12065550111"}, text="hi"
    )
    r = env.client.post(
        f"/bridge/outbox/{item.id}/ack", json={"state": "sent", "parts": -5}, headers=env.headers
    )
    assert r.status_code == 204 and env.carrier().sentSince == 1


def test_the_first_tick_asks_the_carrier_once_and_not_again_within_the_hour(env: Env):
    now = int(time.time())
    assert env.tick(now=now) == 1
    (item,) = env.pending_checks()
    assert item.text == "BALANCE" and item.to == {"phone": SERVICE_NUMBER}
    assert env.tick(now=now + 60) == 0
    assert env.tick(now=now + 4000) == 0  # one is still waiting for the phone
    bridge_outbox.ack(env.bridge.id, item.id, "sent", None, 1)
    assert env.tick(now=now + 4000) == 0  # not due: asked 4000 s ago, nothing since
    assert env.carrier().awaiting is True


def test_checks_are_due_daily_after_25_parts_after_a_top_up_and_after_sms_failures(env: Env):
    now = int(time.time())
    env.tick(now=now)
    (item,) = env.pending_checks()
    bridge_outbox.ack(env.bridge.id, item.id, "sent", None, 1)
    env.sms_event("You have 100 texts left.")  # answered: nothing awaited
    assert env.tick(now=now + 7200) == 0
    # After 25 parts.
    env.ack_sms(parts=25)
    assert env.tick(now=now + 7300) == 1
    for p in env.pending_checks():
        bridge_outbox.ack(env.bridge.id, p.id, "sent", None, 1)
    env.sms_event("You have 70 texts left.")
    # After an sms_<code> failure, but never inside the hour.
    env.ack_sms(state="failed", reason="sms_generic", n=1)
    assert env.carrier().checkDue is True
    assert env.tick(now=now + 7400) == 0
    assert env.tick(now=now + 7300 + 3700) == 1
    for p in env.pending_checks():
        bridge_outbox.ack(env.bridge.id, p.id, "sent", None, 1)
    env.sms_event("You have 70 texts left.")
    # A day later.
    later = now + 7300 + 3700 + 86400 + 10
    assert env.tick(now=later) == 1


def test_three_unanswered_checks_stop_the_automatic_ones_and_the_view_says_so(env: Env):
    now = int(time.time())
    t = now
    sent = 0
    for _ in range(6):
        for p in env.pending_checks():
            bridge_outbox.ack(env.bridge.id, p.id, "sent", None, 1)
        sent += env.tick(now=t)
        t += 86400 + 10
    assert sent == 3
    assert svc.estimate_only(env.carrier()) and svc.view(env.fresh()).estimateOnly is True
    # A reply that parses ends it.
    env.sms_event("You have 10 texts left.")
    assert not svc.estimate_only(env.carrier()) and env.carrier().misses == 0


def test_a_generic_carrier_never_asks(env: Env):
    svc.set_carrier(env.fresh(), "generic")
    assert env.tick() == 0 and env.pending_checks() == []
    r = env.client.post(
        f"/api/family/bridges/{env.bridge.id}/carrier/check", headers=env.world.admin_headers
    )
    assert r.status_code == 409 and "by hand" in r.json()["detail"]


def test_check_now_route(env: Env):
    url = f"/api/family/bridges/{env.bridge.id}/carrier/check"
    r = env.client.post(url, headers=env.world.admin_headers)
    assert r.status_code == 202
    (item,) = env.pending_checks()
    assert r.json() == {"outboxId": item.id}
    r = env.client.post(url, headers=env.world.admin_headers)
    assert r.status_code == 429


def test_low_then_out_then_restored_each_announced_once(env: Env):
    svc.set_balance(env.fresh(), 22, broker=env.broker)
    assert _texts(env.broker) == []
    env.ack_sms(parts=2, n=0)  # 20 left: low
    assert _texts(env.broker) == ["Bridge phone has 20 texts left."]
    env.ack_sms(parts=1, n=1)  # 19: still low, no repeat
    svc.evaluate_level(env.bridge.id, broker=env.broker)
    svc.evaluate_level(env.bridge.id, broker=env.broker)
    assert len(_texts(env.broker)) == 1
    env.ack_sms(parts=19, n=2)  # 0 left: out, escalating
    assert _texts(env.broker)[-1] == "Bridge phone is out of texts. SMS pages will not arrive."
    assert len(_texts(env.broker)) == 2
    low = alerts_store.get(env.world.family_id, f"sys_sms_low_{env.bridge.id}")
    out = alerts_store.get(env.world.family_id, f"sys_sms_out_{env.bridge.id}")
    assert low.status == "handled" and out.status == "open"
    env.sms_event("You have 500 texts remaining.")  # restored quietly
    assert len(_texts(env.broker)) == 2
    assert alerts_store.get(env.world.family_id, f"sys_sms_out_{env.bridge.id}").status == "handled"
    assert env.carrier().level == "ok"


def test_the_carrier_saying_out_is_out_even_without_a_number(env: Env):
    env.sms_event("You have no texts remaining on your plan.")
    assert env.carrier().level == "out"
    assert _texts(env.broker) == ["Bridge phone is out of texts. SMS pages will not arrive."]


def test_unlimited_plans_never_warn(env: Env):
    svc.set_balance(env.fresh(), None, broker=env.broker)
    for n in range(3):
        env.ack_sms(parts=50, n=n)
    assert env.carrier().level == "ok" and _texts(env.broker) == []
    view = svc.view(env.fresh())
    assert view.unlimited is True and view.texts is None


def test_tick_retries_a_notice_that_could_not_be_written(env: Env, monkeypatch):
    from app import notices

    real = notices.notify
    monkeypatch.setattr(notices, "notify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    get_db().collection("bridges").document(env.bridge.id).update(
        {"carrier.balance": {"texts": 5, "at": 1, "source": "manual"}}
    )
    with pytest.raises(RuntimeError):
        svc.evaluate_level(env.bridge.id, broker=env.broker)
    assert env.carrier().level == "ok"
    monkeypatch.setattr(notices, "notify", real)
    assert env.tick(now=int(time.time())) >= 0
    assert env.carrier().level == "low" and len(_texts(env.broker)) == 1


def test_get_carrier_route_shape_and_manual_balance(env: Env):
    admin = env.world.admin_headers
    base = f"/api/family/bridges/{env.bridge.id}/carrier"
    env.sms_event("hello?")
    body = env.client.get(base, headers=admin).json()
    assert body["carrier"]["id"] == "usmobile" and body["carrier"]["canCheck"] is True
    assert body["carrier"]["texts"] is None and body["carrier"]["source"] is None
    assert [c["id"] for c in body["carriers"]] == ["usmobile", "generic"]
    assert body["log"][0]["text"] == "hello?" and body["log"][0]["dir"] == "in"
    r = env.client.put(f"{base}/balance", json={"texts": 120}, headers=admin)
    assert r.status_code == 200
    c = r.json()["carrier"]
    assert c["texts"] == 120 and c["source"] == "manual" and c["unlimited"] is False
    r = env.client.put(f"{base}/balance", json={"unlimited": True}, headers=admin)
    assert r.json()["carrier"]["unlimited"] is True
    for bad in ({}, {"texts": 5, "unlimited": True}, {"texts": -1}):
        assert env.client.put(f"{base}/balance", json=bad, headers=admin).status_code == 422


def test_routes_are_family_scoped_and_admin_only(env: Env, client: TestClient):
    assert client.get(f"/api/family/bridges/{env.bridge.id}/carrier").status_code in (401, 403)
    assert (
        client.get("/api/family/bridges/b_nope/carrier", headers=env.world.admin_headers).status_code == 404
    )


def test_bridge_list_carries_health_and_carrier(env: Env):
    (row,) = client_list(env)
    assert row["carrier"]["id"] == "usmobile" and row["health"]["state"] == "ok"


def client_list(env: Env) -> list[dict]:
    r = env.client.get("/api/family/bridges", headers=env.world.admin_headers)
    assert r.status_code == 200
    return r.json()


def test_tick_runs_the_carrier_pass(env: Env):
    jobs.tick(Routing(env.broker), broker=env.broker)
    assert len(env.pending_checks()) == 1
