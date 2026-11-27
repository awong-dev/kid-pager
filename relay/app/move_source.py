"""The source relay's side of a move (docs/MOVE_DESIGN.md §4, §7, §9).

`moveClaims/{uid}` (server-only) holds each person's accounts-panel answer: claim code, password
hash, email. It outlives the first bundle download, since the bundle can be fetched again."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import authn_home, devcfg, familyexport, move, move_info, movekeys
from app.broker import BrokerClient
from app.config import Settings
from app.db.firestore import get_db
from app.store import bridges as bridges_store
from app.store import devices as devices_store
from app.store import families as families_store
from app.store import users as users_store
from app.store import vapid as vapid_store
from app.wire import StatusEnvelope

if TYPE_CHECKING:
    from app.deployexport import DeploymentExportOptions

logger = logging.getLogger("relay.move")

PASSWORD_MIN = 8
PASSWORD_MAX = 128


class MoveError(Exception):
    """A refusal with the HTTP status and body the route answers with."""

    def __init__(self, status: int, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"detail": self.detail, **self.extra}


class Confirm(BaseModel):
    """What the super was shown and confirmed."""

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int
    caSha: str | None = None


class AccountAnswer(BaseModel):
    """One person's answer in the accounts panel."""

    model_config = ConfigDict(extra="forbid")

    uid: str
    email: str | None = None
    noSignIn: bool = False
    password: str | None = Field(default=None, repr=False)


def _check_ca_pem(v: str | None) -> str | None:
    try:
        return move_info.check_ca(v)
    except ValueError as exc:
        raise ValueError(f"caPem: {exc}") from exc


class PreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targetBaseUrl: str
    # The target's CA certificate (PEM) when its web certificate is signed by its own CA
    caPem: str | None = None

    _ca = field_validator("caPem")(_check_ca_pem)


class FreezeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targetBaseUrl: str
    confirm: Confirm
    accounts: list[AccountAnswer] = []
    caPem: str | None = None

    _ca = field_validator("caPem")(_check_ca_pem)


class CutoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deviceIds: list[str] | None = None


class MoveClaim(BaseModel):
    """`moveClaims/{uid}`."""

    model_config = ConfigDict(extra="ignore")

    uid: str
    moveId: str
    email: str | None = None
    noSignIn: bool = False
    code: str | None = None
    digest: str | None = None
    passwordHash: str | None = None


def _claims() -> Any:
    return get_db().collection("moveClaims")


def get_claims() -> dict[str, MoveClaim]:
    return {s.id: MoveClaim.model_validate(s.to_dict() or {}) for s in _claims().stream()}


def get_claim(uid: str) -> MoveClaim | None:
    snap = _claims().document(uid).get()
    return MoveClaim.model_validate(snap.to_dict() or {}) if snap.exists else None


def _drop_claims() -> None:
    for snap in list(_claims().stream()):
        _claims().document(snap.id).delete()


def _now() -> datetime:
    return datetime.now(UTC)


def _persons(family_ids: set[str]) -> list[users_store.User]:
    """The people the accounts panel covers: everyone with a sign-in in a family, not the outside
    contacts."""
    return sorted(
        (
            u
            for u in users_store.list_users()
            if u.kind == "person" and (u.familyId in family_ids or u.ownerFamilyId in family_ids)
        ),
        key=lambda u: u.alias,
    )


def _source_record(*allowed: str) -> move.MoveRecord:
    rec = move.get_record()
    if rec is None or rec.role != "source":
        raise MoveError(409, "no move is under way on this server")
    if rec.state not in allowed:
        raise MoveError(409, f"the move is {rec.state}")
    return rec


# ---- preflight and Freeze ---------------------------------------------------------------------


def preflight(target_base_url: str, ca_pem: str | None = None) -> dict[str, Any]:
    """The target's `move-info` plus `caFingerprint`."""
    info = _fetch(target_base_url, ca_pem)
    out = info.model_dump()
    out["caFingerprint"] = info.ca.sha if info.ca else None
    return out


def _fetch(target_base_url: str, ca_pem: str | None = None) -> move_info.MoveInfo:
    try:
        return move_info.fetch(target_base_url, ca_pem=ca_pem)
    except move_info.MoveInfoError as exc:
        if exc.code == "bad_target":
            raise MoveError(422, exc.detail) from exc
        if exc.code == "tls":
            hint = (
                "the CA certificate (caPem) does not match the new server's web certificate"
                if ca_pem
                else "its web certificate is not from a public CA: give its CA certificate (caPem)"
            )
            raise MoveError(502, f"the new server did not answer over TLS: {hint}") from exc
        if exc.code == "address_not_allowed":
            raise MoveError(422, f"that address cannot be used: {exc.detail}") from exc
        raise MoveError(502, f"the new server did not answer: {exc}") from exc


def _check_confirm(info: move_info.MoveInfo, confirm: Confirm) -> None:
    sha = info.ca.sha if info.ca else None
    if (info.broker.host, info.broker.port, sha) != (confirm.host, confirm.port, confirm.caSha):
        raise MoveError(
            409,
            "the new server's details changed since you confirmed them",
            broker={"host": info.broker.host, "port": info.broker.port},
            caSha=sha,
        )


class _Answers(BaseModel):
    """The accounts panel, checked: per uid the email and the password hash."""

    emails: dict[str, str] = {}
    no_sign_in: set[str] = set()
    hashes: dict[str, str] = {}


def _check_accounts(
    persons: list[users_store.User], answers: list[AccountAnswer], backend: str
) -> _Answers:
    by_uid = {p.uid: p for p in persons}
    given: dict[str, AccountAnswer] = {}
    unknown = sorted({a.uid for a in answers if a.uid not in by_uid})
    if unknown:
        raise MoveError(422, "unknown people in the accounts list", unknown=unknown)
    for a in answers:
        given[a.uid] = a
    out = _Answers()
    missing: list[str] = []
    invalid: dict[str, str] = {}
    owners: dict[str, list[str]] = {}
    for p in persons:
        a = given.get(p.uid) or AccountAnswer(uid=p.uid)
        if a.noSignIn:
            out.no_sign_in.add(p.uid)
            if a.password:
                invalid[p.uid] = "a person with no sign-in has no password"
            continue
        if backend == "firebase":
            if a.password:
                invalid[p.uid] = "a password is for a home server"
            raw = (a.email or p.email or "").strip()
            if not raw:
                missing.append(p.uid)
                continue
            email = familyexport.normalize_email(raw)
            if email is None:
                invalid[p.uid] = "not an email address"
                continue
            out.emails[p.uid] = email
            owners.setdefault(email, []).append(p.uid)
        elif a.password:
            if not PASSWORD_MIN <= len(a.password) <= PASSWORD_MAX:
                invalid[p.uid] = f"the password needs {PASSWORD_MIN} to {PASSWORD_MAX} characters"
                continue
            out.hashes[p.uid] = authn_home.hash_password(a.password)
    for email, group in owners.items():
        if len(group) > 1:
            for uid in group:
                invalid[uid] = f"{email} is given to more than one person"
    if missing:
        raise MoveError(422, "the accounts list is incomplete", missing=sorted(missing))
    if invalid:
        raise MoveError(422, "the accounts list has errors", invalid=invalid)
    return out


def freeze(req: FreezeRequest) -> str:
    """Mints the move id, records the accounts answers and the confirmed target, freezes every
    family and seeds each pager's move row. Returns the move id."""
    rec = move.get_record()
    if rec is not None and rec.role == "target":
        raise MoveError(409, "this server is the target of a move")
    if rec is not None and rec.state in move.SOURCE_FROZEN_STATES:
        raise MoveError(409, "a move is already under way")
    families = families_store.list_families()
    if not families:
        raise MoveError(409, "there is nothing to move")
    info = _fetch(req.targetBaseUrl, req.caPem)
    _check_confirm(info, req.confirm)
    if info.schemaVersion != familyexport.SCHEMA_VERSION:
        raise MoveError(
            409,
            f"the new server runs schema {info.schemaVersion}, this one {familyexport.SCHEMA_VERSION}: "
            "upgrade both to the same release first",
        )
    move_id = movekeys.new_move_id()
    try:
        devcfg.build_move_order(
            move_id=move_id,
            host=info.broker.host,
            port=info.broker.port,
            ca=devcfg.MoveCa(url=info.ca.url, sha=info.ca.sha) if info.ca else None,
        )
    except ValueError as exc:
        raise MoveError(422, f"the new server's details cannot go in a pager's order: {exc}") from exc
    persons = _persons({f.id for f in families})
    answers = _check_accounts(persons, req.accounts, info.authBackend)
    target = move.MoveTargetCoords(
        baseUrl=move_info.parse_target(req.targetBaseUrl).baseUrl,
        authBackend=info.authBackend,
        host=info.broker.host,
        port=info.broker.port,
        caUrl=info.ca.url if info.ca else None,
        caSha=info.ca.sha if info.ca else None,
        tlsCa=req.caPem,
    )
    _drop_claims()
    for p in persons:
        claim = MoveClaim(
            uid=p.uid,
            moveId=move_id,
            email=answers.emails.get(p.uid) or p.email,
            noSignIn=p.uid in answers.no_sign_in,
            passwordHash=answers.hashes.get(p.uid),
        )
        if info.authBackend == "home" and not claim.noSignIn:
            claim.code, claim.digest = authn_home.new_invite_code()
        _claims().document(p.uid).set(claim.model_dump())
        if info.authBackend == "firebase" and p.uid in answers.emails:
            users_store.update_user(p.uid, email=answers.emails[p.uid])
    move.save_record(
        move.SourceMoveRecord(
            id=move_id,
            frozenAt=_now(),
            target=target,
            bridgeRedirect=target.baseUrl,
            frozenByMove=sorted(f.id for f in families if not f.frozen),
        )
    )
    for fam in families:
        families_store.set_frozen(fam.id, True)
    for dev in devices_store.list_devices():
        get_db().collection("devices").document(dev.id).update(
            {"move": {"id": move_id, "capable": dev.status.mvc == 1}}
        )
    return move_id


# ---- the bundle -------------------------------------------------------------------------------


def export_options(settings: Settings, *, history: bool = True) -> DeploymentExportOptions:
    from app.deployexport import DeploymentExportOptions, env_secrets_set

    rec = _source_record(*move.SOURCE_FROZEN_STATES)
    assert rec.target is not None
    claims = get_claims()
    vapid = (
        vapid_store.VapidKey(
            privateKey=settings.webpush_vapid_private_key,
            publicKey=settings.webpush_vapid_public_key,
        )
        if settings.webpush_vapid_private_key and settings.webpush_vapid_public_key
        else None
    )
    frozen_at = rec.frozenAt if isinstance(rec.frozenAt, datetime) else _now()
    return DeploymentExportOptions(
        move_id=rec.id,
        frozen_at=frozen_at,
        target_base_url=rec.target.baseUrl,
        target_auth_backend=rec.target.authBackend,
        history=history,
        source_kind=familyexport.source_kind(),
        base_url=settings.public_base_url,
        no_sign_in=sorted(u for u, c in claims.items() if c.noSignIn)
        if rec.target.authBackend == "firebase"
        else [],
        password_hashes={u: c.passwordHash for u, c in claims.items() if c.passwordHash},
        invites={u: c.digest for u, c in claims.items() if c.digest and not c.noSignIn},
        vapid=vapid,
        env_secrets=env_secrets_set(settings),
    )


# ---- cutover ----------------------------------------------------------------------------------


def target_imported(rec: move.MoveRecord) -> bool | None:
    """Whether the target's `move-info` reports this move imported; `None` when it cannot be read."""
    if rec.target is None:
        return None
    try:
        info = move_info.fetch(rec.target.baseUrl, ca_pem=rec.target.tlsCa)
    except move_info.MoveInfoError as exc:
        logger.info("move-info of the target unreadable: %s", exc)
        return None
    return info.move is not None and info.move.id == rec.id and info.move.imported


def can_move_itself(dev: devices_store.Device) -> bool:
    """A pager that reports `mvc` and signs with a key the move derived `k'` from."""
    capable = dev.move.capable if dev.move is not None else dev.status.mvc == 1
    return capable and dev.authMode == "hmac"


def _order(dev: devices_store.Device, rec: move.MoveRecord, broker: BrokerClient) -> bool:
    """Publishes the order; the row is stamped only when it was sent."""
    assert rec.target is not None
    t = rec.target
    ca = devcfg.MoveCa(url=t.caUrl, sha=t.caSha) if t.caUrl and t.caSha else None
    sent = devcfg.push_move_order(
        dev.id, move_id=rec.id, host=t.host, port=t.port, ca=ca, broker=broker
    )
    if not sent:
        return False
    get_db().collection("devices").document(dev.id).update(
        {
            "move.id": rec.id,
            "move.sentAt": _now(),
            "move.ackAt": None,
            "move.cancelledAt": None,
        }
    )
    return True


def _cancelled(dev: devices_store.Device) -> bool:
    """A cancel was sent under this move id: the pager's ledger then ignores any order with it
    (MOVE_FIRMWARE_DESIGN.md M12)."""
    return dev.move is not None and dev.move.cancelledAt is not None


def _device(device_id: str) -> devices_store.Device:
    dev = devices_store.get_device(device_id)
    if dev is None or dev.revokedAt is not None:
        raise MoveError(404, "no such pager")
    return dev


def start_cutover(broker: BrokerClient, device_ids: list[str] | None) -> int:
    """Orders every capable pager (or the named ones) onto the target; refused until the target
    reports this move imported. Returns how many orders the broker accepted."""
    rec = _source_record(*move.SOURCE_FROZEN_STATES)
    if rec.state == "frozen":
        imported = target_imported(rec)
        if imported is None:
            raise MoveError(502, "the new server did not answer")
        if not imported:
            raise MoveError(409, "the new server has not imported this move yet")
    if device_ids is None:
        chosen = [
            d
            for d in devices_store.list_devices()
            if d.revokedAt is None
            and can_move_itself(d)
            and (d.move is None or d.move.sentAt is None)
        ]
    else:
        chosen = [_device(i) for i in dict.fromkeys(device_ids)]
        chosen = [d for d in chosen if can_move_itself(d) and not _cancelled(d)]
    ordered = sum(1 for dev in chosen if _order(dev, rec, broker))
    if rec.state == "frozen":
        move.patch_record(state="cutover", cutoverAt=_now())
    return ordered


def resend(broker: BrokerClient, device_id: str) -> None:
    rec = _source_record("cutover")
    dev = _device(device_id)
    if not can_move_itself(dev):
        raise MoveError(409, "this pager cannot move by itself")
    if _cancelled(dev):
        raise MoveError(
            409, "this pager's order was cancelled; it ignores this move from now on: give it a setup code"
        )
    if not _order(dev, rec, broker):
        raise MoveError(502, "the broker did not accept the order; try again")


def _cancel_order(dev: devices_store.Device, rec: move.MoveRecord, broker: BrokerClient) -> None:
    devcfg.cancel_move(dev.id, rec.id, broker)
    get_db().collection("devices").document(dev.id).update({"move.cancelledAt": _now()})


def cancel_device(broker: BrokerClient, device_id: str) -> None:
    rec = _source_record("cutover")
    dev = _device(device_id)
    if dev.move is None or dev.move.sentAt is None:
        raise MoveError(409, "this pager has no order to cancel")
    _cancel_order(dev, rec, broker)


def cancel_move(broker: BrokerClient) -> int:
    """Rolls back: after cutover every pager that was ordered gets a cancel first. Unfreezes every
    family and clears the bridge redirect; the record stays. Returns how many cancels were sent."""
    rec = _source_record(*move.SOURCE_FROZEN_STATES)
    cancelled = 0
    if rec.state == "cutover":
        for dev in devices_store.list_devices():
            if dev.move is not None and dev.move.sentAt is not None and dev.move.cancelledAt is None:
                _cancel_order(dev, rec, broker)
                cancelled += 1
    move.patch_record(state="cancelled", cancelledAt=_now(), bridgeRedirect=None)
    for fam in families_store.list_families():
        if rec.frozenByMove is None or fam.id in rec.frozenByMove:
            families_store.set_frozen(fam.id, False)
    _drop_claims()
    return cancelled


# ---- what the frozen ingest path records ------------------------------------------------------


def note_ack(device_id: str, msg_id: str) -> None:
    """An ack of the pager's current order stamps `move.ackAt`."""
    snap = get_db().collection("devices").document(device_id).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    pending = data.get("pendingCfgMv")
    mv = data.get("move")
    if (
        isinstance(pending, dict)
        and pending.get("id") == msg_id
        and isinstance(mv, dict)
        and mv.get("sentAt") is not None
        and mv.get("cancelledAt") is None
    ):
        get_db().collection("devices").document(device_id).update({"move.ackAt": _now()})


def note_status(device_id: str, env: StatusEnvelope, rec: move.MoveRecord) -> None:
    """Capability and the pager's own report of this move from an online `/status`."""
    if env.state != "online":
        return
    updates: dict[str, Any] = {"move.id": rec.id, "move.capable": env.mvc == 1}
    if env.mv is not None and env.mv.id == rec.id:
        updates["move.last"] = {
            "st": env.mv.st,
            "rsn": env.mv.rsn,
            "at": env.mv.at,
            "ts": _now(),
        }
    get_db().collection("devices").document(device_id).update(updates)


# ---- what the pages read ----------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value is not None else None


def _device_row(dev: devices_store.Device, owners: dict[str, str]) -> dict[str, Any]:
    mv = dev.move
    last = mv.last if mv is not None else None
    return {
        "deviceId": dev.id,
        "label": dev.label,
        "owner": owners.get(dev.ownerUid),
        "capable": can_move_itself(dev),
        "sentAt": _iso(mv.sentAt) if mv else None,
        "ackAt": _iso(mv.ackAt) if mv else None,
        "last": (
            {"st": last.st, "rsn": last.rsn, "at": last.at, "ts": _iso(last.ts)} if last else None
        ),
        "cancelledAt": _iso(mv.cancelledAt) if mv else None,
        "bearer": move.bearer_of(dev.status),
        "lastSeenAt": _iso(move.last_seen(dev.status)),
        "darkSince": _iso(move.dark_since(dev.status)),
    }


def status(rec: move.MoveRecord) -> dict[str, Any]:
    """`GET /api/admin/move` on the source."""
    assert rec.target is not None
    owners = {u.uid: u.alias for u in users_store.list_users()}
    claims = get_claims()
    people = []
    for p in _persons({f.id for f in families_store.list_families()}):
        c = claims.get(p.uid)
        people.append(
            {
                "uid": p.uid,
                "alias": p.alias,
                "displayName": p.displayName,
                "email": (c.email if c else None) or p.email,
                "noSignIn": bool(c and c.noSignIn),
            }
        )
    out: dict[str, Any] = {
        "role": "source",
        "id": rec.id,
        "state": rec.state,
        "target": {**rec.target.model_dump(exclude={"tlsCa"}), "privateCa": bool(rec.target.tlsCa)},
        "bridgeRedirect": rec.bridgeRedirect,
        "devices": [
            _device_row(d, owners) for d in devices_store.list_devices() if d.revokedAt is None
        ],
        "bridges": [
            {
                "bridgeId": b.id,
                "label": b.label,
                "owner": owners.get(b.ownerUid),
                "lastSeenAt": _iso(b.lastSeenAt),
                "redirected": bool(rec.bridgeRedirect),
            }
            for b in bridges_store.list_all()
            if b.paired
        ],
        "people": people,
    }
    if rec.state == "cutover":
        # Start cutover checked it
        out["targetImported"] = True
    elif rec.state == "frozen":
        imported = _target_imported_cached(rec)
        if imported is not None:
            out["targetImported"] = imported
    return out


# The status page polls; a target that did not answer is asked again only after this long (each
# try costs up to the federation client's timeout per address). An answer is not cached.
UNREACHABLE_RETRY_S = 30.0
_unreachable_at: dict[str, float] = {}


def _target_imported_cached(rec: move.MoveRecord) -> bool | None:
    now = time.monotonic()
    at = _unreachable_at.get(rec.id)
    if at is not None and now - at < UNREACHABLE_RETRY_S:
        return None
    value = target_imported(rec)
    _unreachable_at.clear()
    if value is None:
        _unreachable_at[rec.id] = now
    return value


def banner_for(uid: str, rec: move.MoveRecord) -> dict[str, Any] | None:
    """`GET /api/me/move`: where the family went and how this person signs in there."""
    if rec.role != "source" or rec.state not in move.SOURCE_FROZEN_STATES or rec.target is None:
        return None
    out: dict[str, Any] = {"targetBaseUrl": rec.target.baseUrl}
    claim = get_claim(uid)
    if claim is not None and claim.moveId == rec.id:
        if claim.code:
            out["claim"] = {"url": f"{rec.target.baseUrl}/login#invite={claim.code}"}
        if rec.target.authBackend == "firebase" and claim.email and not claim.noSignIn:
            out["email"] = claim.email
    return out
