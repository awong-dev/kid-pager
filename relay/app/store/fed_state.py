"""Federation switches, both off by default: server `settings/federation.enabled`, family flag.
A family is federated iff both are on; reads use a 30 s in-process cache (`CACHE_TTL_S`)."""

from __future__ import annotations

import threading
import time
from datetime import datetime

from google.cloud.firestore import SERVER_TIMESTAMP
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db

CACHE_TTL_S = 30.0
SETTINGS_DOC = "federation"


class ServerState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    enabledBy: str | None = None
    enabledAt: datetime | None = None


_lock = threading.Lock()
_server: tuple[float, bool] | None = None
_families: dict[str, tuple[float, bool]] = {}


def invalidate_cache() -> None:
    global _server
    with _lock:
        _server = None
        _families.clear()


def _ref():
    return get_db().collection("settings").document(SETTINGS_DOC)


def server_state() -> ServerState:
    """The stored switch, uncached (the admin read)."""
    snap = _ref().get()
    if not snap.exists:
        return ServerState()
    return ServerState.model_validate(snap.to_dict() or {})


def server_enabled(now: float | None = None) -> bool:
    """The server switch, through the 30 s cache."""
    global _server
    t = now if now is not None else time.monotonic()
    with _lock:
        if _server is not None and t - _server[0] < CACHE_TTL_S:
            return _server[1]
    value = server_state().enabled
    with _lock:
        _server = (t, value)
    return value


def family_flag(family_id: str) -> bool:
    """`families/{fid}.federation`, uncached (the admin read)."""
    snap = get_db().collection("families").document(family_id).get()
    return bool((snap.to_dict() or {}).get("federation")) if snap.exists else False


def family_enabled(family_id: str | None, now: float | None = None) -> bool:
    """Effective: the server switch and the family's flag, both cached."""
    if not family_id or not server_enabled(now):
        return False
    t = now if now is not None else time.monotonic()
    with _lock:
        hit = _families.get(family_id)
        if hit is not None and t - hit[0] < CACHE_TTL_S:
            return hit[1]
    value = family_flag(family_id)
    with _lock:
        _families[family_id] = (t, value)
    return value


def set_server_enabled(enabled: bool, by: str) -> ServerState:
    """Writes the server switch and drops the cache."""
    update: dict[str, object] = {"enabled": enabled, "enabledBy": by}
    if enabled:
        update["enabledAt"] = SERVER_TIMESTAMP
    _ref().set(update, merge=True)
    invalidate_cache()
    return server_state()


def set_family_enabled(family_id: str, enabled: bool) -> None:
    """`KeyError` for an unknown family."""
    ref = get_db().collection("families").document(family_id)
    if not ref.get().exists:
        raise KeyError(family_id)
    ref.update({"federation": enabled})
    invalidate_cache()
