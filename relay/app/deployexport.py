"""Deployment bundle (`.kpdep`): every family plus the deployment's settings, for the over-the-air
move (docs/MOVE_DESIGN.md §5, §6, §9). The `.kpfam` container (passphrase mandatory); devices carry
the derived key and password, never the original key."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Literal
from urllib.parse import urlsplit

from google.cloud.firestore import FieldFilter
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import authn_home, familyexport, movekeys
from app.broker_admin import BrokerAdmin, make_broker_admin
from app.config import Settings, auth_backend
from app.db import sqlitestore
from app.familyexport import (
    NO_SIGN_IN,
    SCHEMA_VERSION,
    BadPassphrase,
    Collision,
    CorruptFile,
    Dropped,
    FedReport,
    FirebaseImportAuth,
    ImportAuth,
    Imported,
    KpfamFile,
    NotFrozen,
    PassphraseRequired,
    Person,
    Row,
    Source,
)
from app.move import TargetMoveRecord, invalidate_cache
from app.store import vapid as vapid_store

KPDEP_VERSION = 1
# The recent messages and wire ids every bundle carries, history or not: a pager may still ack
# them and may still re-send an up id (docs/MOVE_DESIGN.md §5.2).
FLOOR = timedelta(hours=24)
# A claim code's lifetime on the target.
CLAIM_TTL = timedelta(days=30)
# Message delivery states a pager may still answer.
_OPEN_DELIVERY = ("queued", "sent")

AuthKind = Literal["home", "firebase"]

# Env-level settings the bundle cannot carry; the import report names the ones the source had set.
_ENV_SECRETS = (
    ("SORACOM_AUTH_KEY_ID", "soracom_auth_key_id"),
    ("SORACOM_AUTH_KEY", "soracom_auth_key"),
    ("FW_INDEX_URL", "fw_index_url"),
)


class TargetHoldsFamilies(RuntimeError):
    """The target is not empty; `.report` says what is there."""


class BrokerLoginFailed(RuntimeError):
    """A broker login could not be created; nothing was written to the store."""

    def __init__(self, device_id: str) -> None:
        super().__init__(f"could not create the broker login for device {device_id}")
        self.device_id = device_id


class MoveTarget(BaseModel):
    baseUrl: str
    authBackend: AuthKind


class MoveHeader(BaseModel):
    id: str
    frozenAt: str
    target: MoveTarget

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not movekeys.is_valid_move_id(v):
            raise ValueError(f"invalid move id {v!r}")
        return v


class BundleFamily(BaseModel):
    id: str
    name: str
    idSeed: str
    counts: dict[str, int] = {}
    dropped: list[Dropped] = []


class DeploymentManifest(BaseModel):
    """Line 1 of a `.kpdep`."""

    model_config = ConfigDict(extra="ignore")

    kpdep: int = KPDEP_VERSION
    schemaVersion: int = Field(default_factory=lambda: SCHEMA_VERSION)
    relayVersion: str = Field(default_factory=familyexport.relay_version)
    source: Source
    move: MoveHeader
    families: list[BundleFamily] = []
    # people who only carry a pager: read as the answer "-" on a Firebase target
    noSignIn: list[str] = []
    exportedAt: str
    history: bool
    # env-level settings the source had set, which the operator copies by hand
    envSecrets: list[str] = []
    deployment: dict[str, int] = {}


class DeploymentExportOptions(BaseModel):
    """What the Freeze step decided: the move, its target and the accounts panel's answers."""

    move_id: str
    frozen_at: datetime
    target_base_url: str
    target_auth_backend: AuthKind
    # `False` keeps only the floor of messages and the wire ids; everything else is history
    history: bool = True
    source_kind: Literal["cloud", "home"] = "home"
    base_url: str | None = None
    # uids that get no sign-in on a Firebase target
    no_sign_in: list[str] = []
    # home target: uid -> argon2id PHC string of the password the admin set (`mustChange`)
    password_hashes: dict[str, str] = {}
    # home target: uid -> digest of the claim code minted for that person (`authn_home.new_invite_code`)
    invites: dict[str, str] = {}
    # the source's VAPID key pair; `None` drops the push subscriptions
    vapid: vapid_store.VapidKey | None = None
    env_secrets: list[str] = []

    @field_validator("move_id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not movekeys.is_valid_move_id(v):
            raise ValueError(f"invalid move id {v!r}")
        return v


def env_secrets_set(settings: Settings | None = None) -> list[str]:
    """Names of the env-level settings that are set here, for the manifest."""
    s = settings or Settings.from_env()
    return [name for name, attr in _ENV_SECRETS if getattr(s, attr)]


def _aware(v: Any) -> datetime | None:
    if not isinstance(v, datetime):
        return None
    return v if v.tzinfo else v.replace(tzinfo=UTC)


def _is_open(data: dict[str, Any], cutoff: datetime) -> bool:
    """Whether a message belongs to the floor: written within `FLOOR`, or still answerable."""
    created = _aware(data.get("createdAt"))
    if created is None or created >= cutoff:
        return True
    return any(
        isinstance(d, dict) and d.get("state") in _OPEN_DELIVERY
        for d in (data.get("deliveries") or {}).values()
    )


class _Scope:
    """What the whole deployment holds, shared by every family's collector: a row whose two ends
    are in different families moves with the deployment, and is written once."""

    def __init__(self, fids: set[str], uids: set[str]) -> None:
        self.fids = fids
        self.uids = uids
        self.wire_ids: set[str] = set()


def _deployment_scope(db: Any, fids: list[str]) -> _Scope:
    uids: set[str] = set()
    for fid in fids:
        for field in ("familyId", "ownerFamilyId"):
            uids.update(s.id for s in familyexport._where(db, "users", field, "==", fid))
    return _Scope(set(fids), uids)


class _DeploymentCollector(familyexport._Collector):
    """One family's rows as a move writes them."""

    def __init__(
        self,
        db: Any,
        fid: str,
        opts: DeploymentExportOptions,
        recent_wire_ids: list[tuple[str, dict[str, Any]]],
        scope: _Scope,
    ) -> None:
        # the base class's history switch stays on: `_history` applies `opts.history` itself
        super().__init__(db, fid, history=True, backup=False)
        self.opts = opts
        self.recent_wire_ids = recent_wire_ids
        self.scope = scope
        self.cutoff = datetime.now(UTC) - FLOOR
        home = opts.target_auth_backend == "home"
        self.password_hashes = opts.password_hashes if home else {}
        self.password_done: set[str] = set()
        self.persons: set[str] = set()
        # keys of earlier moves that this family's subscriptions were made against
        self.carried_vapid: dict[str, vapid_store.VapidKey] = {}

    def in_scope(self, uid: str) -> bool:
        return uid in self.scope.uids

    def run(self) -> Iterator[Row]:
        home = self.opts.target_auth_backend == "home"
        for r in super().run():
            parts = r["path"].split("/")
            if not home and len(parts) == 4 and parts[0] == "users" and parts[2] == "credentials":
                # a Firebase target signs people in by email; their home credentials stay here
                self.counts["credentials"] -= 1
                if not self.counts["credentials"]:
                    del self.counts["credentials"]
                self.drop("credentials", "the target signs in with Firebase")
                continue
            yield r

    def _owns(self, data: dict[str, Any]) -> bool:
        """A row shared by several families is written once, by the first of them."""
        ids = familyexport._as_list(data.get("familyIds"))
        return not ids or min(ids) == self.fid

    def _inside(self, data: dict[str, Any]) -> bool:
        return all(f in self.scope.fids for f in familyexport._as_list(data.get("familyIds")))

    def row(self, path: str, data: dict[str, Any]) -> Row:
        parts = path.split("/")
        if len(parts) == 2 and parts[0] == "users" and data.get("kind", "person") == "person":
            self.persons.add(parts[1])
        if (
            len(parts) == 4
            and parts[0] == "users"
            and parts[2:] == ["credentials", "password"]
            and parts[1] in self.password_hashes
        ):
            data = self._password_credential(parts[1])
        return super().row(path, data)

    def _password_credential(self, uid: str) -> dict[str, Any]:
        self.password_done.add(uid)
        return {
            "kind": "password",
            "hash": self.password_hashes[uid],
            "updatedAt": self.opts.frozen_at,
            "mustChange": True,
        }

    def _user_extra(self, uid: str) -> Iterator[Row]:
        if uid in self.password_hashes and uid not in self.password_done:
            yield self.row(f"users/{uid}/credentials/password", {})
        for snap in (
            self.db.collection("users").document(uid).collection("pushSubscriptions").stream()
        ):
            data = snap.to_dict() or {}
            kid = self._carried_kid(data.get("vapidKid"))
            if kid is None:
                self.drop("pushSubscriptions", "the source has no VAPID key for it")
                continue
            yield self.row(f"users/{uid}/pushSubscriptions/{snap.id}", {**data, "vapidKid": kid})

    def _carried_kid(self, kid: str | None) -> str | None:
        """`src` for a subscription made against this server's own key, else the id of the earlier
        move's key it was made against (which travels with the bundle)."""
        if kid is None:
            return vapid_store.SRC_KID if self.opts.vapid is not None else None
        snap = self.db.collection("vapid").document(kid).get()
        if not snap.exists:
            return None
        key = vapid_store.VapidKey.model_validate(snap.to_dict() or {})
        carried = vapid_store.kid_for(key.publicKey)
        self.carried_vapid[carried] = key
        return carried

    def _federation(self) -> Iterator[Row]:
        yield from super()._federation()
        for snap in _where_all(self.db, "fedOutbox", "familyId", self.fid):
            data = snap.to_dict() or {}
            if data.get("state") == "pending":
                yield self.row(f"fedOutbox/{snap.id}", data)

    def _devices(self) -> Iterator[Row]:
        db = self.db
        for snap in familyexport._where(db, "devices", "familyId", "==", self.fid):
            data = snap.to_dict() or {}
            dev = snap.id
            self.device_ids.add(dev)
            if data.get("defaultToUid") not in (None, *self.scope.uids):
                data["defaultToUid"] = None
            if "locatableBy" in data:
                data["locatableBy"] = [
                    u for u in familyexport._as_list(data["locatableBy"]) if u in self.scope.uids
                ]
            data["bookVersion"] = int(data.get("bookVersion") or 0) + 1
            sec = db.collection("deviceSecrets").document(dev).get()
            secret = self._derived_secret(dev, data, (sec.to_dict() or {}) if sec.exists else None)
            if secret is None:
                # no key to derive from: this pager arrives as after a family move
                data["provisionState"] = "moved"
                data["status"] = {}
                for k in [k for k in data if k.startswith(familyexport._PENDING_PREFIX)]:
                    del data[k]
                self.drop("deviceSecrets", "no HMAC key: the pager needs a setup code")
                yield self.row(f"devices/{dev}", data)
                continue
            data["provisionState"] = "moving"
            # A Beam pager arrives over the direct bearer, so its Beam origin is recorded here.
            on_beam = (data.get("status") or {}).get("tls") == "proxy"
            data["move"] = {"id": self.opts.move_id, "state": "waiting", "onBeam": on_beam}
            data.pop("pendingCfgCa", None)
            # an order or cancel in the slot is the source's, signed under `k`; the target sends
            # only proofs
            data.pop("pendingCfgMv", None)
            data.pop("pendingCfgBr", None)
            data.pop("bearerTry", None)
            yield self.row(f"devices/{dev}", data)
            yield self.row(f"deviceSecrets/{dev}", secret)

    def _derived_secret(
        self, dev: str, device: dict[str, Any], secret: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """The bundle's `deviceSecrets` row: the derived key and password, never the key itself."""
        if secret is None or device.get("authMode", "hmac") != "hmac":
            return None
        raw = secret.get("hmacKey")
        try:
            k = base64.b64decode(raw) if isinstance(raw, str) else bytes(raw or b"")
        except ValueError:
            return None
        if len(k) != movekeys.KEY_LEN:
            return None
        k_prime, pw_prime = movekeys.derive(k, self.opts.move_id, dev)
        out: dict[str, Any] = {
            "hmacKey": base64.b64encode(k_prime).decode("ascii"),
            "mqttPassword": pw_prime,
            "upN": int(secret.get("upN") or 0),
            "upBits": int(secret.get("upBits") or 0),
            "downN": 0,
            "sigFailures": 0,
            "authMode": "hmac",
        }
        if secret.get("createdAt") is not None:
            out["createdAt"] = secret["createdAt"]
        return out

    def _bridges(self) -> Iterator[Row]:
        db = self.db
        for snap in familyexport._where(db, "bridges", "familyId", "==", self.fid):
            data = snap.to_dict() or {}
            for k in ("fcmToken", "health", "lastSeenAt"):
                data.pop(k, None)
            yield self.row(f"bridges/{snap.id}", data)
            pending = (
                db.collection("bridges")
                .document(snap.id)
                .collection("outbox")
                .where(filter=FieldFilter("state", "==", "pending"))
            )
            for ob in pending.stream():
                yield self.row(f"bridges/{snap.id}/outbox/{ob.id}", ob.to_dict() or {})
        yield from self._simple("bridgeConversations", "familyId")

    def _allow(self) -> Iterator[Row]:
        for snap in familyexport._where(self.db, "allow", "familyIds", "array_contains", self.fid):
            data = snap.to_dict() or {}
            if not self._owns(data):
                continue
            if self.foreign_uid("allow", data):
                self.drop("allow", "an end is outside the deployment")
                continue
            yield self.row(f"allow/{snap.id}", data)

    def _conversations(self) -> Iterator[Row]:
        self.conv_keys: set[str] = set()
        for snap in familyexport._where(
            self.db, "conversations", "familyIds", "array_contains", self.fid
        ):
            data = snap.to_dict() or {}
            if not self._owns(data):
                continue
            if not self._inside(data) or self.foreign_uid("conversations", data):
                self.drop("conversations", "shared with a family outside the deployment")
                continue
            self.conv_keys.add(snap.id)
            yield self.row(f"conversations/{snap.id}", data)
            alias = data.get("alias")
            if data.get("kind") == "group" and isinstance(alias, str):
                a = self.db.collection("aliases").document(alias).get()
                if a.exists and (a.to_dict() or {}).get("convKey") == snap.id:
                    yield self.row(f"aliases/{alias}", a.to_dict() or {})

    def _history(self) -> Iterator[Row]:
        db = self.db
        full = self.opts.history
        for snap in familyexport._where(db, "messages", "familyIds", "array_contains", self.fid):
            data = snap.to_dict() or {}
            if not self._owns(data):
                continue
            if not self._inside(data) or self.foreign_uid("messages", data):
                self.drop("messages", "shared with a family outside the deployment")
                continue
            if not full and not _is_open(data, self.cutoff):
                continue
            yield self.row(f"messages/{snap.id}", data)
            wid, rcpt = data.get("wireId"), data.get("recipientUid")
            if isinstance(wid, str) and isinstance(rcpt, str):
                w = db.collection("wireIds").document(f"{wid}_{rcpt}").get()
                if w.exists and w.id not in self.scope.wire_ids:
                    self.scope.wire_ids.add(w.id)
                    yield self.row(f"wireIds/{w.id}", w.to_dict() or {})
        for wid_doc, data in self.recent_wire_ids:
            if wid_doc not in self.scope.wire_ids and any(
                wid_doc.endswith(f"_{u}") for u in self.uids
            ):
                self.scope.wire_ids.add(wid_doc)
                yield self.row(f"wireIds/{wid_doc}", data)
        if not full:
            return
        for dev in sorted(self.device_ids):
            for sub in ("locations", "battery", "smsLog"):
                for snap in db.collection("devices").document(dev).collection(sub).stream():
                    data = snap.to_dict() or {}
                    if sub == "smsLog" and self.foreign_uid("smsLog", data):
                        self.drop("smsLog", "names a person outside the family")
                        continue
                    yield self.row(f"devices/{dev}/{sub}/{snap.id}", data)
        yield from self._simple("heldSms", "familyId")
        yield from self._simple("heldChat", "familyId")


def _where_all(db: Any, coll: str, field: str, value: Any) -> Iterator[Any]:
    return familyexport._where(db, coll, field, "==", value)


def _marker_line(marker: dict[str, str]) -> bytes:
    return (json.dumps(marker, separators=(",", ":")) + "\n").encode()


def export_deployment(
    db: Any, opts: DeploymentExportOptions, *, encrypted: bool = False
) -> Iterator[bytes]:
    """The `.kpdep` NDJSON lines (manifest, sections, trailer) of the whole deployment,
    uncompressed. Every family must be frozen and the file must be encrypted (`pack`): it holds the
    derived device credentials and every family's federation private key. `NotFrozen` and
    `PassphraseRequired` are raised when called, before the first line is produced."""
    if not encrypted:
        raise PassphraseRequired(
            "a deployment bundle holds device credentials and federation private keys: "
            "it must be encrypted with a passphrase"
        )
    families = sorted(
        ((s.id, s.to_dict() or {}) for s in db.collection("families").stream()),
        key=lambda kv: kv[0],
    )
    for fid, fdata in families:
        if not fdata.get("frozen"):
            raise NotFrozen(fid)
    return _deployment_lines(db, opts, families)


def _deployment_lines(
    db: Any, opts: DeploymentExportOptions, families: list[tuple[str, dict[str, Any]]]
) -> Iterator[bytes]:
    cutoff = datetime.now(UTC) - FLOOR
    recent = [
        (s.id, s.to_dict() or {})
        for s in db.collection("wireIds")
        .where(filter=FieldFilter("createdAt", ">=", cutoff))
        .stream()
    ]
    with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as spool:
        bundle: list[BundleFamily] = []
        persons: set[str] = set()
        carried: dict[str, vapid_store.VapidKey] = {}
        scope = _deployment_scope(db, [fid for fid, _ in families])
        for fid, fdata in families:
            spool.write(_marker_line({"section": "family", "id": fid}))
            col = _DeploymentCollector(db, fid, opts, recent, scope)
            for row in col.run():
                spool.write(familyexport._row_line(row))
            persons |= col.persons
            carried.update(col.carried_vapid)
            bundle.append(
                BundleFamily(
                    id=fid,
                    name=str(fdata.get("name") or fid),
                    idSeed=str(fdata.get("idSeed") or fid),
                    counts=dict(sorted(col.counts.items())),
                    dropped=[
                        Dropped(collection=c, reason=r, count=n)
                        for (c, r), n in sorted(col.dropped.items())
                    ],
                )
            )
        for what, uids in (
            ("no_sign_in", opts.no_sign_in),
            ("password_hashes", opts.password_hashes),
            ("invites", opts.invites),
        ):
            stray = sorted(set(uids) - persons)
            if stray:
                raise ValueError(f"{what} names someone who is not a person here: {stray}")
        spool.write(_marker_line({"section": "deployment"}))
        dep = _deployment_rows(db, opts, carried)
        for row in dep.rows:
            spool.write(familyexport._row_line(row))
        manifest = DeploymentManifest(
            source=Source(kind=opts.source_kind, baseUrl=opts.base_url),
            move=MoveHeader(
                id=opts.move_id,
                frozenAt=opts.frozen_at.astimezone(UTC).isoformat(timespec="seconds"),
                target=MoveTarget(
                    baseUrl=opts.target_base_url, authBackend=opts.target_auth_backend
                ),
            ),
            families=bundle,
            noSignIn=sorted(opts.no_sign_in),
            exportedAt=datetime.now(UTC).isoformat(timespec="seconds"),
            history=opts.history,
            envSecrets=sorted(opts.env_secrets),
            deployment=dict(sorted(dep.counts.items())),
        )
        digest = hashlib.sha256()
        line = (
            json.dumps(manifest.model_dump(), separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode()
        digest.update(line)
        yield line
        spool.seek(0)
        for line in spool:
            digest.update(line)
            yield line
    yield (json.dumps({"sha256": digest.hexdigest()}) + "\n").encode()


class _DeploymentRows:
    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.counts: dict[str, int] = {}

    def add(self, path: str, data: dict[str, Any]) -> None:
        coll = path.split("/")[-2]
        self.counts[coll] = self.counts.get(coll, 0) + 1
        self.rows.append({"path": path, "data": sqlitestore.to_wire(data)})


def _deployment_rows(
    db: Any, opts: DeploymentExportOptions, carried: dict[str, vapid_store.VapidKey]
) -> _DeploymentRows:
    """Settings (not the counters and the move record), hooks, the source's VAPID key and the
    keys of earlier moves the subscriptions still rely on, and the claim codes' digests."""
    out = _DeploymentRows()
    for snap in db.collection("settings").stream():
        # `meta` holds only counters and sweep markers; the target keeps its own
        if snap.id not in ("meta", "move"):
            out.add(f"settings/{snap.id}", snap.to_dict() or {})
    for snap in db.collection("hooks").stream():
        out.add(f"hooks/{snap.id}", snap.to_dict() or {})
    if opts.vapid is not None:
        out.add(f"vapid/{vapid_store.SRC_KID}", opts.vapid.model_dump())
    for kid, key in sorted(carried.items()):
        out.add(f"vapid/{kid}", key.model_dump())
    if opts.target_auth_backend == "home":
        expires = opts.frozen_at + CLAIM_TTL
        for uid, digest in sorted(opts.invites.items()):
            out.add(
                f"inviteCodes/{digest}",
                {
                    "uid": uid,
                    "createdBy": "move",
                    "createdAt": opts.frozen_at,
                    "expiresAt": expires,
                },
            )
    return out


class KpdepFile(KpfamFile):
    """A readable `.kpdep`: `verify()` checks the trailer, `manifest()` is line 1. Only the
    encrypted container is accepted: a plain gzip file carries no proof of who wrote it."""

    def _plain(self) -> Iterator[bytes]:
        self._src.seek(0)
        magic = self._src.read(len(familyexport.MAGIC))
        self._src.seek(0)
        if magic != familyexport.MAGIC:
            raise CorruptFile("a deployment bundle must be encrypted")
        yield from super()._plain()

    def manifest(self) -> DeploymentManifest:  # type: ignore[override]
        for line in self.lines():
            try:
                m = json.loads(line)
                if m.get("kpdep") != KPDEP_VERSION:
                    raise CorruptFile(f"unsupported kpdep version {m.get('kpdep')!r}")
                return DeploymentManifest.model_validate(m)
            except (ValueError, ValidationError) as exc:
                if isinstance(exc, CorruptFile):
                    raise
                raise CorruptFile(f"bad manifest: {exc}") from exc
        raise CorruptFile("empty file")

    def body(self) -> Iterator[bytes]:
        """The lines between the manifest and the trailer. Call `verify()` first."""
        it = self.lines()
        next(it, None)
        prev = next(it, None)
        for line in it:
            assert prev is not None
            yield prev
            prev = line


def write_kpdep(
    db: Any, dest: BinaryIO, *, passphrase: str, opts: DeploymentExportOptions
) -> None:
    """The encrypted bundle file."""
    if not passphrase:
        raise PassphraseRequired("a deployment bundle needs a passphrase")
    dest.writelines(
        familyexport.pack(export_deployment(db, opts, encrypted=True), passphrase)
    )


class _Sections:
    """A verified bundle split into its sections, each held in a temporary file."""

    DEPLOYMENT = ""

    def __init__(self, src: KpdepFile) -> None:
        src.verify()
        self.manifest = src.manifest()
        self._files: dict[str, Any] = {}
        current: Any = None
        for line in src.body():
            try:
                rec = json.loads(line)
            except ValueError as exc:
                raise CorruptFile("a line is not JSON") from exc
            if isinstance(rec, dict) and "section" in rec:
                key = self._key(rec)
                if key in self._files:
                    raise CorruptFile(f"section {key or 'deployment'!r} appears twice")
                current = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)  # noqa: SIM115
                self._files[key] = current
                continue
            if current is None:
                raise CorruptFile("a row before the first section")
            current.write(line)
        want = [f.id for f in self.manifest.families]
        if list(self._files) != [*want, self.DEPLOYMENT]:
            raise CorruptFile("the sections do not match the manifest")

    @staticmethod
    def _key(rec: dict[str, Any]) -> str:
        if rec["section"] == "deployment":
            return _Sections.DEPLOYMENT
        if rec["section"] == "family" and isinstance(rec.get("id"), str) and rec["id"]:
            return str(rec["id"])
        raise CorruptFile("unknown section")

    def rows(self, key: str) -> Iterator[Row]:
        f = self._files[key]
        f.seek(0)
        for line in f:
            yield familyexport._parse_row(line)

    def close(self) -> None:
        for f in self._files.values():
            f.close()


class DeviceCounts(BaseModel):
    total: int = 0
    # arrive moving, with a login: the pager can switch by itself
    moving: int = 0
    # arrive without credentials: a setup code from this server
    setupCode: int = 0


class DeploymentReport(BaseModel):
    """What a deployment import would do (and, after `apply_deployment_import`, did)."""

    ok: bool
    moveId: str
    families: list[str]
    history: bool
    devices: DeviceCounts = DeviceCounts()
    federation: FedReport = FedReport()
    counts: dict[str, int] = {}
    dropped: list[Dropped] = []
    collisions: list[Collision] = []
    errors: list[str] = []
    notes: list[str] = []
    people: list[Person] = []
    emailsMissing: list[str] = []
    emailsTaken: list[str] = []
    emailsInvalid: list[str] = []
    envSecrets: list[str] = []


class DeploymentImportRefused(RuntimeError):
    """`plan_deployment_import` found collisions or errors; `.report` says which."""

    def __init__(self, report: DeploymentReport) -> None:
        super().__init__(
            "import refused: " + "; ".join(report.errors or ["collisions"])
        )
        self.report = report


class DeploymentImportOptions(BaseModel):
    # Firebase target: SOURCE alias -> email, or "-" for no sign-in; overrides the file's emails
    emails: dict[str, str] = {}
    # default: "home" under AUTH_BACKEND=home, else "cloud"
    target_kind: Literal["cloud", "home"] | None = None
    # default: `PUBLIC_BASE_URL`
    public_base_url: str | None = None
    # A first-run placeholder (the default family and the one admin the first-run page made) the
    # import replaces; see `move_target.find_placeholder`
    placeholder_family_ids: list[str] = []
    placeholder_uids: list[str] = []
    placeholder_aliases: list[str] = []


class DeploymentResult(BaseModel):
    report: DeploymentReport
    # devices whose broker login the import created, and those the broker admin left to hand
    loginsCreated: list[str] = []
    loginsManual: list[str] = []
    devicesNeedingSetup: list[str] = []
    accountsCreated: list[str] = []  # aliases
    accountsPending: list[str] = []  # no-sign-in persons
    devices: list[Imported] = []
    bridges: list[Imported] = []


def _target_kind(opts: DeploymentImportOptions) -> Literal["cloud", "home"]:
    return opts.target_kind or ("home" if auth_backend() == "home" else "cloud")


def _origin(url: str) -> tuple[str, str, str]:
    p = urlsplit(url)
    return (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"))


def _same_base_url(a: str, b: str) -> bool:
    return _origin(a) == _origin(b)


class _Plan:
    """What the planner learned beyond the report."""

    def __init__(self) -> None:
        self.scans: dict[str, familyexport._Scan] = {}
        self.deployment = familyexport._Scan()
        self.answers = familyexport._Answers()
        self.logins: dict[str, str] = {}  # device id -> broker password
        self.users: dict[str, dict[str, Any]] = {}  # uid -> row data, every family
        self.user_family: dict[str, str] = {}
        # uids the sign-in answers could not settle (Firebase target)
        self.missing: list[str] = []
        self.taken: list[str] = []
        self.invalid: list[str] = []
        # what an interrupted run of this import recorded as the first-run rows it replaces
        self.placeholder = _Placeholder()
        self.resume = False


class _Placeholder(BaseModel):
    """The first-run rows an import replaces, as the `importing` record keeps them."""

    family_ids: list[str] = []
    uids: list[str] = []
    aliases: list[str] = []


def _move_doc(db: Any) -> dict[str, Any] | None:
    snap = db.collection("settings").document("move").get()
    return (snap.to_dict() or {}) if snap.exists else None


def _unfinished(db: Any) -> dict[str, Any] | None:
    """`settings/move` while it says an import is under way, else `None`."""
    data = _move_doc(db)
    return data if data is not None and data.get("state") == "importing" else None


def _plan(
    db: Any,
    sections: _Sections,
    opts: DeploymentImportOptions,
    auth: ImportAuth | None,
) -> tuple[DeploymentReport, _Plan]:
    m = sections.manifest
    kind = _target_kind(opts)
    plan = _Plan()
    errors: list[str] = []
    notes: list[str] = []
    coll: list[Collision] = []

    if m.schemaVersion != SCHEMA_VERSION:
        errors.append(
            f"schema version mismatch: file {m.schemaVersion}, this server {SCHEMA_VERSION}; "
            "upgrade both servers to the same release first"
        )
    base = opts.public_base_url if opts.public_base_url is not None else Settings.from_env().public_base_url
    if not base:
        errors.append("PUBLIC_BASE_URL is not set on this server")
    elif not _same_base_url(m.move.target.baseUrl, base):
        errors.append(
            f"this bundle is addressed to {m.move.target.baseUrl}, not to this server ({base})"
        )
    if (m.move.target.authBackend == "home") != (kind == "home"):
        errors.append(
            f"this bundle was made for a {m.move.target.authBackend} sign-in; "
            f"this server uses {'home' if kind == 'home' else 'firebase'}"
        )
    pending = _unfinished(db)
    resume = pending is not None and pending.get("id") == m.move.id
    plan.resume = resume
    if pending is None and _move_doc(db) is not None:
        errors.append("this server already has a move on record")
    if pending is not None and not resume:
        errors.append(
            f"this server holds an unfinished import of another move ({pending.get('id')}); "
            "a different bundle cannot be imported into it"
        )
    if resume:
        assert pending is not None
        plan.placeholder = _Placeholder.model_validate(pending.get("placeholder") or {})
    bundle_families = {f.id for f in m.families}
    # a family of this same bundle is what an interrupted run of it left
    held = [
        s.id
        for s in db.collection("families").stream()
        if s.id not in opts.placeholder_family_ids
        and s.id not in plan.placeholder.family_ids
        and not (resume and s.id in bundle_families)
    ]
    if held:
        errors.append(
            f"this server already holds {len(held)} family(ies): a deployment imports into an "
            "empty server"
        )

    counts: dict[str, int] = {}
    dropped: dict[tuple[str, str], int] = {}
    devices = DeviceCounts()
    fed = FedReport()
    for fam in m.families:
        for d in fam.dropped:
            dropped[(d.collection, d.reason)] = dropped.get((d.collection, d.reason), 0) + d.count

    for fam in m.families:
        fid = fam.id
        rows = list(sections.rows(fid))
        if not rows or rows[0]["path"] != f"families/{fid}":
            raise CorruptFile(f"section {fid!r} does not start with its family document")
        if not familyexport._FAMILY_ID_RE.match(fid):
            errors.append(f"{fid!r} is not a usable family id")
        sc = familyexport._scan(rows)
        plan.scans[fid] = sc
        for k, n in sc.counts.items():
            counts[k] = counts.get(k, 0) + n
        for uid, d in sc.users.items():
            plan.users[uid] = d
            plan.user_family[uid] = fid
        fed.links += sc.fed_links
        fed.announcing += sc.fed_links
        notes.extend(familyexport._federation_notes(db, sc, "move"))
        secrets = {
            r["path"].split("/")[1]: r["data"] for r in rows if r["path"].startswith("deviceSecrets/")
        }
        for dev, drow in sorted(sc.device_rows.items()):
            devices.total += 1
            state = drow.get("provisionState")
            if state == "moving":
                sec = secrets.get(dev)
                pw = (sec or {}).get("mqttPassword")
                if not sec or not isinstance(pw, str) or len(pw) != movekeys.PW_LEN:
                    errors.append(f"device {dev}: no derived credentials in the bundle")
                    continue
                if (drow.get("move") or {}).get("id") != m.move.id:
                    errors.append(f"device {dev}: belongs to another move")
                    continue
                plan.logins[dev] = pw
                devices.moving += 1
            else:
                devices.setupCode += 1
        for uid in sorted(sc.users):
            if uid not in opts.placeholder_uids and familyexport._exists(db, f"users/{uid}"):
                coll.append(Collision(kind="uid", id=uid))
        for a in sorted(sc.aliases):
            if a not in opts.placeholder_aliases and familyexport._exists(db, f"aliases/{a}"):
                coll.append(Collision(kind="alias", id=a))
        for dev in sorted(sc.devices):
            if familyexport._exists(db, f"devices/{dev}") or familyexport._exists(
                db, f"deviceSecrets/{dev}"
            ):
                coll.append(Collision(kind="device", id=dev))
        for br in sorted(sc.bridges):
            if familyexport._exists(db, f"bridges/{br}"):
                coll.append(Collision(kind="bridge", id=br))
        for num in sorted(sc.numbers):
            if familyexport._exists(db, f"smsNumbers/{num}"):
                coll.append(Collision(kind="number", id=num))
        for path in sc.paths:
            if familyexport._exists(db, path):
                coll.append(Collision(kind="path", id=path))
        if sc.fed_family is not None and sc.fed_family.get("fedId"):
            for _ in familyexport._where(db, "fedFamilies", "fedId", "==", sc.fed_family["fedId"]):
                coll.append(Collision(kind="fed", id=str(sc.fed_family["fedId"])))
                break

    dep_rows = list(sections.rows(_Sections.DEPLOYMENT))
    for r in dep_rows:
        if not _deployment_path_ok(r["path"], kind):
            errors.append(f"the deployment section holds a row it may not: {r['path']}")
            continue
        c = r["path"].split("/")[0]
        counts[c] = counts.get(c, 0) + 1
        if c in ("hooks", "vapid", "inviteCodes") and familyexport._exists(db, r["path"]):
            coll.append(Collision(kind="path", id=r["path"]))

    if resume:
        # every row of the bundle is written by a deterministic `set`: what is there is its own
        coll.clear()

    persons = sorted(
        (u for u, d in plan.users.items() if d.get("kind", "person") == "person"),
        key=lambda u: str(plan.users[u].get("alias")),
    )
    people = _answers(db, m, opts, kind, plan, persons, auth, errors)
    if m.envSecrets:
        notes.append(
            "copy these settings from the source's environment by hand: "
            + ", ".join(m.envSecrets)
        )
    if devices.setupCode:
        notes.append(
            f"{devices.setupCode} pager(s) arrive without credentials and need a setup code"
        )
    if not m.history:
        notes.append("the bundle carries only the last 24 hours of messages and none of the other history")

    report = DeploymentReport(
        ok=not errors and not coll,
        moveId=m.move.id,
        families=[f.id for f in m.families],
        history=m.history,
        devices=devices,
        federation=fed,
        counts=dict(sorted(counts.items())),
        dropped=[Dropped(collection=c, reason=r, count=n) for (c, r), n in sorted(dropped.items())],
        collisions=coll,
        errors=errors,
        notes=notes,
        people=people,
        emailsMissing=sorted(str(plan.users[u].get("alias")) for u in plan.missing),
        emailsTaken=sorted(str(plan.users[u].get("alias")) for u in plan.taken),
        emailsInvalid=sorted(str(plan.users[u].get("alias")) for u in plan.invalid),
        envSecrets=m.envSecrets,
    )
    return report, plan


def _deployment_path_ok(path: str, kind: str) -> bool:
    """The rows `_deployment_rows` writes, and nothing else: settings other than the counters and
    the move record, hooks, the VAPID keys, and claim-code digests for a home target."""
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return False
    coll, doc = parts
    if coll == "settings":
        return doc not in ("meta", "move")
    if coll == "hooks":
        return True
    if coll == "vapid":
        return vapid_store.valid_kid(doc)
    return coll == "inviteCodes" and kind == "home"


def _answers(
    db: Any,
    m: DeploymentManifest,
    opts: DeploymentImportOptions,
    kind: str,
    plan: _Plan,
    persons: list[str],
    auth: ImportAuth | None,
    errors: list[str],
) -> list[Person]:
    """The sign-in answer for every person: Firebase needs an email or "no sign-in" for each;
    a home server needs nothing (the claim codes are in the bundle)."""
    ans = plan.answers
    by_alias = {str(plan.users[u].get("alias")): u for u in persons}
    taken, invalid = plan.taken, plan.invalid
    if kind == "cloud":
        auth = auth or FirebaseImportAuth()
        no_sign_in = set(m.noSignIn)
        for a in sorted(opts.emails):
            if a not in by_alias:
                errors.append(f"--email {a}=...: no such person in the bundle")
        owners: dict[str, list[str]] = {}
        for uid in persons:
            a = str(plan.users[uid].get("alias"))
            given = opts.emails.get(a)
            if given is None:
                given = NO_SIGN_IN if uid in no_sign_in else str(plan.users[uid].get("email") or "").strip()
            if not given:
                plan.missing.append(uid)
                continue
            if given == NO_SIGN_IN:
                ans.none.add(uid)
                continue
            email = familyexport.normalize_email(given)
            if email is None:
                invalid.append(uid)
                errors.append(f"--email {a}={given}: not an email address")
                continue
            ans.email[uid] = email
            owners.setdefault(email, []).append(uid)
        for email, group in sorted(owners.items()):
            if len(group) > 1:
                taken.extend(group)
                names = ", ".join(str(plan.users[u].get("alias")) for u in group)
                errors.append(f"{email} is given for {names}: one email per person")
        for uid, email in sorted(ans.email.items()):
            if uid in taken:
                continue
            holder = auth.email_owner(email)
            if holder is not None and holder != uid:
                taken.append(uid)
                errors.append(
                    f"{email} (for {plan.users[uid].get('alias')}) already belongs to another account"
                )
        if plan.missing:
            errors.append(
                f"no email for: {', '.join(sorted(str(plan.users[u].get('alias')) for u in plan.missing))} "
                "(give alias=email, or alias=- for no sign-in)"
            )
        if not any(
            str(plan.users[u].get("role")) in ("admin", "super") and u in ans.email for u in persons
        ):
            errors.append("at least one admin needs a real email: nobody could sign in to run the family")
    elif opts.emails:
        errors.append("--email / --emails-file apply to a Firebase target only")
    out: list[Person] = []
    for uid in persons:
        d = plan.users[uid]
        a = str(d.get("alias"))
        sc = plan.scans[plan.user_family[uid]]
        src_email = str(d.get("email") or "").strip() or None
        answered = uid in ans.email or uid in ans.none
        out.append(
            Person(
                alias=a,
                targetAlias=a,
                name=str(d.get("displayName") or ""),
                role=str(d.get("role") or "member"),
                sourceEmail=src_email,
                hadSignIn=bool(src_email) or uid in sc.with_credentials,
                email=ans.email.get(uid),
                signIn=(uid in ans.email) if answered else None,
            )
        )
    return out


def plan_deployment_import(
    db: Any,
    src: KpdepFile,
    opts: DeploymentImportOptions | None = None,
    auth: ImportAuth | None = None,
) -> DeploymentReport:
    """Dry run: verifies the bundle and reports collisions, errors, counts and every person's
    sign-in answer. Writes nothing and calls no broker."""
    sections = _Sections(src)
    try:
        return _plan(db, sections, opts or DeploymentImportOptions(), auth)[0]
    finally:
        sections.close()


def apply_deployment_import(
    db: Any,
    src: KpdepFile,
    opts: DeploymentImportOptions | None = None,
    auth: ImportAuth | None = None,
    *,
    broker_admin: BrokerAdmin,
) -> DeploymentResult:
    """Plans, refuses (`DeploymentImportRefused`) on any collision or error, creates every device's
    broker login (`BrokerLoginFailed` before any write), then writes each family, the deployment
    section, the Firebase accounts, then removes the first-run rows, clears the families' `importing`
    markers and last replaces the `importing` move record by the final one. An interrupted run is
    repeated with the same bundle."""
    opts = opts or DeploymentImportOptions()
    kind = _target_kind(opts)
    auth = auth or FirebaseImportAuth()
    sections = _Sections(src)
    try:
        report, plan = _plan(db, sections, opts, auth)
        if not report.ok:
            raise DeploymentImportRefused(report)
        return _apply(db, sections, opts, kind, auth, broker_admin, report, plan)
    finally:
        sections.close()


class _MoveRewriter(familyexport._Rewriter):
    """The family rewriter for rows the export already finished: devices and bridges as they are,
    a device's broker password reduced to its hash. When an interrupted run is repeated, the
    federation key it already minted is kept: a peer may have been told of it."""

    def __init__(self, *args: Any, db: Any = None, resume: bool = False, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._db, self._resume = db, resume

    def __call__(self, row: Row) -> Row | None:
        out = self._row(row)
        parts = row["path"].split("/")
        if self._resume and out is not None and parts[0] == "fedFamilies" and len(parts) == 2:
            snap = self._db.collection("fedFamilies").document(parts[1]).get()
            have = (snap.to_dict() or {}) if snap.exists else {}
            if (
                have.get("epoch") == out["data"].get("epoch")
                and have.get("prevPrivateKey") == row["data"].get("privateKey")
                and have.get("privateKey")
            ):
                data = {**out["data"], "privateKey": have["privateKey"]}
                if have.get("publicKey"):
                    data["publicKey"] = have["publicKey"]
                return {"path": out["path"], "data": data}
        return out

    def _row(self, row: Row) -> Row | None:
        parts = row["path"].split("/")
        if len(parts) == 2 and parts[0] == "devices":
            data = dict(row["data"])
            data.pop("pendingCfgMv", None)
            data.pop("pendingCfgBr", None)
            data.pop("bearerTry", None)
            return {"path": row["path"], "data": data}
        if len(parts) == 2 and parts[0] == "bridges":
            return row
        if len(parts) == 2 and parts[0] == "deviceSecrets":
            data = dict(row["data"])
            pw = data.pop("mqttPassword", None)
            if isinstance(pw, str):
                data["mqttPasswordHash"] = hashlib.sha256(pw.encode("utf-8")).hexdigest()
            return {"path": row["path"], "data": data}
        return super().__call__(row)


def _clear_placeholder(db: Any, gone: _Placeholder, keep: _Placeholder) -> None:
    """Removes the first-run admin and family the import replaces: their credentials, sessions,
    backends, alias, user and family documents. A row the bundle writes under the same id is the
    bundle's and stays."""
    for uid in gone.uids:
        if uid in keep.uids:
            continue
        user = db.collection("users").document(uid)
        for sub in ("credentials", "backends", "pushSubscriptions"):
            for snap in list(user.collection(sub).stream()):
                snap.reference.delete()
        for snap in list(familyexport._where(db, "sessions", "uid", "==", uid)):
            snap.reference.delete()
        user.delete()
    for alias in gone.aliases:
        if alias not in keep.aliases:
            db.collection("aliases").document(alias).delete()
    for fid in gone.family_ids:
        if fid not in keep.family_ids:
            db.collection("families").document(fid).delete()


def _no_sign_in(sections: _Sections, plan: _Plan, kind: str) -> set[str]:
    """The people the import gave no way to sign in: no account (Firebase), or neither a claim code
    nor a password (home)."""
    persons = {u for u, d in plan.users.items() if d.get("kind", "person") == "person"}
    if kind == "cloud":
        return set(plan.answers.none)
    claimed = {
        str(r["data"].get("uid"))
        for r in sections.rows(_Sections.DEPLOYMENT)
        if r["path"].startswith("inviteCodes/")
    }
    with_password = {u for sc in plan.scans.values() for u in sc.with_credentials}
    return persons - claimed - with_password


def _apply(
    db: Any,
    sections: _Sections,
    opts: DeploymentImportOptions,
    kind: str,
    auth: ImportAuth,
    broker_admin: BrokerAdmin,
    report: DeploymentReport,
    plan: _Plan,
) -> DeploymentResult:
    m = sections.manifest
    result = DeploymentResult(report=report)

    for dev, pw in sorted(plan.logins.items()):
        outcome = broker_admin.ensure_device(dev, pw, dev)
        if outcome == "error":
            raise BrokerLoginFailed(dev)
        (result.loginsCreated if outcome == "ok" else result.loginsManual).append(dev)

    # The first write: an import is under way until the final record replaces this one; the same
    # bundle may resume it (every write below is a deterministic `set`).
    replaced = _Placeholder(
        family_ids=sorted({*opts.placeholder_family_ids, *plan.placeholder.family_ids}),
        uids=sorted({*opts.placeholder_uids, *plan.placeholder.uids}),
        aliases=sorted({*opts.placeholder_aliases, *plan.placeholder.aliases}),
    )
    db.collection("settings").document("move").set(
        {
            "id": m.move.id,
            "role": "target",
            "state": "importing",
            "placeholder": replaced.model_dump(),
        }
    )
    invalidate_cache()
    batch, pending = db.batch(), 0

    def put(path: str, data: dict[str, Any]) -> None:
        nonlocal batch, pending
        parent, _, doc_id = path.rpartition("/")
        batch.set(db.collection(parent).document(doc_id), sqlitestore.from_wire(data))
        pending += 1
        if pending == familyexport.BATCH:
            batch.commit()
            batch, pending = db.batch(), 0

    roles = {
        u: str(d.get("role") or "member")
        for u, d in plan.users.items()
        if d.get("kind", "person") == "person"
    }
    for fam in m.families:
        fid = fam.id
        sc = plan.scans[fid]
        rows = sections.rows(fid)
        fam_row = next(rows)
        fdata = dict(fam_row["data"])
        fdata.pop("frozen", None)
        fdata["importing"] = True
        db.collection("families").document(fid).set(sqlitestore.from_wire(fdata))
        seqmap: dict[int, int] = {}
        if sc.seqs:
            base = familyexport._reserve_seqs(db, len(sc.seqs))
            seqmap = {s: base + 1 + i for i, s in enumerate(sorted(sc.seqs))}
        rewrite = _MoveRewriter(
            old=fid,
            new=fid,
            amap={},
            seqmap=seqmap,
            roles=roles,
            emails=plan.answers.email,
            no_email=plan.answers.none,
            keep_credentials=(kind == "home"),
            fed_links=sc.fed_links,
            db=db,
            resume=plan.resume,
        )
        for row in rows:
            out = rewrite(row)
            if out is not None:
                put(out["path"], out["data"])
        result.devices += familyexport._imported(sc, sc.device_rows, familyexport.ImportOptions())
        result.bridges += familyexport._imported(sc, sc.bridge_rows, familyexport.ImportOptions())
        result.devicesNeedingSetup += sorted(
            d for d, r in sc.device_rows.items() if r.get("provisionState") != "moving"
        )

    for row in sections.rows(_Sections.DEPLOYMENT):
        data = dict(row["data"])
        if row["path"].startswith("hooks/"):
            data["enabled"] = False
        put(row["path"], data)
    if pending:
        batch.commit()

    if kind == "cloud":
        for uid in sorted(plan.answers.email, key=lambda u: str(plan.users[u].get("alias"))):
            auth.create_account(uid, plan.answers.email[uid])
            auth.set_claims(uid, roles[uid], plan.user_family[uid])
            result.accountsCreated.append(str(plan.users[uid].get("alias")))
        result.accountsPending = sorted(str(plan.users[u].get("alias")) for u in plan.answers.none)

    _clear_placeholder(
        db,
        replaced,
        keep=_Placeholder(
            family_ids=[f.id for f in m.families],
            uids=sorted(plan.users),
            aliases=sorted(a for sc in plan.scans.values() for a in sc.aliases),
        ),
    )
    for fam in m.families:
        db.collection("families").document(fam.id).update({"importing": False})
    record = TargetMoveRecord(
        id=m.move.id,
        sourceBaseUrl=m.source.baseUrl,
        frozenAt=m.move.frozenAt,
        importedAt=datetime.now(UTC),
        noSignIn=sorted(_no_sign_in(sections, plan, kind)),
    )
    db.collection("settings").document("move").set(record.model_dump())
    invalidate_cache()
    return result


def _db() -> Any:
    from app.dbexport import _db as open_db

    return open_db()


def _cli_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="deployexport",
        description="Write or read the whole-deployment move bundle. With SQLite the relay must "
        "be stopped, as for `python -m app.dbexport`. Every family must be frozen first.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_pass(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--passphrase-file",
            help="read the passphrase from this file (else $KPFAM_PASSPHRASE, else prompt)",
        )

    p_exp = sub.add_parser("export", help="write <host>-<date>.kpdep")
    p_exp.add_argument("--target-url", required=True, help="the target's PUBLIC_BASE_URL")
    p_exp.add_argument("--target-auth", choices=["home", "firebase"], required=True)
    p_exp.add_argument("--move-id", help="default: a new one")
    p_exp.add_argument("-o", "--output")
    p_exp.add_argument("--no-history", action="store_true")
    p_exp.add_argument(
        "--no-sign-in", action="append", metavar="ALIAS", help="firebase target: no sign-in"
    )
    add_pass(p_exp)
    p_imp = sub.add_parser("import", help="read a .kpdep into the configured database")
    p_imp.add_argument("file")
    p_imp.add_argument("--dry-run", action="store_true")
    p_imp.add_argument("--email", action="append", metavar="ALIAS=EMAIL")
    p_imp.add_argument("--emails-file", metavar="PATH")
    add_pass(p_imp)
    p_ins = sub.add_parser("inspect", help="print the manifest")
    p_ins.add_argument("file")
    add_pass(p_ins)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _cli_args(argv)
    try:
        if args.command == "export":
            return _cli_export(args)
        if args.command == "import":
            return _cli_import(args)
        return _cli_inspect(args)
    except sqlitestore.DatabaseInUse:
        print(
            "the relay holds the database: stop it first (docker compose stop relay)",
            file=sys.stderr,
        )
        return 2
    except (
        NotFrozen,
        BadPassphrase,
        PassphraseRequired,
        CorruptFile,
        DeploymentImportRefused,
        BrokerLoginFailed,
        ValueError,
        OSError,
    ) as exc:
        if isinstance(exc, DeploymentImportRefused):
            print(exc.report.model_dump_json(indent=2))
        if isinstance(exc, NotFrozen):
            print(f"deployexport: family {exc} is not frozen; freeze every family first", file=sys.stderr)
        else:
            print(f"deployexport: {exc}", file=sys.stderr)
        return 1


def _people(db: Any) -> dict[str, str]:
    """alias -> uid of every person."""
    out: dict[str, str] = {}
    for snap in db.collection("users").stream():
        d = snap.to_dict() or {}
        if d.get("kind", "person") == "person":
            out[str(d.get("alias"))] = snap.id
    return out


def _cli_export(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    passphrase = familyexport._cli_passphrase(args, confirm=True)
    if not passphrase:
        raise ValueError(
            "give a passphrase (--passphrase-file, $KPFAM_PASSPHRASE or the prompt)"
        )
    db = _db()
    people = _people(db)
    unknown = [a for a in args.no_sign_in or [] if a not in people]
    if unknown:
        raise ValueError(f"--no-sign-in: no such person: {', '.join(unknown)}")
    invites: dict[str, str] = {}
    codes: dict[str, str] = {}
    if args.target_auth == "home":
        for alias, uid in sorted(people.items()):
            code, digest = authn_home.new_invite_code()
            invites[uid] = digest
            codes[alias] = code
    opts = DeploymentExportOptions(
        move_id=args.move_id or movekeys.new_move_id(),
        frozen_at=datetime.now(UTC),
        target_base_url=args.target_url.rstrip("/"),
        target_auth_backend=args.target_auth,
        history=not args.no_history,
        source_kind=familyexport.source_kind(),
        base_url=settings.public_base_url,
        no_sign_in=sorted(people[a] for a in args.no_sign_in or []),
        invites=invites,
        vapid=(
            vapid_store.VapidKey(
                privateKey=settings.webpush_vapid_private_key,
                publicKey=settings.webpush_vapid_public_key,
            )
            if settings.webpush_vapid_private_key and settings.webpush_vapid_public_key
            else None
        ),
        env_secrets=env_secrets_set(settings),
    )
    host = urlsplit(opts.target_base_url).hostname or "bundle"
    out = Path(args.output or f"{host}-{datetime.now(UTC).date().isoformat()}.kpdep")
    try:
        with out.open("wb") as fh:
            write_kpdep(db, fh, passphrase=passphrase, opts=opts)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    print(f"deployexport: wrote {out} (move {opts.move_id})", file=sys.stderr)
    for alias, code in codes.items():
        print(f"claim for {alias}: {opts.target_base_url}/login#invite={code}")
    return 0


def _open(args: argparse.Namespace) -> KpdepFile:
    return KpdepFile(open(args.file, "rb"), familyexport._cli_passphrase(args, confirm=False))


def _cli_import(args: argparse.Namespace) -> int:
    emails: dict[str, str] = {}
    if args.emails_file:
        emails.update(familyexport.parse_email_list(Path(args.emails_file).read_text()))
    emails.update(familyexport._pair_args(args.email, "--email"))
    opts = DeploymentImportOptions(emails=emails)
    src = _open(args)
    if args.dry_run:
        report = plan_deployment_import(_db(), src, opts)
        print(report.model_dump_json(indent=2))
        return 0 if report.ok else 1
    from app.broker import make_broker_client

    settings = Settings.from_env()
    admin = make_broker_admin(settings, make_broker_client(settings))
    result = apply_deployment_import(_db(), src, opts, broker_admin=admin)
    print(result.report.model_dump_json(indent=2))
    for dev in result.loginsManual:
        print(f"broker login for {dev} is not managed here: create it by hand", file=sys.stderr)
    for alias in result.accountsPending:
        print(f"no sign-in for {alias}: marked '-'")
    return 0


def _cli_inspect(args: argparse.Namespace) -> int:
    f = _open(args)
    f.verify()
    print(f.manifest().model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
