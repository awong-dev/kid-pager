"""Bearer auth for `/bridge/*`: token `<bridgeId>.<secret>`, only `sha256(token)` is stored."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime

from fastapi import HTTPException, Request

from app.store import bridges as bridges_store

logger = logging.getLogger("relay.bridgeauth")

# `require_bridge` writes `lastSeenAt` at most this often per bridge.
SEEN_WRITE_EVERY_S = 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint_token(bridge_id: str) -> tuple[str, str]:
    """`(token, tokenHash)`; the token is shown to the phone once."""
    token = f"{bridge_id}.{secrets.token_urlsafe(32)}"
    return token, _hash(token)


def verify(token: str) -> bridges_store.Bridge | None:
    bridge_id, sep, _secret = token.partition(".")
    if not sep or not bridge_id:
        return None
    bridge = bridges_store.get(bridge_id)
    if bridge is None or bridge.tokenHash is None:
        return None
    if not hmac.compare_digest(_hash(token).encode(), bridge.tokenHash.encode()):
        return None
    return bridge


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="unauthorized")


def require_bridge(request: Request) -> bridges_store.Bridge:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized()
    bridge = verify(token.strip())
    if bridge is None:
        raise _unauthorized()
    _touch_seen(bridge)
    return bridge


def _touch_seen(bridge: bridges_store.Bridge) -> None:
    """Any authenticated request counts as "seen"; best effort, at most one write a minute."""
    last = bridge.lastSeenAt
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if (datetime.now(UTC) - last).total_seconds() < SEEN_WRITE_EVERY_S:
            return
    try:
        bridges_store.touch_seen(bridge.id)
    except Exception:
        logger.exception("bridge lastSeenAt touch failed bridge=%s", bridge.id)
