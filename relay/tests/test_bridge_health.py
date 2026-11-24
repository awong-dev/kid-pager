"""`app/bridge_health.py` and `require_bridge`'s `lastSeenAt` touch."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app import bridge_health, jobs
from app.db.firestore import get_db
from app.store import alerts as alerts_store
from app.store import allow as allow_store
from app.store import bridges as bridges_store
from app.store import externals as externals_store
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


def _setup(client: TestClient, world: World, **kw):
    bridge, headers = pair_bridge(client, world, **kw)
    get_db().collection("users").document("kid").update(
        {"policy": {"out": "people_sms", "in": "people_sms"}}
    )
    ext = externals_store.get_or_create(world.family_id, MOM, "Mom")
    allow_store.set_edge("kid", ext.uid, message=True, locate=False)
    make_pager_device("d_kid", "kid")
    get_db().collection("devices").document("d_kid").update({"familyId": world.family_id})
    return bridge, headers


def _heartbeat(client: TestClient, headers: dict, **status) -> dict:
    body = {
        "battery": 80,
        "listenerBound": True,
        "smsDefault": True,
        "accessibility": True,
        **status,
    }
    resp = client.post("/bridge/heartbeat", json={"status": body}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _texts(broker: FakeBrokerClient) -> list[str]:
    out = [json.loads(p.payload) for p in broker.published if p.topic.endswith("/down")]
    return [d["body"] for d in out if d.get("from") == "system"]


def _seen(bridge_id: str, ago_s: int) -> None:
    get_db().collection("bridges").document(bridge_id).update(
        {"lastSeenAt": datetime.now(UTC) - timedelta(seconds=ago_s)}
    )


def test_offline_after_threshold_one_notice_then_restored_by_heartbeat(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    bridge, headers = _setup(client, world)
    _heartbeat(client, headers)
    broker.clear()
    now = int(time.time())
    # Inside the window: nothing.
    bridge_health.evaluate_all(now=now + 14 * 60, broker=broker)
    assert _texts(broker) == []
    # Past 15 minutes of silence: one notice, however many ticks follow.
    for extra in (16 * 60, 17 * 60, 40 * 60):
        bridge_health.evaluate_all(now=now + extra, broker=broker)
    (text,) = _texts(broker)
    assert text.startswith("Bridge phone offline since ") and len(text.encode()) <= 90
    fresh = bridges_store.get(bridge.id)
    assert fresh and fresh.health and fresh.health.state == "offline"
    alert = alerts_store.get(world.family_id, f"sys_bridge_offline_{bridge.id}")
    assert alert is not None and alert.status == "open"
    # The phone talks again: restored at once, and the "down" alert closes.
    broker.clear()
    _heartbeat(client, headers)
    assert _texts(broker) == ["Bridge phone is back."]
    fresh = bridges_store.get(bridge.id)
    assert fresh and fresh.health and fresh.health.state == "ok"
    alert = alerts_store.get(world.family_id, f"sys_bridge_offline_{bridge.id}")
    assert alert is not None and alert.status == "handled"


def test_tick_evaluates_and_repeated_ticks_do_not_duplicate(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    from app.routing import Routing
    from app.tasks import InlineTaskQueue

    bridge, headers = _setup(client, world)
    _heartbeat(client, headers)
    broker.clear()
    _seen(bridge.id, 20 * 60)
    routing = Routing(broker)
    for _ in range(3):
        jobs.tick(routing, task_queue=InlineTaskQueue(), broker=broker)
    assert len(_texts(broker)) == 1


def test_a_failed_notice_leaves_state_unchanged_and_the_next_tick_retries(
    client: TestClient, world: World, broker: FakeBrokerClient, monkeypatch
):
    from app import notices

    bridge, headers = _setup(client, world)
    _heartbeat(client, headers)
    _seen(bridge.id, 20 * 60)
    real = notices.notify
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        raise RuntimeError("alert store down")

    monkeypatch.setattr(notices, "notify", boom)
    assert bridge_health.evaluate_all(broker=broker) == 0  # logged, not raised
    fresh = bridges_store.get(bridge.id)
    assert fresh and (fresh.health is None or fresh.health.state == "ok")
    monkeypatch.setattr(notices, "notify", real)
    broker.clear()
    assert bridge_health.evaluate_all(broker=broker) == 1
    assert len(_texts(broker)) == 1


def test_sms_role_lost_is_degraded_then_restored(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    bridge, headers = _setup(client, world)
    _heartbeat(client, headers)
    broker.clear()
    _heartbeat(client, headers, smsDefault=False)
    (text,) = _texts(broker)
    assert text == "Bridge phone lost SMS role."
    fresh = bridges_store.get(bridge.id)
    assert fresh and fresh.health and fresh.health.state == "degraded"
    assert fresh.health.flags == ["sms"]
    # Same heartbeat again: no second notice.
    _heartbeat(client, headers, smsDefault=False)
    assert len(_texts(broker)) == 1
    # Recovered.
    _heartbeat(client, headers)
    assert _texts(broker)[-1] == "Bridge phone is fully working again."
    alert = alerts_store.get(world.family_id, f"sys_bridge_degraded_{bridge.id}")
    assert alert is not None and alert.status == "handled"
    fresh = bridges_store.get(bridge.id)
    assert fresh and fresh.health and fresh.health.state == "ok" and not fresh.health.flags


def test_a_second_lost_capability_is_news_but_respects_the_hold_off(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    bridge, headers = _setup(client, world)
    _heartbeat(client, headers)
    broker.clear()
    _heartbeat(client, headers, listenerBound=False)
    assert _texts(broker) == ["Bridge phone lost chat alerts."]
    _heartbeat(client, headers, listenerBound=False, accessibility=False)
    # Inside the 60 minute hold-off: recorded, not announced.
    assert len(_texts(broker)) == 1
    fresh = bridges_store.get(bridge.id)
    assert fresh and fresh.health and fresh.health.flags == ["listener", "accessibility"]


def test_a_capability_never_seen_working_is_not_degraded(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    bridge, headers = _setup(client, world, sim=None, voice="+12065550199")
    broker.clear()
    _heartbeat(client, headers, smsDefault=False, listenerBound=False, accessibility=False)
    assert _texts(broker) == []
    fresh = bridges_store.get(bridge.id)
    assert fresh and fresh.health and fresh.health.state == "ok"


def test_low_battery_not_charging_notifies_once_and_clears_quietly(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    bridge, headers = _setup(client, world)
    _heartbeat(client, headers)
    broker.clear()
    _heartbeat(client, headers, battery=12, charging=False)
    _heartbeat(client, headers, battery=11, charging=False)
    assert _texts(broker) == ["Bridge phone battery is 12% and not charging."]
    _heartbeat(client, headers, battery=11, charging=True)
    assert len(_texts(broker)) == 1
    alert = alerts_store.get(world.family_id, f"sys_bridge_battery_{bridge.id}")
    assert alert is not None and alert.status == "handled"


def test_battery_without_a_charging_flag_never_warns(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    _, headers = _setup(client, world)
    broker.clear()
    _heartbeat(client, headers, battery=5)
    assert _texts(broker) == []


def test_heartbeat_stores_the_new_status_fields(client: TestClient, world: World):
    bridge, headers = _setup(client, world)
    _heartbeat(
        client,
        headers,
        charging=True,
        batteryExempt=False,
        carrier={"simOperator": "310260", "simName": "US Mobile", "netName": "T-Mobile"},
    )
    fresh = bridges_store.get(bridge.id)
    assert fresh and fresh.status.charging is True and fresh.status.batteryExempt is False
    assert fresh.status.carrier and fresh.status.carrier.simName == "US Mobile"
    # A later heartbeat without them keeps what is stored.
    _heartbeat(client, headers)
    again = bridges_store.get(bridge.id)
    assert again and again.status.carrier and again.status.carrier.simOperator == "310260"


def test_any_authenticated_request_touches_last_seen_at_once_a_minute(
    client: TestClient, world: World
):
    bridge, headers = _setup(client, world)
    _seen(bridge.id, 5 * 60)
    before = bridges_store.get(bridge.id).lastSeenAt
    assert client.get("/bridge/outbox", headers=headers).status_code == 200
    after = bridges_store.get(bridge.id).lastSeenAt
    assert after > before
    # Inside the minute no write: the stored value does not move.
    assert client.get("/bridge/outbox", headers=headers).status_code == 200
    assert bridges_store.get(bridge.id).lastSeenAt == after


def test_unpair_clears_health_and_closes_the_alerts(
    client: TestClient, world: World, broker: FakeBrokerClient
):
    bridge, headers = _setup(client, world)
    _heartbeat(client, headers)
    bridge_health.evaluate_all(now=int(time.time()) + 3600, broker=broker)
    assert alerts_store.get(world.family_id, f"sys_bridge_offline_{bridge.id}").status == "open"
    resp = client.delete(f"/api/family/bridges/{bridge.id}", headers=world.admin_headers)
    assert resp.status_code == 200 and resp.json()["health"] is None
    assert alerts_store.get(world.family_id, f"sys_bridge_offline_{bridge.id}").status == "handled"
    assert bridge_health.evaluate_all(broker=broker) == 0
