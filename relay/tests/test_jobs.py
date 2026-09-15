"""Tests for `app.jobs.tick()` retries and `app.jobs.sweep()` retention."""

from __future__ import annotations

import json
import time as _time
from datetime import UTC, datetime, timedelta

from app import jobs
from app.db.firestore import get_db
from app.routing import Routing
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import devices as devices_store
from app.store import locations as locations_store
from app.store import messages as messages_store
from app.store import settings as settings_store
from app.store import users as users_store
from app.store.settings import RetentionSetting
from app.tasks import InlineTaskQueue
from tests.fake_transport import FakeBrokerClient


def _make_user(uid: str, alias: str) -> None:
    users_store.create_user(uid=uid, alias=alias, display_name=alias)


def _make_pager_device(device_id: str, owner_uid: str):
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


def test_tick_retries_only_queued_deliveries_capped_oldest_first():
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    _make_pager_device("pgr-tick-1", "student")

    broker = FakeBrokerClient()
    broker.fail_publish = True
    routing = Routing(broker)

    ids = []
    for i in range(12):
        result = routing.send(
            sender_uid="mom",
            recipient_alias="student",
            kind="text",
            body=f"msg {i}",
            origin_backend_kind="webapp",
            wire_id=f"w_tick_{i}",
        )
        ids.append(result.messages[0].id)
    assert broker.published == []

    broker.fail_publish = False
    result = jobs.tick(routing, task_queue=InlineTaskQueue())

    assert result.devicesChecked == 1
    assert result.retriesAttempted == 10
    assert len(broker.published) == 10
    payload_ids = [json.loads(p.payload)["id"] for p in broker.published]
    assert payload_ids == ids[:10]


def test_tick_does_not_retry_already_sent_deliveries():
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    _make_pager_device("pgr-tick-2", "student")

    broker = FakeBrokerClient()
    routing = Routing(broker)
    routing.send(
        sender_uid="mom",
        recipient_alias="student",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    assert len(broker.published) == 1
    broker.clear()

    result = jobs.tick(routing, task_queue=InlineTaskQueue())
    assert result.retriesAttempted == 0
    assert broker.published == []


def test_tick_gives_up_after_five_failed_attempts():
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    _make_pager_device("pgr-tick-fail", "student")

    broker = FakeBrokerClient()
    broker.fail_publish = True
    routing = Routing(broker)
    result = routing.send(
        sender_uid="mom",
        recipient_alias="student",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    msg_id = result.messages[0].id
    pager_bid = next(
        bid for bid, d in result.messages[0].deliveries.items() if d.kind == "pager"
    )
    # The send() delivery attempt counts as attempt 1.
    msg = messages_store.get_message(msg_id)
    assert msg.deliveries[pager_bid].attempts == 1
    assert msg.deliveries[pager_bid].error == "broker publish failed"

    for _ in range(4):
        tick_result = jobs.tick(routing, task_queue=InlineTaskQueue())
        assert tick_result.retriesAttempted == 1

    msg = messages_store.get_message(msg_id)
    assert msg.deliveries[pager_bid].attempts == 5
    assert msg.deliveries[pager_bid].state == "failed"
    assert "pgr-tick-fail" not in msg.pendingDeviceIds

    final_tick = jobs.tick(routing, task_queue=InlineTaskQueue())
    assert final_tick.retriesAttempted == 0
    assert final_tick.devicesChecked == 1


def test_tick_skips_revoked_devices():
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    _make_pager_device("pgr-tick-3", "student")

    broker = FakeBrokerClient()
    broker.fail_publish = True
    routing = Routing(broker)
    routing.send(
        sender_uid="mom",
        recipient_alias="student",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    devices_store.revoke_device("pgr-tick-3")

    result = jobs.tick(routing, task_queue=InlineTaskQueue())
    assert result.devicesChecked == 0
    assert result.retriesAttempted == 0


def _insert_stale_message(
    sender_uid: str, recipient_uid: str, *, days_ago: float, wire_id: str | None = None
) -> str:
    from app.ids import new_id

    msg_id = new_id("m_")
    key = messages_store.conv_key(sender_uid, recipient_uid)
    doc = {
        "seq": 1,
        "convKey": key,
        "uids": sorted([sender_uid, recipient_uid]),
        "senderUid": sender_uid,
        "recipientUid": recipient_uid,
        "kind": "text",
        "body": "stale",
        "loc": None,
        "wireId": wire_id,
        "originBackendKind": "webapp",
        "originBackendId": None,
        "ts": int(_time.time()),
        "createdAt": datetime.now(UTC) - timedelta(days=days_ago),
        "deliveries": {},
        "pendingDeviceIds": [],
    }
    get_db().collection("messages").document(msg_id).set(doc)
    if wire_id:
        get_db().collection("wireIds").document(f"{wire_id}_{recipient_uid}").set(
            {"messageId": msg_id}
        )
    return msg_id


def _insert_stale_location(device_id: str, *, days_ago: float) -> str:
    ts = int(_time.time())
    doc = {
        "ts": ts,
        "fixTs": ts,
        "lat": 1.0,
        "lon": 2.0,
        "accM": 5,
        "src": "gnss",
        "cached": False,
        "reqId": None,
        "createdAt": datetime.now(UTC) - timedelta(days=days_ago),
    }
    _, ref = get_db().collection("devices").document(device_id).collection("locations").add(doc)
    return ref.id


def _insert_stale_loc_wire_id(loc_id: str, device_id: str, *, days_ago: float) -> None:
    get_db().collection("locWireIds").document(loc_id).set(
        {"deviceId": device_id, "createdAt": datetime.now(UTC) - timedelta(days=days_ago)}
    )


def _insert_stale_loc_req(device_id: str, *, days_ago: float) -> None:
    get_db().collection("locReqs").document(device_id).set(
        {
            "messageId": "m_stale",
            "requesterUids": ["x"],
            "createdAt": datetime.now(UTC) - timedelta(days=days_ago),
        }
    )


def test_sweep_deletes_messages_and_their_wire_ids_past_retention():
    settings_store.set_retention(
        messages=RetentionSetting(n=2, unit="weeks"), locations=RetentionSetting(n=1, unit="weeks")
    )
    stale_id = _insert_stale_message("a", "b", days_ago=15, wire_id="w_stale")
    fresh_id = _insert_stale_message("a", "b", days_ago=1, wire_id="w_fresh")

    result = jobs.sweep()

    assert messages_store.get_message(stale_id) is None
    assert messages_store.get_message(fresh_id) is not None
    assert not get_db().collection("wireIds").document("w_stale_b").get().exists
    assert get_db().collection("wireIds").document("w_fresh_b").get().exists
    assert result.messagesDeleted == 1
    assert result.wireIdsDeleted == 1


def test_sweep_deletes_orphaned_wire_ids_independently_of_their_message():
    settings_store.set_retention(
        messages=RetentionSetting(n=2, unit="weeks"), locations=RetentionSetting(n=1, unit="weeks")
    )
    msg = messages_store.create_message(
        sender_uid="orphan-a", recipient_uid="orphan-b", kind="text", ts=1000, body="hi",
        wire_id="w_orphan",
    )
    assert msg is not None
    wire_ref = get_db().collection("wireIds").document("w_orphan_orphan-b")
    assert wire_ref.get().exists

    get_db().collection("messages").document(msg.id).delete()
    wire_ref.update({"createdAt": datetime.now(UTC) - timedelta(days=15)})

    result = jobs.sweep()

    assert not wire_ref.get().exists
    assert result.orphanedWireIdsDeleted == 1
    assert result.wireIdsDeleted == 0


def test_sweep_deletes_conversations_past_retention():
    settings_store.set_retention(
        messages=RetentionSetting(n=2, unit="weeks"), locations=RetentionSetting(n=1, unit="weeks")
    )
    key = messages_store.conv_key("conv-a", "conv-b")
    _insert_stale_message("conv-a", "conv-b", days_ago=15)
    get_db().collection("conversations").document(key).set(
        {
            "uids": sorted(["conv-a", "conv-b"]),
            "lastMessageAt": datetime.now(UTC) - timedelta(days=15),
            "lastPreview": "stale preview text",
            "unread": {"conv-b": 1},
        }
    )
    assert messages_store.get_conversation(key) is not None

    result = jobs.sweep()

    assert messages_store.get_conversation(key) is None
    assert result.conversationsDeleted == 1


def test_sweep_batches_correctly_with_a_small_sweep_batch(monkeypatch):
    monkeypatch.setenv("SWEEP_BATCH", "2")
    settings_store.set_retention(
        messages=RetentionSetting(n=1, unit="days"), locations=RetentionSetting(n=1, unit="days")
    )
    ids = [_insert_stale_message("a", "b", days_ago=5) for _ in range(5)]

    result = jobs.sweep()

    assert result.messagesDeleted == 5
    for mid in ids:
        assert messages_store.get_message(mid) is None


def test_sweep_is_idempotent_across_two_consecutive_calls(monkeypatch):
    monkeypatch.setenv("SWEEP_BATCH", "2")
    settings_store.set_retention(
        messages=RetentionSetting(n=1, unit="days"), locations=RetentionSetting(n=1, unit="days")
    )
    for _ in range(5):
        _insert_stale_message("a", "b", days_ago=5)

    first = jobs.sweep()
    second = jobs.sweep()

    assert first.messagesDeleted == 5
    assert second.messagesDeleted == 0
    assert second.wireIdsDeleted == 0


def test_sweep_respects_days_vs_weeks_units_independently_per_class():
    settings_store.set_retention(
        messages=RetentionSetting(n=1, unit="days"), locations=RetentionSetting(n=1, unit="weeks")
    )
    stale_by_1_day = _insert_stale_message("a", "b", days_ago=2)
    within_1_week = _insert_stale_location("dev-units", days_ago=2)

    result = jobs.sweep()

    assert messages_store.get_message(stale_by_1_day) is None
    assert result.messagesDeleted == 1
    remaining_ids = {f.id for f in locations_store.list_locations("dev-units", limit=10)}
    assert within_1_week in remaining_ids
    assert result.locationsDeleted == 0


def test_sweep_deletes_locations_loc_wire_ids_and_loc_reqs_together():
    settings_store.set_retention(
        messages=RetentionSetting(n=4, unit="weeks"), locations=RetentionSetting(n=1, unit="days")
    )
    stale_loc = _insert_stale_location("dev-loc-sweep", days_ago=3)
    fresh_loc = _insert_stale_location("dev-loc-sweep", days_ago=0.01)
    _insert_stale_loc_wire_id("l_stale1", "dev-loc-sweep", days_ago=3)
    _insert_stale_loc_req("dev-loc-sweep", days_ago=3)

    result = jobs.sweep()

    remaining_ids = {f.id for f in locations_store.list_locations("dev-loc-sweep", limit=10)}
    assert stale_loc not in remaining_ids
    assert fresh_loc in remaining_ids
    assert not get_db().collection("locWireIds").document("l_stale1").get().exists
    assert not get_db().collection("locReqs").document("dev-loc-sweep").get().exists
    assert result.locationsDeleted == 1
    assert result.locWireIdsDeleted == 1
    assert result.locReqsDeleted == 1


def test_sweep_leaves_fresh_documents_untouched():
    settings_store.set_retention(
        messages=RetentionSetting(n=4, unit="weeks"), locations=RetentionSetting(n=1, unit="weeks")
    )
    fresh_msg = _insert_stale_message("a", "b", days_ago=0.01)
    fresh_loc = _insert_stale_location("dev-fresh", days_ago=0.01)

    result = jobs.sweep()

    assert messages_store.get_message(fresh_msg) is not None
    remaining_ids = {f.id for f in locations_store.list_locations("dev-fresh", limit=10)}
    assert fresh_loc in remaining_ids
    assert result.messagesDeleted == 0
    assert result.locationsDeleted == 0


def test_sweep_deletes_expired_gchat_link_codes_and_leaves_unexpired_ones(monkeypatch):
    now = int(_time.time())
    backends_store.set_gchat_link_code("expired1", "a", "bid-a", now - 60)
    backends_store.set_gchat_link_code("stillvalid1", "b", "bid-b", now + 600)

    result = jobs.sweep()

    assert get_db().collection("gchatLinkCodes").document("expired1").get().exists is False
    assert get_db().collection("gchatLinkCodes").document("stillvalid1").get().exists is True
    assert result.gchatLinkCodesDeleted == 1

    second = jobs.sweep()
    assert second.gchatLinkCodesDeleted == 0
    assert get_db().collection("gchatLinkCodes").document("stillvalid1").get().exists is True


def test_sweep_marks_settings_meta_last_swept_at():
    settings_store.set_retention(
        messages=RetentionSetting(n=4, unit="weeks"), locations=RetentionSetting(n=1, unit="weeks")
    )
    before = settings_store.get_meta().lastSweepAt
    assert before is None

    jobs.sweep()

    after = settings_store.get_meta().lastSweepAt
    assert after is not None
