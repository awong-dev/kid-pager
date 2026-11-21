# ruff: noqa: F811
"""Tests for `app/receipts.py` (PROTOCOL.md §3.2 `rcpt`)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app import clockfmt, deliveries, jobs, receipts, wirecbor
from app.db.firestore import get_db
from app.routing import Routing
from app.store import allow as allow_store
from app.store import devices as devices_store
from app.store import externals as externals_store
from app.store import messages as messages_store
from app.tasks import InlineTaskQueue
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

DEVICE = "d_kid"


def _setup(world: World, *, name: str = "Mom", mb: int | None = None):
    get_db().collection("users").document("kid").update(
        {"policy": {"out": "people_sms", "in": "people_sms"}}
    )
    ext = externals_store.get_or_create(world.family_id, MOM, name)
    allow_store.set_edge("kid", ext.uid, message=True, locate=False)
    make_pager_device(DEVICE, "kid")
    if mb is not None:
        devices_store.update_status(DEVICE, mb=mb)
    return ext


def _send(
    broker: FakeBrokerClient,
    alias: str,
    *,
    origin: str = "pager",
    wire: str = "u_abc123",
):
    result = Routing(broker).send(
        sender_uid="kid",
        recipient_alias=alias,
        kind="text",
        body="hi",
        origin_backend_kind=origin,
        wire_id=wire,
    )
    assert result.rejected == [], result.rejected
    broker.clear()
    return result.messages[0]


def _ack(client, headers, msg, state, reason=None):
    r = client.post(
        f"/bridge/outbox/ob_{msg.id}_sms/ack",
        json={"state": state, "reason": reason, "tier": 1},
        headers=headers,
    )
    assert r.status_code == 204


def _down(broker: FakeBrokerClient) -> list[dict]:
    out = [json.loads(p.payload) for p in broker.published if p.topic.endswith("/down")]
    return [d for d in out if d.get("from") == "system" or d.get("kind") == "rcpt"]


def test_failed_without_mb_is_a_system_row_once(
    client: TestClient, world: World, broker
):
    _, headers = pair_bridge(client, world)
    ext = _setup(world)
    msg = _send(broker, ext.alias)
    _ack(client, headers, msg, "failed", "sms_timeout")
    (down,) = _down(broker)
    assert down["from"] == "system" and "kind" not in down and down["ack"] is None
    when = clockfmt.clock(msg.ts, world.family_id)
    assert down["body"] == f"to Mom {when}: not delivered (sms_timeout)"
    assert (
        down["id"]
        == "m_"
        + __import__("hashlib")
        .sha256(f"rcpt:{DEVICE}:u_abc123:sms:failed".encode())
        .hexdigest()[:8]
    )
    assert broker.published[0].qos == 1
    # a retried ack is not a transition
    _ack(client, headers, msg, "failed", "sms_timeout")
    assert len(_down(broker)) == 1


def test_sent_without_mb_sends_nothing(client: TestClient, world: World, broker):
    _, headers = pair_bridge(client, world)
    ext = _setup(world)
    msg = _send(broker, ext.alias)
    _ack(client, headers, msg, "sent")
    assert _down(broker) == []


def test_mb_device_gets_rcpt_for_sent_and_failed(
    client: TestClient, world: World, broker
):
    _, headers = pair_bridge(client, world)
    ext = _setup(world, mb=1024)
    msg = _send(broker, ext.alias)
    _ack(client, headers, msg, "sent")
    (sent,) = _down(broker)
    assert sent["kind"] == "rcpt" and sent["ref"] == "u_abc123" and sent["st"] == "sent"
    assert (
        "rsn" not in sent
        and "from" not in sent
        and "body" not in sent
        and sent["ack"] is None
    )
    broker.clear()
    msg2 = _send(broker, ext.alias, wire="u_def456")
    _ack(client, headers, msg2, "failed", "sms_timeout")
    (failed,) = _down(broker)
    assert (
        failed["st"] == "failed"
        and failed["rsn"] == "sms_timeout"
        and failed["ref"] == "u_def456"
    )
    assert failed["id"] != sent["id"]
    assert list(failed) == ["v", "id", "ts", "kind", "ref", "st", "rsn", "ack"]


def test_rcpt_cbor_keys_and_reason_clipped(client: TestClient, world: World, broker):
    _, headers = pair_bridge(client, world)
    ext = _setup(world, mb=1024)
    devices_store.set_wire(DEVICE, "cbor")
    msg = _send(broker, ext.alias)
    _ack(client, headers, msg, "failed", "x" * 40)
    (pub,) = [p for p in broker.published if p.topic.endswith("/down")]
    raw = wirecbor.decode(pub.payload)
    assert raw["kind"] == "rcpt" and raw["ref"] == "u_abc123" and raw["st"] == "failed"
    assert raw["rsn"] == "x" * 16
    import cbor2

    keys = set(cbor2.loads(pub.payload))
    assert {74, 75, 46} <= keys


def test_web_origin_and_non_receipt_kinds_are_noops(
    client: TestClient, world: World, broker
):
    _, headers = pair_bridge(client, world)
    ext = _setup(world, mb=1024)
    msg = _send(broker, ext.alias, origin="webapp", wire="u_web")
    _ack(client, headers, msg, "failed", "sms_timeout")
    assert _down(broker) == []
    assert (
        receipts.on_delivery_terminal("nope", "sms", "failed", broker=broker) == "skip"
    )
    # a webapp-kind delivery never produces one even when the message is pager-origin
    m = messages_store.create_message(
        sender_uid="kid",
        recipient_uid="adm",
        kind="text",
        ts=1,
        body="x",
        wire_id="u_w2",
        origin_backend_kind="pager",
        deliveries={"w": {"kind": "webapp", "state": "queued", "attempts": 0}},
    )
    assert deliveries.mark_sent(m.id, "w") is True
    assert _down(broker) == []


def test_tick_bridge_offline_and_unpair_publish(
    client: TestClient, world: World, broker
):
    bridge, _ = pair_bridge(client, world)
    ext = _setup(world)
    msg = _send(broker, ext.alias)
    old = datetime.now(UTC) - timedelta(hours=25)
    get_db().collection("bridges").document(bridge.id).collection("outbox").document(
        f"ob_{msg.id}_sms"
    ).update({"createdAt": old})
    jobs.tick(Routing(broker), task_queue=InlineTaskQueue())
    jobs.tick(Routing(broker), task_queue=InlineTaskQueue())
    (down,) = _down(broker)
    assert down["body"].endswith("not delivered (bridge_offline)")

    broker.clear()
    msg2 = _send(broker, ext.alias, wire="u_two")
    r = client.delete(f"/api/family/bridges/{bridge.id}", headers=world.admin_headers)
    assert r.status_code == 200
    (down2,) = _down(broker)
    assert down2["body"].endswith("not delivered (unpaired)")
    assert messages_store.get_message(msg2.id).deliveries["sms"].error == "unpaired"


def test_inline_failure_publishes(client: TestClient, world: World, broker):
    """`SmsBackend.deliver` fails the delivery inline (`no_bridge`) and the pager is told."""
    from app.backends.sms import SmsBackend
    from app.store.backends import Backend

    ext = _setup(world)
    get_db().collection("users").document("kid").update({"smsNumber": "+12065550100"})
    msg = messages_store.create_message(
        sender_uid="kid",
        recipient_uid=ext.uid,
        kind="text",
        ts=1_700_000_000,
        body="hi",
        wire_id="u_inline",
        origin_backend_kind="pager",
        deliveries={"sms": {"kind": "sms", "state": "queued", "attempts": 0}},
    )
    row = Backend(id="sms", kind="sms", config={"phone": MOM})
    out = SmsBackend().deliver(
        msg, messages_store.get_message(msg.id).deliveries["sms"], row
    )
    assert out.error == "no_bridge"
    (down,) = _down(broker)
    assert down["body"].endswith("not delivered (no_bridge)")


def test_long_cjk_name_keeps_the_body_bounded(client: TestClient, world: World, broker):
    _, headers = pair_bridge(client, world)
    ext = _setup(world, name="王小明" * 6)
    msg = _send(broker, ext.alias)
    _ack(client, headers, msg, "failed", "sms_timeout_and_a_long_tail")
    (down,) = _down(broker)
    assert len(down["body"].encode()) <= receipts.NOTICE_BODY_MAX_BYTES
    assert down["body"].startswith("to 王小明")
    assert "(sms_timeout_and_a_lo)" in down["body"]


def test_nick_beats_display_name(client: TestClient, world: World, broker):
    _, headers = pair_bridge(client, world)
    ext = _setup(world)
    get_db().collection("users").document("kid").collection("book").document(
        ext.uid
    ).set({"nick": "Grandma"})
    msg = _send(broker, ext.alias)
    _ack(client, headers, msg, "failed", "sms_timeout")
    (down,) = _down(broker)
    assert down["body"].startswith("to Grandma ")


def test_broker_down_never_raises(client: TestClient, world: World, broker):
    _, headers = pair_bridge(client, world)
    ext = _setup(world, mb=1024)
    msg = _send(broker, ext.alias)
    broker.fail_publish = True
    _ack(client, headers, msg, "failed", "sms_timeout")
    assert messages_store.get_message(msg.id).deliveries["sms"].state == "failed"


def _send_from(broker: FakeBrokerClient, alias: str, *, wire: str, body: str = "hi", device: str | None = DEVICE):
    result = Routing(broker).send(
        sender_uid="kid",
        recipient_alias=alias,
        kind="text",
        body=body,
        origin_backend_kind="pager",
        wire_id=wire,
        device_id=device,
        origin_device_id=device,
    )
    assert result.rejected == [], result.rejected
    broker.clear()
    return result.messages


def test_origin_device_only_gets_the_receipt(client: TestClient, world: World, broker):
    _, headers = pair_bridge(client, world)
    ext = _setup(world, mb=1024)
    make_pager_device("d_other", "kid")
    devices_store.update_status("d_other", mb=1024)
    (msg,) = _send_from(broker, ext.alias, wire="u_abc123")
    assert msg.originDeviceId == DEVICE
    _ack(client, headers, msg, "sent")
    downs = [p for p in broker.published if p.topic.endswith("/down")]
    assert [p.topic for p in downs] == [f"pager/{DEVICE}/down"]
    # a message without the field (stored before it existed) goes to every device
    broker.clear()
    old = _send(broker, ext.alias, wire="u_old")
    assert old.originDeviceId is None
    _ack(client, headers, old, "sent")
    topics = sorted(p.topic for p in broker.published if p.topic.endswith("/down"))
    assert topics == ["pager/d_kid/down", "pager/d_other/down"]


def test_split_message_ref_is_the_sent_wire_id(client: TestClient, world: World, broker):
    _, headers = pair_bridge(client, world)
    ext = _setup(world, mb=1024)
    body = " ".join(["word"] * 600)  # about 3 KB: three parts
    parts = _send_from(broker, ext.alias, wire="u_split1", body=body)
    assert len(parts) >= 3
    assert [m.wireId for m in parts][:2] == ["u_split1", "u_split1~1"]
    assert {m.originDeviceId for m in parts} == {DEVICE}
    # sent only once every part is sent, and its ref has no ~i suffix
    for m in parts[:-1]:
        _ack(client, headers, m, "sent")
        assert _down(broker) == []
    _ack(client, headers, parts[-1], "sent")
    (sent,) = _down(broker)
    assert sent["ref"] == "u_split1" and sent["st"] == "sent"


def test_split_message_fails_on_the_first_failed_part(
    client: TestClient, world: World, broker
):
    _, headers = pair_bridge(client, world)
    ext = _setup(world, mb=1024)
    parts = _send_from(broker, ext.alias, wire="u_split2", body=" ".join(["word"] * 600))
    _ack(client, headers, parts[0], "sent")
    _ack(client, headers, parts[1], "failed", "sms_timeout")
    (failed,) = _down(broker)
    assert failed["ref"] == "u_split2" and failed["st"] == "failed"
    # a second failing part is quiet, and the last part's `sent` never completes it
    _ack(client, headers, parts[2], "failed", "sms_timeout")
    assert len(_down(broker)) == 1
