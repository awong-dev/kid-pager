"""`app.devauth` and `app.wirecbor` (docs/PROTOCOL.md §14, §10). Vectors come from
`tools/authvectors.json`; `test_first_vector_cbor_bytes_by_hand` pins the format byte by byte."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import cbor2
import pytest

from app import devauth, wire, wirecbor

VECTORS_PATH = Path(__file__).resolve().parents[2] / "tools" / "authvectors.json"
VECTORS: list[dict[str, Any]] = json.loads(VECTORS_PATH.read_text())


def _key(vector: dict[str, Any]) -> bytes:
    return base64.b64decode(vector["key_b64"])


def test_at_least_eight_vectors_covering_the_required_kinds():
    assert len(VECTORS) >= 8
    labels = {v["label"] for v in VECTORS}
    assert labels == {
        "down_msg",
        "ack",
        "up_msg_to",
        "status",
        "loc",
        "book",
        "cfg",
        "contact_req",
    }


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["label"])
def test_json_vector_round_trips(vector: dict[str, Any]):
    key = _key(vector)
    topic = vector["topic"]
    obj = vector["obj"]

    signed = devauth.sign_json(key, topic, obj)
    assert signed == base64.b64decode(vector["json_signed_b64"])

    ok, without_sig = devauth.verify(key, topic, signed)
    assert ok
    assert json.loads(without_sig) == obj


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["label"])
def test_cbor_vector_round_trips(vector: dict[str, Any]):
    key = _key(vector)
    topic = vector["topic"]
    obj = vector["obj"]

    signed = devauth.sign_cbor(key, topic, obj)
    assert signed == base64.b64decode(vector["cbor_signed_b64"])

    ok, without_sig = devauth.verify(key, topic, signed)
    assert ok
    assert wirecbor.decode(without_sig) == obj


FLIP_VECTORS = [v for v in VECTORS if v["label"] in ("down_msg", "book")]


@pytest.mark.parametrize("fmt", ["json", "cbor"])
@pytest.mark.parametrize("vector", FLIP_VECTORS, ids=lambda v: v["label"])
def test_flipping_any_byte_fails(vector: dict[str, Any], fmt: str):
    key = _key(vector)
    signed = base64.b64decode(vector[f"{fmt}_signed_b64"])
    for i in range(len(signed)):
        corrupted = bytearray(signed)
        corrupted[i] ^= 0x01
        ok, _ = devauth.verify(key, vector["topic"], bytes(corrupted))
        assert not ok, f"byte {i} flip in {fmt.upper()} vector {vector['label']!r} was not detected"


@pytest.mark.parametrize("fmt", ["json", "cbor"])
def test_wrong_key_fails(fmt: str):
    vector = VECTORS[0]
    signed = base64.b64decode(vector[f"{fmt}_signed_b64"])
    wrong_key = bytes((b + 1) % 256 for b in base64.b64decode(vector["key_b64"]))
    ok, _ = devauth.verify(wrong_key, vector["topic"], signed)
    assert not ok


def test_json_sig_not_last_fails():
    """§14.3: `sig` MUST be the last pair; a valid tag anywhere else is malformed."""
    vector = next(v for v in VECTORS if v["label"] == "down_msg")
    key = _key(vector)
    signed = base64.b64decode(vector["json_signed_b64"])
    # Move `,"sig":"...".` from just before the final `}` to just after `{`.
    body = signed.decode("utf-8")
    assert body.endswith("}")
    head, sig_part = body[1:-1].rsplit(",", 1)
    assert sig_part.startswith('"sig":')
    reordered = "{" + sig_part + "," + head + "}"
    ok, _ = devauth.verify(key, vector["topic"], reordered.encode("utf-8"))
    assert not ok


def test_cbor_sig_not_last_fails():
    """§14.3: the CBOR verifier requires the payload to end in `0x0D 0x48 <8 bytes>`; a map with
    the sig pair moved off the end must fail."""
    vector = next(v for v in VECTORS if v["label"] == "down_msg")
    key = _key(vector)
    signed = base64.b64decode(vector["cbor_signed_b64"])
    header = signed[:1]
    pairs = signed[1:-10]
    sig_pair = signed[-10:]
    reordered = header + sig_pair + pairs
    ok, _ = devauth.verify(key, vector["topic"], reordered)
    assert not ok


@pytest.mark.parametrize(
    "unsigned",
    [b'{"v":1,"id":"m_aaaaaaaa"}', wirecbor.encode({"v": 1})],
    ids=["json", "cbor"],
)
def test_truncated_payload_missing_sig_fails(unsigned: bytes):
    ok, _ = devauth.verify(b"k" * 32, "pager/pgr-0001/up", unsigned)
    assert not ok


@pytest.mark.parametrize(
    ("sign", "sig"),
    [(devauth.sign_json, "x"), (devauth.sign_cbor, b"x")],
    ids=["json", "cbor"],
)
def test_sign_rejects_obj_that_already_has_sig(sign: Any, sig: str | bytes):
    with pytest.raises(ValueError):
        sign(b"k" * 32, "pager/pgr-0001/up", {"v": 1, "sig": sig})


def test_first_vector_cbor_bytes_by_hand():
    """Hand-decoded `down_msg` vector: `a7` map(7), keys 0-5, then key 13 `sig` = `0d 48` + tag(8).
    The tag is HMAC-SHA256 over `topic + b"\\x00"` + the 46 bytes before `0d 48`; key `range(32)`."""
    vector = next(v for v in VECTORS if v["label"] == "down_msg")
    signed = base64.b64decode(vector["cbor_signed_b64"])
    expected_hex = (
        "a70001016a6d5f3766336132623130021a68c45fa0"
        "0366706172656e74046d6265207468657265206174203505f60d488244d598bacf0895"
    )
    assert signed.hex() == expected_hex
    assert signed[0] == 0xA7  # map header, count 7
    assert signed[1:3] == bytes([0x00, 0x01])  # v: 1
    assert signed[-10:] == bytes.fromhex("0d488244d598bacf0895")

    import hmac as hmac_mod

    prefix = signed[:-10]
    key = _key(vector)
    expected_tag = hmac_mod.new(
        key, vector["topic"].encode("utf-8") + b"\x00" + prefix, "sha256"
    ).digest()[:8]
    assert signed[-8:] == expected_tag


def test_is_cbor_detects_first_byte():
    assert wirecbor.is_cbor(bytes([0xA0]))
    assert wirecbor.is_cbor(bytes([0xBF]))
    assert not wirecbor.is_cbor(b"{}")
    assert not wirecbor.is_cbor(b"")
# docs/PROTOCOL.md §0, §3.1: an unknown CBOR integer key is dropped, not a decode failure.


def test_decode_drops_a_single_unknown_integer_key_not_the_whole_envelope():
    # Key 99 is not in wirecbor.KEYMAP; it sits alongside two keys the relay knows.
    raw = cbor2.dumps({0: 1, 21: "online", 99: "from-the-future"})
    decoded = wirecbor.decode(raw)
    assert decoded == {"v": 1, "state": "online"}


def test_decode_envelope_bytes_accepts_cbor_with_an_unknown_key():
    raw = cbor2.dumps({0: 1, 21: "online", 25: "s_aabbccdd", 99: "from-the-future"})
    result = wire.decode_envelope_bytes(raw)
    assert result is not None
    data, encoding = result
    assert encoding == "cbor"
    assert data == {"v": 1, "state": "online", "session": "s_aabbccdd"}


@pytest.mark.parametrize(
    ("obj", "expected"),
    [
        pytest.param(
            {"cfg": {"ca": {"url": "https://x/y.pem", "sha": b"\x00\x01\xfe\xff"}}},
            {"cfg": {"ca": {"url": "https://x/y.pem", "sha": "AAH-_w"}}},  # base64url, no padding
            id="bytes_encoded_recursively",
        ),
        pytest.param(
            {"a": 1, "b": [1, "x", None, {"c": True}]},
            {"a": 1, "b": [1, "x", None, {"c": True}]},
            id="non_bytes_unchanged",
        ),
    ],
)
def test_to_json_safe(obj: dict[str, Any], expected: dict[str, Any]):
    safe = wirecbor.to_json_safe(obj)
    assert safe == expected
    json.dumps(safe)


# docs/PROTOCOL.md §13.2, §10: the `/loc` `cell` sub-map, key 49 (mcc=0 mnc=1 tac=2 ci=3 rsrp=4).


def test_wirecbor_cell_encodes_at_key_49_with_expected_submap():
    encoded = wirecbor.encode({"cell": {"mcc": "310", "mnc": "41", "tac": 1, "ci": 2}})
    assert encoded == cbor2.dumps({49: {0: "310", 1: "41", 2: 1, 3: 2}})


def test_wirecbor_cell_unknown_subkey_is_dropped_not_raised():
    """§13.2: unknown `cell` sub-keys (99 here) are ignored and must not fail the decode."""
    raw = cbor2.dumps({49: {0: "310", 1: "410", 2: 12345, 3: 87654321, 99: "future"}})
    decoded = wirecbor.decode(raw)
    assert decoded == {"cell": {"mcc": "310", "mnc": "410", "tac": 12345, "ci": 87654321}}


def test_loc_envelope_with_cell_example_cbor_hex():
    """An example `/loc` answering `no_fix` with a `cell`: the exact CBOR bytes."""
    obj = {
        "v": 1,
        "id": "l_3c9a11f0",
        "ts": 1757700000,
        "loc": None,
        "req": "m_7f3a2b10",
        "err": "no_fix",
        "cell": {"mcc": "310", "mnc": "410", "tac": 12345, "ci": 87654321, "rsrp": -95},
    }
    encoded = wirecbor.encode(obj)
    assert wirecbor.decode(encoded) == obj
    # Pinned so a change to key ordering or encoding is a deliberate diff.
    assert (
        encoded.hex()
        == "a70001016a6c5f3363396131316630021a68c45fa008f6096a6d5f37663361326231300b666e6f5f6669781831a50063333130016334313002193039031a05397fb104385e"
    )
# `/status` `link` field, key 50: MQTT-session generation within a boot.


def test_status_envelope_with_link_example_cbor_hex():
    """An example online `/status` carrying `link`: the exact CBOR bytes."""
    obj = {
        "v": 1,
        "state": "online",
        "mode": "sleep",
        "batt_mv": 3280,
        "rssi": -95,
        "session": "s_3ab91c02",
        "ts": 1757700000,
        "fw": "0.1.0",
        "link": 3,
    }
    encoded = wirecbor.encode(obj)
    assert wirecbor.decode(encoded) == obj
    assert (
        encoded.hex()
        == "a9000115666f6e6c696e651665736c65657017190cd01818385e18196a735f3361623931633032021a"
        "68c45fa0181a65302e312e30183203"
    )
