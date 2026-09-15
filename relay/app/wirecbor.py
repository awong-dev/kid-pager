"""CBOR wire envelope (docs/PROTOCOL.md §10): integer-keyed maps on the wire, text keys in JSON.

from __future__ import annotations

from typing import Any

import cbor2

KEYMAP: dict[str, int] = {
    "v": 0,
    "id": 1,
    "ts": 2,
    "from": 3,
    "body": 4,
    "ack": 5,
    "kind": 6,
    "to": 7,
    "loc": 8,
    "req": 9,
    "cached": 10,
    "err": 11,
    "n": 12,
    "sig": 13,
    "bv": 14,
    "name": 15,
    "ph": 16,
    "d": 17,
    "c": 18,
    "p": 19,
    "more": 20,
    "state": 21,
    "mode": 22,
    "batt_mv": 23,
    "rssi": 24,
    "session": 25,
    "fw": 26,
    "loc_period_s": 27,
    "loc_min_s": 28,
    "ok": 29,
    "pw": 30,
    "k": 31,
    "host": 32,
    "port": 33,
    "ca": 34,
    "flags": 35,
    "label": 36,
    "apn": 37,
    "cfg": 38,
}
REVERSE_KEYMAP: dict[int, str] = {v: k for k, v in KEYMAP.items()}

# Sub-map keys, PROTOCOL.md §10 "Sub-map keys".
LOC_KEYMAP: dict[str, int] = {"lat": 0, "lon": 1, "acc": 2, "fix_ts": 3, "src": 4}
CONTACT_KEYMAP: dict[str, int] = {"a": 0, "n": 1, "t": 2}
REQUEST_KEYMAP: dict[str, int] = {"n": 0, "s": 1}
LOCK_KEYMAP: dict[str, int] = {"clear": 0, "auto": 1}

_REVERSE_LOC = {v: k for k, v in LOC_KEYMAP.items()}
_REVERSE_CONTACT = {v: k for k, v in CONTACT_KEYMAP.items()}
_REVERSE_REQUEST = {v: k for k, v in REQUEST_KEYMAP.items()}
_REVERSE_LOCK = {v: k for k, v in LOCK_KEYMAP.items()}
_REVERSE_CFG = {v: k for k, v in CFG_KEYMAP.items()}


def _value_to_int_keys(name: str, value: Any) -> Any:
    if name == "loc" and isinstance(value, dict):
        return {LOC_KEYMAP[k]: v for k, v in value.items()}
    if name == "c" and isinstance(value, list):
        return [{CONTACT_KEYMAP[k]: v for k, v in item.items()} for item in value]
    if name == "p" and isinstance(value, list):
        return [{REQUEST_KEYMAP[k]: v for k, v in item.items()} for item in value]
    if name == "cfg" and isinstance(value, dict):
        out: dict[int, Any] = {}
        for k, v in value.items():
            if k == "lock" and isinstance(v, dict):
                out[CFG_KEYMAP["lock"]] = {LOCK_KEYMAP[lk]: lv for lk, lv in v.items()}
            else:
                # §3.2: unknown members of cfg are ignored: a future member passes through under
                # its JSON name.
                out[k] = v
        return out
    return value


def _value_to_names(name: str, value: Any) -> Any:
    if name == "loc" and isinstance(value, dict):
        return {_REVERSE_LOC[k]: v for k, v in value.items()}
    if name == "c" and isinstance(value, list):
        return [{_REVERSE_CONTACT[k]: v for k, v in item.items()} for item in value]
    if name == "p" and isinstance(value, list):
        return [{_REVERSE_REQUEST[k]: v for k, v in item.items()} for item in value]
    if name == "cfg" and isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == CFG_KEYMAP["lock"] and isinstance(v, dict):
                out["lock"] = {_REVERSE_LOCK[lk]: lv for lk, lv in v.items()}
            else:
                out[_REVERSE_CFG.get(k, k)] = v
        return out
    return value


def translate_to_int(obj: dict[str, Any]) -> dict[int, Any]:
    """`obj` (JSON names) -> a dict keyed by `KEYMAP` integers, nested sub-maps one level down."""
    return {KEYMAP[name]: _value_to_int_keys(name, value) for name, value in obj.items()}


def translate_to_names(raw: dict[int, Any]) -> dict[str, Any]:
    """Inverse of `translate_to_int`; unknown integer keys are dropped, not raised (§3.1)."""
    result: dict[str, Any] = {}
    for k, v in raw.items():
        result[name] = _value_to_names(name, v)
    return result


def map_header(count: int) -> bytes:
    """CBOR definite-length map header (RFC 8949 §3.1, major type 5) for `count` pairs."""
    if count < 24:
        return bytes([0xA0 | count])
    if count < 256:
        return bytes([0xB8, count])
    if count < 65536:
        return bytes([0xB9]) + count.to_bytes(2, "big")
    return bytes([0xBA]) + count.to_bytes(4, "big")


def parse_map_header(b: bytes) -> tuple[int, int]:
    """Reads a definite-length map header: `(count, header_len)`; `ValueError` for anything else."""
    if not b or not is_cbor(b):
        raise ValueError("not a CBOR definite-length map")
    first = b[0]
    info = first & 0x1F
    if info < 24:
        return info, 1
    if info == 24:
        return b[1], 2
    if info == 25:
        return int.from_bytes(b[1:3], "big"), 3
    if info == 26:
        return int.from_bytes(b[1:5], "big"), 5
    raise ValueError("unsupported CBOR map header (indefinite or reserved)")


def is_cbor(b: bytes) -> bool:
    """§3's first-byte dispatch: `0x7B` (JSON `{`) vs `0xA0-0xBF` (a definite-length CBOR map)."""
    return bool(b) and 0xA0 <= b[0] <= 0xBF


def encode(obj: dict[str, Any]) -> bytes:
    """`obj` (JSON names) -> a definite-length CBOR map with integer keys; values are unchanged."""
    return cbor2.dumps(translate_to_int(obj))


def decode(b: bytes) -> dict[str, Any]:
    """Inverse of `encode`: CBOR bytes -> a dict with JSON names."""
    raw = cbor2.loads(b)
    if not isinstance(raw, dict):
        raise TypeError("top-level CBOR value is not a map")
    return translate_to_names(raw)
