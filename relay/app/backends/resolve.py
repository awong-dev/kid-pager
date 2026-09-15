"""Inbound-reply recipient resolution for adapters whose webhook must pick a recipient
out of free text: `@alias ` prefix, else the only allowed peer, else a usage hint."""

from __future__ import annotations

from dataclasses import dataclass

from app.store import allow as allow_store
from app.store import users as users_store
from app.wire import is_valid_alias

USAGE_HINT = "reply @name your message, or ask your admin"


@dataclass(frozen=True, slots=True)
class ResolvedReply:
    recipient_alias: str
    body: str


def resolve_reply(sender_uid: str, text: str) -> ResolvedReply | None:
    """`None` means no recipient resolved; the caller sends `USAGE_HINT` over its own channel."""
    text = text.strip()
    if not text:
        return None

    if text.startswith("@"):
        # The first token after `@` is the alias, case-folded to match `ALIAS_RE`.
        parts = text[1:].split(None, 1)
        if len(parts) != 2:
            return None
        alias, body = parts[0].lower(), parts[1].strip()
        if not is_valid_alias(alias) or not body:
            return None
        return ResolvedReply(recipient_alias=alias, body=body)

    peers = allow_store.allowed_recipients(sender_uid)
    if len(peers) == 1:
        peer = users_store.get_user(peers[0])
        if peer is not None:
            return ResolvedReply(recipient_alias=peer.alias, body=text)
    return None
