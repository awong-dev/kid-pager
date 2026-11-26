"""`fedLinks/{fid}_{remoteFedId}` and `fedInvites/{sha256(code)}`; the invite code is never stored.
A link joins one local and one remote family; `state: "reauth"` marks one restored from a backup."""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime
from typing import Literal

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter, Transaction
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import valid_base_path
from app.db.firestore import get_db, run_transaction

LinkState = Literal["active", "suspended", "reauth"]
InviteOutcome = Literal["ok", "unknown", "used", "expired", "wrong_family", "wrong_link"]

INVITE_TTL_S = 24 * 3600
MAX_LINKS_PER_FAMILY = 16
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


class LinkExists(Exception):
    pass


class LimitReached(Exception):
    pass


class Health(BaseModel):
    """`app.notices.Health`'s shape, so the sender can pass it to `notices.enter_bad`/`enter_ok`."""

    model_config = ConfigDict(extra="ignore")

    state: str = "ok"
    since: int = 0
    notifiedAt: int | None = None

    @field_validator("since", mode="before")
    @classmethod
    def _since_int(cls, v: object) -> object:
        return 0 if v is None else v

    @field_validator("notifiedAt", mode="before")
    @classmethod
    def _notified_int(cls, v: object) -> object:
        return v if v is None or isinstance(v, int) else None


class Remote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fedId: str
    name: str
    publicKey: str
    epoch: int = 0
    host: str
    port: int = 443
    # where the peer's relay is mounted (`""` at the root, `/pager`); see `config.valid_base_path`
    path: str = ""
    tlsCa: str | None = None
    prevPublicKey: str | None = None

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not valid_base_path(v):
            raise ValueError("not a base path")
        return v

    @property
    def where(self) -> str:
        """Where the family lives, for people to read: `host`, `host:8443`, `host/pager`."""
        return f"{self.host}{'' if self.port == 443 else f':{self.port}'}{self.path}"


class Announce(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pending: bool = False
    since: int = 0
    attempts: int = 0
    nextAt: float = 0


class FedLink(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    familyId: str
    remote: Remote
    state: LinkState = "active"
    shared: list[str] = Field(default_factory=list)
    createdBy: str | None = None
    createdAt: datetime | None = None
    lastOkAt: datetime | None = None
    lastErrAt: datetime | None = None
    health: Health = Field(default_factory=Health)
    announce: Announce | None = None
    # Set when a reauthorization was accepted on the code alone because the other family's key
    # is not the one this link stored (it moved since the backup): compare fingerprints.
    remoteKeyChanged: bool = False

    @property
    def announce_pending(self) -> bool:
        return self.announce is not None and self.announce.pending


def valid_host(host: str) -> bool:
    return bool(HOST_RE.match(host)) and ".." not in host


def link_id(family_id: str, remote_fed_id: str) -> str:
    return f"{family_id}_{remote_fed_id}"


def _links():
    return get_db().collection("fedLinks")


def _invites():
    return get_db().collection("fedInvites")


def _link(snap) -> FedLink:
    return FedLink.model_validate({"id": snap.id, **(snap.to_dict() or {})})


def get(lid: str) -> FedLink | None:
    snap = _links().document(lid).get()
    return _link(snap) if snap.exists else None


def get_for(family_id: str, remote_fed_id: str) -> FedLink | None:
    return get(link_id(family_id, remote_fed_id))


def list_for_family(family_id: str) -> list[FedLink]:
    rows = (
        _link(s)
        for s in _links().where(filter=FieldFilter("familyId", "==", family_id)).stream()
    )
    return sorted(rows, key=lambda link: (link.remote.name.casefold(), link.id))


def list_all() -> list[FedLink]:
    return sorted((_link(s) for s in _links().stream()), key=lambda link: link.id)


def create(
    family_id: str,
    *,
    remote: Remote,
    created_by: str | None,
) -> FedLink:
    """`state: active`, nothing shared. `LinkExists` if already linked, `LimitReached` at 16."""
    lid = link_id(family_id, remote.fedId)
    if len(list_for_family(family_id)) >= MAX_LINKS_PER_FAMILY:
        raise LimitReached(family_id)
    try:
        _links().document(lid).create(
            {
                "familyId": family_id,
                "remote": remote.model_dump(),
                "state": "active",
                "shared": [],
                "createdBy": created_by,
                "createdAt": SERVER_TIMESTAMP,
                "lastOkAt": None,
                "lastErrAt": None,
                "health": {"state": "ok", "since": 0, "notifiedAt": None},
                "announce": None,
                "remoteKeyChanged": False,
            }
        )
    except AlreadyExists:
        raise LinkExists(lid) from None
    link = get(lid)
    assert link is not None
    return link


def set_state(lid: str, state: LinkState) -> FedLink:
    """`KeyError` for no such link."""
    ref = _links().document(lid)
    if not ref.get().exists:
        raise KeyError(lid)
    ref.update({"state": state})
    link = get(lid)
    assert link is not None
    return link


def set_shared(lid: str, shared: list[str]) -> FedLink:
    """Replaces the list. `KeyError` for no such link."""
    ref = _links().document(lid)
    if not ref.get().exists:
        raise KeyError(lid)
    ref.update({"shared": sorted(set(shared))})
    link = get(lid)
    assert link is not None
    return link


def remove_shared_uid(uid: str) -> int:
    """Drops `uid` from every link's `shared` (a deleted member). Returns the links changed."""
    changed = 0
    for snap in _links().where(filter=FieldFilter("shared", "array_contains", uid)).stream():
        shared = [u for u in (snap.to_dict() or {}).get("shared") or [] if u != uid]
        snap.reference.update({"shared": shared})
        changed += 1
    return changed


def record_ok(lid: str) -> None:
    _links().document(lid).update({"lastOkAt": SERVER_TIMESTAMP})


def record_err(lid: str) -> None:
    _links().document(lid).update({"lastErrAt": SERVER_TIMESTAMP})


def set_health(lid: str, **fields: object) -> None:
    """Merges `fields` into `health` (`state`, `since`, `notifiedAt`)."""
    _links().document(lid).update({f"health.{k}": v for k, v in fields.items()})


def update_remote(lid: str, **fields: object) -> None:
    """Merges `fields` into `remote` (an accepted `moved`)."""
    _links().document(lid).update({f"remote.{k}": v for k, v in fields.items()})


def apply_reauth(lid: str, remote: Remote, *, key_changed: bool) -> FedLink:
    """The other family's new key, epoch and address replace the old; the link is active again."""
    _links().document(lid).update(
        {
            "remote": remote.model_dump(),
            "state": "active",
            "remoteKeyChanged": key_changed,
            "announce": None,
        }
    )
    link = get(lid)
    assert link is not None
    return link


def set_announce(lid: str, announce: Announce | None) -> None:
    _links().document(lid).update(
        {"announce": announce.model_dump() if announce is not None else None}
    )


def pending_announce(family_id: str) -> list[FedLink]:
    return [link for link in list_for_family(family_id) if link.announce_pending]


def delete(lid: str) -> None:
    _links().document(lid).delete()


def announce_pending_data(data: dict, *, now: float | None = None) -> dict:
    """A stored link row marked for announcement (pure: the import rewrites rows with it)."""
    return {
        **data,
        "announce": {
            "pending": True,
            "since": int(time.time() if now is None else now),
            "attempts": 0,
            "nextAt": 0,
        },
    }


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def create_invite(
    code: str,
    family_id: str,
    created_by: str,
    *,
    now: float | None = None,
    link_id: str | None = None,
) -> int:
    """Stores the invite for `code` (a fresh random value); returns its expiry (unix s)."""
    now = time.time() if now is None else now
    expires = int(now) + INVITE_TTL_S
    _invites().document(code_hash(code)).create(
        {
            "familyId": family_id,
            "linkId": link_id,
            "expiresAt": expires,
            "createdBy": created_by,
            "usedAt": None,
        }
    )
    return expires


def get_invite(code: str) -> dict | None:
    """The invite document for `code`, or None."""
    snap = _invites().document(code_hash(code)).get()
    return (snap.to_dict() or {}) if snap.exists else None


def consume_invite(
    code: str,
    family_id: str,
    *,
    now: float | None = None,
    used_by: str | None = None,
    used_pk: str | None = None,
    link_id: str | None = None,
) -> InviteOutcome:
    """Marks `code` used once (`usedBy`, `usedPk`); `unknown`, `used`, `expired` write nothing."""
    now = time.time() if now is None else now
    ref = _invites().document(code_hash(code))

    def _txn(transaction: Transaction) -> InviteOutcome:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return "unknown"
        data = snap.to_dict() or {}
        if data.get("familyId") != family_id:
            return "wrong_family"
        if data.get("linkId") != link_id:
            return "wrong_link"  # a reauthorization code is for its link only, and only there
        if data.get("usedAt") is not None:
            return "used"
        if int(data.get("expiresAt") or 0) < now:
            return "expired"
        transaction.update(
            ref, {"usedAt": int(now), "usedBy": used_by, "usedPk": used_pk}
        )
        return "ok"

    return run_transaction(_txn)
