"""Per-move device key and broker password derivation (docs/MOVE_DESIGN.md §6.1).

The source derives `k'` and `pw'` from a device's key `k`; the pager derives the same pair from the
same inputs, so neither value is ever transported. `tools/move_vectors.json` is shared with the
firmware's host tests."""

from __future__ import annotations

import base64
import os
import re

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# docs/MOVE_DESIGN.md §12.1: `cfg.mv.id`.
MOVE_ID_RE = re.compile(r"^[a-z0-9_]{3,16}$")
MOVE_ID_PREFIX = "mv_"
_MOVE_ID_RANDOM_BYTES = 6

KEY_LEN = 32
# `pw'` is the 43-character unpadded base64url text of 32 bytes.
PW_LEN = 43
_INFO_KEY = b"kp-move-key|"
_INFO_PW = b"kp-move-pw|"


def is_valid_move_id(move_id: str) -> bool:
    return MOVE_ID_RE.fullmatch(move_id) is not None


def new_move_id() -> str:
    """`mv_` + 12 lowercase hex characters from `os.urandom`."""
    return MOVE_ID_PREFIX + os.urandom(_MOVE_ID_RANDOM_BYTES).hex()


def _hkdf(k: bytes, move_id: str, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_LEN, salt=move_id.encode("ascii"), info=info
    ).derive(k)


def derive(k: bytes, move_id: str, dev_id: str) -> tuple[bytes, str]:
    """`(k', pw')` for device `dev_id` under move `move_id`; `k` is the device's 32-byte key."""
    if len(k) != KEY_LEN:
        raise ValueError(f"device key must be {KEY_LEN} bytes, got {len(k)}")
    if not is_valid_move_id(move_id):
        raise ValueError(f"invalid move id {move_id!r}")
    dev = dev_id.encode("ascii")
    k_prime = _hkdf(k, move_id, _INFO_KEY + dev)
    pw_raw = _hkdf(k, move_id, _INFO_PW + dev)
    pw_prime = base64.urlsafe_b64encode(pw_raw).rstrip(b"=").decode("ascii")[:PW_LEN]
    return k_prime, pw_prime
