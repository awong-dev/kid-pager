"""`app.ingest.Ingest` against registered `devices/{id}` docs: drop + system reply (PROTOCOL.md §4.2
case 3), ack routing, and the online-edge republish."""

from __future__ import annotations

import json
import time

import pytest

from app import devauth, devcfg
from app.ingest import Ingest
from app.routing import Routing
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import device_secrets as device_secrets_store
from app.store import devices as devices_store
from app.store import messages as messages_store
from app.store import users as users_store
from tests.conftest import (
    ack_payload,
    offline_status_payload,
    online_status_payload,
    status_topic,
    up_message_payload,
    up_topic,
)
from tests.fake_transport import FakeBrokerClient

# Any fixed 32-byte HMAC key will do.
_HMAC_KEY = b"k" * 32


def _make_user(uid: str, alias: str) -> None:
    users_store.create_user(uid=uid, alias=alias, display_name=alias)


def _make_pager_device(device_id: str, owner_uid: str, *, default_to_uid: str | None = None):
    # These tests send unsigned JSON, so they use the "password" auth mode.
    devices_store.create_device(
        device_id=device_id,
        owner_uid=owner_uid,
        label="d",
        mqtt_username=device_id,
        mqtt_password_hash="x",
        default_to_uid=default_to_uid,
        auth_mode="password",
    )
    backends_store.create_backend(
        owner_uid, kind="pager", config={"deviceId": device_id}, enabled=True
    )


def _make_hmac_pager_device(
    device_id: str, owner_uid: str, *, key: bytes = _HMAC_KEY, default_to_uid: str | None = None
):
    """An `authMode: "hmac"` device with a `deviceSecrets/{device_id}` row."""
    devices_store.create_device(
        device_id=device_id,
        owner_uid=owner_uid,
        label="d",
        mqtt_username=device_id,
        mqtt_password_hash="x",
        default_to_uid=default_to_uid,
        auth_mode="hmac",
    )
    backends_store.create_backend(
        owner_uid, kind="pager", config={"deviceId": device_id}, enabled=True
    )
    device_secrets_store.create(device_id, hmac_key=key, mqtt_password_hash="y")
    return key


def _ingest() -> tuple[Ingest, FakeBrokerClient]:
    broker = FakeBrokerClient()
    routing = Routing(broker)
    return Ingest(broker, routing), broker


def _pager_delivery_state(msg_id: str) -> str:
    msg = messages_store.get_message(msg_id)
    bid = next(bid for bid, d in msg.deliveries.items() if d.kind == "pager")
    return msg.deliveries[bid].state


@pytest.mark.parametrize("alias", ["stranger", "stranger2"], ids=["unknown", "disallowed"])
def test_unknown_or_disallowed_to_alias_gets_one_system_reply_and_is_not_stored(alias):
    _make_user("student", "student")
    if alias == "stranger2":
        # Deliberately no allow edge student->stranger2.
        _make_user("stranger2", "stranger2")
    _make_pager_device("pgr-v2-1", "student")

    ingest, broker = _ingest()
    ingest.handle_up(
        up_topic("pgr-v2-1"),
        json.dumps(
            {
                "v": 1,
                "id": "u_unknown1",
                "ts": int(time.time()),
                "from": "student",
                "to": alias,
                "body": "hi",
                "ack": None,
            }
        ).encode("utf-8"),
    )

    assert len(broker.published) == 1
    sent = json.loads(broker.published[0].payload)
    assert sent["from"] == "system"
    assert sent["body"] == "unknown recipient"

    # The dropped up-message was never stored as a message doc for anyone.
    assert messages_store.get_conversation(messages_store.conv_key("student", alias)) is None


def test_no_to_and_no_allowed_contacts_gets_no_system_reply():
    # §4.2 case 1 (no `to`) has no error path -- an empty broadcast set is
    # silently a no-op, never a system reply.
    _make_user("student", "student")
    _make_pager_device("pgr-v2-3", "student")

    ingest, broker = _ingest()
    ingest.handle_up(up_topic("pgr-v2-3"), up_message_payload("u_broadcast0", "hi"))
    assert broker.published == []


def test_v2_ack_updates_the_right_pager_delivery():
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    allow_store.set_edge("student", "mom", message=True, locate=True)
    _make_pager_device("pgr-v2-4", "student")

    ingest, broker = _ingest()
    routing = ingest._routing
    result = routing.send(
        sender_uid="mom",
        recipient_alias="student",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    msg = result.messages[0]
    assert len(broker.published) == 1

    ingest.handle_up(up_topic("pgr-v2-4"), ack_payload(msg.id, "shown", ts=1_700_000_100))
    refreshed = messages_store.get_message(msg.id)
    pager_bid = next(bid for bid, d in refreshed.deliveries.items() if d.kind == "pager")
    assert refreshed.deliveries[pager_bid].state == "shown"
    assert refreshed.deliveries[pager_bid].shownTs == 1_700_000_100
    assert "pgr-v2-4" not in refreshed.pendingDeviceIds


def test_v2_ack_wrong_device_is_dropped():
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    allow_store.set_edge("student", "mom", message=True, locate=True)
    _make_pager_device("pgr-v2-5a", "student")
    _make_pager_device("pgr-v2-5b", "mom")  # unrelated device owned by someone else

    ingest, _broker = _ingest()
    routing = ingest._routing
    result = routing.send(
        sender_uid="mom",
        recipient_alias="student",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    msg = result.messages[0]

    ingest.handle_up(up_topic("pgr-v2-5b"), ack_payload(msg.id, "shown"))
    refreshed = messages_store.get_message(msg.id)
    pager_bid = next(bid for bid, d in refreshed.deliveries.items() if d.kind == "pager")
    assert refreshed.deliveries[pager_bid].state == "sent"  # unchanged


def test_v2_ack_idempotent_repeat_is_noop():
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    allow_store.set_edge("student", "mom", message=True, locate=True)
    _make_pager_device("pgr-v2-6", "student")

    ingest, _broker = _ingest()
    routing = ingest._routing
    msg = routing.send(
        sender_uid="mom",
        recipient_alias="student",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    ).messages[0]

    ingest.handle_up(up_topic("pgr-v2-6"), ack_payload(msg.id, "shown", ts=1_700_001_000))
    ingest.handle_up(up_topic("pgr-v2-6"), ack_payload(msg.id, "shown", ts=1_700_009_999))
    refreshed = messages_store.get_message(msg.id)
    pager_bid = next(bid for bid, d in refreshed.deliveries.items() if d.kind == "pager")
    assert refreshed.deliveries[pager_bid].shownTs == 1_700_001_000


def test_v2_republish_on_session_change_resends_still_pending():
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    allow_store.set_edge("student", "mom", message=True, locate=True)
    _make_pager_device("pgr-v2-7", "student")

    broker = FakeBrokerClient()
    broker.fail_publish = True
    routing = Routing(broker)
    ingest = Ingest(broker, routing)

    result = routing.send(
        sender_uid="mom",
        recipient_alias="student",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    msg = result.messages[0]
    assert broker.published == []  # publish failed -> stayed queued

    broker.fail_publish = False
    ingest.handle_status(status_topic("pgr-v2-7"), online_status_payload("s_00000001"))
    # This first online /status also bootstraps bookVersion 0 -> 1 and publishes a book
    # (PROTOCOL.md §5.3); filter to the resent message's id.
    matches = [p for p in broker.published if json.loads(p.payload).get("id") == msg.id]
    assert len(matches) == 1


def test_v2_republish_on_link_change_same_session_resends_still_pending():
    """A modem MQTT session resume bumps `link` but not `session` and still republishes."""
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    allow_store.set_edge("student", "mom", message=True, locate=True)
    _make_pager_device("pgr-v2-link-1", "student")

    broker = FakeBrokerClient()
    routing = Routing(broker)
    ingest = Ingest(broker, routing)

    # Baseline: session s_00000001, link 1. The first online /status also publishes a bootstrap
    # book; cleared here.
    ingest.handle_status(
        status_topic("pgr-v2-link-1"), online_status_payload("s_00000001", link=1)
    )
    assert [json.loads(p.payload)["kind"] for p in broker.published] == ["book"]
    broker.clear()

    broker.fail_publish = True
    result = routing.send(
        sender_uid="mom",
        recipient_alias="student",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    msg = result.messages[0]
    assert broker.published == []  # publish failed -> stayed queued

    broker.fail_publish = False
    # Same session, `link` bumped 1 -> 2: a resume, not a reboot.
    ingest.handle_status(
        status_topic("pgr-v2-link-1"), online_status_payload("s_00000001", link=2)
    )
    # The unacked bootstrap book is also republished; filter to the resent message's id.
    matches = [p for p in broker.published if json.loads(p.payload).get("id") == msg.id]
    assert len(matches) == 1


@pytest.mark.parametrize("baseline_link", [1, None], ids=["link_unchanged", "link_newly_appears"])
def test_v2_no_republish_on_an_unchanged_session_when_link_is_unchanged_or_first_reported(baseline_link):
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    _make_pager_device("pgr-v2-link-2", "student")

    broker = FakeBrokerClient()
    routing = Routing(broker)
    ingest = Ingest(broker, routing)

    # The first online /status publishes a bootstrap book (PROTOCOL.md §5.3); cleared here.
    # With `baseline_link` None the device never reported `link`.
    extra = {} if baseline_link is None else {"link": baseline_link}
    ingest.handle_status(status_topic("pgr-v2-link-2"), online_status_payload("s_00000002", **extra))
    assert [json.loads(p.payload)["kind"] for p in broker.published] == ["book"]
    broker.clear()

    broker.fail_publish = True
    routing.send(
        sender_uid="mom",
        recipient_alias="student",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    broker.fail_publish = False

    # Same session, same link (or `link` present for the first time): no edge.
    ingest.handle_status(
        status_topic("pgr-v2-link-2"), online_status_payload("s_00000002", link=1)
    )
    assert broker.published == []
    assert devices_store.get_device("pgr-v2-link-2").status.link == 1


def test_republish_reuses_id_but_gets_a_fresh_n_and_both_publishes_verify():
    """A republish reuses the message `id` with a fresh `n`; both envelopes verify."""
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    allow_store.set_edge("student", "mom", message=True, locate=True)
    key = _make_hmac_pager_device("pgr-hmac-republish", "student")

    broker = FakeBrokerClient()
    routing = Routing(broker)

    msg = routing.send(
        sender_uid="mom",
        recipient_alias="student",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    ).messages[0]
    assert len(broker.published) == 1

    ok = routing.redeliver_pager(msg, "pgr-hmac-republish")
    assert ok
    assert len(broker.published) == 2

    topic = "pager/pgr-hmac-republish/down"
    decoded = []
    for published in broker.published:
        verified, unsigned = devauth.verify(key, topic, published.payload)
        assert verified
        decoded.append(json.loads(unsigned))

    assert decoded[0]["id"] == decoded[1]["id"] == msg.id
    assert decoded[0]["n"] != decoded[1]["n"]


@pytest.mark.parametrize("sign, wire", [(devauth.sign_json, "json"), (devauth.sign_cbor, "cbor")])
def test_hmac_valid_signed_up_message_is_accepted(sign, wire):
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    allow_store.set_edge("student", "mom", message=True, locate=True)
    key = _make_hmac_pager_device("pgr-hmac-1", "student")

    ingest, _broker = _ingest()
    msg = ingest._routing.send(
        sender_uid="mom", recipient_alias="student", kind="text", body="hi",
        origin_backend_kind="webapp",
    ).messages[0]

    topic = up_topic("pgr-hmac-1")
    payload = sign(key, topic, {"v": 1, "id": msg.id, "ts": 1_700_000_100, "ack": "shown", "n": 1})
    ingest.handle_up(topic, payload)

    assert _pager_delivery_state(msg.id) == "shown"
    assert devices_store.get_device("pgr-hmac-1").wire == wire


def test_hmac_device_wire_flips_to_cbor_after_one_cbor_envelope():
    key = _make_hmac_pager_device("pgr-hmac-wire", "student2")
    _make_user("student2", "student2")

    ingest, _broker = _ingest()
    topic = up_topic("pgr-hmac-wire")

    json_payload = devauth.sign_json(
        key, topic, {"v": 1, "id": "m_nonexist1", "ts": 1_700_000_001, "ack": "shown", "n": 1}
    )
    ingest.handle_up(topic, json_payload)
    assert devices_store.get_device("pgr-hmac-wire").wire == "json"

    cbor_payload = devauth.sign_cbor(
        key, topic, {"v": 1, "id": "m_nonexist2", "ts": 1_700_000_002, "ack": "shown", "n": 2}
    )
    ingest.handle_up(topic, cbor_payload)
    assert devices_store.get_device("pgr-hmac-wire").wire == "cbor"


def test_unsigned_up_message_from_hmac_device_is_dropped():
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    allow_store.set_edge("student", "mom", message=True, locate=True)
    _make_hmac_pager_device("pgr-hmac-3", "student")

    ingest, _broker = _ingest()
    msg = ingest._routing.send(
        sender_uid="mom", recipient_alias="student", kind="text", body="hi",
        origin_backend_kind="webapp",
    ).messages[0]

    # No `sig` or `n`: the plain-JSON shape of a password-mode device.
    ingest.handle_up(up_topic("pgr-hmac-3"), ack_payload(msg.id, "shown"))

    assert _pager_delivery_state(msg.id) == "sent"  # unchanged -- dropped before parse
    assert device_secrets_store.get("pgr-hmac-3").sigFailures == 1


def test_replayed_n_is_dropped():
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    allow_store.set_edge("student", "mom", message=True, locate=True)
    key = _make_hmac_pager_device("pgr-hmac-4", "student")

    ingest, _broker = _ingest()
    routing = ingest._routing
    msg1 = routing.send(
        sender_uid="mom", recipient_alias="student", kind="text", body="one",
        origin_backend_kind="webapp",
    ).messages[0]
    msg2 = routing.send(
        sender_uid="mom", recipient_alias="student", kind="text", body="two",
        origin_backend_kind="webapp",
    ).messages[0]

    topic = up_topic("pgr-hmac-4")
    ingest.handle_up(
        topic,
        devauth.sign_json(
            key, topic, {"v": 1, "id": msg1.id, "ts": 1_700_000_001, "ack": "shown", "n": 5}
        ),
    )
    # Same n=5 again, addressed at a different message -- must not be
    # accepted just because the rest of the envelope differs.
    ingest.handle_up(
        topic,
        devauth.sign_json(
            key, topic, {"v": 1, "id": msg2.id, "ts": 1_700_000_002, "ack": "shown", "n": 5}
        ),
    )

    assert _pager_delivery_state(msg1.id) == "shown"
    assert _pager_delivery_state(msg2.id) == "sent"  # replay dropped


def test_n_inside_window_but_unseen_is_accepted():
    _make_user("mom", "mom")
    _make_user("student", "student")
    allow_store.set_edge("mom", "student", message=True, locate=True)
    allow_store.set_edge("student", "mom", message=True, locate=True)
    key = _make_hmac_pager_device("pgr-hmac-5", "student")

    ingest, _broker = _ingest()
    routing = ingest._routing
    msg1 = routing.send(
        sender_uid="mom", recipient_alias="student", kind="text", body="one",
        origin_backend_kind="webapp",
    ).messages[0]
    msg2 = routing.send(
        sender_uid="mom", recipient_alias="student", kind="text", body="two",
        origin_backend_kind="webapp",
    ).messages[0]

    topic = up_topic("pgr-hmac-5")
    ingest.handle_up(
        topic,
        devauth.sign_json(
            key, topic, {"v": 1, "id": msg1.id, "ts": 1_700_000_001, "ack": "shown", "n": 10}
        ),
    )
    # n=8 < upN=10, but the window bit for gap=2 has never been set.
    ingest.handle_up(
        topic,
        devauth.sign_json(
            key, topic, {"v": 1, "id": msg2.id, "ts": 1_700_000_002, "ack": "shown", "n": 8}
        ),
    )

    assert _pager_delivery_state(msg1.id) == "shown"
    assert _pager_delivery_state(msg2.id) == "shown"


def test_unsigned_lwt_status_is_accepted_from_hmac_device():
    key = _make_hmac_pager_device("pgr-hmac-lwt", "student3")
    _make_user("student3", "student3")

    ingest, _broker = _ingest()
    status_t = status_topic("pgr-hmac-lwt")
    # Bring the device online first (signed), so the offline LWT is a real
    # transition worth observing.
    online = devauth.sign_json(
        key,
        status_t,
        {
            "v": 1,
            "state": "online",
            "mode": "sleep",
            "batt_mv": 3300,
            "rssi": -90,
            "session": "s_00000001",
            "ts": 1_700_000_000,
            "n": 1,
        },
    )
    ingest.handle_status(status_t, online)
    assert devices_store.get_device("pgr-hmac-lwt").status.state == "online"

    # The broker-generated LWT: unsigned, exactly {v, state, session}.
    ingest.handle_status(status_t, offline_status_payload("s_00000001"))

    device = devices_store.get_device("pgr-hmac-lwt")
    assert device.status.state == "offline"
    assert device.status.authAlarm is not True
    assert device_secrets_store.get("pgr-hmac-lwt").sigFailures == 0


@pytest.mark.parametrize(
    "fields, expect, logs",
    [
        pytest.param(
            {"tls": "pinned", "ca_fp": "0123456789abcdef", "loc_backoff_s": 40, "sms_lost": 1},
            {"tls": "pinned", "caFp": "0123456789abcdef", "locBackoffS": 40, "smsLost": 1},
            [],
            id="tls_ca_fp_loc_backoff_s_sms_lost",
        ),
        pytest.param({"loc_move_s": 3600}, {"locMoveS": 3600}, [], id="loc_move_s"),
        pytest.param(
            {"rst": 4, "stage": 6, "abn": 2, "link": 1},
            {"rst": 4, "stage": 6, "abn": 2},
            # rst=4 is panic, stage=6 is "input/ui" per the firmware's stage table.
            ["status {dev}: state=online link=1 rst=4 stage=input/ui abn=2"],
            id="rst_stage_abn",
        ),
        pytest.param(
            {"stallcmd": "AT+CEREG?"},
            {"stallcmd": "AT+CEREG?"},
            ["status {dev}: state=online link=None rst=None stage=None abn=None stallcmd=AT+CEREG?"],
            id="stallcmd",
        ),
        # A `stage` index the relay's table predates (future firmware) is logged as the raw int.
        pytest.param({"stage": 200}, {"stage": 200}, ["stage=200"], id="unresolved_stage_index_as_raw_int"),
    ],
)
def test_status_fields_are_persisted_and_logged(caplog, fields, expect, logs):
    _make_user("statususer1", "statususer1")
    _make_pager_device("pgr-status-1", "statususer1")
    ingest, _broker = _ingest()

    with caplog.at_level("INFO", logger="relay.ingest"):
        ingest.handle_status(status_topic("pgr-status-1"), online_status_payload("s_00000001", **fields))

    status = devices_store.get_device("pgr-status-1").status
    for attr, value in expect.items():
        assert getattr(status, attr) == value
    for fragment in logs:
        assert any(fragment.format(dev="pgr-status-1") in rec.message for rec in caplog.records)


@pytest.mark.parametrize(
    "fields, expect, logs, absent",
    [
        # Firmware without `rst`/`stage`/`abn`/`stallcmd` still validates and stores them as `None`; the
        # log line is still emitted and prints `-` for `stallcmd` (PROTOCOL.md §0).
        pytest.param(
            {},
            {"rst": None, "stage": None, "abn": None, "stallcmd": None, "state": "online"},
            [
                "status {dev}: state=online link=None rst=None stage=None abn=None",
                "status {dev}: state=online link=None rst=None stage=None abn=None stallcmd=-",
            ],
            [],
            id="without_rst_stage_abn_stallcmd",
        ),
        pytest.param({"xport": "wifi"}, {"state": "online"}, [], ["xport"], id="retired_xport"),
    ],
)
def test_status_with_absent_or_retired_fields_is_accepted_and_stores_nothing_for_them(
    caplog, fields, expect, logs, absent
):
    _make_user("statususer2", "statususer2")
    _make_pager_device("pgr-status-2", "statususer2")
    ingest, _broker = _ingest()

    with caplog.at_level("INFO", logger="relay.ingest"):
        ingest.handle_status(status_topic("pgr-status-2"), online_status_payload("s_00000001", **fields))

    status = devices_store.get_device("pgr-status-2").status
    for attr, value in expect.items():
        assert getattr(status, attr) == value
    for attr in absent:
        assert not hasattr(status, attr)
    for fragment in logs:
        assert any(fragment.format(dev="pgr-status-2") in rec.message for rec in caplog.records)


@pytest.mark.parametrize(
    "bad_fields",
    [
        pytest.param([{"rst": 256}, {"stage": -1}, {"abn": 65536}], id="out_of_range_rst_stage_abn"),
        pytest.param([{"stallcmd": "A" * 25}, {"stallcmd": "AT+\x00BAD"}], id="bad_stallcmd"),
    ],
)
def test_status_with_malformed_fields_is_rejected(bad_fields):
    _make_user("statususer3", "statususer3")
    _make_pager_device("pgr-status-3", "statususer3")
    ingest, _broker = _ingest()

    for i, fields in enumerate(bad_fields, start=1):
        ingest.handle_status(
            status_topic("pgr-status-3"), online_status_payload(f"s_0000000{i}", **fields)
        )

    # No status was stored for this device.
    assert devices_store.get_device("pgr-status-3").status.state is None


@pytest.mark.parametrize(
    "first_tls, logged",
    [("pinned", True), ("broken", False)],
    ids=["transition_into_broken", "already_broken"],
)
def test_status_logs_security_event_only_on_transition_into_broken(caplog, first_tls, logged):
    _make_user("catrustuser2", "catrustuser2")
    _make_pager_device("pgr-catrust-2", "catrustuser2")
    ingest, _broker = _ingest()

    ingest.handle_status(
        status_topic("pgr-catrust-2"), online_status_payload("s_00000001", tls=first_tls)
    )
    with caplog.at_level("ERROR", logger="relay.ingest"):
        caplog.clear()
        ingest.handle_status(
            status_topic("pgr-catrust-2"), online_status_payload("s_00000002", tls="broken")
        )
    assert any(
        "SECURITY tls-broken device=pgr-catrust-2" in rec.message for rec in caplog.records
    ) is logged
    if logged:
        assert devices_store.get_device("pgr-catrust-2").status.tls == "broken"


def test_status_accepts_n_well_above_the_old_32_bit_ceiling():
    """`n = (epoch << 20) | lo` with a 32-bit epoch can exceed 2**32."""
    key = _make_hmac_pager_device("pgr-bign", "bignuser")
    _make_user("bignuser", "bignuser")
    ingest, _broker = _ingest()
    status_t = status_topic("pgr-bign")

    n_big = (4096 << 20) | 1  # epoch=4096 alone already exceeds a 12-bit epoch
    assert n_big >= 2**32
    payload = devauth.sign_json(
        key,
        status_t,
        {
            "v": 1,
            "state": "online",
            "mode": "sleep",
            "batt_mv": 3300,
            "rssi": -90,
            "session": "s_00000001",
            "ts": 1_700_000_000,
            "n": n_big,
        },
    )
    ingest.handle_status(status_t, payload)
    assert devices_store.get_device("pgr-bign").status.state == "online"
    assert device_secrets_store.get("pgr-bign").upN == n_big


def _signed_online(key: bytes, topic: str, *, ts: int, n: int) -> bytes:
    return devauth.sign_json(
        key,
        topic,
        {
            "v": 1,
            "state": "online",
            "mode": "sleep",
            "batt_mv": 3300,
            "rssi": -90,
            "session": "s_00000001",
            "ts": ts,
            "n": n,
        },
    )


def _books(broker: FakeBrokerClient) -> list[dict]:
    return [json.loads(p.payload) for p in broker.published if json.loads(p.payload).get("kind") == "book"]


def test_status_bootstrap_bumps_zero_to_one_and_pushes_once():
    key = _make_hmac_pager_device("pgr-boot-1", "student-boot1")
    _make_user("student-boot1", "student-boot1")
    ingest, broker = _ingest()
    status_t = status_topic("pgr-boot-1")

    ingest.handle_status(status_t, _signed_online(key, status_t, ts=1_700_000_000, n=1))

    assert devcfg.get_book_version("pgr-boot-1") == 1
    books = _books(broker)
    assert len(books) == 1
    assert books[0]["bv"] == 1
    broker.clear()

    # Second status, same session: no edge, so `bootstrap_book_version` is a no-op
    # (bookVersion is already 1).
    ingest.handle_status(status_t, _signed_online(key, status_t, ts=1_700_000_001, n=2))

    assert devcfg.get_book_version("pgr-boot-1") == 1
    assert _books(broker) == []


def test_status_bv_behind_renudges_same_id(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://relay.example.com")
    key = _make_hmac_pager_device("pgr-renudge-1", "student-renudge1")
    _make_user("student-renudge1", "student-renudge1")
    ingest, broker = _ingest()
    status_t = status_topic("pgr-renudge-1")

    # First online status bootstraps bookVersion 0 -> 1 and publishes a nudge (bpull=1, hmac,
    # PUBLIC_BASE_URL set; PROTOCOL.md §3.7).
    online1 = devauth.sign_json(
        key,
        status_t,
        {
            "v": 1,
            "state": "online",
            "mode": "sleep",
            "batt_mv": 3300,
            "rssi": -90,
            "session": "s_00000001",
            "ts": 1_700_000_000,
            "bpull": 1,
            "n": 1,
        },
    )
    ingest.handle_status(status_t, online1)

    books = [
        json.loads(p.payload) for p in broker.published if json.loads(p.payload).get("kind") == "book"
    ]
    assert len(books) == 1
    first_nudge = books[0]
    assert "url" in first_nudge
    assert first_nudge["bv"] == 1
    broker.clear()

    # Second status: `bv` is behind bookVersion; the pending nudge already carries bv 1, so
    # `renudge_if_behind` re-publishes it under the same `id`.
    online2 = devauth.sign_json(
        key,
        status_t,
        {
            "v": 1,
            "state": "online",
            "mode": "sleep",
            "batt_mv": 3300,
            "rssi": -90,
            "session": "s_00000001",
            "ts": 1_700_000_001,
            "bpull": 1,
            "bv": 0,
            "n": 2,
        },
    )
    ingest.handle_status(status_t, online2)

    books2 = [
        json.loads(p.payload) for p in broker.published if json.loads(p.payload).get("kind") == "book"
    ]
    assert len(books2) == 1
    assert books2[0]["id"] == first_nudge["id"]
    assert "url" in books2[0]


def test_up_message_to_external_alias_without_relay_number_gets_one_sms_not_set_up_reply():
    """The relay never texts an SMS contact (the pager does, from `cfg.sms`):
    an `up` addressed to one takes the §4.2 case-3 path -- one derived-id
    `unknown recipient` reply, nothing stored, even with an explicit edge."""
    from app.db.firestore import get_db
    from app.store import externals as externals_store
    from app.store import families as families_store

    fam = families_store.create_family(name="F", created_by="root").id
    users_store.create_user(uid="student", alias="student", display_name="student", family_id=fam)
    ext = externals_store.get_or_create(fam, "+12065550100", "Grandma")
    allow_store.set_edge("student", ext.uid, message=True, locate=False)
    _make_pager_device("pgr-v2-ext", "student")
    payload = json.dumps(
        {
            "v": 1,
            "id": "u_ext1",
            "ts": int(time.time()),
            "from": "student",
            "to": ext.alias,
            "body": "hi",
            "ack": None,
        }
    ).encode("utf-8")

    ingest, broker = _ingest()
    ingest.handle_up(up_topic("pgr-v2-ext"), payload)
    ingest.handle_up(up_topic("pgr-v2-ext"), payload)  # at-least-once redelivery

    sent = [json.loads(p.payload) for p in broker.published]
    # The student has no relay SMS number.
    assert [m["body"] for m in sent] == ["sms not set up; ask your admin"] * 2
    assert all(m["from"] == "system" for m in sent)
    assert sent[0]["id"] == sent[1]["id"]
    assert list(get_db().collection("messages").stream()) == []
    assert messages_store.get_conversation(messages_store.conv_key("student", ext.uid)) is None


def test_contact_req_for_contact_implied_by_open_policy_is_in_book():
    from app.db.firestore import get_db
    from app.store import contacts as contacts_store
    from app.store import externals as externals_store
    from app.store import families as families_store

    fam = families_store.create_family(name="F", created_by="root").id
    users_store.create_user(uid="student", alias="student", display_name="student", family_id=fam)
    externals_store.get_or_create(fam, "+12065550123", "Gma")  # no edge at all
    _make_pager_device("pgr-v2-imp", "student")
    payload = json.dumps(
        {
            "v": 1,
            "id": "u_imp1",
            "ts": int(time.time()),
            "kind": "contact_req",
            "name": "Gma",
            "ph": "2065550123",
            "ack": None,
        }
    ).encode("utf-8")

    # Under the default `people` policy the contact is not on the pager's
    # list, so the request is an add (the modem owner's settings refuse texting).
    ingest, broker = _ingest()
    ingest.handle_up(up_topic("pgr-v2-imp"), payload)
    row = contacts_store.get_by_device_and_req("pgr-v2-imp", "u_imp1")
    assert row is not None and row.status == "added"
    assert [json.loads(p.payload)["body"] for p in broker.published if b"system" in p.payload] == [
        "Gma: added; settings don't allow texting"
    ]

    # Under `open` the contact is implied: already in the book, no row.
    get_db().collection("users").document("student").update(
        {"policy": {"out": "open", "in": "any"}}
    )
    broker.clear()
    ingest.handle_up(
        up_topic("pgr-v2-imp"), payload.replace(b"u_imp1", b"u_imp2")
    )
    assert contacts_store.get_by_device_and_req("pgr-v2-imp", "u_imp2") is None
    bodies = [json.loads(p.payload)["body"] for p in broker.published]
    assert bodies == ["Gma: already in your book"]


def _added_world(policy: tuple[str, str] = ("people_sms", "people_sms"), device: str = "pgr-ad"):
    """A kid with a relay number and an added (marked) external, no edge."""
    from app.db.firestore import get_db
    from app.store import externals as externals_store
    from app.store import families as families_store

    fam = families_store.create_family(name="F", created_by="root").id
    users_store.create_user(uid="student", alias="student", display_name="student", family_id=fam)
    get_db().collection("users").document("student").update(
        {"policy": {"out": policy[0], "in": policy[1]}}
    )
    users_store.set_sms_number("student", "+12065550999")
    ext = externals_store.get_or_create(fam, "+12065550100", "Grandma")
    get_db().collection("users").document("student").collection("book").document(ext.uid).set(
        {"added": True, "familyId": fam}
    )
    _make_pager_device(device, "student")
    return fam, ext


def _send(ingest: Ingest, device: str, msg_id: str, to: str) -> None:
    ingest.handle_up(
        up_topic(device),
        json.dumps(
            {
                "v": 1,
                "id": msg_id,
                "ts": int(time.time()),
                "from": "student",
                "to": to,
                "body": "hi",
                "ack": None,
            }
        ).encode("utf-8"),
    )


def _system(broker: FakeBrokerClient) -> list[dict]:
    return [d for d in (json.loads(p.payload) for p in broker.published) if d.get("from") == "system"]


def _count_pushes(monkeypatch) -> list[dict]:
    from app import alerts as alerts_module

    pushed: list[dict] = []
    monkeypatch.setattr(
        alerts_module, "_push_alert", lambda family_id, alert, fcm_client=None: pushed.append(alert)
    )
    return pushed


def test_refused_added_fixable_alerts_once_uses_the_nickname_and_stores_nothing(monkeypatch):
    from app.db.firestore import get_db
    from app.store import alerts as alerts_store

    pushed = _count_pushes(monkeypatch)
    fam, ext = _added_world()
    ingest, broker = _ingest()

    _send(ingest, "pgr-ad", "u_ad1", ext.alias)
    _send(ingest, "pgr-ad", "u_ad2", ext.alias)

    replies = _system(broker)
    assert [r["body"] for r in replies] == ["Grandma: needs a parent's OK; resend once approved"] * 2
    assert replies[0]["id"] != replies[1]["id"]
    alerts = alerts_store.list_alerts(fam, "all")
    assert [a.id for a in alerts] == [f"cr_student_{ext.uid}"]
    assert alerts[0].status == "open" and alerts[0].kind == "contact_request"
    assert alerts[0].contactRequestKey is None and alerts[0].peerPhone == "+12065550100"
    assert len(pushed) == 1

    get_db().collection("users").document("student").collection("book").document(ext.uid).update(
        {"nick": "Gran"}
    )
    _send(ingest, "pgr-ad", "u_ad3", ext.alias)
    assert _system(broker)[-1]["body"] == "Gran: needs a parent's OK; resend once approved"

    for msg_id in ("u_ad1", "u_ad2", "u_ad3"):
        assert messages_store.get_message(msg_id) is None
    assert list(get_db().collection("messages").stream()) == []
    assert messages_store.get_conversation(messages_store.conv_key("student", ext.uid)) is None


def test_refused_added_unfixable_no_alert(monkeypatch):
    from app.store import alerts as alerts_store

    pushed = _count_pushes(monkeypatch)
    fam, ext = _added_world(policy=("people", "people"))
    ingest, broker = _ingest()

    _send(ingest, "pgr-ad", "u_ad4", ext.alias)

    assert [r["body"] for r in _system(broker)] == ["Grandma: not allowed"]
    assert alerts_store.list_alerts(fam, "all") == [] and pushed == []


def test_refused_not_added_keeps_unknown_recipient():
    from app.db.firestore import get_db
    from app.store import alerts as alerts_store

    fam, ext = _added_world()
    get_db().collection("users").document("student").collection("book").document(ext.uid).delete()
    ingest, broker = _ingest()

    _send(ingest, "pgr-ad", "u_ad5", ext.alias)

    assert [r["body"] for r in _system(broker)] == ["unknown recipient"]
    assert alerts_store.list_alerts(fam, "all") == []


def test_dismissed_within_24h_replies_not_approved(monkeypatch):
    from app.store import alerts as alerts_store

    pushed = _count_pushes(monkeypatch)
    fam, ext = _added_world()
    ingest, broker = _ingest()
    _send(ingest, "pgr-ad", "u_ad6", ext.alias)
    alerts_store.decide(fam, f"cr_student_{ext.uid}", "dismissed", "parent")

    _send(ingest, "pgr-ad", "u_ad7", ext.alias)

    assert [r["body"] for r in _system(broker)][-1] == "Grandma: not approved"
    assert alerts_store.get(fam, f"cr_student_{ext.uid}").status == "dismissed"
    assert len(pushed) == 1


def test_dismissed_25h_reopens(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.db.firestore import get_db
    from app.store import alerts as alerts_store

    pushed = _count_pushes(monkeypatch)
    fam, ext = _added_world()
    ingest, broker = _ingest()
    _send(ingest, "pgr-ad", "u_ad8", ext.alias)
    alert_id = f"cr_student_{ext.uid}"
    alerts_store.decide(fam, alert_id, "dismissed", "parent")
    get_db().collection("families").document(fam).collection("alerts").document(alert_id).update(
        {"decidedAt": datetime.now(UTC) - timedelta(hours=25)}
    )

    _send(ingest, "pgr-ad", "u_ad9", ext.alias)

    assert [r["body"] for r in _system(broker)][-1] == (
        "Grandma: needs a parent's OK; resend once approved"
    )
    reopened = alerts_store.get(fam, alert_id)
    assert reopened.status == "open" and reopened.decidedAt is None
    assert len(alerts_store.list_alerts(fam, "all")) == 1
    assert len(pushed) == 2


def test_marker_never_permits():
    """Marker + `people_sms` + no edge is still refused by routing."""
    _fam, ext = _added_world()
    result = Routing(FakeBrokerClient()).send(
        sender_uid="student",
        recipient_alias=ext.alias,
        kind="text",
        body="hi",
        origin_backend_kind="pager",
    )
    assert result.messages == [] and [r.reason for r in result.rejected] == ["not_allowed"]
