"""The target relay's side of a move (docs/MOVE_DESIGN.md §4, §5.4, §7, §10).

A bundled pager is `moving`/`waiting` until its first signed `/status`; `mv{st:"try"}` makes it
`trying` and is answered with the proof; the proof's ack or `st:"ok"` makes it `moved`."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud.firestore import DELETE_FIELD, transactional
from pydantic import BaseModel

from app import ca_resolve, devcfg, familyexport, firmware, move, move_info, soracom
from app.broker import BrokerClient
from app.config import Settings, auth_backend
from app.db.firestore import get_db
from app.move_source import MoveError
from app.store import bridges as bridges_store
from app.store import devices as devices_store
from app.store import families as families_store
from app.store import fed_links as fed_links_store
from app.store import users as users_store
from app.wire import StatusEnvelope

logger = logging.getLogger("relay.move.target")

# With `bridgeRedirect` set, a phone that has asked again within this long is still here.
BRIDGE_STILL_HERE = timedelta(minutes=10)

# A first-run relay: the default family and the one admin `authn_home.create_first_admin` makes.
_PLACEHOLDER_FAMILY = "default"
_PLACEHOLDER_CREATED_BY = "setup"
# Data a placeholder relay must not hold.
_PLACEHOLDER_EMPTY = (
    "devices",
    "deviceSecrets",
    "bridges",
    "messages",
    "conversations",
    "allow",
    "contactNames",
    "smsNumbers",
    "inviteCodes",
    "hooks",
    "fedFamilies",
    "fedLinks",
    "fedInvites",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


# ---- move-info ---------------------------------------------------------------------------------


class Placeholder(BaseModel):
    """What the first-run page made, which an import replaces."""

    family_id: str
    uid: str
    alias: str


def find_placeholder(db: Any = None) -> Placeholder | None:
    """The first-run admin and default family when they are all this server holds (home sign-in
    only: the first-run page exists there)."""
    db = db or get_db()
    if auth_backend() != "home":
        return None
    # `limit(2)`: the public move-info asks this, so it never reads a whole collection
    fams = list(db.collection("families").limit(2).stream())
    if len(fams) != 1 or fams[0].id != _PLACEHOLDER_FAMILY:
        return None
    if (fams[0].to_dict() or {}).get("createdBy") != _PLACEHOLDER_CREATED_BY:
        return None
    users = list(db.collection("users").limit(2).stream())
    if len(users) != 1:
        return None
    data = users[0].to_dict() or {}
    if data.get("role") != "super" or data.get("kind", "person") != "person":
        return None
    if data.get("familyId") != _PLACEHOLDER_FAMILY:
        return None
    for coll in _PLACEHOLDER_EMPTY:
        if list(db.collection(coll).limit(1).stream()):
            return None
    return Placeholder(family_id=fams[0].id, uid=users[0].id, alias=str(data.get("alias") or ""))


def unclaimed(db: Any = None) -> bool:
    """No user and no family: nobody can sign in yet, as on the first-run page, so the encrypted
    bundle and its passphrase are the credential."""
    db = db or get_db()
    return not list(db.collection("users").limit(1).stream()) and not list(
        db.collection("families").limit(1).stream()
    )


def import_unfinished() -> bool:
    """An import started here and did not finish; the same bundle can be run again."""
    rec = move.get_record()
    return rec is not None and rec.role == "target" and rec.state == "importing"


def is_empty(db: Any = None) -> bool:
    """Whether an import would be accepted by what this server holds."""
    db = db or get_db()
    if move.get_record() is not None:
        return False
    return not list(db.collection("families").limit(1).stream()) or find_placeholder(db) is not None


def info(settings: Settings) -> dict[str, Any]:
    """`GET /api/public/move-info` (§7.1): only what a pager is told anyway."""
    if not settings.public_base_url:
        raise MoveError(503, "PUBLIC_BASE_URL is not set on this server")
    ca_pem = ca_resolve.current_pem(settings)
    ca = None
    if ca_pem:
        url, sha = ca_resolve.ca_pointer(ca_pem, settings)
        ca = {"url": url, "sha": sha.hex()}
    rec = move.get_record()
    imported = rec is not None and rec.role == "target" and rec.importedAt is not None
    body = {
        "v": 1,
        "authBackend": auth_backend(),
        "schemaVersion": familyexport.SCHEMA_VERSION,
        "relayVersion": familyexport.relay_version(),
        "baseUrl": settings.public_base_url,
        "broker": {"host": settings.broker_host, "port": settings.broker_port},
        "ca": ca,
        "beam": (
            {"host": settings.broker_host, "port": settings.beam_port}
            if settings.beam_port is not None
            else None
        ),
        "empty": is_empty(),
        "move": {"id": rec.id, "imported": True} if imported and rec is not None else None,
    }
    return move_info.MoveInfo.model_validate(body).model_dump(mode="json")


# ---- arrival -----------------------------------------------------------------------------------


def _device_ref(device_id: str) -> Any:
    return get_db().collection("devices").document(device_id)


def waiting_for_arrival(device: devices_store.Device) -> bool:
    """An imported pager whose move here is still open."""
    mv = device.move
    return (
        device.provisionState == "moving"
        and mv is not None
        and mv.state in ("waiting", "trying")
    )


def note_arrival(device: devices_store.Device) -> bool:
    """Stamps `move.arrivedAt` on the first signed `/status` of an open move. True the first time:
    the caller republishes what the pager is owed, whatever its session says."""
    if not waiting_for_arrival(device) or (device.move and device.move.arrivedAt is not None):
        return False
    _device_ref(device.id).update({"move.arrivedAt": _now()})
    return True


def note_report(
    device: devices_store.Device,
    env: StatusEnvelope,
    n: object,
    broker: BrokerClient,
    settings: Settings | None = None,
) -> None:
    """The pager's own `mv` report on an online `/status`: `try` is answered with the proof
    (`ok` = this status's `n`), `ok` commits."""
    mv = device.move
    if env.mv is None or mv is None or env.mv.id != mv.id or not waiting_for_arrival(device):
        return
    if env.mv.st == "ok":
        commit(device.id, broker, settings)
    elif env.mv.st == "try" and isinstance(n, int) and not isinstance(n, bool):
        try:
            devcfg.push_move_proof(device.id, mv.id, n, broker)
        except ValueError:
            logger.warning("move proof for %s not sent: n=%r out of range", device.id, n)
            return
        _device_ref(device.id).update({"move.state": "trying", "move.trialAt": _now()})


def note_ack(
    device_id: str, msg_id: str, broker: BrokerClient, settings: Settings | None = None
) -> None:
    """The ack of the proof commits."""
    snap = _device_ref(device_id).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    pending = data.get("pendingCfgMv")
    mv = data.get("move")
    if not isinstance(pending, dict) or pending.get("id") != msg_id or not isinstance(mv, dict):
        return
    obj = pending.get("obj")
    cfg_mv = ((obj or {}).get("cfg") or {}).get("mv") if isinstance(obj, dict) else None
    if (
        isinstance(cfg_mv, dict)
        and "ok" in cfg_mv
        and cfg_mv.get("id") == mv.get("id")
        and data.get("provisionState") == "moving"
        and mv.get("state") in ("waiting", "trying")
    ):
        commit(device_id, broker, settings)


def _claim_commit(device_id: str) -> bool:
    """Check-and-set in one transaction: True for the caller that moves the pager from an open
    state to `moved`, False for any other (the proof's ack and an `mv ok` status can arrive
    together)."""
    ref = _device_ref(device_id)
    db = get_db()

    @transactional
    def _run(txn: Any) -> bool:
        snap = ref.get(transaction=txn)
        data = (snap.to_dict() or {}) if snap.exists else {}
        mv = data.get("move")
        if (
            data.get("provisionState") != "moving"
            or not isinstance(mv, dict)
            or mv.get("state") not in ("waiting", "trying")
        ):
            return False
        txn.update(
            ref,
            {"move.state": "moved", "move.movedAt": _now(), "provisionState": "provisioned"},
        )
        return True

    return _run(db.transaction())


def commit(device_id: str, broker: BrokerClient, settings: Settings | None = None) -> bool:
    """`moved`: the pager committed here; an OTA job it holds is checked against this server's own
    bucket. False when another caller already committed it (or its move record is not open)."""
    if not _claim_commit(device_id):
        return False
    logger.info("pager %s moved", device_id)
    try:
        reissue_ota(device_id, broker, settings or Settings.from_env())
    except Exception:
        logger.exception("ota re-issue after move failed device=%s", device_id)
    return True


def _own_url(settings: Settings, url: object) -> bool:
    if not isinstance(url, str):
        return False
    bases = [settings.fw_bucket_base]
    if settings.fw_cache == "local" and settings.public_base_url:
        bases.append(f"{settings.public_base_url}/fw-cache/")
    return any(b and url.startswith(b) for b in bases)


def foreign_download(settings: Settings | None, obj: dict[str, Any]) -> bool:
    """A `cfg` holding an OTA or font download (not its cancel) whose URL is not this server's."""
    cfg = obj.get("cfg")
    if not isinstance(cfg, dict):
        return False
    settings = settings or Settings.from_env()
    for key in ("ota", "ast"):
        part = cfg.get(key)
        if isinstance(part, dict) and "cancel" not in part and not _own_url(settings, part.get("url")):
            return True
    return False


def reissue_ota(device_id: str, broker: BrokerClient, settings: Settings) -> None:
    """A job whose URL is not this server's is cancelled and, when this server's index has the
    build (and a move-capable build, for a pager that has moved), issued again (§5.2)."""
    dev = devices_store.get_device(device_id)
    if dev is None or dev.otaJob is None:
        return
    job = dev.otaJob
    snap = _device_ref(device_id).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    if job.part == "assets":
        slot, key = data.get("pendingCfgAst"), "ast"
    else:
        slot, key = data.get("pendingCfgOta"), "ota"
    obj = slot.get("obj") if isinstance(slot, dict) else None
    cfg = ((obj or {}).get("cfg") or {}).get(key) if isinstance(obj, dict) else None
    if not isinstance(cfg, dict) or "cancel" in cfg or _own_url(settings, cfg.get("url")):
        return
    meta = job.model_dump(mode="python", exclude={"at"})
    if job.part == "assets":
        devcfg.cancel_ast(device_id, broker)
        try:
            build = firmware.load_assets_index(settings).find(job.target16)
        except (firmware.FirmwareNotConfigured, firmware.FirmwareIndexUnavailable):
            build = None
        if build is not None:
            devcfg.push_ast(device_id, firmware.build_cfg_ast(build, settings), broker, job=meta)
        return
    devcfg.cancel_ota(device_id, broker)
    try:
        index = firmware.load_index(settings)
    except (firmware.FirmwareNotConfigured, firmware.FirmwareIndexUnavailable):
        return
    target = index.find(job.target16)
    if target is None or firmware.move_refusal(target, move_state="moved") is not None:
        return
    choice = firmware.choose(index, job.target16, dev.status.img)
    meta.update(kind=choice.kind, osz=choice.obj.osz, estBytes=firmware.estimate_bytes(choice.obj.osz))
    devcfg.push_ota(device_id, firmware.build_cfg_ota(choice, settings), broker, job=meta)


def note_bearer(device: devices_store.Device, env: StatusEnvelope) -> None:
    """Remembers that a pager of this move was on Beam the first time it reports so, for the
    switch-back guard: its last bearer alone would forget it."""
    mv = device.move
    if env.tls != "proxy" or mv is None or mv.state is None or mv.onBeam:
        return
    _device_ref(device.id).update({"move.onBeam": True})


def end_for_setup_code(device_id: str) -> None:
    """A setup code was issued: the pager's move record ends, in whatever state it was, and the
    order or proof pending for it goes with it."""
    snap = _device_ref(device_id).get()
    mv = ((snap.to_dict() or {}).get("move")) if snap.exists else None
    if not isinstance(mv, dict) or mv.get("state") is None:
        return
    _device_ref(device_id).update(
        {
            "move.state": "reprovisioned",
            "move.reprovisionedAt": _now(),
            "pendingCfgMv": DELETE_FIELD,
        }
    )


# ---- Beam --------------------------------------------------------------------------------------


def _target_record(*allowed: str) -> move.MoveRecord:
    rec = move.get_record()
    if rec is None or rec.role != "target":
        raise MoveError(409, "no move has been imported on this server")
    if rec.state not in allowed:
        raise MoveError(409, f"the move is {rec.state}")
    return rec


def _beam_devices() -> list[devices_store.Device]:
    return [
        d
        for d in devices_store.list_devices()
        if d.revokedAt is None
        and (d.status.tls == "proxy" or (d.move is not None and d.move.onBeam))
    ]


def beam_group_name(settings: Settings) -> str:
    return f"pager-beam-{settings.broker_host}"


def beam_view(settings: Settings, rec: move.MoveRecord) -> dict[str, Any] | None:
    """The Beam block of the target page; `None` when this server has no Beam listener."""
    if settings.beam_port is None:
        return None
    beam = rec.beam or move.BeamState()
    devs = _beam_devices()
    return {
        "host": settings.broker_host,
        "port": settings.beam_port,
        "count": len(devs),
        "movedCount": sum(1 for d in devs if d.move is not None and d.move.state == "moved"),
        "switched": beam.switched,
        "soracom": settings.soracom_configured,
        "byHand": beam.byHand,
    }


def _soracom_client(settings: Settings) -> soracom.SoracomClient:
    assert settings.soracom_auth_key_id and settings.soracom_auth_key
    return soracom.SoracomClient(settings.soracom_auth_key_id, settings.soracom_auth_key)


def beam_switch(settings: Settings, *, by_hand: bool) -> dict[str, Any]:
    """§10: a new group with this server's Beam listener as its destination, then every SIM of the
    old group moved into it; `by_hand` records that the admin did it in the console."""
    rec = _target_record("imported")
    if settings.beam_port is None:
        raise MoveError(409, "this server has no Beam listener (BEAM_PORT is not set)")
    if rec.beam is not None and rec.beam.switched:
        raise MoveError(409, "the Beam group is already switched")
    group = beam_group_name(settings)
    if by_hand:
        state = move.BeamState(switched=True, byHand=True, group=group, switchedAt=_now())
        move.patch_record(beam=state.model_dump(mode="python"))
        return {"switched": True, "byHand": True, "moved": 0}
    if not settings.soracom_configured:
        raise MoveError(409, "this server has no Soracom key: do the switch by hand")
    moved: list[str] = []
    old_gid = new_gid = None
    try:
        with _soracom_client(settings) as client:
            old_gid = soracom.find_group(client, settings.soracom_beam_group)
            if old_gid is None:
                raise MoveError(409, f"no Soracom group named {settings.soracom_beam_group}")
            destination = f"mqtts://{settings.broker_host}:{settings.beam_port}"
            new_gid, _created = soracom.ensure_group_created(client, group, destination)
            sims = [s for s in soracom.list_sims(client) if s.groupId == old_gid and s.imsi]
            for sim in sims:
                assert sim.imsi is not None
                soracom.enroll(client, sim.imsi, new_gid)
                moved.append(sim.imsi)
    except soracom.SoracomError as exc:
        _restore(settings, moved, old_gid)
        raise MoveError(502, f"soracom: {exc.detail}; the SIMs moved so far were put back") from exc
    state = move.BeamState(
        switched=True,
        byHand=False,
        group=group,
        oldGroupId=old_gid,
        newGroupId=new_gid,
        imsis=moved,
        switchedAt=_now(),
    )
    move.patch_record(beam=state.model_dump(mode="python"))
    return {"switched": True, "byHand": False, "moved": len(moved)}


def _restore(settings: Settings, imsis: list[str], old_gid: str | None) -> list[str]:
    """Puts `imsis` back in the old group; returns the ones that could not be."""
    if not imsis or old_gid is None:
        return list(imsis)
    left: list[str] = []
    try:
        with _soracom_client(settings) as client:
            for imsi in imsis:
                try:
                    soracom.enroll(client, imsi, old_gid)
                except soracom.SoracomError:
                    left.append(imsi)
    except soracom.SoracomError:
        return list(imsis)
    return left


def beam_switch_back(settings: Settings) -> dict[str, Any]:
    """Only while no Beam pager has moved here; the recorded SIMs go back to their group."""
    rec = _target_record("imported")
    beam = rec.beam
    if beam is None or not beam.switched:
        raise MoveError(409, "the Beam group is not switched")
    if any(d.move is not None and d.move.state == "moved" for d in _beam_devices()):
        raise MoveError(409, "a Beam pager has already moved here: it cannot go back")
    if beam.byHand:
        move.patch_record(beam=move.BeamState(group=beam.group).model_dump(mode="python"))
        return {"switched": False, "byHand": True, "moved": 0}
    if not settings.soracom_configured:
        raise MoveError(409, "this server has no Soracom key: move the SIMs back by hand")
    left = _restore(settings, beam.imsis, beam.oldGroupId)
    if left:
        move.patch_record(beam={**beam.model_dump(mode="python"), "imsis": left})
        raise MoveError(502, f"soracom: {len(left)} SIM(s) could not be moved back; try again")
    move.patch_record(beam=move.BeamState(group=beam.group).model_dump(mode="python"))
    return {"switched": False, "byHand": False, "moved": len(beam.imsis)}


# ---- bridges and End ---------------------------------------------------------------------------


def send_bridges_back() -> dict[str, Any]:
    """Rollback (§4.1): this server's bridge routes send phones to the source."""
    rec = _target_record("imported")
    if not rec.sourceBaseUrl:
        raise MoveError(409, "the bundle did not name the source server")
    move.patch_record(bridgeRedirect=rec.sourceBaseUrl.rstrip("/"))
    return {"bridgeRedirect": rec.sourceBaseUrl.rstrip("/")}


def end_move() -> dict[str, Any]:
    """Archives the record. Logins and the pagers' rows stay."""
    _target_record("imported")
    move.patch_record(state="ended", endedAt=_now())
    return {"state": "ended"}


# ---- what the page reads -----------------------------------------------------------------------


_NO_CREDENTIALS = "moved"


def _remedy(dev: devices_store.Device) -> str:
    if dev.provisionState == _NO_CREDENTIALS:
        return "setup_code"
    mv = dev.move
    if mv is not None and mv.state in ("waiting", "trying"):
        return "switch_now_or_setup"
    return "none"


def _device_state(dev: devices_store.Device) -> str:
    if dev.move is not None and dev.move.state is not None:
        return dev.move.state
    return "waiting"


def _device_rows(owners: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for d in sorted(devices_store.list_devices(), key=lambda x: x.id):
        if d.revokedAt is not None:
            continue
        has_move = d.move is not None and d.move.state is not None
        if not has_move and d.provisionState != _NO_CREDENTIALS:
            continue
        dark = move.dark_since(d.status) if _device_state(d) in ("waiting", "trying") else None
        rows.append(
            {
                "deviceId": d.id,
                "label": d.label,
                "owner": owners.get(d.ownerUid),
                "state": _device_state(d),
                "bearer": move.bearer_of(d.status),
                "lastSeenAt": _iso(move.last_seen(d.status)),
                "darkSince": _iso(dark),
                "remedy": _remedy(d),
            }
        )
    return rows


def _bridge_rows(rec: move.MoveRecord, owners: dict[str, str]) -> list[dict[str, Any]]:
    imported = rec.importedAt
    now = _now()
    rows = []
    for b in bridges_store.list_all():
        if not b.paired:
            continue
        seen = _aware(b.lastSeenAt) if b.lastSeenAt is not None else None
        arrived = seen is not None and (imported is None or seen >= _aware(imported))
        if arrived and rec.bridgeRedirect:
            arrived = now - seen <= BRIDGE_STILL_HERE
        rows.append(
            {
                "bridgeId": b.id,
                "label": b.label,
                "owner": owners.get(b.ownerUid),
                "lastSeenAt": _iso(seen),
                "arrived": arrived,
            }
        )
    return rows


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


GET_USERS_CHUNK = 100  # Firebase Auth's limit per `get_users` call


def _last_sign_ins(uids: list[str]) -> dict[str, datetime]:
    """The latest sign-in each person is known to have made: a relay session's creation (home) or
    Firebase Auth's last sign-in time. A person with none is absent."""
    out: dict[str, datetime] = {}
    if auth_backend() == "home":
        for snap in get_db().collection("sessions").stream():
            data = snap.to_dict() or {}
            made = data.get("createdAt")
            if isinstance(made, datetime):
                uid = str(data.get("uid"))
                made = _aware(made)
                if uid not in out or made > out[uid]:
                    out[uid] = made
        return out
    from firebase_admin import auth as fb_auth

    for i in range(0, len(uids), GET_USERS_CHUNK):
        chunk = uids[i : i + GET_USERS_CHUNK]
        try:
            found = fb_auth.get_users([fb_auth.UidIdentifier(u) for u in chunk]).users
        except Exception:  # unreadable Auth: nobody is shown as signed in
            logger.exception("Firebase Auth lookup of %d people failed", len(chunk))
            continue
        for u in found:
            stamp = u.user_metadata.last_sign_in_timestamp
            if stamp:
                out[u.uid] = datetime.fromtimestamp(stamp / 1000, UTC)
    return out


def _people_rows(rec: move.MoveRecord) -> list[dict[str, Any]]:
    people = [
        u for u in sorted(users_store.list_users(), key=lambda x: x.alias) if u.kind == "person"
    ]
    last = _last_sign_ins([u.uid for u in people])
    since = _aware(rec.importedAt) if rec.importedAt else None
    no_sign_in = set(rec.noSignIn)
    rows = []
    for u in people:
        seen = last.get(u.uid)
        rows.append(
            {
                "uid": u.uid,
                "alias": u.alias,
                "displayName": u.displayName,
                "email": u.email,
                "noSignIn": u.uid in no_sign_in,
                "signedIn": seen is not None and (since is None or seen >= since),
                "lastSignInAt": _iso(seen),
            }
        )
    return rows


def _fed_rows() -> list[dict[str, Any]]:
    names = {f.id: f.name for f in families_store.list_families()}
    rows = []
    for link in fed_links_store.list_all():
        rows.append(
            {
                "linkId": link.id,
                "family": names.get(link.familyId, link.familyId),
                "peer": link.remote.name or link.remote.where,
                "announced": link.announce is None or not link.announce.pending,
            }
        )
    return rows


def status(rec: move.MoveRecord, settings: Settings) -> dict[str, Any]:
    """`GET /api/admin/move` on the target."""
    owners = {u.uid: u.alias for u in users_store.list_users()}
    return {
        "role": "target",
        "id": rec.id,
        "state": rec.state,
        "sourceBaseUrl": rec.sourceBaseUrl,
        "importedAt": _iso(rec.importedAt),
        "endedAt": _iso(rec.endedAt),
        "bridgeRedirect": rec.bridgeRedirect,
        "devices": _device_rows(owners),
        "bridges": _bridge_rows(rec, owners),
        "people": _people_rows(rec),
        "fedLinks": _fed_rows(),
        "beam": beam_view(settings, rec),
    }

