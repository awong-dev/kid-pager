"""`users/{uid}/backends/{bid}` CRUD, plus lookup collections for the gchat inbound webhook."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict


logger = logging.getLogger(__name__)



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
) -> Backend:
    ref = _backends(uid).document(bid)
    ref.set(
        {
            "kind": kind,
            "config": config or {},
            "enabled": enabled,
        }
    )
    fetched = get_backend(uid, bid)
    assert fetched is not None
    return fetched




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
    for snap in _backends(uid).stream():
        data = snap.to_dict() or {}
            out.append(Backend.model_validate({"id": snap.id, **data}))
    return out


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
