"""`users/{uid}/backends/{bid}` CRUD, plus lookup collections for the gchat inbound webhook."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Literal, get_args

from google.cloud.firestore import SERVER_TIMESTAMP, Transaction
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db, run_transaction

logger = logging.getLogger(__name__)

BackendKind = Literal["pager", "webapp", "gchat", "sms", "bridge", "fed"]


class Backend(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    kind: BackendKind
    config: dict = {}
    enabled: bool = True
    verifiedAt: datetime | None = None


def _backends(uid: str):
    return get_db().collection("users").document(uid).collection("backends")


def create_backend(
    uid: str,
    *,
    kind: BackendKind,
    config: dict | None = None,
    enabled: bool = True,
    bid: str | None = None,
) -> Backend:
    """`bid` pins the document id (idempotent `set`); default is a random one."""
    bid = bid or uuid.uuid4().hex[:12]
    ref = _backends(uid).document(bid)
    ref.set(
        {
            "kind": kind,
            "config": config or {},
            "enabled": enabled,
            "verifiedAt": SERVER_TIMESTAMP if kind in ("webapp", "sms", "bridge", "fed") else None,
        }
    )
    fetched = get_backend(uid, bid)
    assert fetched is not None
    return fetched


def _owner_is_external(uid: str) -> bool:
    snap = get_db().collection("users").document(uid).get()
    return snap.exists and (snap.to_dict() or {}).get("kind") == "external"


# Backend kinds valid only on an external user.
_EXTERNAL_ONLY_KINDS = ("sms", "bridge", "fed")


def _live_kind(uid: str, bid: str, data: dict, external: bool | None = None) -> bool:
    kind = data.get("kind")
    if kind not in get_args(BackendKind):
        logger.warning("skipping backend %s/%s of retired kind %r", uid, bid, kind)
        return False
    if kind in _EXTERNAL_ONLY_KINDS:
        if external is None:
            external = _owner_is_external(uid)
        if not external:
            logger.warning("skipping %s backend %s/%s on a non-external owner", kind, uid, bid)
            return False
    return True


def get_backend(uid: str, bid: str) -> Backend | None:
    snap = _backends(uid).document(bid).get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    if not _live_kind(uid, bid, data):
        return None
    return Backend.model_validate({"id": bid, **data})


def list_backends(uid: str) -> list[Backend]:
    out: list[Backend] = []
    external: bool | None = None  # looked up at most once, only if an sms row is seen
    for snap in _backends(uid).stream():
        data = snap.to_dict() or {}
        if data.get("kind") in _EXTERNAL_ONLY_KINDS and external is None:
            external = _owner_is_external(uid)
        if _live_kind(uid, snap.id, data, external):
            out.append(Backend.model_validate({"id": snap.id, **data}))
    return out


def has_kind(uid: str, kind: str) -> bool:
    """True when `uid` has a live backend row of `kind`."""
    return any(b.kind == kind for b in list_backends(uid))


def update_backend(
    uid: str,
    bid: str,
    *,
    config: dict | None = None,
    enabled: bool | None = None,
    verified: bool | None = None,
) -> Backend:
    """`verified`: None leaves `verifiedAt`, True sets it to now, False clears it."""
    updates: dict[str, object] = {}
    if config is not None:
        updates["config"] = config
    if enabled is not None:
        updates["enabled"] = enabled
    if verified is True:
        updates["verifiedAt"] = SERVER_TIMESTAMP
    elif verified is False:
        updates["verifiedAt"] = None
    if updates:
        _backends(uid).document(bid).update(updates)
    fetched = get_backend(uid, bid)
    if fetched is None:
        raise KeyError(f"no such backend: {uid}/{bid}")
    return fetched


def record_member_channel(
    uid: str, bid: str, member_uid: str, via: str, conv: str | None
) -> None:
    """Atomic read-modify-write of `config.via[member]` / `config.voiceConv[member]`."""
    ref = _backends(uid).document(bid)

    def _txn(transaction: Transaction) -> None:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return
        data = snap.to_dict() or {}
        if not _live_kind(uid, bid, data):
            return
        config = dict(data.get("config") or {})
        via_map = dict(config.get("via") or {})
        conv_map = dict(config.get("voiceConv") or {})
        changed = False
        if via_map.get(member_uid) != via:
            via_map[member_uid] = via
            changed = True
        if via in ("gvoice", "whatsapp") and conv and conv_map.get(member_uid) != conv:
            conv_map[member_uid] = conv
            changed = True
        if not changed:
            return
        config["via"] = via_map
        if conv_map:
            config["voiceConv"] = conv_map
        transaction.update(ref, {"config": config})

    run_transaction(_txn)


def delete_backend(uid: str, bid: str) -> None:
    _backends(uid).document(bid).delete()


def _gchat_link_codes():
    return get_db().collection("gchatLinkCodes")


def set_gchat_link_code(code: str, uid: str, bid: str, expires_at: int) -> None:
    """Stores a single-use link code; `expires_at` is a Unix time the caller checks."""
    _gchat_link_codes().document(code).set({"uid": uid, "bid": bid, "expiresAt": expires_at})


def pop_gchat_link_code(code: str) -> tuple[str, str, int] | None:
    """Read-then-delete: `(uid, bid, expiresAt)` or None; the caller checks `expiresAt`."""
    ref = _gchat_link_codes().document(code)
    snap = ref.get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    ref.delete()
    uid, bid, expires_at = data.get("uid"), data.get("bid"), data.get("expiresAt")
    if not uid or not bid or expires_at is None:
        return None
    return uid, bid, expires_at


def _gchat_spaces():
    return get_db().collection("gchatSpaces")


def set_gchat_space(space_id: str, uid: str, bid: str, sender_name: str | None = None) -> None:
    """`sender_name` is Chat's `message.sender.name` from the `/link` message; None = not pinned."""
    _gchat_spaces().document(space_id).set(
        {"uid": uid, "bid": bid, "senderName": sender_name}
    )


def get_by_gchat_space(space_id: str) -> tuple[str, str, str | None] | None:
    """`(uid, bid, senderName)` for Chat space `space_id`, or None; callers re-check the sender."""
    snap = _gchat_spaces().document(space_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    uid, bid = data.get("uid"), data.get("bid")
    if not uid or not bid:
        return None
    return uid, bid, data.get("senderName")
