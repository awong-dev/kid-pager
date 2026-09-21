"""Device-authenticated envelope signing and verification (docs/PROTOCOL.md §14).
`verify` reads only the trailing signature bytes before the payload may be parsed (§14.4)."""

from __future__ import annotations

import base64
import hmac
import json
import re
from typing import Any

import cbor2

from app import wirecbor

# §14.3: `sig` is CBOR key 13, an 8-byte string: `0x0D` (small uint 13) then `0x48`
# (bstr, length 8).
_SIG_KEY = wirecbor.KEYMAP["sig"]
_CBOR_SIG_MARKER = bytes([_SIG_KEY, 0x40 | 8])
_CBOR_SIG_LEN = len(_CBOR_SIG_MARKER) + 8  # 10 bytes total, per §14.3.

# §14.3: JSON emits `,"sig":"<11 base64url chars>"}` last in the payload (8 raw bytes,
# base64url, no padding).
_JSON_SIG_RE = re.compile(rb',"sig":"([A-Za-z0-9_-]{11})"\}\Z')


def tag(key: bytes, topic: str, p: bytes) -> bytes:
    """§14.3: `HMAC-SHA256(K_dev, topic \\x00 P)[0:8]`."""
    return hmac.new(key, topic.encode("utf-8") + b"\x00" + p, "sha256").digest()[:8]


def sign_cbor(key: bytes, topic: str, obj: dict[str, Any]) -> bytes:
    """§14.3 CBOR rule: header count one above the pairs written (`P`), then the `sig` pair."""
    if "sig" in obj:
        raise ValueError("obj must not already contain sig")
    fields = wirecbor.translate_to_int(obj)
    body = cbor2.dumps(fields)
    count, header_len = wirecbor.parse_map_header(body)
    p = wirecbor.map_header(count + 1) + body[header_len:]
    return p + _CBOR_SIG_MARKER + tag(key, topic, p)


def sign_json(key: bytes, topic: str, obj: dict[str, Any]) -> bytes:
    """§14.3 JSON rule: MAC the minified JSON without `sig` (`P`), append `,"sig":"<tag>"}`."""
    if "sig" in obj:
        raise ValueError("obj must not already contain sig")
    p = json.dumps(wirecbor.to_json_safe(obj), separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    if not p.endswith(b"}"):
        raise ValueError("obj did not serialise to a JSON object")
    b64 = base64.urlsafe_b64encode(tag(key, topic, p)).rstrip(b"=").decode("ascii")
    return p[:-1] + f',"sig":"{b64}"}}'.encode()


def verify(key: bytes, topic: str, payload: bytes) -> tuple[bool, bytes]:
    """§14.3/§14.4: returns `(ok, payload_without_sig)`; `(False, b"")` on failure."""
    if wirecbor.is_cbor(payload):
        return _verify_cbor(key, topic, payload)
    return _verify_json(key, topic, payload)


def _verify_cbor(key: bytes, topic: str, payload: bytes) -> tuple[bool, bytes]:
    if len(payload) < _CBOR_SIG_LEN:
        return False, b""
    marker = payload[-_CBOR_SIG_LEN : -_CBOR_SIG_LEN + 2]
    if marker != _CBOR_SIG_MARKER:
        return False, b""
    got_tag = payload[-8:]
    p = payload[: -_CBOR_SIG_LEN]
    if not hmac.compare_digest(got_tag, tag(key, topic, p)):
        return False, b""
    # Rewrite the header count down by one so the result is directly decodable CBOR.
    count_plus_one, header_len = wirecbor.parse_map_header(p)
    without_sig = wirecbor.map_header(count_plus_one - 1) + p[header_len:]
    return True, without_sig


def _verify_json(key: bytes, topic: str, payload: bytes) -> tuple[bool, bytes]:
    match = _JSON_SIG_RE.search(payload)
    if match is None:
        return False, b""
    b64 = match.group(1).decode("ascii")
    try:
        got_tag = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except ValueError:
        return False, b""
    if len(got_tag) != 8:
        return False, b""
    p = payload[: match.start()] + b"}"
    if not hmac.compare_digest(got_tag, tag(key, topic, p)):
        return False, b""
    return True, p
