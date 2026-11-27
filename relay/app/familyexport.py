"""Family export and import: one family as a `.kpfam` file (gzip NDJSON, optionally AES-256-GCM
under a passphrase) of manifest, `{"path","data"}` rows and a sha256 trailer."""

from __future__ import annotations

import argparse
import getpass
import gzip
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import zlib
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from firebase_admin import auth as fb_auth
from google.cloud.firestore import FieldFilter, transactional
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app import authn_home, datamap
from app.config import Settings, auth_backend
from app.db import sqlitestore
from app.store import fed_families as fed_families_store
from app.store import fed_links as fed_links_store
from app.store import users as users_store

KPFAM_VERSION = 1
# `settings/meta.schemaVersion` as written by `app/bootstrap.py` and `app/location.py`.
SCHEMA_VERSION = 2

Row = dict[str, Any]


class FamilyNotFound(LookupError):
    pass


class NotFrozen(RuntimeError):
    """A move export of an unfrozen family; a backup takes a live one."""


class BadPassphrase(ValueError):
    pass


class PassphraseRequired(ValueError):
    """The file would hold the family's federation private key: it must be encrypted."""


class CorruptFile(ValueError):
    pass


def relay_version() -> str:
    try:
        return metadata.version("relay")
    except metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


class Source(BaseModel):
    kind: Literal["cloud", "home"]
    baseUrl: str | None = None


class ManifestFamily(BaseModel):
    id: str
    name: str
    idSeed: str


class Dropped(BaseModel):
    collection: str
    reason: str
    count: int


class Manifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kpfam: int = KPFAM_VERSION
    # a `move` (frozen family, carries the federation private key) or a `backup` (live family,
    # never carries a private key)
    kind: Literal["move", "backup"] = "move"
    schemaVersion: int = Field(default_factory=lambda: SCHEMA_VERSION)
    relayVersion: str = Field(default_factory=relay_version)
    source: Source
    family: ManifestFamily
    exportedAt: str
    history: bool
    counts: dict[str, int] = {}
    dropped: list[Dropped] = []


def source_kind() -> Literal["cloud", "home"]:
    """The manifest's `source.kind`, the one rule for the CLI and the HTTP route: the cloud when
    AUTH_BACKEND is firebase, a home server otherwise."""
    return "cloud" if auth_backend() == "firebase" else "home"


class ExportOptions(BaseModel):
    history: bool = True
    # A backup rather than a move: an unfrozen family, and the file never holds a private key.
    backup: bool = False
    source_kind: Literal["cloud", "home"] = "home"
    base_url: str | None = None

    @property
    def kind(self) -> Literal["move", "backup"]:
        return "backup" if self.backup else "move"


_STRIP_FAMILY_FIELDS = ("frozen", "importing")


def _wire(data: dict[str, Any] | None) -> dict[str, Any]:
    return sqlitestore.to_wire(data or {})


def _where(db: Any, coll: str, field: str, op: str, value: Any) -> Iterator[Any]:
    return db.collection(coll).where(filter=FieldFilter(field, op, value)).stream()


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return [x for x in v if isinstance(x, str)]
    return []


class _Collector:
    def __init__(self, db: Any, fid: str, history: bool, backup: bool) -> None:
        self.db = db
        self.fid = fid
        self.history = history
        self.backup = backup
        self.uids: set[str] = set()
        self.device_ids: set[str] = set()
        self.counts: dict[str, int] = {}
        self.dropped: dict[tuple[str, str], int] = {}


    def row(self, path: str, data: dict[str, Any]) -> Row:
        coll = path.split("/")[-2]
        self.counts[coll] = self.counts.get(coll, 0) + 1
        return {"path": path, "data": _wire(data)}

    def drop(self, coll: str, reason: str) -> None:
        self.dropped[(coll, reason)] = self.dropped.get((coll, reason), 0) + 1

    def foreign_uid(self, coll: str, data: dict[str, Any]) -> bool:
        """True when a `uid_fields` value of the registry row names someone outside the family."""
        for f in datamap.get(coll).uid_fields:
            if any(not self.in_scope(u) for u in _as_list(data.get(f))):
                return True
        return False

    def in_scope(self, uid: str) -> bool:
        """Whether `uid` moves with this export."""
        return uid in self.uids


    def run(self) -> Iterator[Row]:
        db, fid = self.db, self.fid
        fam = db.collection("families").document(fid).get()
        if not fam.exists:
            raise FamilyNotFound(fid)
        fdata = fam.to_dict() or {}
        for k in _STRIP_FAMILY_FIELDS:
            fdata.pop(k, None)
        yield self.row(f"families/{fid}", fdata)
        yield from self._federation()

        alerts = [
            (snap.id, snap.to_dict() or {})
            for snap in db.collection("families")
            .document(fid)
            .collection("alerts")
            .stream()
        ]

        users: dict[str, dict[str, Any]] = {}
        for snap in _where(db, "users", "familyId", "==", fid):
            users[snap.id] = snap.to_dict() or {}
        for snap in _where(db, "users", "ownerFamilyId", "==", fid):
            users[snap.id] = snap.to_dict() or {}
        self.uids = set(users)

        for aid, data in alerts:
            if self.foreign_uid("alerts", data):
                self.drop("alerts", "names a person outside the family")
                continue
            yield self.row(f"families/{fid}/alerts/{aid}", data)

        for uid in sorted(users):
            data = users[uid]
            yield self.row(f"users/{uid}", data)
            for sub in ("backends", "book", "credentials"):
                for snap in (
                    db.collection("users").document(uid).collection(sub).stream()
                ):
                    if sub == "book" and not self.in_scope(snap.id):
                        self.drop("book", "names a person outside the family")
                        continue
                    yield self.row(f"users/{uid}/{sub}/{snap.id}", snap.to_dict() or {})
            yield from self._user_extra(uid)
            alias = data.get("alias")
            if isinstance(alias, str):
                a = db.collection("aliases").document(alias).get()
                if a.exists and (a.to_dict() or {}).get("uid") == uid:
                    yield self.row(f"aliases/{alias}", a.to_dict() or {})
            num = data.get("smsNumber")
            if isinstance(num, str):
                n = db.collection("smsNumbers").document(num).get()
                if n.exists and (n.to_dict() or {}).get("uid") == uid:
                    yield self.row(f"smsNumbers/{num}", n.to_dict() or {})

        yield from self._allow()
        yield from self._devices()
        yield from self._bridges()
        yield from self._simple("contactNames", "familyId")
        for uid in sorted(self.uids):
            for snap in _where(db, "contactRequests", "ownerUid", "==", uid):
                data = snap.to_dict() or {}
                if self.foreign_uid("contactRequests", data):
                    self.drop("contactRequests", "names a person outside the family")
                    continue
                yield self.row(f"contactRequests/{snap.id}", data)
        yield from self._conversations()
        if self.history:
            yield from self._history()

    def _user_extra(self, uid: str) -> Iterator[Row]:
        """Rows a subclass adds after a user's subcollections."""
        return iter(())

    def _federation(self) -> Iterator[Row]:
        """The family's identity and its links. A move carries the private key; a backup never does.
        Invites and outbox rows stay behind."""
        ident = self.db.collection("fedFamilies").document(self.fid).get()
        if ident.exists:
            data = ident.to_dict() or {}
            if self.backup:
                data.pop("privateKey", None)
                data.pop("prevPrivateKey", None)
            yield self.row(f"fedFamilies/{self.fid}", data)
        for snap in _where(self.db, "fedLinks", "familyId", "==", self.fid):
            yield self.row(f"fedLinks/{snap.id}", snap.to_dict() or {})

    def _simple(self, coll: str, field: str) -> Iterator[Row]:
        for snap in _where(self.db, coll, field, "==", self.fid):
            data = snap.to_dict() or {}
            if self.foreign_uid(coll, data):
                self.drop(coll, "names a person outside the family")
                continue
            yield self.row(f"{coll}/{snap.id}", data)

    def _allow(self) -> Iterator[Row]:
        for snap in _where(self.db, "allow", "familyIds", "array_contains", self.fid):
            data = snap.to_dict() or {}
            if self.foreign_uid("allow", data):
                self.drop("allow", "an end is outside the family")
                continue
            yield self.row(f"allow/{snap.id}", data)

    def _devices(self) -> Iterator[Row]:
        db = self.db
        for snap in _where(db, "devices", "familyId", "==", self.fid):
            data = snap.to_dict() or {}
            self.device_ids.add(snap.id)
            # A device's own owner is inside the family by construction; the two other
            # uid-carrying fields may still name someone outside it.
            if data.get("defaultToUid") not in (None, *self.uids):
                data["defaultToUid"] = None
            if "locatableBy" in data:
                data["locatableBy"] = [
                    u for u in _as_list(data["locatableBy"]) if u in self.uids
                ]
            yield self.row(f"devices/{snap.id}", data)
            sec = db.collection("deviceSecrets").document(snap.id).get()
            if sec.exists:
                yield self.row(f"deviceSecrets/{snap.id}", sec.to_dict() or {})

    def _bridges(self) -> Iterator[Row]:
        for snap in _where(self.db, "bridges", "familyId", "==", self.fid):
            data = snap.to_dict() or {}
            data.pop("fcmToken", None)
            yield self.row(f"bridges/{snap.id}", data)
        yield from self._simple("bridgeConversations", "familyId")

    def _conversations(self) -> Iterator[Row]:
        self.conv_keys: set[str] = set()
        for snap in _where(
            self.db, "conversations", "familyIds", "array_contains", self.fid
        ):
            data = snap.to_dict() or {}
            if _as_list(data.get("familyIds")) != [self.fid] or self.foreign_uid(
                "conversations", data
            ):
                self.drop("conversations", "shared with another family")
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
        for snap in _where(db, "messages", "familyIds", "array_contains", self.fid):
            data = snap.to_dict() or {}
            if _as_list(data.get("familyIds")) != [self.fid] or self.foreign_uid(
                "messages", data
            ):
                self.drop("messages", "shared with another family")
                continue
            yield self.row(f"messages/{snap.id}", data)
            wid, rcpt = data.get("wireId"), data.get("recipientUid")
            if isinstance(wid, str) and isinstance(rcpt, str):
                w = db.collection("wireIds").document(f"{wid}_{rcpt}").get()
                if w.exists:
                    yield self.row(f"wireIds/{w.id}", w.to_dict() or {})
        for dev in sorted(self.device_ids):
            for sub in ("locations", "battery", "smsLog"):
                for snap in (
                    db.collection("devices").document(dev).collection(sub).stream()
                ):
                    data = snap.to_dict() or {}
                    if sub == "smsLog" and self.foreign_uid("smsLog", data):
                        self.drop("smsLog", "names a person outside the family")
                        continue
                    yield self.row(f"devices/{dev}/{sub}/{snap.id}", data)
        yield from self._simple("heldSms", "familyId")
        yield from self._simple("heldChat", "familyId")


def _parse_row(line: bytes) -> Row:
    rec = json.loads(line)
    if not (
        isinstance(rec, dict)
        and isinstance(rec.get("path"), str)
        and isinstance(rec.get("data"), dict)
    ):
        raise CorruptFile("a line is not a {path, data} row")
    return rec


def _manifest_line(m: Manifest) -> bytes:
    return (
        json.dumps(m.model_dump(), separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _row_line(row: Row) -> bytes:
    return (
        json.dumps(row, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode()


def export_family(
    db: Any, fid: str, opts: ExportOptions | None = None, *, encrypted: bool = False
) -> Iterator[bytes]:
    """The `.kpfam` NDJSON lines (manifest, rows, trailer) of family `fid`, uncompressed.

    A move (the default) needs the family frozen and, when the family has a federation identity,
    an encrypted file: it holds the private key. A backup (`opts.backup`) takes a live family and
    never holds a private key, so a passphrase is optional.

    Raises `FamilyNotFound` / `NotFrozen` / `PassphraseRequired` when called, before the first line
    is produced. `encrypted` says whether the caller will wrap the lines in a passphrase
    (`pack`). Rows are spooled to disk because the manifest (line 1) carries the counts.
    """
    opts = opts or ExportOptions()
    fam = db.collection("families").document(fid).get()
    if not fam.exists:
        raise FamilyNotFound(fid)
    fdata = fam.to_dict() or {}
    if not opts.backup:
        if not fdata.get("frozen"):
            raise NotFrozen(fid)
        if not encrypted and db.collection("fedFamilies").document(fid).get().exists:
            raise PassphraseRequired(
                "this file would hold the family's federation private key: give a passphrase "
                "(an unencrypted file could be used to impersonate the family to the families "
                "it is linked with)"
            )
    return _export_lines(db, fid, fdata, opts)


def _export_lines(
    db: Any, fid: str, fdata: dict[str, Any], opts: ExportOptions
) -> Iterator[bytes]:
    col = _Collector(db, fid, opts.history, opts.backup)
    with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as spool:
        for row in col.run():
            spool.write(_row_line(row))
        manifest = Manifest(
            kind=opts.kind,
            source=Source(kind=opts.source_kind, baseUrl=opts.base_url),
            family=ManifestFamily(
                id=fid,
                name=str(fdata.get("name") or fid),
                idSeed=str(fdata.get("idSeed") or fid),
            ),
            exportedAt=datetime.now(UTC).isoformat(timespec="seconds"),
            history=opts.history,
            counts=dict(sorted(col.counts.items())),
            dropped=[
                Dropped(collection=c, reason=r, count=n)
                for (c, r), n in sorted(col.dropped.items())
            ],
        )
        digest = hashlib.sha256()
        line = _manifest_line(manifest)
        digest.update(line)
        yield line
        spool.seek(0)
        for line in spool:
            digest.update(line)
            yield line
    yield (json.dumps({"sha256": digest.hexdigest()}) + "\n").encode()


MAGIC = b"KPFAM1"
_SALT_LEN = 16
_SCRYPT_LOG2_N = 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_CHUNK = 64 * 1024
_HEADER_LEN = len(MAGIC) + 3 + _SALT_LEN


def _key(passphrase: str, salt: bytes, log2n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=1 << log2n, r=r, p=p).derive(
        passphrase.encode()
    )


def _nonce(i: int) -> bytes:
    return i.to_bytes(12, "big")


def gzip_stream(lines: Iterable[bytes]) -> Iterator[bytes]:
    comp = zlib.compressobj(6, zlib.DEFLATED, 31)
    for line in lines:
        out = comp.compress(line)
        if out:
            yield out
    yield comp.flush()


def encrypt_stream(chunks: Iterable[bytes], passphrase: str) -> Iterator[bytes]:
    """`KPFAM1` + log2(n) r p + salt, then `len(4, BE) | AES-GCM(chunk)` frames. Each frame's nonce
    is its index; its AAD is the final-frame flag, so a truncated or reordered file fails to open."""
    salt = os.urandom(_SALT_LEN)
    aead = AESGCM(_key(passphrase, salt, _SCRYPT_LOG2_N, _SCRYPT_R, _SCRYPT_P))
    yield MAGIC + bytes([_SCRYPT_LOG2_N, _SCRYPT_R, _SCRYPT_P]) + salt

    def frame(i: int, data: bytes, last: bool) -> bytes:
        ct = aead.encrypt(_nonce(i), data, b"\x01" if last else b"\x00")
        return struct.pack(">I", len(ct)) + ct

    buf = b""
    i = 0
    for chunk in chunks:
        buf += chunk
        while len(buf) > _CHUNK:
            yield frame(i, buf[:_CHUNK], False)
            buf, i = buf[_CHUNK:], i + 1
    yield frame(i, buf, True)


def decrypt_stream(src: BinaryIO, passphrase: str) -> Iterator[bytes]:
    head = src.read(_HEADER_LEN)
    if len(head) < _HEADER_LEN or not head.startswith(MAGIC):
        raise CorruptFile("not an encrypted kpfam file")
    log2n, r, p = head[len(MAGIC)], head[len(MAGIC) + 1], head[len(MAGIC) + 2]
    if not (10 <= log2n <= 20 and 1 <= r <= 16 and 1 <= p <= 4):
        raise CorruptFile("unsupported key-derivation parameters")
    aead = AESGCM(_key(passphrase, head[len(MAGIC) + 3 :], log2n, r, p))
    i = 0
    pending = _read_frame(src)
    while True:
        if pending is None:
            raise CorruptFile("truncated file (no final frame)")
        nxt = _read_frame(src)
        last = nxt is None
        try:
            yield aead.decrypt(_nonce(i), pending, b"\x01" if last else b"\x00")
        except InvalidTag as exc:
            if i == 0:
                raise BadPassphrase("wrong passphrase, or the file is damaged") from exc
            raise CorruptFile("damaged, truncated or reordered file") from exc
        if last:
            return
        pending, i = nxt, i + 1


def _read_frame(src: BinaryIO) -> bytes | None:
    hdr = src.read(4)
    if not hdr:
        return None
    if len(hdr) < 4:
        raise CorruptFile("truncated frame header")
    (n,) = struct.unpack(">I", hdr)
    if n > _CHUNK + 64:
        raise CorruptFile("oversized frame")
    body = src.read(n)
    if len(body) < n:
        raise CorruptFile("truncated frame")
    return body


def pack(lines: Iterable[bytes], passphrase: str | None) -> Iterator[bytes]:
    """Lines from `export_family` -> the bytes of the file."""
    z = gzip_stream(lines)
    return encrypt_stream(z, passphrase) if passphrase else z


def write_kpfam(
    db: Any,
    fid: str,
    dest: BinaryIO,
    *,
    passphrase: str | None,
    opts: ExportOptions | None = None,
) -> None:
    dest.writelines(
        pack(export_family(db, fid, opts, encrypted=bool(passphrase)), passphrase)
    )


class KpfamFile:
    """A readable `.kpfam` (seekable binary file). `lines()` decodes and hashes; `verify()` checks
    the trailer; `manifest()` is line 1; `rows()` yields the document rows after verifying."""

    def __init__(self, src: BinaryIO, passphrase: str | None = None) -> None:
        self._src = src
        self._pass = passphrase

    def _plain(self) -> Iterator[bytes]:
        self._src.seek(0)
        magic = self._src.read(len(MAGIC))
        self._src.seek(0)
        if magic == MAGIC:
            if not self._pass:
                raise BadPassphrase("the file is encrypted; a passphrase is required")
            chunks: Iterable[bytes] = decrypt_stream(self._src, self._pass)
        elif magic[:2] == b"\x1f\x8b":
            chunks = iter(lambda: self._src.read(_CHUNK), b"")
        else:
            raise CorruptFile("not a kpfam file")
        dec = zlib.decompressobj(31)
        try:
            for chunk in chunks:
                yield dec.decompress(chunk)
            yield dec.flush()
        except (zlib.error, EOFError, gzip.BadGzipFile) as exc:
            raise CorruptFile(f"damaged gzip stream: {exc}") from exc
        if not dec.eof:
            raise CorruptFile("truncated gzip stream")

    def lines(self) -> Iterator[bytes]:
        buf = b""
        for chunk in self._plain():
            buf += chunk
            while (i := buf.find(b"\n")) >= 0:
                yield buf[: i + 1]
                buf = buf[i + 1 :]
        if buf:
            raise CorruptFile("last line has no newline")

    def manifest(self) -> Manifest:
        for line in self.lines():
            try:
                m = json.loads(line)
                if m.get("kpfam") != KPFAM_VERSION:
                    raise CorruptFile(f"unsupported kpfam version {m.get('kpfam')!r}")
                return Manifest.model_validate(m)
            except ValueError as exc:
                if isinstance(exc, CorruptFile):
                    raise
                raise CorruptFile(f"bad manifest: {exc}") from exc
        raise CorruptFile("empty file")

    def verify(self) -> None:
        digest = hashlib.sha256()
        prev: bytes | None = None
        for line in self.lines():
            if prev is not None:
                digest.update(prev)
            prev = line
        if prev is None:
            raise CorruptFile("empty file")
        try:
            trailer = json.loads(prev)
        except ValueError as exc:
            raise CorruptFile("missing trailer") from exc
        if not (isinstance(trailer, dict) and set(trailer) == {"sha256"}):
            raise CorruptFile("missing trailer")
        if trailer["sha256"] != digest.hexdigest():
            raise CorruptFile("checksum mismatch")

    def rows(self) -> Iterator[Row]:
        """Document rows (not the manifest or trailer). Call `verify()` first."""
        it = self.lines()
        next(it, None)  # manifest
        prev = next(it, None)
        for line in it:
            assert prev is not None
            yield _parse_row(prev)
            prev = line


BATCH = 400
_FAMILY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
# Pending-push slots on a device, cleared on import: the new server's pager has acked none of them.
_PENDING_PREFIX = "pending"


class ImportRefused(RuntimeError):
    """`plan_import` found collisions or errors; `.report` says which."""

    def __init__(self, report: Report) -> None:
        super().__init__(
            "import refused: " + "; ".join(report.errors or ["collisions"])
        )
        self.report = report


class Collision(BaseModel):
    kind: Literal["family", "uid", "device", "bridge", "number", "alias", "path", "fed"]
    id: str


class Person(BaseModel):
    """One person in the file and the sign-in answer for them."""

    alias: str  # source alias
    targetAlias: str
    name: str
    role: str  # final role after the import
    sourceEmail: str | None = None
    hadSignIn: bool = False
    email: str | None = None  # resolved answer; null when none or "no sign-in"
    signIn: bool | None = None  # null: unanswered


class FedReport(BaseModel):
    """What an import does to the family's links. A move: `announcing` = links waiting to tell the
    other family the new address. A backup: `reauth` = links that need the other family's admin to
    confirm. `links` is the number of links in the file."""

    links: int = 0
    reauth: int = 0
    announcing: int = 0


class Report(BaseModel):
    """What an import would do (and, after `apply_import`, did)."""

    ok: bool
    kind: Literal["move", "backup"] = "move"
    federation: FedReport = FedReport()
    sourceFamilyId: str
    targetFamilyId: str
    idSeed: str
    history: bool
    counts: dict[str, int] = {}
    dropped: list[Dropped] = []
    collisions: list[Collision] = []
    errors: list[str] = []
    notes: list[str] = []
    people: list[Person] = []
    emailsMissing: list[str] = []
    emailsTaken: list[str] = []
    emailsInvalid: list[str] = []


class Invite(BaseModel):
    uid: str
    alias: str
    code: str
    expiresAt: str


class Imported(BaseModel):
    """A device or bridge phone that arrived, so the web can say which pagers need a setup code
    and which bridge phones need a new pair code."""

    id: str
    label: str
    ownerAlias: str | None = None


class ImportResult(BaseModel):
    report: Report
    invites: list[Invite] = []
    accountsCreated: list[str] = []  # aliases
    accountsPending: list[str] = []  # persons with no sign-in yet (cloud)
    promoted: str | None = None  # alias made super (home, `--super`)
    devices: list[Imported] = []
    bridges: list[Imported] = []


class ImportOptions(BaseModel):
    as_id: str | None = None
    alias_map: dict[str, str] = {}
    # home target with no super: the imported admin to promote
    super_alias: str | None = None
    # cloud target: SOURCE alias -> email, or "-" for no sign-in. Every person in the file needs
    # an answer (a person whose file doc already holds an email defaults to it).
    emails: dict[str, str] = {}
    # default: "home" under AUTH_BACKEND=home, else "cloud"
    target_kind: Literal["cloud", "home"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _admin_emails_synonym(cls, data: Any) -> Any:
        if isinstance(data, dict) and "admin_emails" in data:
            data = dict(data)
            data["emails"] = {**data.pop("admin_emails"), **data.get("emails", {})}
        return data


NO_SIGN_IN = "-"


def normalize_email(value: str) -> str | None:
    """Lowercased address, or None when it is not one: a single `@`, a non-empty local part, a
    domain with a dot, no whitespace, at most 254 characters."""
    v = value.strip()
    if not v or len(v) > 254 or any(c.isspace() for c in v) or v.count("@") != 1:
        return None
    local, _, domain = v.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    return v.lower()


def parse_email_list(text: str) -> dict[str, str]:
    """The bulk answer list: one person per line, `alias,email`, `alias=email` or
    `alias<whitespace>email`; a leading `@` on the alias is allowed; `-` means no sign-in; blank
    lines and `#` comments are skipped, and so is a line with an empty answer (a template line
    not yet filled in). Raises `ValueError` naming the line number."""
    out: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cut = min((i for i in (line.find(","), line.find("=")) if i >= 0), default=-1)
        if cut >= 0:
            alias, value = line[:cut], line[cut + 1 :]
        else:
            bits = line.split()
            if len(bits) == 1:
                raise ValueError(
                    f"line {n}: want alias,email (or alias,- for no sign-in)"
                )
            alias, value = bits[0], " ".join(bits[1:])
        alias, value = alias.strip().removeprefix("@").strip(), value.strip()
        if not alias or any(c.isspace() for c in alias):
            raise ValueError(f"line {n}: bad alias {alias!r}")
        if not value:
            continue
        if any(c.isspace() for c in value):
            raise ValueError(f"line {n}: {value!r} is not one email address or '-'")
        if alias in out:
            raise ValueError(f"line {n}: {alias} appears twice")
        out[alias] = value
    return out


class ImportAuth(Protocol):
    """The auth-backend seam an import needs: accounts and claims (cloud), invite codes (home)."""

    def create_account(self, uid: str, email: str | None) -> None: ...

    def set_claims(self, uid: str, role: str, family_id: str | None) -> None: ...

    def mint_invite(self, uid: str, created_by: str) -> tuple[str, datetime]: ...

    def email_owner(self, email: str) -> str | None:
        """The uid that already holds this email in the target's auth, if any."""
        ...


class FirebaseImportAuth:
    """Firebase Auth with the uid pinned. A uid that already exists (an earlier, half-finished
    import) is reused."""

    def create_account(self, uid: str, email: str | None) -> None:
        kwargs: dict[str, Any] = {"uid": uid}
        if email:
            kwargs["email"] = email
        try:
            fb_auth.create_user(**kwargs)
        except fb_auth.UidAlreadyExistsError:
            # an earlier, half-finished import: fill in the email if the account has none
            if email and not fb_auth.get_user(uid).email:
                fb_auth.update_user(uid, email=email)

    def email_owner(self, email: str) -> str | None:
        try:
            return str(fb_auth.get_user_by_email(email).uid)
        except fb_auth.UserNotFoundError:
            return None

    def set_claims(self, uid: str, role: str, family_id: str | None) -> None:
        fb_auth.set_custom_user_claims(uid, {"role": role, "fam": family_id or ""})

    def mint_invite(self, uid: str, created_by: str) -> tuple[str, datetime]:
        raise NotImplementedError("invite codes exist only on a home server")


class HomeImportAuth:
    """Home accounts are `users/{uid}` documents; sign-in is by an invite code redeemed later."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def create_account(self, uid: str, email: str | None) -> None:
        return None

    def set_claims(self, uid: str, role: str, family_id: str | None) -> None:
        return None

    def mint_invite(self, uid: str, created_by: str) -> tuple[str, datetime]:
        return authn_home.mint_invite(uid, created_by, self._db)

    def email_owner(self, email: str) -> str | None:
        return None


def _target_kind(opts: ImportOptions) -> Literal["cloud", "home"]:
    return opts.target_kind or ("home" if auth_backend() == "home" else "cloud")


class _Scan:
    """What one pass over the bundle's rows learned."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.users: dict[str, dict[str, Any]] = {}  # uid -> data (wire form)
        self.aliases: dict[str, dict[str, Any]] = {}  # alias -> {"uid"|"convKey"}
        self.devices: set[str] = set()
        self.bridges: set[str] = set()
        self.device_rows: dict[str, dict[str, Any]] = {}
        self.bridge_rows: dict[str, dict[str, Any]] = {}
        self.numbers: set[str] = set()
        self.with_credentials: set[str] = set()
        self.paths: list[
            str
        ] = []  # non-history, non-identity rows, checked for existence
        self.seqs: set[int] = set()
        # the family's identity row and how many links arrive
        self.fed_family: dict[str, Any] | None = None
        self.fed_links = 0
        self.family_data: dict[str, Any] | None = None


_IDENTITY = {"users", "aliases", "smsNumbers", "devices", "bridges"}


def _coll_of(path: str) -> str:
    return path.split("/")[-2]


def _scan(rows: Iterable[Row]) -> _Scan:
    sc = _Scan()
    for row in rows:
        path, data = row["path"], row["data"]
        parts = path.split("/")
        coll = parts[-2]
        sc.counts[coll] = sc.counts.get(coll, 0) + 1
        if len(parts) == 2:
            if coll == "users":
                sc.users[parts[1]] = data
            elif coll == "aliases":
                sc.aliases[parts[1]] = data
            elif coll == "smsNumbers":
                sc.numbers.add(parts[1])
            elif coll == "devices":
                sc.devices.add(parts[1])
                sc.device_rows[parts[1]] = data
            elif coll == "bridges":
                sc.bridges.add(parts[1])
                sc.bridge_rows[parts[1]] = data
            elif coll == "families":
                sc.family_data = data
            elif coll == "messages":
                seq = data.get("seq")
                if isinstance(seq, int) and not isinstance(seq, bool):
                    sc.seqs.add(seq)
        if coll == "credentials" and parts[0] == "users":
            sc.with_credentials.add(parts[1])
        if len(parts) == 2 and coll == "fedFamilies":
            sc.fed_family = data
        elif len(parts) == 2 and coll == "fedLinks":
            try:
                # the peer's coordinates (host, port, path) are used to build URLs: check them now
                fed_links_store.Remote.model_validate(data.get("remote"))
            except ValidationError as exc:
                raise CorruptFile(f"{path}: bad remote ({exc.error_count()} errors)") from exc
            sc.fed_links += 1
        if (
            coll in datamap.BY_NAME
            and datamap.get(coll).export in ("data", "move")
            and coll not in _IDENTITY | {"families", "backends", "book"}
        ):
            sc.paths.append(path)
    return sc


def _exists(db: Any, path: str) -> bool:
    parent, _, doc_id = path.rpartition("/")
    return db.collection(parent).document(doc_id).get().exists


def _has_super(db: Any) -> bool:
    return any(
        True
        for _ in db.collection("users")
        .where(filter=FieldFilter("role", "==", "super"))
        .stream()
    )


def _final_roles(
    db: Any, sc: _Scan, opts: ImportOptions, kind: str
) -> tuple[dict[str, str], str | None]:
    """uid -> role after the move, and the uid `--super` promotes."""
    roles = {
        uid: str(d.get("role") or "member")
        for uid, d in sc.users.items()
        if d.get("kind", "person") == "person"
    }
    if _has_super(db):
        return {u: ("admin" if r == "super" else r) for u, r in roles.items()}, None
    if kind == "cloud":
        return roles, None  # an imported super keeps `super`: the target has none
    roles = {u: ("admin" if r == "super" else r) for u, r in roles.items()}
    promoted = None
    if opts.super_alias:
        promoted = next(
            (
                u
                for u, d in sc.users.items()
                if d.get("alias") == opts.super_alias and roles.get(u) == "admin"
            ),
            None,
        )
        if promoted:
            roles[promoted] = "super"
    return roles, promoted


class _Answers:
    """The resolved sign-in answers (cloud): uid -> email, and the uids marked "no sign-in"."""

    def __init__(self) -> None:
        self.email: dict[str, str] = {}
        self.none: set[str] = set()


def _email_template(sc: _Scan) -> str:
    lines = [
        "# alias,email   (alias,- = no sign-in, e.g. a child who only carries a pager)",
    ]
    for uid, d in sorted(sc.users.items(), key=lambda kv: str(kv[1].get("alias"))):
        if d.get("kind", "person") != "person":
            continue
        lines.append(f"{d.get('alias')},{str(d.get('email') or '').strip()}")
    return "\n".join(lines) + "\n"


def email_template(src: KpfamFile) -> str:
    """A ready-to-fill answer list for `parse_email_list`: `alias,` per person, prefilled when
    the file already holds the person's email."""
    src.verify()
    return _email_template(_scan(src.rows()))


def _federation_notes(db: Any, sc: _Scan, kind: str) -> list[str]:
    """What happens to federation: the key is rotated, the links arrive and are announced once the
    family is effective here, and the target's switches decide when."""
    has_fed_people = any(d.get("fed") for d in sc.users.values())
    if sc.fed_family is None:
        if has_fed_people:
            return [
                (
                    "this file carries no federation links; its federated contacts arrive "
                    "unsendable until the family is linked again here"
                )
            ]
        return []
    server = db.collection("settings").document("federation").get()
    server_on = bool((server.to_dict() or {}).get("enabled")) if server.exists else False
    fam_on = bool((sc.family_data or {}).get("federation"))
    if kind == "backup":
        notes = [
            (
                f"federation: restored from a backup, which holds no key: the family keeps its "
                f"federation id, gets a new key, and {sc.fed_links} link(s) arrive needing "
                "reauthorization (nothing is sent or accepted on them until the other family's "
                "admin confirms)"
            )
        ]
        if not fam_on:
            notes.append(
                "the family's own federation flag arrives off: a super turns it on for this family"
            )
        return notes
    notes = [
        (
            f"federation: the family's key is rotated on import and {sc.fed_links} link(s) "
            "arrive; each linked family is told the new address and key once this family is "
            "effective here"
        )
    ]
    if not server_on:
        notes.append(
            "federation is OFF on this server: the links stay dormant until a super turns the "
            "server switch on (Admin > Federation)"
        )
    if not fam_on:
        notes.append(
            "the family's own federation flag arrives off: a super turns it on for this family"
        )
    if server_on and fam_on:
        notes.append("federation is on here: the links are announced within a minute")
    return notes


def _plan(
    db: Any,
    src: KpfamFile,
    opts: ImportOptions,
    auth: ImportAuth | None = None,
) -> tuple[Report, _Scan, Manifest, _Answers]:
    src.verify()
    m = src.manifest()
    sc = _scan(src.rows())
    kind = _target_kind(opts)
    old, seed = m.family.id, m.family.idSeed
    new = opts.as_id or old
    errors: list[str] = []
    coll: list[Collision] = []
    notes: list[str] = []

    if m.schemaVersion != SCHEMA_VERSION:
        errors.append(
            f"schema version mismatch: file {m.schemaVersion}, this server {SCHEMA_VERSION}; "
            "upgrade both servers to the same release first"
        )
    if not _FAMILY_ID_RE.match(new):
        errors.append(f"{new!r} is not a usable family id")
    fam = db.collection("families").document(new).get()
    if fam.exists:
        if (fam.to_dict() or {}).get("importing"):
            errors.append(
                f"family {new!r} holds a partial import: delete it and retry, or import as another id"
            )
        coll.append(Collision(kind="family", id=new))

    amap = dict(opts.alias_map)
    for a in amap:
        if a not in sc.aliases:
            errors.append(f"--alias {a}=...: no such alias in the file")
    for a, b in amap.items():
        if not users_store.ALIAS_RE.match(b) or b in users_store.RESERVED_ALIASES:
            errors.append(f"--alias {a}={b}: not a valid alias")
    renamed = {amap.get(a, a) for a in sc.aliases}
    if len(renamed) != len(sc.aliases):
        errors.append(
            "the alias renames collide with each other or with an alias in the file"
        )
    for a in sorted(sc.aliases):
        if _exists(db, f"aliases/{amap.get(a, a)}"):
            coll.append(Collision(kind="alias", id=amap.get(a, a)))

    for uid in sorted(sc.users):
        if _exists(db, f"users/{uid}"):
            coll.append(Collision(kind="uid", id=uid))
    for dev in sorted(sc.devices):
        if _exists(db, f"devices/{dev}") or _exists(db, f"deviceSecrets/{dev}"):
            coll.append(Collision(kind="device", id=dev))
    for br in sorted(sc.bridges):
        if _exists(db, f"bridges/{br}"):
            coll.append(Collision(kind="bridge", id=br))
    for num in sorted(sc.numbers):
        if _exists(db, f"smsNumbers/{num}"):
            coll.append(Collision(kind="number", id=num))
    for path in sc.paths:
        p = _rewrite_path(path, old, new)
        if _exists(db, p):
            coll.append(Collision(kind="path", id=p))
    if sc.fed_family is not None and sc.fed_family.get("fedId"):
        # Two families with one fedId on a server would make `to` ambiguous (a backup restored
        # beside the family it was taken from).
        for _ in _where(db, "fedFamilies", "fedId", "==", sc.fed_family["fedId"]):
            coll.append(Collision(kind="fed", id=str(sc.fed_family["fedId"])))
            break

    persons = {u for u, d in sc.users.items() if d.get("kind", "person") == "person"}
    by_alias = {d.get("alias"): u for u, d in sc.users.items()}
    has_super = _has_super(db)
    roles, _promoted = _final_roles(db, sc, opts, kind)
    answers = _Answers()
    missing: list[str] = []
    taken: list[str] = []
    invalid: list[str] = []
    people: list[Person] = []
    if kind == "cloud":
        auth = auth or FirebaseImportAuth()
        for a in sorted(opts.emails):
            if by_alias.get(a) not in persons:
                errors.append(f"--email {a}=...: no such person in the file")
        owners: dict[str, list[str]] = {}
        for uid in sorted(persons, key=lambda u: str(sc.users[u].get("alias"))):
            a = str(sc.users[uid].get("alias"))
            prefill = str(sc.users[uid].get("email") or "").strip()
            given = opts.emails.get(a, prefill)
            if not given:
                missing.append(a)
                continue
            if given == NO_SIGN_IN:
                answers.none.add(uid)
                continue
            email = normalize_email(given)
            if email is None:
                invalid.append(a)
                errors.append(f"--email {a}={given}: not an email address")
                continue
            answers.email[uid] = email
            owners.setdefault(email, []).append(a)
        for email, group in sorted(owners.items()):
            if len(group) > 1:
                taken.extend(group)
                errors.append(
                    f"{email} is given for {', '.join(group)}: one email per person"
                )
        for uid, email in sorted(answers.email.items()):
            a = str(sc.users[uid].get("alias"))
            if a in taken:
                continue
            holder = auth.email_owner(email)
            if holder is not None and holder != uid:
                taken.append(a)
                errors.append(f"{email} (for {a}) already belongs to another account")
        if missing:
            errors.append(
                f"no email for: {', '.join(missing)} "
                "(give alias=email, or alias=- for no sign-in)"
            )
        if not any(
            roles.get(u) in ("admin", "super") and u in answers.email for u in persons
        ):
            errors.append(
                "at least one admin needs a real email: nobody could sign in to run the family"
            )
        if opts.super_alias:
            errors.append("--super applies to a home server only")
    else:
        if opts.emails:
            errors.append(
                "--email / --emails-file / --admin-email apply to a cloud target only"
            )
        if has_super and opts.super_alias:
            errors.append("the target already has a super; --super is not needed")
        if not has_super and not opts.super_alias:
            errors.append(
                "the target has no super: name an imported admin with --super alias"
            )
        if opts.super_alias and not has_super:
            u = by_alias.get(opts.super_alias)
            if u not in persons or str(sc.users[u].get("role")) not in (
                "admin",
                "super",
            ):
                errors.append(f"--super {opts.super_alias}: not an admin in the file")
    for uid in sorted(persons, key=lambda u: str(sc.users[u].get("alias"))):
        d = sc.users[uid]
        a = str(d.get("alias"))
        src_email = str(d.get("email") or "").strip() or None
        answered = uid in answers.email or uid in answers.none
        people.append(
            Person(
                alias=a,
                targetAlias=amap.get(a, a),
                name=str(d.get("displayName") or ""),
                role=roles.get(uid, str(d.get("role") or "member")),
                sourceEmail=src_email,
                hadSignIn=bool(src_email) or uid in sc.with_credentials,
                email=answers.email.get(uid),
                signIn=(uid in answers.email) if answered else None,
            )
        )
    if sc.seqs:
        notes.append(f"{len(sc.seqs)} message sequence numbers will be renumbered")
    notes.extend(_federation_notes(db, sc, m.kind))
    if not m.history:
        notes.append(
            "the file carries no history (messages, locations, battery, SMS log)"
        )

    backup = m.kind == "backup"
    report = Report(
        ok=not errors and not coll,
        kind=m.kind,
        federation=FedReport(
            links=sc.fed_links,
            reauth=sc.fed_links if backup else 0,
            announcing=0 if backup else sc.fed_links,
        ),
        sourceFamilyId=old,
        targetFamilyId=new,
        idSeed=seed,
        history=m.history,
        counts=dict(sorted(sc.counts.items())),
        dropped=m.dropped,
        collisions=coll,
        errors=errors,
        notes=notes,
        people=people,
        emailsMissing=sorted(missing),
        emailsTaken=sorted(taken),
        emailsInvalid=sorted(invalid),
    )
    return report, sc, m, answers


def plan_import(
    db: Any,
    src: KpfamFile,
    opts: ImportOptions | None = None,
    auth: ImportAuth | None = None,
) -> Report:
    """Dry run: verifies the file and reports collisions, errors, counts, and every person's
    sign-in answer. Writes nothing."""
    return _plan(db, src, opts or ImportOptions(), auth)[0]


def _rewrite_path(path: str, old: str, new: str) -> str:
    if old == new:
        return path
    parts = path.split("/")
    if parts[0] == "families" and parts[1] == old:
        parts[1] = new
    elif parts[0] in ("contactNames", "fedLinks") and parts[1].startswith(old + "_"):
        parts[1] = new + parts[1][len(old) :]
    elif parts[0] == "fedFamilies" and parts[1] == old:
        parts[1] = new
    return "/".join(parts)


def _map_family(v: Any, old: str, new: str) -> Any:
    if v == old:
        return new
    if isinstance(v, list):
        return (
            sorted({new if x == old else x for x in v})
            if all(isinstance(x, str) for x in v)
            else v
        )
    return v


class _Rewriter:
    def __init__(
        self,
        *,
        old: str,
        new: str,
        amap: dict[str, str],
        seqmap: dict[int, int],
        roles: dict[str, str],
        emails: dict[str, str],
        no_email: set[str],
        keep_credentials: bool,
        fed_links: int = 0,
        backup: bool = False,
    ) -> None:
        self.old, self.new = old, new
        self.fed_links = fed_links
        self.backup = backup
        self.amap, self.seqmap, self.roles = amap, seqmap, roles
        # both keyed by uid
        self.emails, self.no_email = emails, no_email
        self.keep_credentials = keep_credentials

    def _alias(self, v: Any) -> Any:
        return self.amap.get(v, v) if isinstance(v, str) else v

    def __call__(self, row: Row) -> Row | None:
        path = row["path"]
        data = dict(row["data"])
        parts = path.split("/")
        coll = parts[-2]
        for f in ("familyId", "ownerFamilyId"):
            if f in data:
                data[f] = _map_family(data[f], self.old, self.new)
        if "familyIds" in data:
            data["familyIds"] = _map_family(data["familyIds"], self.old, self.new)
        path = _rewrite_path(path, self.old, self.new)
        if coll == "credentials" and not self.keep_credentials:
            return None
        if coll == "users" and len(parts) == 2:
            data["alias"] = self._alias(data.get("alias"))
            if parts[1] in self.roles:
                data["role"] = self.roles[parts[1]]
            if parts[1] in self.emails:
                data["email"] = self.emails[parts[1]]
            elif parts[1] in self.no_email:
                data.pop("email", None)
        elif coll == "aliases":
            path = f"aliases/{self._alias(parts[1])}"
        elif coll == "messages":
            if "senderAlias" in data:
                data["senderAlias"] = self._alias(data["senderAlias"])
            if isinstance(data.get("seq"), int) and data["seq"] in self.seqmap:
                data["seq"] = self.seqmap[data["seq"]]
        elif coll == "conversations":
            if "alias" in data:
                data["alias"] = self._alias(data["alias"])
            parts_map = data.get("participants")
            if isinstance(parts_map, dict):
                data["participants"] = {
                    u: (
                        {**p, "alias": self._alias(p.get("alias"))}
                        if isinstance(p, dict) and "alias" in p
                        else p
                    )
                    for u, p in parts_map.items()
                }
        elif coll == "alerts":
            for f in ("subjectAlias", "peerAlias"):
                if f in data:
                    data[f] = self._alias(data[f])
            link_id = data.get("fedLinkId")
            if isinstance(link_id, str) and link_id.startswith(self.old + "_"):
                data["fedLinkId"] = self.new + link_id[len(self.old) :]
        elif coll == "fedFamilies" and len(parts) == 2:
            # a new key, `epoch + 1`, the old private key kept to sign the announcement; with no
            # link to announce to it is not kept at all.
            data = fed_families_store.rotated(data)
            if self.backup or not self.fed_links:
                data["prevPrivateKey"] = None
        elif coll == "fedLinks" and len(parts) == 2:
            if self.backup:
                # nothing flows until the other family's admin confirms a reauthorization
                data = {**data, "state": "reauth", "remoteKeyChanged": False, "announce": None}
            else:
                data = fed_links_store.announce_pending_data(data)
        elif coll == "contactRequests" and "alias" in data:
            data["alias"] = self._alias(data["alias"])
        elif coll == "devices" and len(parts) == 2:
            # the pager has to be re-provisioned against this server
            data["provisionState"] = "moved"
            data["status"] = {}
            data["bookVersion"] = int(data.get("bookVersion") or 0) + 1
            for k in [k for k in data if k.startswith(_PENDING_PREFIX)]:
                del data[k]
        elif coll == "bridges" and len(parts) == 2:
            # the imported bridge is unpaired; "new pair code" re-pairs the phone
            data.pop("fcmToken", None)
            data["tokenHash"] = None
            data["lastSeenAt"] = None
            data["health"] = None
        return {"path": path, "data": data}


def _reserve_seqs(db: Any, n: int) -> int:
    """`settings/meta.seqCounter += n`; returns the counter before."""
    ref = db.collection("settings").document("meta")

    @transactional
    def _run(txn: Any) -> int:
        snap = ref.get(transaction=txn)
        base = int((snap.to_dict() or {}).get("seqCounter") or 0) if snap.exists else 0
        if snap.exists:
            txn.update(ref, {"seqCounter": base + n})
        else:
            txn.set(
                ref,
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "lastSweepAt": None,
                    "seqCounter": base + n,
                },
            )
        return base

    return _run(db.transaction())


def apply_import(
    db: Any,
    src: KpfamFile,
    opts: ImportOptions | None = None,
    auth: ImportAuth | None = None,
) -> ImportResult:
    """Plans, refuses (`ImportRefused`) on any collision or error, then writes: the family doc with
    `importing: true`, the rows in batches of 400, the accounts, and last clears the marker."""
    opts = opts or ImportOptions()
    kind = _target_kind(opts)
    auth = auth or (HomeImportAuth(db) if kind == "home" else FirebaseImportAuth())
    report, sc, _m, answers = _plan(db, src, opts, auth)
    if not report.ok:
        raise ImportRefused(report)
    old, new, seed = report.sourceFamilyId, report.targetFamilyId, report.idSeed
    roles, promoted_uid = _final_roles(db, sc, opts, kind)

    # the family doc first, flagged `importing`
    rows_it = src.rows()
    fam_row = next(rows_it)  # the family doc is always the first row
    rows_it.close()
    assert fam_row["path"] == f"families/{old}"
    fam = dict(fam_row["data"])
    fam.pop("frozen", None)
    # `federation` (the super's switch for this family) arrives as the file had it; the target's
    # own server switch still decides when anything happens.
    fam["importing"] = True
    if seed != new:
        fam["idSeed"] = seed
    else:
        fam.pop("idSeed", None)
    db.collection("families").document(new).set(sqlitestore.from_wire(fam))

    seqmap: dict[int, int] = {}
    if sc.seqs:
        base = _reserve_seqs(db, len(sc.seqs))
        seqmap = {old_seq: base + 1 + i for i, old_seq in enumerate(sorted(sc.seqs))}
    rewrite = _Rewriter(
        old=old,
        new=new,
        amap=dict(opts.alias_map),
        seqmap=seqmap,
        roles=roles,
        emails=answers.email,
        no_email=answers.none,
        keep_credentials=(kind == "home"),
        fed_links=sc.fed_links,
        backup=_m.kind == "backup",
    )
    batch, pending = db.batch(), 0
    for row in src.rows():
        if row["path"] == f"families/{old}":
            continue
        out = rewrite(row)
        if out is None:
            continue
        parent, _, doc_id = out["path"].rpartition("/")
        batch.set(
            db.collection(parent).document(doc_id), sqlitestore.from_wire(out["data"])
        )
        pending += 1
        if pending == BATCH:
            batch.commit()
            batch, pending = db.batch(), 0
    if pending:
        batch.commit()

    result = ImportResult(report=report)
    result.devices = _imported(sc, sc.device_rows, opts)
    result.bridges = _imported(sc, sc.bridge_rows, opts)
    persons = {u for u, d in sc.users.items() if d.get("kind", "person") == "person"}
    if kind == "cloud":
        def target_alias(uid: str) -> str:
            old_alias = sc.users[uid].get("alias")
            return str(opts.alias_map.get(old_alias, old_alias))

        for uid in sorted(answers.email, key=target_alias):
            auth.create_account(uid, answers.email[uid])
            auth.set_claims(uid, roles[uid], new)
            result.accountsCreated.append(target_alias(uid))
        result.accountsPending = sorted(target_alias(u) for u in answers.none)
    else:
        for uid in sorted(persons):
            if roles[uid] in ("admin", "super") and uid not in sc.with_credentials:
                alias = opts.alias_map.get(
                    sc.users[uid].get("alias"), sc.users[uid].get("alias")
                )
                code, expires = auth.mint_invite(uid, "familyimport")
                result.invites.append(
                    Invite(
                        uid=uid,
                        alias=str(alias),
                        code=code,
                        expiresAt=expires.isoformat(timespec="seconds"),
                    )
                )
        if promoted_uid:
            result.promoted = str(opts.super_alias)
    db.collection("families").document(new).update({"importing": False})
    return result


def _imported(
    sc: _Scan, rows: dict[str, dict[str, Any]], opts: ImportOptions
) -> list[Imported]:
    out: list[Imported] = []
    for rid in sorted(rows):
        data = rows[rid]
        owner = sc.users.get(str(data.get("ownerUid") or ""))
        old_alias = owner.get("alias") if owner else None
        alias = opts.alias_map.get(old_alias, old_alias) if old_alias else None
        out.append(
            Imported(id=rid, label=str(data.get("label") or ""), ownerAlias=alias)
        )
    return out


def _db() -> Any:
    from app.dbexport import _db as open_db

    return open_db()


def _cli_passphrase(args: argparse.Namespace, *, confirm: bool) -> str | None:
    """`--passphrase-file`, else `$KPFAM_PASSPHRASE`, else a prompt. Never an argument."""
    if getattr(args, "no_passphrase", False):
        print(
            "warning: the file will NOT be encrypted; it holds device HMAC keys and message bodies",
            file=sys.stderr,
        )
        return None
    if args.passphrase_file:
        return (
            Path(args.passphrase_file).read_text(encoding="utf-8").rstrip("\r\n")
            or None
        )
    env = os.environ.get("KPFAM_PASSPHRASE")
    if env:
        return env
    if not sys.stdin.isatty():
        return None
    first = getpass.getpass("passphrase (empty for none): ")
    if confirm and first and getpass.getpass("again: ") != first:
        raise ValueError("the passphrases differ")
    return first or None


def _pair_args(values: list[str] | None, flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values or []:
        key, sep, val = v.partition("=")
        if not sep or not key or not val:
            raise ValueError(f"{flag} wants old=new, got {v!r}")
        out[key] = val
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="familyexport",
        description="Move one family between servers. With SQLite "
        "the relay must be stopped, as for `python -m app.dbexport`.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_pass(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--passphrase-file",
            help="read the passphrase from this file (else $KPFAM_PASSPHRASE, else prompt)",
        )

    p_exp = sub.add_parser("export", help="write <family>-<date>.kpfam")
    p_exp.add_argument("family")
    p_exp.add_argument(
        "-o", "--output", help="file name (default <family>-<date>.kpfam)"
    )
    p_exp.add_argument(
        "--no-history",
        action="store_true",
        help="leave out messages, locations, battery, SMS log",
    )
    p_exp.add_argument(
        "--no-passphrase",
        action="store_true",
        help="write an unencrypted file (warned)",
    )
    add_pass(p_exp)
    p_bak = sub.add_parser(
        "backup",
        help="write <family>-<date>.kpfam from a LIVE family (no federation private key inside; "
        "restore it with `import`, then reauthorize each link)",
    )
    p_bak.add_argument("family")
    p_bak.add_argument("-o", "--output")
    p_bak.add_argument("--no-history", action="store_true")
    p_bak.add_argument(
        "--no-passphrase",
        action="store_true",
        help="write an unencrypted file (warned: it holds message bodies)",
    )
    add_pass(p_bak)
    p_imp = sub.add_parser("import", help="read a .kpfam into the configured database")
    p_imp.add_argument("file")
    p_imp.add_argument(
        "--dry-run",
        action="store_true",
        help="report collisions and counts; write nothing",
    )
    p_imp.add_argument("--as", dest="as_id", help="import under this family id")
    p_imp.add_argument(
        "--alias",
        action="append",
        metavar="OLD=NEW",
        help="rename an alias (repeatable)",
    )
    p_imp.add_argument(
        "--super",
        dest="super_alias",
        metavar="ALIAS",
        help="home: the imported admin to promote when the server has no super",
    )
    p_imp.add_argument(
        "--email",
        "--admin-email",
        dest="email",
        action="append",
        metavar="ALIAS=EMAIL",
        help="cloud: the sign-in email for this person, or ALIAS=- for none (repeatable; "
        "overrides --emails-file). Every person needs an answer.",
    )
    p_imp.add_argument(
        "--emails-file",
        metavar="PATH",
        help="cloud: one 'alias,email' (or 'alias=email', 'alias email') per line; '-' for no "
        "sign-in; '#' comments. `inspect --template` writes a starting point.",
    )
    add_pass(p_imp)
    p_ins = sub.add_parser("inspect", help="print the manifest")
    p_ins.add_argument("file")
    p_ins.add_argument(
        "--template",
        action="store_true",
        help="print only the email answer list to fill in (for import --emails-file)",
    )
    add_pass(p_ins)
    args = parser.parse_args(argv)

    try:
        if args.command in ("export", "backup"):
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
        FamilyNotFound,
        NotFrozen,
        BadPassphrase,
        PassphraseRequired,
        CorruptFile,
        ImportRefused,
        ValueError,
        OSError,
    ) as exc:
        if isinstance(exc, ImportRefused):
            print(exc.report.model_dump_json(indent=2))
        print(f"familyexport: {_explain(exc)}", file=sys.stderr)
        return 1


def _explain(exc: Exception) -> str:
    if isinstance(exc, FamilyNotFound):
        return f"no such family {exc}"
    if isinstance(exc, NotFrozen):
        return "the family is not frozen; freeze it first, or use `backup` for a live family"
    return str(exc)


def _cli_export(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    opts = ExportOptions(
        history=not args.no_history,
        backup=args.command == "backup",
        source_kind=source_kind(),
        base_url=settings.public_base_url,
    )
    passphrase = _cli_passphrase(args, confirm=True)
    if args.command == "backup":
        pass  # no key inside, so no passphrase is forced (message bodies are: encrypt anyway)
    elif passphrase is None and not args.no_passphrase:
        raise ValueError(
            "give a passphrase (--passphrase-file, $KPFAM_PASSPHRASE or the prompt) "
            "or --no-passphrase for an unencrypted file"
        )
    out = Path(
        args.output or f"{args.family}-{datetime.now(UTC).date().isoformat()}.kpfam"
    )
    try:
        with out.open("wb") as fh:
            write_kpfam(_db(), args.family, fh, passphrase=passphrase, opts=opts)
    except BaseException:
        out.unlink(missing_ok=True)  # never leave a half-written (or refused) file behind
        raise
    print(f"familyexport: wrote {out}", file=sys.stderr)
    return 0


def _open(args: argparse.Namespace) -> KpfamFile:
    return KpfamFile(open(args.file, "rb"), _cli_passphrase(args, confirm=False))


def _cli_import(args: argparse.Namespace) -> int:
    emails: dict[str, str] = {}
    if args.emails_file:
        emails.update(parse_email_list(Path(args.emails_file).read_text()))
    emails.update(_pair_args(args.email, "--email"))
    opts = ImportOptions(
        as_id=args.as_id,
        alias_map=_pair_args(args.alias, "--alias"),
        super_alias=args.super_alias,
        emails=emails,
    )
    src = _open(args)
    if args.dry_run:
        report = plan_import(_db(), src, opts)
        print(report.model_dump_json(indent=2))
        if report.emailsMissing:
            print(
                "familyexport: fill in this list, save it, and pass it as --emails-file:\n"
                + email_template(src),
                file=sys.stderr,
            )
        return 0 if report.ok else 1
    result = apply_import(_db(), src, opts)
    print(result.report.model_dump_json(indent=2))
    for inv in result.invites:
        print(f"invite for {inv.alias}: {inv.code} (expires {inv.expiresAt})")
    for alias in result.accountsPending:
        print(
            f"no sign-in for {alias}: marked '-' (add one from the family page later)"
        )
    print(
        "next: issue a setup code for each pager, re-pair each bridge phone",
        file=sys.stderr,
    )
    return 0


def _cli_inspect(args: argparse.Namespace) -> int:
    f = _open(args)
    f.verify()
    if args.template:
        print(email_template(f), end="")
        return 0
    print(f.manifest().model_dump_json(indent=2))
    print(
        "familyexport: answer list for a cloud import (--emails-file):\n"
        + email_template(f),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
