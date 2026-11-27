"""Move wire pieces: `cfg.mv` builders and slot, `/status` `mvc`/`mv`, the `moving` receipt,
firmware `caps` (docs/MOVE_DESIGN.md §12)."""

from __future__ import annotations

import json
import time

import cbor2
import pytest
from pydantic import ValidationError

from app import devauth, devcfg, firmware, receipts, wirecbor
from app.db.firestore import get_db
from app.ingest import Ingest
from app.store import device_secrets as device_secrets_store
from app.store import devices as devices_store
from app.store import users as users_store
from app.wire import MoveReport, StatusEnvelope
from tests.conftest import online_status_payload, status_topic
from tests.fake_transport import FakeBrokerClient

KEY = b"k" * 32
MOVE_ID = "mv_0123456789ab"
CA_SHA = "ab" * 32
LIMIT = 640
WORST_N = 2**53 - 1


def _device(device_id: str = "pgr-mv-1", *, mvc: bool = True, mb: int | None = None,
            wire: str | None = None, auth: str = "hmac") -> str:
    if users_store.get_user("kid") is None:
        users_store.create_user(uid="kid", alias="kid", display_name="kid")
    devices_store.create_device(
        device_id=device_id, owner_uid="kid", label="d", mqtt_username=device_id,
        mqtt_password_hash="x", auth_mode=auth,
    )
    if auth == "hmac":
        device_secrets_store.create(device_id, hmac_key=KEY, mqtt_password_hash="y")
    if mvc:
        devices_store.update_status(device_id, mvc=1)
    if mb is not None:
        devices_store.update_status(device_id, mb=mb)
    if wire is not None:
        get_db().collection("devices").document(device_id).set({"wire": wire}, merge=True)
    return device_id


def _raw(device_id: str) -> dict:
    return get_db().collection("devices").document(device_id).get().to_dict() or {}


def _signed_sizes(obj: dict) -> tuple[int, int]:
    """Signed CBOR and JSON byte counts at the worst-case `n`."""
    worst = {**obj, "n": WORST_N}
    cbor_len = len(wirecbor.encode(worst)) + 10
    json_len = len(json.dumps(wirecbor.to_json_safe(worst), separators=(",", ":")).encode()) + len(
        ',"sig":""'
    ) + 11
    return cbor_len, json_len


def _max_ca() -> devcfg.MoveCa:
    return devcfg.MoveCa(url="https://" + "a" * 192, sha=CA_SHA)


# ---- wire keys -------------------------------------------------------------------------------


def test_keymap_pins():
    assert wirecbor.KEYMAP["mvc"] == 77 and wirecbor.KEYMAP["mv"] == 78
    assert wirecbor.CFG_KEYMAP["mv"] == 7
    assert wirecbor.MV_KEYMAP == {"id": 0, "host": 1, "port": 2, "ca": 3, "cancel": 4, "ok": 5}
    assert wirecbor.MV_REPORT_KEYMAP == {"id": 0, "st": 1, "rsn": 2, "at": 3}
    assert len(set(wirecbor.KEYMAP.values())) == len(wirecbor.KEYMAP)


def test_cfg_mv_order_cbor_layout_and_round_trip():
    obj = devcfg.build_move_order(
        move_id=MOVE_ID, host="pager.example.net", port=8883,
        ca=devcfg.MoveCa(url="https://pager.example.net/kid-pager/ca/x.pem", sha=CA_SHA),
    )
    raw = wirecbor.encode(obj)
    mv = cbor2.loads(raw)[wirecbor.KEYMAP["cfg"]][7]
    assert mv == {
        0: MOVE_ID, 1: "pager.example.net", 2: 8883,
        3: {0: "https://pager.example.net/kid-pager/ca/x.pem", 1: bytes.fromhex(CA_SHA)},
    }
    assert wirecbor.decode(raw) == obj


def test_cfg_mv_cancel_and_proof_cbor_layout():
    cancel = cbor2.loads(wirecbor.encode(devcfg.build_move_cancel(MOVE_ID)))[38][7]
    assert cancel == {0: MOVE_ID, 4: True}
    proof = cbor2.loads(wirecbor.encode(devcfg.build_move_proof(MOVE_ID, 12345)))[38][7]
    assert proof == {0: MOVE_ID, 5: 12345}


def test_cfg_mv_json_carries_ca_sha_as_base64url():
    obj = devcfg.build_move_order(move_id=MOVE_ID, host="h.example", port=1, ca=_max_ca())
    js = wirecbor.to_json_safe(obj)["cfg"]["mv"]
    assert js["ca"]["sha"] == "q6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6s"
    assert set(js) == {"id", "host", "port", "ca"}


def test_status_mv_report_cbor_round_trip():
    raw = cbor2.dumps({0: 1, 21: "online", 22: "sleep", 25: "s_1", 2: 1, 77: 1,
                       78: {0: MOVE_ID, 1: "fail", 2: "ca", 3: 2, 9: "ignored"}})
    names = wirecbor.decode(raw)
    assert names["mvc"] == 1
    assert names["mv"] == {"id": MOVE_ID, "st": "fail", "rsn": "ca", "at": 2}
    assert cbor2.loads(wirecbor.encode(names))[78] == {0: MOVE_ID, 1: "fail", 2: "ca", 3: 2}


# ---- builders and size limits ----------------------------------------------------------------


def test_envelope_sizes_match_the_design():
    mid = "mv_" + "a" * 13
    order = devcfg.build_move_order(move_id=mid, host="h" * 127, port=65535, ca=_max_ca())
    assert _signed_sizes(order) == (445, 563)
    assert _signed_sizes(devcfg.build_move_cancel(mid)) == (73, 159)
    assert _signed_sizes(devcfg.build_move_proof(mid, WORST_N)) == (81, 167)
    typical = devcfg.build_move_order(
        move_id=MOVE_ID, host="pager.example.net", port=8883,
        ca=devcfg.MoveCa(url="https://pager.example.net/kid-pager/ca/" + "a" * 64 + ".pem", sha=CA_SHA),
    )
    assert _signed_sizes(typical)[1] == 358
    for o in (order, typical):
        assert max(_signed_sizes(o)) <= LIMIT


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": ""},
        {"host": "h" * 128},
        {"host": "bad host"},
        {"host": "ho\nst"},
        {"port": 0},
        {"port": 65536},
        {"port": True},
        {"move_id": "MV_ABC"},
        {"move_id": "ab"},
        {"move_id": "x" * 17},
    ],
)
def test_build_move_order_rejects_out_of_range_fields(kwargs: dict):
    args = {"move_id": MOVE_ID, "host": "h.example", "port": 8883, "ca": None} | kwargs
    with pytest.raises(ValueError):
        devcfg.build_move_order(**args)


def test_build_move_order_accepts_the_limits_and_ip_literals():
    for host in ("h" * 127, "192.0.2.1", "[2001:db8::1]", "2001:db8::1", "a-b_c.example"):
        devcfg.build_move_order(move_id=MOVE_ID, host=host, port=1, ca=None)
    devcfg.build_move_order(move_id="abc", host="h", port=65535, ca=None)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": "http://x.example/ca.pem", "sha": CA_SHA},
        {"url": "https://" + "a" * 193, "sha": CA_SHA},
        {"url": "https://x.example/ca.pem", "sha": "ab" * 31},
        {"url": "https://x.example/ca.pem", "sha": "AB" * 32},
        {"url": "https://x.example/ca.pem", "sha": b"\x00" * 31},
    ],
)
def test_move_ca_rejects_bad_pointers(kwargs: dict):
    with pytest.raises(ValidationError):
        devcfg.MoveCa(**kwargs)


def test_move_ca_url_limit_is_200_bytes_inclusive():
    assert devcfg.MoveCa(url="https://" + "a" * 192, sha=CA_SHA)
    assert devcfg.MoveCa(url="https://" + "a" * 192, sha=bytes.fromhex(CA_SHA)).sha == bytes.fromhex(
        CA_SHA
    )


@pytest.mark.parametrize("n", [-1, WORST_N + 1, True])
def test_build_move_proof_rejects_bad_n(n):
    with pytest.raises(ValueError):
        devcfg.build_move_proof(MOVE_ID, n)


def test_proof_n_bounds():
    assert devcfg.build_move_proof(MOVE_ID, 0)["cfg"]["mv"]["ok"] == 0
    assert devcfg.build_move_proof(MOVE_ID, WORST_N)["cfg"]["mv"]["ok"] == WORST_N


# ---- push, slot, ack, republish --------------------------------------------------------------


def _order_kwargs(broker: FakeBrokerClient) -> dict:
    return {"move_id": MOVE_ID, "host": "h.example", "port": 8883,
            "ca": devcfg.MoveCa(url="https://h.example/ca.pem", sha=CA_SHA), "broker": broker}


def test_push_move_order_signs_stores_and_publishes():
    d = _device()
    broker = FakeBrokerClient()
    assert devcfg.push_move_order(d, **_order_kwargs(broker)) is True
    [pub] = broker.published
    assert pub.topic == f"pager/{d}/down" and pub.qos == 1 and pub.retain is False
    env = json.loads(pub.payload)
    assert env["kind"] == "cfg" and env["cfg"]["mv"]["id"] == MOVE_ID
    assert devauth.verify(KEY, pub.topic, pub.payload)[0]
    slot = _raw(d)["pendingCfgMv"]
    assert slot["id"] == env["id"] and slot["acked"] is False
    assert slot["obj"]["cfg"]["mv"]["host"] == "h.example"


def test_push_move_order_in_cbor_for_a_cbor_device():
    d = _device(wire="cbor")
    broker = FakeBrokerClient()
    assert devcfg.push_move_order(d, **_order_kwargs(broker))
    raw = broker.published[0].payload
    assert devauth.verify(KEY, broker.published[0].topic, raw)[0]
    assert cbor2.loads(raw)[38][7][0] == MOVE_ID
    assert len(raw) <= LIMIT


def test_push_move_order_requires_mvc():
    d = _device(mvc=False)
    broker = FakeBrokerClient()
    assert devcfg.push_move_order(d, **_order_kwargs(broker)) is False
    assert broker.published == [] and "pendingCfgMv" not in _raw(d)


def test_push_move_order_unknown_device_is_false():
    broker = FakeBrokerClient()
    assert devcfg.push_move_order("nope", **_order_kwargs(broker)) is False
    assert devcfg.cancel_move("nope", MOVE_ID, broker) is False
    assert devcfg.push_move_proof("nope", MOVE_ID, 1, broker) is False
    assert broker.published == []


def test_slot_holds_only_the_newest_and_leaves_other_slots():
    d = _device()
    broker = FakeBrokerClient()
    devcfg.push_loc(d, gnss=True, broker=broker)
    devcfg.push_move_order(d, **_order_kwargs(broker))
    first = _raw(d)["pendingCfgMv"]["id"]
    devcfg.cancel_move(d, MOVE_ID, broker)
    raw = _raw(d)
    assert raw["pendingCfgMv"]["id"] != first
    assert raw["pendingCfgMv"]["obj"]["cfg"]["mv"] == {"id": MOVE_ID, "cancel": True}
    assert "pendingCfgLoc" in raw


def test_push_move_proof_publishes_ok_n():
    d = _device()
    broker = FakeBrokerClient()
    assert devcfg.push_move_proof(d, MOVE_ID, 4242, broker)
    assert json.loads(broker.published[0].payload)["cfg"]["mv"] == {"id": MOVE_ID, "ok": 4242}


def test_failed_publish_keeps_the_slot_for_republish():
    d = _device()
    broker = FakeBrokerClient()
    broker.fail_publish = True
    assert devcfg.push_move_order(d, **_order_kwargs(broker)) is False
    assert _raw(d)["pendingCfgMv"]["acked"] is False
    broker.fail_publish = False
    devcfg.republish_pending(d, broker)
    assert json.loads(broker.published[0].payload)["cfg"]["mv"]["id"] == MOVE_ID


def test_ack_marks_the_slot_and_stops_republish():
    d = _device()
    broker = FakeBrokerClient()
    devcfg.push_move_order(d, **_order_kwargs(broker))
    sent_id = json.loads(broker.published[0].payload)["id"]
    broker.clear()
    devcfg.republish_pending(d, broker)
    again = json.loads(broker.published[0].payload)
    assert again["id"] == sent_id  # same envelope id, re-signed with a fresh n
    assert devcfg.ack(d, sent_id) is True
    assert _raw(d)["pendingCfgMv"]["acked"] is True
    broker.clear()
    devcfg.republish_pending(d, broker)
    assert broker.published == []


def test_ack_through_ingest_and_online_edge_republish():
    d = _device(auth="password")
    broker = FakeBrokerClient()
    devcfg.push_move_order(d, **_order_kwargs(broker))
    sent_id = json.loads(broker.published[0].payload)["id"]
    ingest = Ingest(broker, None)  # type: ignore[arg-type]
    broker.clear()
    ingest.handle_status(status_topic(d), online_status_payload("s_00000001", mvc=1))
    assert any(
        json.loads(p.payload).get("cfg", {}).get("mv") for p in broker.published
        if p.topic.endswith("/down")
    )
    assert devcfg.ack(d, sent_id)


# ---- /status mvc and mv ----------------------------------------------------------------------


def _status(**extra) -> dict:
    return {"state": "online", "mode": "sleep", "ts": 1_700_000_000, "session": "s_00000001", **extra}


def test_status_envelope_mvc_only_one_is_legal():
    assert StatusEnvelope.model_validate(_status(mvc=1)).mvc == 1
    for bad in (0, 2, True, "1"):
        assert StatusEnvelope.model_validate(_status(mvc=bad)).mvc is None
    assert StatusEnvelope.model_validate(_status()).mvc is None


def test_status_envelope_mv_report():
    env = StatusEnvelope.model_validate(
        _status(mv={"id": MOVE_ID, "st": "fail", "rsn": "budget", "at": 4})
    )
    assert env.mv == MoveReport(id=MOVE_ID, st="fail", rsn="budget", at=4)
    env = StatusEnvelope.model_validate(_status(mv={"id": MOVE_ID, "st": "pend", "at": 0}))
    assert env.mv is not None and env.mv.rsn is None


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "BAD", "st": "pend", "at": 0},
        {"id": MOVE_ID, "st": "nope", "at": 0},
        {"id": MOVE_ID, "st": "fail", "rsn": "toolong99", "at": 0},
        {"id": MOVE_ID, "st": "pend", "at": 256},
        {"id": MOVE_ID, "st": "pend", "at": -1},
        {"id": MOVE_ID, "st": "pend", "at": "1"},
        {"id": MOVE_ID, "st": "pend"},
        {"st": "pend", "at": 0},
        "text",
        [1],
        None,
    ],
)
def test_status_envelope_malformed_mv_drops_alone(bad):
    env = StatusEnvelope.model_validate(_status(mv=bad, mvc=1, fw="keep"))
    assert env.mv is None and env.mvc == 1 and env.fw == "keep"


def test_status_envelope_mv_limits_are_inclusive():
    for at in (0, 255):
        assert StatusEnvelope.model_validate(_status(mv={"id": "abc", "st": "ok", "at": at})).mv
    assert StatusEnvelope.model_validate(
        _status(mv={"id": "x" * 16, "st": "fail", "rsn": "budget".ljust(8, "x"), "at": 1})
    ).mv


def test_cbor_status_with_mv_reaches_the_envelope():
    raw = cbor2.dumps({0: 1, 21: "online", 22: "sleep", 25: "s_00000001", 2: 1_700_000_000, 77: 1,
                       78: {0: MOVE_ID, 1: "try", 3: 1}})
    env = StatusEnvelope.model_validate(wirecbor.decode(raw))
    assert env.mvc == 1 and env.mv == MoveReport(id=MOVE_ID, st="try", at=1)


def test_ingest_stores_mvc_and_clears_it_when_absent():
    d = _device(mvc=False, auth="password")
    ingest = Ingest(FakeBrokerClient(), None)  # type: ignore[arg-type]
    ingest.handle_status(status_topic(d), online_status_payload("s_00000001", mvc=1))
    assert devices_store.get_device(d).status.mvc == 1
    ingest.handle_status(status_topic(d), online_status_payload("s_00000002"))
    assert devices_store.get_device(d).status.mvc is None


# ---- moving receipt --------------------------------------------------------------------------


def test_moving_receipt_shape_and_signature():
    d = _device(mb=1024)
    broker = FakeBrokerClient()
    device = devices_store.get_device(d)
    assert receipts.send_moving_receipt(device, "u_91c0aa11", broker) is True
    [pub] = broker.published
    obj = json.loads(pub.payload)
    assert {k: v for k, v in obj.items() if k not in ("ts", "n", "sig")} == {
        "v": 1, "id": obj["id"], "kind": "rcpt", "ref": "u_91c0aa11", "st": "failed",
        "rsn": "moving", "ack": None,
    }
    import hashlib

    want = "m_" + hashlib.sha256(f"rcpt:{d}:u_91c0aa11:moving:failed".encode()).hexdigest()[:8]
    assert obj["id"] == want
    assert devauth.verify(KEY, pub.topic, pub.payload)[0]
    assert pub.qos == 1 and pub.retain is False
    assert "pendingBook" not in _raw(d)


def test_moving_receipt_id_is_stable_per_ref():
    d = _device(mb=1024)
    broker = FakeBrokerClient()
    device = devices_store.get_device(d)
    receipts.send_moving_receipt(device, "u_91c0aa11", broker)
    receipts.send_moving_receipt(device, "u_91c0aa11", broker)
    receipts.send_moving_receipt(device, "u_91c0aa12", broker)
    ids = [json.loads(p.payload)["id"] for p in broker.published]
    assert ids[0] == ids[1] != ids[2]


def test_moving_receipt_needs_mb():
    d = _device()
    broker = FakeBrokerClient()
    assert receipts.send_moving_receipt(devices_store.get_device(d), "u_91c0aa11", broker) is False
    assert broker.published == []


def test_moving_receipt_publish_failure_is_false():
    d = _device(mb=1024)
    broker = FakeBrokerClient()
    broker.fail_publish = True
    assert receipts.send_moving_receipt(devices_store.get_device(d), "u_1", broker) is False


def test_moving_receipt_size_at_the_maxima():
    d = _device(mb=1024, wire="cbor")
    broker = FakeBrokerClient()
    ref = "u_" + "9" * 14
    assert len(ref) == 16
    receipts.send_moving_receipt(devices_store.get_device(d), ref, broker)
    cbor_len = len(broker.published[0].payload)
    obj = {"v": 1, "id": "m_" + "a" * 8, "ts": int(time.time()), "kind": "rcpt", "ref": ref,
           "st": "failed", "rsn": "moving", "ack": None, "n": WORST_N}
    assert len(json.dumps(obj, separators=(",", ":"))) + len(',"sig":""') + 11 == 161
    assert cbor_len <= LIMIT


# ---- firmware caps ---------------------------------------------------------------------------


def _build(**extra) -> dict:
    return {
        "id": "a1" * 32, "version": "v1", "size": 1000, "published": 1,
        "full": {"path": "fw/" + "a1" * 8 + "/full.z", "osz": 900, "osha": "c3" * 32},
        "deltas": [], **extra,
    }


def test_fwbuild_caps_default_empty_and_parse():
    assert firmware.FwBuild.model_validate(_build()).caps == []
    assert firmware.FwBuild.model_validate(_build(caps=["mv"])).caps == ["mv"]
    idx = firmware.FirmwareIndex.model_validate({"v": 1, "builds": [_build(caps=["mv"])]})
    assert idx.builds[0].caps == ["mv"]


def test_fwbuild_caps_must_be_a_list_of_strings():
    for bad in ("mv", [1], {"mv": 1}):
        with pytest.raises(ValidationError):
            firmware.FwBuild.model_validate(_build(caps=bad))
