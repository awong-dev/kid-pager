"""`bridges/{bridgeId}` and `bridgePairCodes/{code}`, server-only (default-deny in firestore.rules).
`simNumber`/`voiceNumber` are accepted; `status.*Number` is what the phone last reported."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter, Transaction
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.db.firestore import get_db, run_transaction
from app.ids import new_id

logger = logging.getLogger("relay.bridges")

PAIR_CODE_TTL = timedelta(minutes=10)

# Status keys a heartbeat or pair may overwrite; `tier2Count` and `error` are relay-owned.
PHONE_STATUS_KEYS = (
    "battery",
    "listenerBound",
    "smsDefault",
    "smsCapable",
    "accessibility",
    "whatsapp",
    "accounts",
    "simNumber",
    "voiceNumber",
    "version",
)


class BridgeStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    battery: int | None = None
    listenerBound: bool = False
    smsDefault: bool = False
    # The phone's own "I can send SMS" bit; `caps.sms` also requires a SIM
    # (`bridge_numbers.caps_for`).
    smsCapable: bool = False
    accessibility: bool = False
    # WhatsApp installed and its notifications listened to (the phone's bit; `caps.whatsapp`).
    whatsapp: bool = False
    accounts: list[str] = Field(default_factory=list)
    simNumber: str | None = None
    voiceNumber: str | None = None
    tier2Count: int = 0
    version: str | None = None
    error: str | None = None
    unpaired: bool = False


class BridgeCaps(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sms: bool = False
    gchat: bool = False
    gvoice: bool = False
    whatsapp: bool = False


class Bridge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    ownerUid: str
    familyId: str
    label: str
    tokenHash: str | None = None
    pairedAt: datetime | None = None
    fcmToken: str | None = None
    lastSeenAt: datetime | None = None
    simNumber: str | None = None
    voiceNumber: str | None = None
    status: BridgeStatus = Field(default_factory=BridgeStatus)
    caps: BridgeCaps = Field(default_factory=BridgeCaps)
    createdAt: datetime | None = None
    createdBy: str | None = None

    @property
    def paired(self) -> bool:
        return self.tokenHash is not None


def _bridges():
    return get_db().collection("bridges")


def _codes():
    return get_db().collection("bridgePairCodes")


def _from_snap(snap) -> Bridge:
    """A row that fails validation is logged and read as an unpaired bridge instead of raising."""
    data = snap.to_dict() or {}
    try:
        return Bridge.model_validate({"id": snap.id, **data})
    except ValidationError:
        logger.exception("bridge row %s fails validation; treating it as unpaired", snap.id)
        return Bridge(
            id=snap.id,
            ownerUid=str(data.get("ownerUid") or ""),
            familyId=str(data.get("familyId") or ""),
            label=str(data.get("label") or ""),
            tokenHash=None,
        )


def create(owner_uid: str, family_id: str, label: str, created_by: str) -> Bridge:
    bridge_id = new_id("b_")
    _bridges().document(bridge_id).create(
        {
            "ownerUid": owner_uid,
            "familyId": family_id,
            "label": label,
            "tokenHash": None,
            "pairedAt": None,
            "fcmToken": None,
            "lastSeenAt": None,
            "simNumber": None,
            "voiceNumber": None,
            "status": BridgeStatus().model_dump(),
            "caps": BridgeCaps().model_dump(),
            "createdAt": SERVER_TIMESTAMP,
            "createdBy": created_by,
        }
    )
    fetched = get(bridge_id)
    assert fetched is not None
    return fetched


def get(bridge_id: str) -> Bridge | None:
    snap = _bridges().document(bridge_id).get()
    if not snap.exists:
        return None
    return _from_snap(snap)


def list_for_family(family_id: str) -> list[Bridge]:
    query = _bridges().where(filter=FieldFilter("familyId", "==", family_id))
    return sorted((_from_snap(s) for s in query.stream()), key=lambda b: b.createdAt or _EPOCH)


def list_for_owner(owner_uid: str) -> list[Bridge]:
    query = _bridges().where(filter=FieldFilter("ownerUid", "==", owner_uid))
    return sorted((_from_snap(s) for s in query.stream()), key=lambda b: b.createdAt or _EPOCH)


def list_all() -> list[Bridge]:
    return [_from_snap(s) for s in _bridges().stream()]


_EPOCH = datetime.fromtimestamp(0, tz=UTC)


def get_by_sms_number(e164: str, *, prefer_owner: str | None = None) -> Bridge | None:
    """The paired bridge with accepted `simNumber` or `voiceNumber` `e164`; `prefer_owner` wins."""
    matches: list[Bridge] = []
    for field in ("simNumber", "voiceNumber"):
        for snap in _bridges().where(filter=FieldFilter(field, "==", e164)).stream():
            bridge = _from_snap(snap)
            if bridge.paired:
                matches.append(bridge)
    if prefer_owner is not None:
        for bridge in matches:
            if bridge.ownerUid == prefer_owner:
                return bridge
    return matches[0] if matches else None


def _update(bridge_id: str, updates: dict[str, Any]) -> None:
    try:
        _bridges().document(bridge_id).update(updates)
    except NotFound as exc:
        raise KeyError(f"no such bridge: {bridge_id!r}") from exc


def set_token_hash(bridge_id: str, token_hash: str, *, caps: BridgeCaps, status: dict) -> None:
    updates: dict[str, Any] = {
        "tokenHash": token_hash,
        "pairedAt": SERVER_TIMESTAMP,
        "lastSeenAt": SERVER_TIMESTAMP,
        "caps": caps.model_dump(),
        "status.unpaired": False,
        "status.error": None,
    }
    for key in PHONE_STATUS_KEYS:
        if key in status:
            updates[f"status.{key}"] = status[key]
    _update(bridge_id, updates)


def set_numbers(
    bridge_id: str,
    *,
    sim_number: str | None,
    voice_number: str | None,
    caps: BridgeCaps | None = None,
) -> None:
    updates: dict[str, Any] = {"simNumber": sim_number, "voiceNumber": voice_number}
    if caps is not None:
        updates["caps"] = caps.model_dump()
    _update(bridge_id, updates)


def set_caps(bridge_id: str, caps: BridgeCaps) -> None:
    _update(bridge_id, {"caps": caps.model_dump()})


def set_sim_number(bridge_id: str, sim_number: str | None) -> None:
    _update(bridge_id, {"simNumber": sim_number})


def set_owner(bridge_id: str, owner_uid: str) -> None:
    _update(bridge_id, {"ownerUid": owner_uid})


def set_label(bridge_id: str, label: str) -> None:
    _update(bridge_id, {"label": label})


def set_error(bridge_id: str, error: str | None) -> None:
    _update(bridge_id, {"status.error": error})


def touch(bridge_id: str, status: dict, fcm_token: str | None = None) -> None:
    """Heartbeat write: `lastSeenAt`, phone-reported status keys, and the FCM token if sent."""
    updates: dict[str, Any] = {"lastSeenAt": SERVER_TIMESTAMP}
    for key in PHONE_STATUS_KEYS:
        if key in status:
            updates[f"status.{key}"] = status[key]
    if fcm_token:
        updates["fcmToken"] = fcm_token
    _update(bridge_id, updates)


def increment_tier2(bridge_id: str) -> None:
    from google.cloud.firestore import Increment

    _update(bridge_id, {"status.tier2Count": Increment(1)})


def unpair(bridge_id: str) -> None:
    """Clears the token and FCM token, keeps the row."""


def delete(bridge_id: str) -> None:
    _bridges().document(bridge_id).delete()


def create_pair_code(bridge_id: str, *, now: datetime | None = None) -> tuple[str, datetime]:
    now = now or datetime.now(UTC)
    expires_at = now + PAIR_CODE_TTL
    for _ in range(20):
        code = f"{secrets.randbelow(10**8):08d}"
        try:
            _codes().document(code).create({"bridgeId": bridge_id, "expiresAt": expires_at})
        except AlreadyExists:
            continue
        return code, expires_at
    raise RuntimeError("could not allocate a pairing code")


def consume_pair_code(code: str, *, now: datetime | None = None) -> str | None:
    """Read + delete in one transaction; an expired code is deleted and returns None."""
    now = now or datetime.now(UTC)
    ref = _codes().document(code)

    def _txn(transaction: Transaction) -> str | None:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        transaction.delete(ref)
        expires_at = data.get("expiresAt")
        if expires_at is None or expires_at <= now:
            return None
        return data.get("bridgeId")

    return run_transaction(_txn)


def peek_pair_code(code: str, *, now: datetime | None = None) -> str | None:
    """Read-only twin of `consume_pair_code`: the bridge id for a live code, else None."""
    now = now or datetime.now(UTC)
    data = _codes().document(code).get().to_dict() or {}
    expires_at = data.get("expiresAt")
    if expires_at is None or expires_at <= now:
        return None
    return data.get("bridgeId")


def list_expired_pair_codes(now: datetime) -> list[str]:
    query = _codes().where(filter=FieldFilter("expiresAt", "<", now))
    return [snap.id for snap in query.stream()]


def delete_pair_code(code: str) -> None:
    _codes().document(code).delete()
