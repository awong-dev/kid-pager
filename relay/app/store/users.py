"""`users/{uid}` and `aliases/{alias}`; the alias doc id is the uniqueness constraint and is
created in the same transaction as the user."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud.firestore import SERVER_TIMESTAMP, Transaction
from pydantic import BaseModel, ConfigDict, Field

from app.db.firestore import get_db, run_transaction
from app.store import backends as backends_store

# Same shape as PROTOCOL.md §3.1's alias regex (from/to on the wire).
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
RESERVED_ALIASES = {"system"}

# "super" is the global superadmin, "admin" a family admin.
Role = Literal["super", "admin", "member"]
Kind = Literal["person", "external"]
# Plain `str`, not restricted to the policy literals: `app/policy.py` validates them.


class Policy(BaseModel):
    """`users/{uid}.policy`. `in` is a Python keyword, hence `in_` with `Field(alias="in")`."""

    model_config = ConfigDict(populate_by_name=True)

    out: str
    in_: str = Field(alias="in")


class Notify(BaseModel):
    """`users/{uid}.notify`: whether this user's family-admin alerts are pushed."""

    alerts: bool = True


# Defaults at user creation by role: `member` -> `people`/`people`, `admin`/`super` ->
# `open`/`any`.
_DEFAULT_MEMBER_POLICY = {"out": "people", "in": "people"}
_DEFAULT_ADMIN_POLICY = {"out": "open", "in": "any"}


def _default_policy(role: Role) -> dict[str, str]:
    return dict(_DEFAULT_MEMBER_POLICY if role == "member" else _DEFAULT_ADMIN_POLICY)


class User(BaseModel):
    model_config = ConfigDict(extra="ignore")

    uid: str
    alias: str
    displayName: str
    email: str | None = None
    # Sign-in number only, never an SMS route.
    phone: str | None = None
    role: Role = "member"
    # Every user belongs to one family; `None` only for externals.
    familyId: str | None = None
    kind: Kind = "person"
    # An external belongs to one family (`familyId` stays null so rules and `sameFam` hold).
    ownerFamilyId: str | None = None
    policy: Policy = Field(default_factory=lambda: Policy.model_validate(_DEFAULT_MEMBER_POLICY))
    notify: Notify = Field(default_factory=Notify)
    disabled: bool = False
    createdAt: datetime | None = None
    smsNumber: str | None = None
    # Set on an external standing for a subscribed Google Chat conversation: `{bridgeId,
    # conversationId, source, link, isGroup, title, canReply}`.
    chat: dict | None = None


class SmsNumberTaken(Exception):
    """`smsNumbers/{e164}` is held by another uid (`holder_uid`)."""

    def __init__(self, holder_uid: str) -> None:
        super().__init__(f"sms number already assigned to {holder_uid!r}")
        self.holder_uid = holder_uid


class AliasTaken(Exception):
    """`alias` is already registered to a different (or, on create, the same) uid."""


class InvalidAlias(Exception):
    pass


def _users() -> object:
    return get_db().collection("users")


def _aliases() -> object:
    return get_db().collection("aliases")


def _validate_alias(alias: str) -> None:
    if alias in RESERVED_ALIASES or not ALIAS_RE.match(alias):
        raise InvalidAlias(f"invalid or reserved alias: {alias!r}")


def create_user(
    *,
    uid: str,
    alias: str,
    display_name: str,
    email: str | None = None,
    phone: str | None = None,
    role: Role = "member",
    family_id: str | None = None,
    kind: Kind = "person",
    owner_family_id: str | None = None,
    chat: dict | None = None,
) -> User:
    """Creates the user and alias atomically (`AliasTaken` if taken); policy defaults by `role`."""
    _validate_alias(alias)
    if kind == "person" and family_id is None:
        raise ValueError("a person must have a familyId")
    if kind == "external" and family_id is not None:
        raise ValueError("an external must not have a familyId")
    db = get_db()
    user_ref = db.collection("users").document(uid)
    alias_ref = db.collection("aliases").document(alias)

    def _txn(transaction: Transaction) -> None:
        transaction.create(alias_ref, {"uid": uid})
        transaction.create(
            user_ref,
            {
                "alias": alias,
                "displayName": display_name,
                "email": email,
                "phone": phone,
                "role": role,
                "familyId": family_id,
                "kind": kind,
                "ownerFamilyId": owner_family_id,
                "policy": _default_policy(role),
                "notify": {"alerts": True},
                "disabled": False,
                "createdAt": SERVER_TIMESTAMP,
                **({"chat": chat} if chat is not None else {}),
            },
        )

    try:
        run_transaction(_txn)
    except AlreadyExists as exc:
        raise AliasTaken(f"alias already registered: {alias!r}") from exc

    # Every non-external user gets an implicit webapp backend, outside the uid/alias transaction
    # so a failed backend write does not roll back the user. An external's only backend is `sms`.
    if kind == "person":
        backends_store.create_backend(uid, kind="webapp", config={}, enabled=True)

    fetched = get_user(uid)
    assert fetched is not None
    return fetched


def get_user(uid: str) -> User | None:
    snap = get_db().collection("users").document(uid).get()
    if not snap.exists:
        return None
    return User.model_validate({"uid": uid, **(snap.to_dict() or {})})


def get_uid_for_alias(alias: str) -> str | None:
    snap = get_db().collection("aliases").document(alias).get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    return data.get("uid")


def get_user_by_alias(alias: str) -> User | None:
    uid = get_uid_for_alias(alias)
    if uid is None:
        return None
    return get_user(uid)


def list_users() -> list[User]:
    return [
        User.model_validate({"uid": snap.id, **(snap.to_dict() or {})})
        for snap in get_db().collection("users").stream()
    ]


def update_user(
    uid: str,
    *,
    display_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    role: Role | None = None,
    disabled: bool | None = None,
    notify_alerts: bool | None = None,
) -> User:
    """Patch-semantics update of mutable fields; `alias` is not editable here."""
    updates: dict[str, object] = {}
    if display_name is not None:
        updates["displayName"] = display_name
    if email is not None:
        updates["email"] = email
    if phone is not None:
        updates["phone"] = phone
    if role is not None:
        updates["role"] = role
    if disabled is not None:
        updates["disabled"] = disabled
    if notify_alerts is not None:
        updates["notify"] = {"alerts": notify_alerts}
    ref = get_db().collection("users").document(uid)
    if updates:
        try:
            ref.update(updates)
        except NotFound as exc:
            raise KeyError(f"no such user: {uid!r}") from exc
    fetched = get_user(uid)
    if fetched is None:
        raise KeyError(f"no such user: {uid!r}")
    return fetched


def _sms_numbers():
    return get_db().collection("smsNumbers")


def get_uid_for_sms_number(e164: str) -> str | None:
    """The uid `smsNumbers/{e164}` names, or `None`."""
    snap = _sms_numbers().document(e164).get()
    if not snap.exists:
        return None
    return (snap.to_dict() or {}).get("uid")


def _free_sms_index(e164: str, uid: str) -> None:
    ref = _sms_numbers().document(e164)
    snap = ref.get()
    if snap.exists and (snap.to_dict() or {}).get("uid") == uid:
        ref.delete()


def set_sms_number(uid: str, e164: str | None) -> User:
    """Sets or clears `smsNumber`, reserving the index first; `SmsNumberTaken` if held."""
    user = get_user(uid)
    if user is None:
        raise KeyError(f"no such user: {uid!r}")
    old = user.smsNumber
    # Known race, accepted at family volume: two admins assigning the same
    # number in the same second can both pass the reservation below.
    if e164 is not None:
        ref = _sms_numbers().document(e164)
        try:
            ref.create({"uid": uid})
        except AlreadyExists:
            holder = (ref.get().to_dict() or {}).get("uid")
            if holder != uid:
                holder_user = get_user(holder) if holder else None
                if holder_user is not None and holder_user.smsNumber == e164:
                    raise SmsNumberTaken(holder) from None
                ref.set({"uid": uid})
    get_db().collection("users").document(uid).update({"smsNumber": e164})
    if old is not None and old != e164:
        _free_sms_index(old, uid)
    fetched = get_user(uid)
    assert fetched is not None
    return fetched


def delete_user(uid: str) -> None:
    """Deletes `users/{uid}` and its alias; no cascade to backends, devices, messages or edges."""
    db = get_db()
    user = get_user(uid)
    user_ref = db.collection("users").document(uid)

    if user is not None:
        alias_ref = db.collection("aliases").document(user.alias)

        def _txn(transaction: Transaction) -> None:
            transaction.delete(alias_ref)
            transaction.delete(user_ref)

        run_transaction(_txn)
        if user.smsNumber:
            _free_sms_index(user.smsNumber, uid)
    else:
        user_ref.delete()
