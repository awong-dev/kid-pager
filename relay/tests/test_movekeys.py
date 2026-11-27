"""`app.movekeys`: per-move key derivation against `tools/move_vectors.json` (MOVE_DESIGN.md §6.1)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from app import movekeys

VECTORS_PATH = Path(__file__).resolve().parents[2] / "tools" / "move_vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text())["vectors"]


def _hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 with the standard library, independent of `cryptography`."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, block = b"", b""
    for i in range(1, -(-length // 32) + 1):
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
    return out[:length]


@pytest.mark.parametrize("v", VECTORS, ids=[v["label"] for v in VECTORS])
def test_vectors(v: dict):
    k_prime, pw_prime = movekeys.derive(bytes.fromhex(v["k"]), v["move_id"], v["dev_id"])
    assert k_prime.hex() == v["k_prime"]
    assert pw_prime == v["pw_prime"]


@pytest.mark.parametrize("v", VECTORS, ids=[v["label"] for v in VECTORS])
def test_vectors_match_a_stdlib_hkdf(v: dict):
    k, salt, dev = bytes.fromhex(v["k"]), v["move_id"].encode(), v["dev_id"].encode()
    assert _hkdf_sha256(k, salt, b"kp-move-key|" + dev, 32).hex() == v["k_prime"]
    pw = base64.urlsafe_b64encode(_hkdf_sha256(k, salt, b"kp-move-pw|" + dev, 32))
    assert pw.rstrip(b"=").decode()[:43] == v["pw_prime"]


def test_design_vector_is_in_the_file():
    assert any(
        v["k_prime"] == "c1c9df9c17e6e2307e97fd99747e0f3e61d98f5448995b1465b5443c94b3d44b"
        and v["pw_prime"] == "24asWQY9sAuF2T3kIJ2dIyLAETt3IZhwQ2Xd4rHc72Y"
        for v in VECTORS
    )


def test_outputs_are_distinct_and_well_formed():
    k = bytes(range(32))
    k_prime, pw = movekeys.derive(k, "mv_0123456789ab", "pgr-a")
    assert len(k_prime) == 32 and k_prime != k
    assert len(pw) == movekeys.PW_LEN and "=" not in pw
    other = movekeys.derive(k, "mv_0123456789ab", "pgr-b")
    assert other[0] != k_prime and other[1] != pw


@pytest.mark.parametrize(
    ("k", "move_id"),
    [(b"short", "mv_0123456789ab"), (bytes(32), "MV_0123456789AB"), (bytes(32), "mv"), (bytes(32), "")],
)
def test_derive_rejects_bad_input(k: bytes, move_id: str):
    with pytest.raises(ValueError):
        movekeys.derive(k, move_id, "pgr-a")


def test_new_move_id_shape_and_uniqueness():
    ids = {movekeys.new_move_id() for _ in range(50)}
    assert len(ids) == 50
    for i in ids:
        assert i.startswith("mv_") and len(i) == 15
        assert movekeys.is_valid_move_id(i)
        assert i == i.lower() and all(c in "0123456789abcdef" for c in i[3:])
