"""`hooks/{id}`: registered HTTP hooks; CRUD, the 8-hook limit, per-hook counters, a 30 s cache.
The secret (32 random bytes, base64url) is the outbound HMAC key and the callback bearer token."""

from __future__ import annotations

import base64
import binascii
import secrets
import threading
import time
from datetime import datetime
from typing import Literal

from google.cloud.firestore import SERVER_TIMESTAMP, Increment
from pydantic import BaseModel, ConfigDict, Field

from app.db.firestore import get_db
from app.ids import new_id

MAX_HOOKS = 8
CACHE_TTL_S = 30.0
CONSECUTIVE_ERRORS_FOR_DOWN = 3

HookMode = Literal["filter", "processor"]
FailMode = Literal["open", "closed"]


class HookStats(BaseModel):
    model_config = ConfigDict(extra="ignore")

    calls: int = 0
    errors: int = 0
    timeouts: int = 0
    # Processor deliveries given up on after the third attempt.
    dropped: int = 0
    # Errors since the last success; `hook_down` is raised when this reaches 3.
    consecutiveErrors: int = 0
    lastErr: str | None = None
    lastOkAt: datetime | None = None


class Hook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    url: str
    secret: str
    mode: HookMode
    families: list[str] | Literal["*"] = "*"
    order: int = 0
    timeoutMs: int = 2000
    failMode: FailMode = "open"
    context: int = 20
    enabled: bool = True
    createdBy: str | None = None
    createdAt: datetime | None = None
    stats: HookStats = Field(default_factory=HookStats)

    def covers(self, family_ids: set[str]) -> bool:
        """A hook sees a conversation only if it covers every local family taking part."""
        if self.families == "*":
            return True
        return bool(family_ids) and family_ids <= set(self.families)

    def key(self) -> bytes:
        """The HMAC key: the secret's raw 32 bytes."""
        return decode_secret(self.secret)


class HookLimit(Exception):
    """`MAX_HOOKS` hooks already exist."""


def new_secret() -> str:
    return (
        base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    )


def decode_secret(secret: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    except (binascii.Error, ValueError):
        return secret.encode("utf-8")


def _hooks():
    return get_db().collection("hooks")


def _decode(hook_id: str, data: dict) -> Hook:
    return Hook.model_validate({"id": hook_id, **data})


def list_hooks() -> list[Hook]:
    hooks = [_decode(s.id, s.to_dict() or {}) for s in _hooks().stream()]
    hooks.sort(key=lambda h: (h.order, h.id))
    return hooks


def get(hook_id: str) -> Hook | None:
    snap = _hooks().document(hook_id).get()
    if not snap.exists:
        return None
    return _decode(hook_id, snap.to_dict() or {})


def create(
    *,
    name: str,
    url: str,
    mode: HookMode,
    families: list[str] | Literal["*"],
    order: int,
    timeout_ms: int,
    fail_mode: FailMode,
    context: int,
    enabled: bool,
    created_by: str | None,
) -> Hook:
    """Raises `HookLimit` at `MAX_HOOKS`; checked by a plain read, as only a super creates hooks."""
    if len(list_hooks()) >= MAX_HOOKS:
        raise HookLimit()
    hook_id = new_id("hk_")
    _hooks().document(hook_id).set(
        {
            "name": name,
            "url": url,
            "secret": new_secret(),
            "mode": mode,
            "families": families,
            "order": order,
            "timeoutMs": timeout_ms,
            "failMode": fail_mode,
            "context": context,
            "enabled": enabled,
            "createdBy": created_by,
            "createdAt": SERVER_TIMESTAMP,
            "stats": HookStats().model_dump(exclude={"lastOkAt"}),
        }
    )
    invalidate_cache()
    fetched = get(hook_id)
    assert fetched is not None
    return fetched


def update(hook_id: str, fields: dict) -> Hook | None:
    ref = _hooks().document(hook_id)
    if not ref.get().exists:
        return None
    if fields:
        ref.update(fields)
    invalidate_cache()
    return get(hook_id)


def rotate_secret(hook_id: str) -> Hook | None:
    return update(hook_id, {"secret": new_secret()})


def delete(hook_id: str) -> bool:
    ref = _hooks().document(hook_id)
    if not ref.get().exists:
        return False
    ref.delete()
    invalidate_cache()
    return True


def record_call(
    hook_id: str, *, ok: bool, timeout: bool = False, error: str | None = None
) -> int:
    """Counts one call; returns the consecutive-error count (0 on success or a deleted hook)."""
    ref = _hooks().document(hook_id)
    snap = ref.get()
    if not snap.exists:
        return 0
    if ok:
        ref.update(
            {
                "stats.calls": Increment(1),
                "stats.consecutiveErrors": 0,
                "stats.lastOkAt": SERVER_TIMESTAMP,
            }
        )
        return 0
    before = int(
        ((snap.to_dict() or {}).get("stats") or {}).get("consecutiveErrors") or 0
    )
    ref.update(
        {
            "stats.calls": Increment(1),
            "stats.errors": Increment(1),
            "stats.timeouts": Increment(1 if timeout else 0),
            "stats.consecutiveErrors": before + 1,
            "stats.lastErr": (error or "error")[:200],
        }
    )
    return before + 1


def record_dropped(hook_id: str) -> None:
    ref = _hooks().document(hook_id)
    if ref.get().exists:
        ref.update({"stats.dropped": Increment(1)})


def messages_by_ref(ref: str) -> list[dict]:
    """Stored copies (one per recipient and part) of the message a hook knew as `ref`."""
    from google.cloud.firestore import FieldFilter

    query = get_db().collection("messages").where(filter=FieldFilter("hookRef", "==", ref))
    return [{"id": s.id, **(s.to_dict() or {})} for s in query.stream()]


def annotate_message(
    msg_id: str, hook_id: str, *, labels: list[str], reason: str | None, at: int
) -> None:
    """Merges a late verdict into `messages/{id}.hooks.{hookId}`: labels unioned (max 8)."""
    ref = get_db().collection("messages").document(msg_id)
    snap = ref.get()
    if not snap.exists:
        return
    old = ((snap.to_dict() or {}).get("hooks") or {}).get(hook_id) or {}
    merged = list(dict.fromkeys([*(old.get("labels") or []), *labels]))[:8]
    ref.update(
        {
            f"hooks.{hook_id}": {
                "action": old.get("action") or "annotate",
                "labels": merged,
                "reason": reason if reason is not None else old.get("reason"),
                "at": at,
            }
        }
    )


_cache_lock = threading.Lock()
_cache: tuple[float, list[Hook]] | None = None


def invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def enabled_cached(now: float | None = None) -> list[Hook]:
    """Enabled hooks, ordered, from a 30 s cache dropped on any write in this process."""
    global _cache
    t = now if now is not None else time.monotonic()
    with _cache_lock:
        if _cache is not None and t - _cache[0] < CACHE_TTL_S:
            return _cache[1]
    hooks = [h for h in list_hooks() if h.enabled]
    with _cache_lock:
        _cache = (t, hooks)
    return hooks
