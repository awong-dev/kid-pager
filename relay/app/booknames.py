"""The name a pager's book shows for a peer: nickname, else display name, <= 16 code points."""

from __future__ import annotations

from app import book
from app.store import users as users_store
from app.store.messages import Conversation

UNKNOWN_NAME = "?"


def book_name(
    owner_uid: str, peer_uid: str, *, group: Conversation | None = None
) -> str:
    """`owner_uid`'s book label for `peer_uid`; `group` names a group peer."""
    if group is not None:
        return (group.name or group.alias or UNKNOWN_NAME)[: book.NICK_MAX_CODEPOINTS]
    snap = book._book_col(owner_uid).document(peer_uid).get()
    nick = (snap.to_dict() or {}).get("nick") if snap.exists else None
    if isinstance(nick, str) and nick:
        return nick[: book.NICK_MAX_CODEPOINTS]
    peer = users_store.get_user(peer_uid)
    if peer is None:
        return UNKNOWN_NAME
    return peer.displayName[: book.NICK_MAX_CODEPOINTS] or UNKNOWN_NAME
