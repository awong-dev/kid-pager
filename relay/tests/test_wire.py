"""Unit tests for app.wire envelope validation, independent of MQTT."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app import devauth, wirecbor
from app.wire import (
    CellInfo,
    DownEnvelope,
    LocEnvelope,
    StatusEnvelope,
    UpEnvelope,
    build_down_payload,
    is_valid_alias,
    parse_envelope_bytes,
    resolve_ts,
    strip_control_chars,
)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            {"v": 1, "id": "m_aaaaaaaa", "ts": 1_700_000_000, "ack": "shown"},
            {"is_ack": True, "body": None},
            id="ack",
        ),
        pytest.param(
            {"v": 1, "id": "u_aaaaaaaa", "ts": 1_700_000_000, "from": "student", "body": "ok", "ack": None},
            {"is_ack": False, "body": "ok"},
            id="content",
        ),
        pytest.param(
            {"v": 1, "id": "m_aaaaaaaa", "ts": 1_700_000_000, "ack": "shown", "prio": "high"},  # reserved, §11
            {"is_ack": True},
            id="unknown-fields-ignored",
        ),
        pytest.param(
            {"v": 1, "id": "u_aaaaaaaa", "ts": 1_700_000_000, "from": "system", "body": "hi", "ack": None},
            {"from_": "system"},
            id="from-system",
        ),
        pytest.param(
            {
                "v": 1,
                "id": "u_aaaaaaaa",
                "ts": 1_700_000_000,
                "from": "student",
                "to": "mom",
                "body": "ok coming",
                "ack": None,
            },
            {"to": "mom"},
            id="to-present",
        ),
        pytest.param(
            {
                "v": 1,
                "id": "u_aaaaaaaa",
                "ts": 1_700_000_000,
                "from": "student",
                "body": "ok coming",
                "ack": None,
            },
            {"to": None},
            id="to-absent",
        ),
    ],
)
def test_up_envelope_valid_shapes(payload: dict, expected: dict):
    env = UpEnvelope.model_validate(payload)
    for attr, value in expected.items():
        assert getattr(env, attr) == value


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"v": 1, "id": "m_aaaaaaaa", "ts": 1_700_000_000, "ack": "shown", "body": "nope"},
            id="ack-with-body",
        ),
        pytest.param(  # PROTOCOL.md §3.1: `to` is restricted to up content messages
            {"v": 1, "id": "m_aaaaaaaa", "ts": 1_700_000_000, "ack": "shown", "to": "mom"},
            id="ack-with-to",
        ),
        pytest.param(
            {"v": 1, "id": "u_aaaaaaaa", "ts": 1_700_000_000, "from": "student", "ack": None},
            id="content-without-body",
        ),
        pytest.param(
            {
                "v": 1,
                "id": "u_aaaaaaaa",
                "ts": 1_700_000_000,
                "from": "student",
                "body": "hi\x01there",
                "ack": None,
            },
            id="body-with-control-char",
        ),
        pytest.param({"v": 1, "id": "BAD ID!", "ts": 1_700_000_000, "ack": "shown"}, id="invalid-id-format"),
        pytest.param(
            {"v": 1, "id": "u_aaaaaaaa", "ts": 1_700_000_000, "from": "Not Valid", "body": "hi", "ack": None},
            id="invalid-from-alias",
        ),
        pytest.param(
            {
                "v": 1,
                "id": "u_aaaaaaaa",
                "ts": 1_700_000_000,
                "from": "student",
                "to": "Bad To!",
                "body": "ok coming",
                "ack": None,
            },
            id="invalid-to-alias",
        ),
    ],
)
def test_up_envelope_malformed_shapes_are_rejected(payload: dict):
    with pytest.raises(ValidationError):
        UpEnvelope.model_validate(payload)


def _up_with_body(body: str) -> dict:
    return {"v": 1, "id": "u_aaaaaaaa", "ts": 1_700_000_000, "from": "student", "body": body, "ack": None}


def test_content_message_body_cap_is_1024_utf8_bytes_with_no_codepoint_cap():
    assert UpEnvelope.model_validate(_up_with_body("a" * 1024)).body == "a" * 1024
    assert UpEnvelope.model_validate(_up_with_body("é" * 512)).body == "é" * 512
    for too_long in ("a" * 1025, "é" * 513):
        with pytest.raises(ValidationError):
            UpEnvelope.model_validate(_up_with_body(too_long))


def _online_status(**overrides: object) -> dict:
    base = {
        "v": 1,
        "state": "online",
        "mode": "sleep",
        "batt_mv": 3280,
        "rssi": -85,
        "session": "s_aabbccdd",
        "ts": 1_700_000_000,
    }
    base.update(overrides)
    return base


# PROTOCOL.md §14.2: `n = (epoch << 20) | lo` with a 32-bit epoch, so `n` can exceed 2**32.
@pytest.mark.parametrize(
    ("n", "accepted"),
    [
        (0, True),
        (2**32 - 1, True),
        (2**32, True),
        (2**32 + 1, True),
        (2**53 - 1, True),
        (2**53, False),
        (2**53 + 1, False),
        (-1, False),
    ],
)
def test_n_is_accepted_up_to_2_53_minus_1_and_rejected_beyond(n: int, accepted: bool):
    cases = [
        (UpEnvelope, {"v": 1, "id": "m_aaaaaaaa", "ts": 1_700_000_000, "ack": "shown", "n": n}),
        (StatusEnvelope, _online_status(n=n)),
    ]
    if accepted:
        loc = {"v": 1, "id": "l_aaaaaaaa", "ts": 1_700_000_000, "loc": None, "req": None, "err": "no_fix", "n": n}
        cases.append((LocEnvelope, loc))
    for model, payload in cases:
        if accepted:
            assert model.model_validate(payload).n == n
        else:
            with pytest.raises(ValidationError):
                model.model_validate(payload)


@pytest.mark.parametrize(
    ("payload", "valid"),
    [
        pytest.param({"v": 1, "state": "online", "session": "s_aabbccdd"}, False, id="online-requires-mode-batt-ts"),
        pytest.param({"v": 1, "state": "offline", "session": "s_aabbccdd"}, True, id="offline-only-needs-session"),
        pytest.param({"v": 1, "state": "offline", "session": "not-a-session"}, False, id="invalid-session-format"),
        pytest.param({k: v for k, v in _online_status().items() if k != "batt_mv"}, True, id="online-without-batt-mv"),
        pytest.param({k: v for k, v in _online_status().items() if k != "mode"}, False, id="online-without-mode"),
        pytest.param({k: v for k, v in _online_status().items() if k != "ts"}, False, id="online-without-ts"),
    ],
)
def test_status_required_core_fields(payload: dict, valid: bool):
    if not valid:
        with pytest.raises(ValidationError):
            StatusEnvelope.model_validate(payload)
        return
    env = StatusEnvelope.model_validate(payload)
    assert env.state == payload["state"]
    assert env.batt_mv == payload.get("batt_mv")


_OPTIONAL_STATUS_FIELDS = (
    "tls", "ca_fp", "loc_backoff_s", "sms_lost", "loc_move_s", "link", "rst", "stage", "abn", "stallcmd",
    "mb", "car",
)


@pytest.mark.parametrize(
    ("overrides", "expected", "ignored"),
    [
        pytest.param(
            {
                "tls": "broken",
                "ca_fp": "0123456789abcdef",
                "loc_backoff_s": 320,
                "sms_lost": 2,
                "loc_move_s": 3600,
                "link": 3,
                "rst": 4,
                "stage": 6,
                "abn": 2,
                "stallcmd": "AT+CEREG?",
            },
            {
                "tls": "broken",
                "ca_fp": "0123456789abcdef",
                "loc_backoff_s": 320,
                "sms_lost": 2,
                "loc_move_s": 3600,
                "link": 3,
                "rst": 4,
                "stage": 6,
                "abn": 2,
                "stallcmd": "AT+CEREG?",
            },
            (),
            id="full",
        ),
        pytest.param({}, dict.fromkeys(_OPTIONAL_STATUS_FIELDS), (), id="minimal-all-absent-as-none"),
        pytest.param({"tls": "proxy"}, {"tls": "proxy"}, (), id="tls-proxy"),
        pytest.param({"link": 0}, {"link": 0}, (), id="link-zero-is-not-none"),
        pytest.param({"xport": "wifi"}, {}, ("xport",), id="retired-xport-ignored"),
        pytest.param({"some_future_field": "whatever"}, {}, ("some_future_field",), id="unknown-field-ignored"),
    ],
)
def test_status_optional_fields_are_accepted_or_absent(overrides: dict, expected: dict, ignored: tuple[str, ...]):
    env = StatusEnvelope.model_validate(_online_status(**overrides))
    assert env.state == "online"
    for attr, value in expected.items():
        assert getattr(env, attr) == value
    for name in ignored:
        assert not hasattr(env, name)


@pytest.mark.parametrize(
    ("field", "ok", "rejected", "dropped"),
    [
        pytest.param("mb", (1024,), (), (0, -1, 8193), id="mb"),
        pytest.param("car", ("Soracom", "x" * 24), (), ("x" * 25,), id="car"),
        pytest.param("tls", (), ("not-a-state",), (), id="tls"),
        pytest.param("ca_fp", (), ("too-short", "0123456789ABCDEF"), (), id="ca_fp"),  # must be lowercase
        pytest.param("sms_lost", (), (-1,), (), id="sms_lost"),
        pytest.param("loc_move_s", (0, 86400), (-1, 86401), (), id="loc_move_s"),
        pytest.param("link", (), (-1,), (), id="link"),
        pytest.param("rst", (0, 255), (-1, 256), (), id="rst"),
        pytest.param("stage", (0, 255), (-1, 256), (), id="stage"),
        pytest.param("abn", (0, 65535), (-1, 65536), (), id="abn"),
        pytest.param("stallcmd", ("", "A" * 24), ("A" * 25, "AT+CEREG\x00", "AT+é"), (), id="stallcmd"),
        pytest.param("batt_mv", (0, 5203, 10000), (-1, 10001), (), id="batt_mv"),
        pytest.param("temp_dc", (235, -400, 1250), (-401, 1251), (), id="temp_dc"),
    ],
)
def test_status_field_bounds(field: str, ok: tuple, rejected: tuple, dropped: tuple):
    """`ok` values are kept, `rejected` ones fail validation, `dropped` ones validate as absent."""
    for value in ok:
        assert getattr(StatusEnvelope.model_validate(_online_status(**{field: value})), field) == value
    for value in rejected:
        with pytest.raises(ValidationError):
            StatusEnvelope.model_validate(_online_status(**{field: value}))
    for value in dropped:
        assert getattr(StatusEnvelope.model_validate(_online_status(**{field: value})), field) is None


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("tls", 39),
        ("rst", 53),
        ("stage", 54),
        ("abn", 55),
        ("stallcmd", 59),
        ("why", 60),  # `/loc`; equal to firmware's key
        ("loc_move_s", 61),  # `/status`
        ("car", 70),
        ("temp_dc", 71),
        ("mb", 76),
    ],
)
def test_cbor_keymap_pins(name: str, key: int):
    """PROTOCOL.md §10."""
    assert wirecbor.KEYMAP[name] == key
    assert 52 not in wirecbor.KEYMAP.values()
    assert len(set(wirecbor.KEYMAP.values())) == len(wirecbor.KEYMAP)


@pytest.mark.parametrize(
    "overrides",
    [
        {"mb": 1024},
        {"rst": 4, "stage": 6, "abn": 2},
        {"stallcmd": "AT+CEREG?"},
        {"car": "Soracom", "tls": "proxy"},
        {"temp_dc": -35},
    ],
    ids=["mb", "rst-stage-abn", "stallcmd", "car-tls", "temp_dc"],
)
def test_status_fields_round_trip_cbor(overrides: dict):
    obj = _online_status(**overrides)
    wire = wirecbor.translate_to_int(obj)
    for name, value in overrides.items():
        assert wire[wirecbor.KEYMAP[name]] == value
    assert wirecbor.translate_to_names({wirecbor.KEYMAP[k]: v for k, v in overrides.items()}) == overrides
    decoded = wirecbor.decode(wirecbor.encode(obj))
    assert decoded == obj
    env = StatusEnvelope.model_validate(decoded)
    for name, value in overrides.items():
        assert getattr(env, name) == value


def test_resolve_ts_substitutes_for_zero():
    assert resolve_ts(1_700_000_000) == 1_700_000_000
    assert resolve_ts(0) > 0


def test_strip_control_chars():
    assert strip_control_chars("a\x00b\x1fc\x7fd") == "abcd"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(b'{"body":"' + b"x" * 1300 + b'"}', None, id="oversize"),
        pytest.param(b"\xff\xfe", None, id="invalid-utf8"),
        pytest.param(b"[1,2,3]", None, id="array-not-object"),
        pytest.param(b'"just a string"', None, id="string-not-object"),
        pytest.param(b'{"a":1}', {"a": 1}, id="valid-object"),
    ],
)
def test_parse_envelope_bytes(raw: bytes, expected: dict | None):
    assert parse_envelope_bytes(raw) == expected


@pytest.mark.parametrize(
    ("alias", "valid"),
    [
        ("parent", True),
        ("student", True),
        ("mom", True),
        ("system", True),
        ("a", True),  # 1 char, minimum length
        ("a" * 16, True),  # 16 chars, maximum length
        ("user-1", True),
        ("user_1", True),
        ("u1_-a", True),
        ("0start", True),  # may start with a digit
        ("a" * 17, False),  # too long
        ("Parent", False),  # uppercase
        ("-leading-hyphen", False),
        ("_leading-underscore", False),
        ("", False),  # empty
        ("has space", False),
        ("hasüni", False),
    ],
)
def test_alias_shapes(alias: str, valid: bool):
    assert is_valid_alias(alias) is valid


def test_up_worst_case_up_message_fits_640_byte_hard_limit():
    """PROTOCOL.md §3.3: a maximal up message with `to` stays under the 640-byte hard limit. A body
    of 160 two-byte code points minifies to 438 bytes with `ensure_ascii=False` (raw UTF-8)."""
    body = "ñ" * 160  # 160 code points, 320 UTF-8 bytes
    obj = {
        "v": 1,
        "id": "u" * 16,
        "ts": 1_700_000_000,
        "from": "a" * 16,
        "to": "b" * 16,
        "body": body,
        "ack": None,
    }
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(raw) <= 640
    assert len(raw) > 400  # sanity: this really is a near-worst-case payload

    data = parse_envelope_bytes(raw)
    assert data is not None
    env = UpEnvelope.model_validate(data)
    assert env.from_ == "a" * 16
    assert env.to == "b" * 16
    assert env.body == body


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            {"v": 1, "id": "m_aaaaaaaa", "ts": 1_700_000_000, "from": "parent", "body": "hi", "ack": None},
            {"kind": "msg"},
            id="default-kind-is-msg",
        ),
        pytest.param(
            {"v": 1, "id": "m_aaaaaaaa", "ts": 1_700_000_000, "kind": "loc_req", "from": "mom", "ack": None},
            {"kind": "loc_req", "body": None},
            id="loc-req-has-no-body",
        ),
        pytest.param(
            {"v": 1, "id": "m_aaaaaaaa", "ts": 1_700_000_000, "from": "mom", "body": "hi", "ack": None},
            {"sndr": None},
            id="sndr-absent",
        ),
        pytest.param(
            {
                "v": 1,
                "id": "m_aaaaaaaa",
                "ts": 1_700_000_000,
                "from": "family",
                "sndr": "mom",
                "body": "hi",
                "ack": None,
            },
            {"sndr": "mom", "from_": "family"},
            id="sndr-on-group-msg",
        ),
    ],
)
def test_down_envelope_valid_shapes(payload: dict, expected: dict):
    env = DownEnvelope.model_validate(payload)
    for attr, value in expected.items():
        assert getattr(env, attr) == value


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "v": 1,
                "id": "m_aaaaaaaa",
                "ts": 1_700_000_000,
                "kind": "loc_req",
                "from": "mom",
                "body": "should not be here",
                "ack": None,
            },
            id="loc-req-with-body",
        ),
        pytest.param(
            {"v": 1, "id": "m_aaaaaaaa", "ts": 1_700_000_000, "from": "parent", "ack": None},
            id="msg-without-body",
        ),
        pytest.param(
            {
                "v": 1,
                "id": "m_aaaaaaaa",
                "ts": 1_700_000_000,
                "from": "family",
                "sndr": "Bad Alias!",
                "body": "hi",
                "ack": None,
            },
            id="invalid-sndr-alias",
        ),
        pytest.param(  # PROTOCOL.md §4: `sndr` only appears on a `msg`
            {
                "v": 1,
                "id": "m_aaaaaaaa",
                "ts": 1_700_000_000,
                "kind": "loc_req",
                "from": "mom",
                "sndr": "mom",
                "ack": None,
            },
            id="sndr-on-loc-req",
        ),
    ],
)
def test_down_envelope_malformed_shapes_are_rejected(payload: dict):
    with pytest.raises(ValidationError):
        DownEnvelope.model_validate(payload)


def test_build_down_payload_loc_req_has_no_body_key():
    raw = build_down_payload(
        msg_id="m_aaaaaaaa", ts=1_700_000_000, kind="loc_req", from_="mom"
    )
    obj = json.loads(raw)
    assert obj["kind"] == "loc_req"
    assert "body" not in obj
    assert obj["ack"] is None
    assert len(raw) <= 640
    # Round-trips through the same shape validation used elsewhere.
    DownEnvelope.model_validate(obj)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({}, id="msg-without-body"),
        pytest.param({"body": ""}, id="msg-with-empty-body"),
        pytest.param({"kind": "loc_req", "sndr": "mom"}, id="sndr-on-loc-req"),
    ],
)
def test_build_down_payload_raises(kwargs: dict):
    with pytest.raises(ValueError):
        build_down_payload(msg_id="m_aaaaaaaa", ts=1_700_000_000, **kwargs)


def test_build_down_payload_dm_bytes_byte_identical_regardless_of_sndr_support():
    raw = build_down_payload(msg_id="m_aaaaaaaa", ts=1_700_000_000, body="hi", from_="mom")
    assert raw == b'{"v":1,"id":"m_aaaaaaaa","ts":1700000000,"from":"mom","body":"hi","ack":null}'


def test_build_down_payload_with_sndr_round_trips_and_orders_after_from():
    raw = build_down_payload(
        msg_id="m_aaaaaaaa", ts=1_700_000_000, body="hi", from_="family", sndr="mom"
    )
    obj = json.loads(raw)
    assert obj["sndr"] == "mom"
    # Field order: from, sndr, body (sndr takes the slot `to` would occupy).
    keys = list(obj.keys())
    assert keys.index("from") < keys.index("sndr") < keys.index("body")
    DownEnvelope.model_validate(obj)


def test_group_down_page_carries_sndr_in_both_encodings():
    raw = build_down_payload(
        msg_id="m_aaaaaaaa", ts=1_700_000_000, body="hi", from_="family", sndr="mom"
    )
    obj = json.loads(raw)
    assert obj["sndr"] == "mom"

    cbor_bytes = wirecbor.encode(obj)
    assert wirecbor.KEYMAP["sndr"] == 51
    decoded = wirecbor.decode(cbor_bytes)
    assert decoded["sndr"] == "mom"
    assert decoded["from"] == "family"


def test_down_worst_case_group_message_signed_fits_640_bytes_both_encodings():
    """Worst-case signed group `/down msg` (16-char id, `from` and `sndr`, 160-code-point body) is
    under the 640-byte hard limit in both encodings, signed with the real `app.devauth` signer."""
    body = "ñ" * 160  # 160 code points, 320 UTF-8 bytes
    obj = {
        "v": 1,
        "id": "m" * 16,
        "ts": 1_700_000_000,
        "from": "g" * 16,
        "sndr": "s" * 16,
        "body": body,
        "n": 2**53 - 1,  # worst-case 52-bit replay counter (§14.2)
        "ack": None,
    }
    key = b"k" * 32
    topic = "pager/pgr-0001/down"

    json_signed = devauth.sign_json(key, topic, obj)
    assert len(json_signed) <= 640
    assert len(json_signed) > 400  # sanity: a near-worst-case payload

    cbor_signed = devauth.sign_cbor(key, topic, obj)
    assert len(cbor_signed) <= 640

    # The unsigned shape (without `n`, which DownEnvelope does not model) is a valid group `msg`.
    DownEnvelope.model_validate({k: v for k, v in obj.items() if k != "n"})


def _base_loc(**overrides):
    obj = {
        "v": 1,
        "id": "l_3c9a11f0",
        "ts": 1_700_000_000,
        "loc": {
            "lat": 37.774929,
            "lon": -122.419416,
            "acc": 14,
            "fix_ts": 1_700_000_000,
            "src": "gnss",
        },
        "req": None,
        "cached": False,
    }
    obj.update(overrides)
    return obj


def test_signed_cell_only_loc_with_why_verifies_and_parses():
    key = b"k" * 32
    topic = "pager/pgr-1/loc"
    obj = _base_loc(
        loc=None,
        err="no_fix",
        cell={"mcc": "310", "mnc": "410", "tac": 1234, "ci": 56789, "rsrp": -95},
        why="still",
        n=(7 << 20) | 3,
    )
    payload = devauth.sign_cbor(key, topic, obj)
    assert len(payload) < 640
    ok, unsigned = devauth.verify(key, topic, payload)
    assert ok
    decoded = wirecbor.decode(unsigned)
    assert decoded["why"] == "still"
    env = LocEnvelope.model_validate(decoded)
    assert env.loc is None and env.err == "no_fix" and env.req is None
    assert env.why == "still"


# LocEnvelope.why: known values accepted, anything else stored as absent (PROTOCOL.md §13.2).
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        *[pytest.param({"why": w}, w, id=f"why-{w}") for w in ("still", "cell", "move", "gnss", "stop")],
        *[
            pytest.param({"why": w}, None, id=f"unknown-{w!r}")
            for w in ("unknown", "GNSS", "", 7, 3.5, True, ["still"], {"why": "still"})
        ],
        pytest.param({}, None, id="absent"),
    ],
)
def test_loc_envelope_why_known_values_only(overrides: dict, expected: str | None):
    assert LocEnvelope.model_validate(_base_loc(**overrides)).why == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            _base_loc(),
            {"req": None, "err": None, "loc.lat": pytest.approx(37.774929), "cached": False},
            id="periodic-fix",
        ),
        pytest.param(
            _base_loc(req="m_7f3a2b10", cached=True), {"req": "m_7f3a2b10", "cached": True}, id="on-demand-with-req"
        ),
        pytest.param(
            {
                "v": 1,
                "id": "l_3c9a11f0",
                "ts": 1_700_000_000,
                "loc": None,
                "req": "m_7f3a2b10",
                "err": "no_fix",
            },
            {"loc": None, "err": "no_fix"},
            id="no-fix-error",
        ),
        pytest.param(
            {"v": 1, "id": "l_3c9a11f0", "ts": 1_700_000_000, "loc": None, "req": None, "err": "disabled"},
            {"err": "disabled"},
            id="disabled-error",
        ),
        pytest.param(
            b'{"v":1,"id":"l_3c9a11f0","ts":1757700000,'
            b'"loc":{"lat":37.774929,"lon":-122.419416,"acc":14,'
            b'"fix_ts":1757699991,"src":"gnss"},'
            b'"req":"m_7f3a2b10","cached":false}',
            {"req": "m_7f3a2b10", "loc.src": "gnss"},
            id="protocol-example",
        ),
    ],
)
def test_loc_envelope_valid_shapes(payload: dict | bytes, expected: dict):
    if isinstance(payload, bytes):
        assert len(payload) <= 640
        payload = parse_envelope_bytes(payload)
        assert payload is not None
    env = LocEnvelope.model_validate(payload)
    for path, value in expected.items():
        got = env
        for part in path.split("."):
            got = getattr(got, part)
        assert got == value


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_base_loc(err="no_fix"), id="both-loc-and-err"),
        pytest.param(
            {"v": 1, "id": "l_3c9a11f0", "ts": 1_700_000_000, "loc": None, "req": None}, id="neither-loc-nor-err"
        ),
        pytest.param({k: v for k, v in _base_loc().items() if k != "req"}, id="missing-req-key"),  # §13.2
        pytest.param(  # §3.5's ts=0 substitution is for the envelope's `ts`, not `loc.fix_ts`
            _base_loc(loc={**_base_loc()["loc"], "fix_ts": 0}), id="zero-fix-ts-with-real-loc"
        ),
    ],
)
def test_loc_envelope_malformed_shapes_are_rejected(payload: dict):
    with pytest.raises(ValidationError):
        LocEnvelope.model_validate(payload)


@pytest.mark.parametrize(
    ("loc_overrides", "valid"),
    [
        ({"lat": 90.0001}, False),
        ({"lat": -90.0001}, False),
        ({"lon": 180.0001}, False),
        ({"lon": -180.0001}, False),
        ({"acc": -1}, False),
        ({"lat": 90.0, "lon": -180.0, "acc": 0}, True),
    ],
)
def test_loc_fix_bounds(loc_overrides: dict, valid: bool):
    obj = _base_loc()
    obj["loc"].update(loc_overrides)
    if not valid:
        with pytest.raises(ValidationError):
            LocEnvelope.model_validate(obj)
        return
    env = LocEnvelope.model_validate(obj)
    assert env.loc is not None
    for name, value in loc_overrides.items():
        assert getattr(env.loc, name) == value


def test_loc_with_unknown_int_key_still_parses():
    """A `/loc` carrying a key this relay does not know is decoded with it dropped."""
    raw = wirecbor.translate_to_int(_base_loc())
    raw[99] = "future"
    import cbor2

    decoded = wirecbor.decode(cbor2.dumps(raw))
    assert "future" not in decoded.values()
    assert LocEnvelope.model_validate(decoded).id == "l_3c9a11f0"


def _cell(**overrides) -> dict:
    obj = {"mcc": "310", "mnc": "410", "tac": 12345, "ci": 87654321, "rsrp": -95}
    obj.update(overrides)
    return obj


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param(
            {}, {"mcc": "310", "mnc": "410", "tac": 12345, "ci": 87654321, "rsrp": -95}, id="full"
        ),
        pytest.param({"rsrp": None}, {"rsrp": None}, id="without-rsrp"),
        pytest.param({"mnc": "05"}, {"mnc": "05"}, id="two-digit-mnc-leading-zero-preserved"),
    ],
)
def test_cell_info_valid_variants(overrides: dict, expected: dict):
    info = CellInfo.model_validate(_cell(**overrides))
    for attr, value in expected.items():
        assert getattr(info, attr) == value


_NOT_NONE = object()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(  # §13.2: the pager sends `cell` whenever it answers without a GNSS fix
            {
                "v": 1,
                "id": "l_3c9a11f0",
                "ts": 1_700_000_000,
                "loc": None,
                "req": "m_7f3a2b10",
                "err": "no_fix",
                "cell": _cell(),
            },
            {"loc": None, "err": "no_fix", "cell.mcc": "310", "cell.ci": 87654321},
            id="no-fix-with-valid-cell",
        ),
        pytest.param(_base_loc(), {"cell": None}, id="cell-absent-is-backward-compatible"),
        pytest.param(  # PROTOCOL.md §13.2: a real `loc` and a `cell` validate together
            _base_loc(cell=_cell()), {"loc": _NOT_NONE, "cell": _NOT_NONE}, id="gnss-fix-and-cell-both-recorded"
        ),
        pytest.param(_base_loc(cell=None), {"cell": None}, id="cell-null-is-absent"),
    ],
)
def test_loc_envelope_cell_present_or_absent(payload: dict, expected: dict):
    env = LocEnvelope.model_validate(payload)
    for path, value in expected.items():
        got = env
        for part in path.split("."):
            got = getattr(got, part)
        if value is _NOT_NONE:
            assert got is not None
        else:
            assert got == value


@pytest.mark.parametrize(
    "bad_cell",
    [
        {"mcc": "31", "mnc": "410", "tac": 1, "ci": 1},  # mcc not 3 digits
        {"mcc": "310", "mnc": "4100", "tac": 1, "ci": 1},  # mnc too long
        {"mcc": "31x", "mnc": "410", "tac": 1, "ci": 1},  # mcc non-digit
        {"mcc": "310", "mnc": "410", "tac": -1, "ci": 1},  # tac out of range
        {"mcc": "310", "mnc": "410", "tac": 65536, "ci": 1},  # tac out of range
        {"mcc": "310", "mnc": "410", "tac": 1, "ci": -1},  # ci out of range
        {"mcc": "310", "mnc": "410", "tac": 1, "ci": 268_435_456},  # ci out of range (2**28)
        {"mcc": "310", "mnc": "410", "tac": 1, "ci": 1, "rsrp": -29},  # rsrp out of range
        {"mcc": "310", "mnc": "410", "tac": 1, "ci": 1, "rsrp": -157},  # rsrp out of range
        {"mcc": "310", "mnc": "410"},  # missing required tac/ci
        "not-a-map",
    ],
)
def test_loc_envelope_malformed_cell_is_treated_as_absent(bad_cell):
    """PROTOCOL.md §13.2: a malformed cell is treated as absent; the rest of the envelope parses."""
    obj = {
        "v": 1,
        "id": "l_3c9a11f0",
        "ts": 1_700_000_000,
        "loc": None,
        "req": None,
        "err": "no_fix",
        "cell": bad_cell,
    }
    env = LocEnvelope.model_validate(obj)
    assert env.cell is None
    assert env.err == "no_fix"


def test_ota_status_keys_on_the_cbor_wire():
    """PROTOCOL.md §10: envelope keys 62-67 and cfg.ota = 4."""
    assert [wirecbor.KEYMAP[k] for k in ("img", "ota", "ota_t", "ota_st", "ota_pct", "ota_err")] == [
        62, 63, 64, 65, 66, 67,
    ]
    assert wirecbor.CFG_KEYMAP["ota"] == 4
    assert wirecbor.OTA_KEYMAP == {
        "img": 0, "isz": 1, "url": 2, "osz": 3, "osha": 4,
        "fmt": 5, "base": 6, "psz": 7, "cancel": 8, "fv": 9,
    }
    assert wirecbor.CFG_KEYMAP["ast"] == 6
    assert (wirecbor.KEYMAP["ast"], wirecbor.KEYMAP["ast_id"]) == (72, 73)
    status = {
        "v": 1, "state": "online", "mode": "sleep", "batt_mv": 3300, "session": "s_00000001",
        "ts": 1_700_000_000, "img": "0123456789abcdef", "ota": 1, "ota_t": "fedcba9876543210",
        "ota_st": "dl", "ota_pct": 12, "ota_err": "http",
    }
    assert wirecbor.decode(wirecbor.encode(status)) == status


def test_status_bs_cbor_round_trip_and_unknown_subkey_dropped():
    # key 68, sub-map keys 0..11, arrays pass through
    assert wirecbor.KEYMAP["bs"] == 68
    bs = {
        "sq": 3, "dt": 3600, "sl": 3530, "aw": [60, 0, 0, 10, 0, 0], "ns": 180, "x1": 2,
        "rl": 10, "rf": [0, 2, 0], "mvn": 3790, "cn": 1, "md": [0, 0, 0], "re": 13,
    }
    status = {
        "v": 1, "state": "online", "mode": "sleep", "batt_mv": 3300, "session": "s_00000001",
        "ts": 1_700_000_000, "bs": bs,
    }
    assert wirecbor.decode(wirecbor.encode(status)) == status
    raw = wirecbor.translate_to_int(status)
    assert raw[68][1] == 3600 and raw[68][3] == [60, 0, 0, 10, 0, 0]
    raw[68][99] = 7  # a future sub-key
    assert wirecbor.translate_to_names(raw)["bs"] == bs
    # a bad `bs` never fails the whole status
    env = StatusEnvelope.model_validate({**status, "bs": {"aw": "x"}})
    assert env.state == "online"

