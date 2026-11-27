"""`settings/move`: the move record both relays keep (docs/MOVE_DESIGN.md §8.2, §4). Never deleted;
both sides carry `bridgeRedirect`, where their `/bridge/*` routes send phones while it is set."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db
from app.store import devices as devices_store

Role = Literal["source", "target"]
State = Literal["frozen", "cutover", "cancelled", "ended", "importing", "imported"]

# Source states in which the deployment is frozen for the move.
SOURCE_FROZEN_STATES: tuple[str, ...] = ("frozen", "cutover")

# "Dark" is silence for twice the bearer's keepalive (§7.2).
DARK_AFTER_DIRECT = timedelta(minutes=24)
DARK_AFTER_BEAM = timedelta(minutes=60)

HELLO_PREFIX = "kp-bridge-hello|"


class MoveTargetCoords(BaseModel):
    """What the source learned from the target's `move-info` and the super confirmed at Freeze."""

    model_config = ConfigDict(extra="ignore")

    baseUrl: str
    authBackend: Literal["home", "firebase"]
    host: str
    port: int
    # `ca.url` and `ca.sha` of the target's `move-info`; `None` when it has no broker CA
    caUrl: str | None = None
    caSha: str | None = None
    # The target's web-certificate CA (PEM) when it is not a public one; every later call to the
    # target trusts exactly this. Not the CA pointer a pager gets.
    tlsCa: str | None = None


class SourceMoveRecord(BaseModel):
    """`settings/move` as Freeze leaves it."""

    id: str
    role: Literal["source"] = "source"
    state: Literal["frozen", "cutover", "cancelled", "ended"] = "frozen"
    frozenAt: datetime
    target: MoveTargetCoords
    cutoverAt: datetime | None = None
    cancelledAt: datetime | None = None
    bridgeRedirect: str | None = None
    # Families Freeze froze; Cancel unfreezes these and leaves one frozen by hand before it
    frozenByMove: list[str] = []


class TargetMoveRecord(BaseModel):
    """`settings/move` as an import leaves it."""

    id: str
    role: Literal["target"] = "target"
    state: Literal["imported"] = "imported"
    sourceBaseUrl: str | None = None
    frozenAt: str
    importedAt: datetime
    bridgeRedirect: str | None = None
    # People the import left with no way to sign in
    noSignIn: list[str] = []


class BeamState(BaseModel):
    """What the target's Beam switch did (docs/MOVE_DESIGN.md §10), kept so it can be undone: the
    group the SIMs came from, the new group and the SIMs moved between them."""

    model_config = ConfigDict(extra="ignore")

    switched: bool = False
    byHand: bool = False
    group: str | None = None
    oldGroupId: str | None = None
    newGroupId: str | None = None
    imsis: list[str] = []
    switchedAt: datetime | None = None


class MoveRecord(BaseModel):
    """Either side's `settings/move`, as read."""

    model_config = ConfigDict(extra="ignore")

    id: str
    role: Role
    state: State
    frozenAt: datetime | str | None = None
    target: MoveTargetCoords | None = None
    sourceBaseUrl: str | None = None
    importedAt: datetime | None = None
    cutoverAt: datetime | None = None
    cancelledAt: datetime | None = None
    endedAt: datetime | None = None
    bridgeRedirect: str | None = None
    noSignIn: list[str] = []
    beam: BeamState | None = None
    # `None` on a record written before the field existed: Cancel then unfreezes every family
    frozenByMove: list[str] | None = None

    @property
    def frozen_for_move(self) -> bool:
        return self.role == "source" and self.state in SOURCE_FROZEN_STATES


def _ref() -> Any:
    return get_db().collection("settings").document("move")


def get_record() -> MoveRecord | None:
    snap = _ref().get()
    if not snap.exists:
        return None
    return MoveRecord.model_validate(snap.to_dict() or {})


def save_record(record: SourceMoveRecord | TargetMoveRecord) -> None:
    """Replaces the whole document."""
    _ref().set(record.model_dump(mode="python"))
    invalidate_cache()


def patch_record(**fields: Any) -> None:
    """Merges `fields` into the existing record."""
    _ref().set(fields, merge=True)
    invalidate_cache()


# The write guard and delivery ask about the record on every request; the answer is kept this long.
CACHE_TTL_S = 5.0
_cache: tuple[Any, float, MoveRecord | None] | None = None


def invalidate_cache() -> None:
    global _cache
    _cache = None


def cached_record() -> MoveRecord | None:
    """`get_record()`, read at most once per `CACHE_TTL_S` for the current database; this
    process's own writes to the record drop it at once, another instance's are seen within the
    TTL."""
    global _cache
    db = get_db()
    now = time.monotonic()
    held = _cache
    if held is not None and held[0] is db and now < held[1]:
        return held[2]
    rec = get_record()
    _cache = (db, now + CACHE_TTL_S, rec)
    return rec


def frozen_for_move() -> bool:
    """True while this relay is a source whose deployment is frozen for a move."""
    rec = cached_record()
    return rec is not None and rec.frozen_for_move


def bridge_moved(rec: MoveRecord | None = None) -> dict[str, str] | None:
    """The `moved` object of a bridge answer (§8.3): `{url, id}` while `bridgeRedirect` is set."""
    rec = rec if rec is not None else get_record()
    if rec is None or not rec.bridgeRedirect:
        return None
    return {"url": rec.bridgeRedirect.rstrip("/"), "id": rec.id}


def hello_sig(token_hash: str, nonce: str, bridge_id: str, move_id: str) -> str:
    """HMAC-SHA256 keyed by the ASCII bytes of the bridge's stored `tokenHash` (§8.4)."""
    message = f"{HELLO_PREFIX}{nonce}|{bridge_id}|{move_id}".encode("ascii")
    return hmac.new(token_hash.encode("ascii"), message, hashlib.sha256).hexdigest()


def bearer_of(status: devices_store.DeviceStatus) -> Literal["direct", "beam"] | None:
    """`tls: "proxy"` is a pager on Beam (PROTOCOL.md §5.1)."""
    if status.tls is None:
        return None
    return "beam" if status.tls == "proxy" else "direct"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def last_seen(status: devices_store.DeviceStatus) -> datetime | None:
    return _aware(status.updatedAt)


def dark_since(status: devices_store.DeviceStatus, now: datetime | None = None) -> datetime | None:
    """When the pager was last heard from, if that is longer ago than the bearer's dark window."""
    seen = last_seen(status)
    if seen is None:
        return None
    window = DARK_AFTER_BEAM if bearer_of(status) == "beam" else DARK_AFTER_DIRECT
    return seen if (now or datetime.now(UTC)) - seen > window else None


def delivery_held(device_id: str | None) -> bool:
    """True for a pager the target imported that has not yet made a signed `/status` here. A page for
    it waits as it does for any offline pager, and is not counted as a failed attempt."""
    if not device_id:
        return False
    rec = cached_record()
    if rec is None or rec.role != "target":
        return False
    snap = get_db().collection("devices").document(device_id).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    mv = data.get("move")
    return (
        data.get("provisionState") == "moving"
        and isinstance(mv, dict)
        and mv.get("state") == "waiting"
        and mv.get("arrivedAt") is None
    )
