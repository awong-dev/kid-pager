"""`vapid/{kid}`: VAPID key pairs of servers this one was moved from, server-only. A Web Push
subscription's `vapidKid` names the key it was made against (docs/MOVE_DESIGN.md §9): `src` is the
key of the server that exported the last move; a key from an earlier move is kept under
`kid_for(its public key)`."""

from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db

SRC_KID = "src"
_KID_RE = re.compile(r"vk_[0-9a-f]{16}")


def kid_for(public_key: str) -> str:
    return "vk_" + hashlib.sha256(public_key.encode("ascii", "replace")).hexdigest()[:16]


def valid_kid(kid: str) -> bool:
    return kid == SRC_KID or _KID_RE.fullmatch(kid) is not None


class VapidKey(BaseModel):
    """Both halves are base64url, as `WEBPUSH_VAPID_PRIVATE_KEY` / `WEBPUSH_VAPID_PUBLIC_KEY`."""

    model_config = ConfigDict(extra="ignore")

    privateKey: str
    publicKey: str


def _ref(kid: str):
    return get_db().collection("vapid").document(kid)


def get(kid: str) -> VapidKey | None:
    snap = _ref(kid).get()
    if not snap.exists:
        return None
    return VapidKey.model_validate(snap.to_dict() or {})


def get_src() -> VapidKey | None:
    return get(SRC_KID)


def put_src(key: VapidKey) -> None:
    _ref(SRC_KID).set(key.model_dump())
