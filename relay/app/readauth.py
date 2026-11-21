"""`relay/firestore.rules` read rules in Python, for the home SSE live stream.

One function per `match` block; an error inside a rule is a deny, as in Firestore.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.auth import Principal
from app.db.firestore import get_db

Data = Mapping[str, Any] | None


class _Deny(Exception):
    """A rule expression that errors; the request is denied."""


def _field(data: Data, name: str) -> Any:
    """`resource.data.<name>` spelled bare: errors when absent."""
    if data is None or name not in data:
        raise _Deny(name)
    return data[name]


def _getf(data: Data, name: str, default: Any) -> Any:
    """`resource.data.get(name, default)`; still errors on a null resource."""
    if data is None:
        raise _Deny(name)
    return data.get(name, default)


def _in(value: Any, container: Any) -> bool:
    if not isinstance(container, (list, tuple)):
        raise _Deny("in")
    return value in container


def _parent_device(d: str) -> Data:
    """`get(/devices/$(d)).data`; errors when the device does not exist."""
    snap = get_db().collection("devices").document(d).get()
    if not snap.exists:
        raise _Deny(f"devices/{d}")
    return snap.to_dict()


def _auth(p: Principal | None) -> Principal:
    """`request.auth` dereferenced: errors when unauthenticated."""
    if p is None:
        raise _Deny("request.auth")
    return p


def _auth_uid(p: Principal | None) -> str:
    return _auth(p).uid


def registered(p: Principal | None) -> bool:
    return get_db().collection("users").document(_auth_uid(p)).get().exists


def role(p: Principal | None) -> str:
    """`request.auth.token.get('role', '')`."""
    return _auth(p).role or ""


def fam(p: Principal | None) -> str:
    """`request.auth.token.get('fam', '')`."""
    return _auth(p).family_id or ""


def isSuper(p: Principal) -> bool:
    return role(p) == "super"


def isFamAdmin(p: Principal, f: Any) -> bool:
    return role(p) == "admin" and fam(p) == f


def sameFam(p: Principal, f: Any) -> bool:
    return fam(p) != "" and fam(p) == f


# Operands are thunks so `_or`/`_and` follow Firestore's error semantics
# (`error || true` is true, `error && false` is false).
def _or(*terms: Callable[[], bool]) -> bool:
    err = False
    for term in terms:
        try:
            if term():
                return True
        except _Deny:
            err = True
    if err:
        raise _Deny("||")
    return False


def _and(*terms: Callable[[], bool]) -> bool:
    err = False
    for term in terms:
        try:
            if not term():
                return False
        except _Deny:
            err = True
    if err:
        raise _Deny("&&")
    return True


def _authed(p: Principal | None) -> bool:
    """`request.auth != null`."""
    return p is not None


def families(p: Principal | None, f: str, data: Data) -> bool:
    return _and(
        lambda: _authed(p), lambda: _or(lambda: isSuper(p), lambda: sameFam(p, f))
    )


def alerts(p: Principal | None, f: str, a: str, data: Data) -> bool:
    return _and(
        lambda: _authed(p), lambda: _or(lambda: isSuper(p), lambda: isFamAdmin(p, f))
    )


def users(p: Principal | None, uid: str, data: Data) -> bool:
    return _and(
        lambda: _authed(p),
        lambda: _or(
            lambda: isSuper(p),
            lambda: _auth_uid(p) == uid,
            lambda: sameFam(p, _getf(data, "familyId", None)),
        ),
    )


def backends(p: Principal | None, uid: str, b: str, data: Data) -> bool:
    return _or(lambda: _auth_uid(p) == uid, lambda: isSuper(_auth(p)))


def book(p: Principal | None, uid: str, peer: str, data: Data) -> bool:
    return _and(
        lambda: _authed(p),
        lambda: _or(
            lambda: isSuper(p),
            lambda: _auth_uid(p) == uid,
            lambda: isFamAdmin(p, _getf(data, "familyId", None)),
        ),
    )


def messages(p: Principal | None, id_: str, data: Data) -> bool:
    return _and(
        lambda: registered(p),
        lambda: _or(
            lambda: isSuper(p),
            lambda: _in(_auth_uid(p), _field(data, "uids")),
            lambda: _and(
                lambda: role(p) == "admin",
                lambda: _in(fam(p), _getf(data, "familyIds", [])),
            ),
        ),
    )


def conversations(p: Principal | None, k: str, data: Data) -> bool:
    return _and(
        lambda: registered(p),
        lambda: _or(
            lambda: isSuper(p),
            lambda: _in(_auth_uid(p), _field(data, "uids")),
            lambda: _and(
                lambda: role(p) == "admin",
                lambda: _in(fam(p), _getf(data, "familyIds", [])),
            ),
        ),
    )


def devices(p: Principal | None, d: str, data: Data) -> bool:
    return _and(
        lambda: _authed(p),
        lambda: _or(
            lambda: isSuper(p),
            lambda: _and(
                lambda: sameFam(p, _getf(data, "familyId", None)),
                lambda: _or(
                    lambda: _field(data, "ownerUid") == _auth_uid(p),
                    lambda: _in(_auth_uid(p), _getf(data, "locatableBy", [])),
                    lambda: role(p) == "admin",
                ),
            ),
        ),
    )


def locations(p: Principal | None, d: str, l_: str, data: Data) -> bool:
    return _and(
        lambda: _authed(p),
        lambda: _or(
            lambda: isSuper(p),
            lambda: _and(
                lambda: sameFam(p, _getf(_parent_device(d), "familyId", None)),
                lambda: _or(
                    lambda: _field(_parent_device(d), "ownerUid") == _auth_uid(p),
                    lambda: _in(
                        _auth_uid(p), _getf(_parent_device(d), "locatableBy", [])
                    ),
                    lambda: role(p) == "admin",
                ),
            ),
        ),
    )


def sms_log(p: Principal | None, d: str, l_: str, data: Data) -> bool:
    return _and(
        lambda: _authed(p),
        lambda: _or(
            lambda: isSuper(p),
            lambda: _field(_parent_device(d), "ownerUid") == _auth_uid(p),
            lambda: isFamAdmin(p, _getf(_parent_device(d), "familyId", None)),
        ),
    )


def allow(p: Principal | None, e: str, data: Data) -> bool:
    return _and(
        lambda: _authed(p),
        lambda: _or(
            lambda: isSuper(p),
            lambda: _in(_auth_uid(p), [_field(data, "fromUid"), _field(data, "toUid")]),
            lambda: _and(
                lambda: role(p) == "admin",
                lambda: _in(fam(p), _getf(data, "familyIds", [])),
            ),
        ),
    )


def contact_requests(p: Principal | None, r: str, data: Data) -> bool:
    return _and(
        lambda: _authed(p),
        lambda: _or(
            lambda: isSuper(p),
            lambda: _auth_uid(p) == _field(data, "ownerUid"),
            lambda: isFamAdmin(p, _getf(data, "familyId", None)),
        ),
    )


# `settings/federation` is server-only; other fed collections have no readable path.
_SERVER_ONLY_SETTINGS = {"federation"}


def settings(p: Principal | None, s: str, data: Data) -> bool:
    return s not in _SERVER_ONLY_SETTINGS and registered(p)


# `match` block by path shape (`*` = wildcard); anything absent is default-deny.
_READ: dict[tuple[str, ...], Callable[..., bool]] = {
    ("families", "*"): families,
    ("families", "*", "alerts", "*"): alerts,
    ("users", "*"): users,
    ("users", "*", "backends", "*"): backends,
    ("users", "*", "book", "*"): book,
    ("messages", "*"): messages,
    ("conversations", "*"): conversations,
    ("devices", "*"): devices,
    ("devices", "*", "locations", "*"): locations,
    ("devices", "*", "smsLog", "*"): sms_log,
    ("allow", "*"): allow,
    ("contactRequests", "*"): contact_requests,
    ("settings", "*"): settings,
}


def _shape(segs: list[str]) -> tuple[str, ...]:
    return tuple("*" if i % 2 else s for i, s in enumerate(segs))


def can_read(principal: Principal | None, path: str, data: Data) -> bool:
    """`allow read` for the document at `path` holding `data` (None when absent)."""
    segs = path.split("/")
    if len(segs) % 2 or not all(segs):
        return False
    rule = _READ.get(_shape(segs))
    if rule is None:
        return False
    try:
        return bool(rule(principal, *segs[1::2], data))
    except _Deny:
        return False


def can_write(principal: Principal | None, path: str) -> bool:
    """`match /{document=**} { allow write: if false; }`."""
    return False


class _Filters:
    """What a query's declared filters prove about every result document."""

    def __init__(self, where: list[Any]) -> None:
        self.eq: dict[str, Any] = {}
        self.contains: dict[str, list[Any]] = {}
        for field, op, value in where:
            if op == "==":
                self.eq.setdefault(field, value)
            elif op == "array-contains":
                self.contains.setdefault(field, []).append(value)
            else:
                raise _Deny(op)

    def eq_is(self, field: str, value: Any) -> bool:
        """`resource.data.<field> == value` is provable."""
        return field in self.eq and self.eq[field] == value

    def has(self, field: str, value: Any) -> bool:
        """`value in resource.data.<field>` is provable."""
        return value in self.contains.get(field, [])


def _list_families(p: Principal, w: _Filters) -> bool:
    # sameFam(f) names the document id, which no filter binds.
    return isSuper(p)


def _list_alerts(p: Principal, w: _Filters, f: str) -> bool:
    return alerts(p, f, "", None)


def _list_users(p: Principal, w: _Filters) -> bool:
    # `request.auth.uid == uid` binds the document id: not provable.
    return isSuper(p) or ("familyId" in w.eq and sameFam(p, w.eq["familyId"]))


def _list_backends(p: Principal, w: _Filters, uid: str) -> bool:
    return backends(p, uid, "", None)


def _list_book(p: Principal, w: _Filters, uid: str) -> bool:
    return (
        isSuper(p)
        or p.uid == uid
        or ("familyId" in w.eq and isFamAdmin(p, w.eq["familyId"]))
    )


def _list_messages(p: Principal, w: _Filters) -> bool:
    return registered(p) and (
        isSuper(p)
        or w.has("uids", p.uid)
        or (role(p) == "admin" and w.has("familyIds", fam(p)))
    )


_list_conversations = _list_messages


def _list_devices(p: Principal, w: _Filters) -> bool:
    return isSuper(p) or (
        "familyId" in w.eq
        and sameFam(p, w.eq["familyId"])
        and (
            w.eq_is("ownerUid", p.uid)
            or w.has("locatableBy", p.uid)
            or role(p) == "admin"
        )
    )


def _list_locations(p: Principal, w: _Filters, d: str) -> bool:
    return locations(p, d, "", None)


def _list_sms_log(p: Principal, w: _Filters, d: str) -> bool:
    return sms_log(p, d, "", None)


def _list_allow(p: Principal, w: _Filters) -> bool:
    return (
        isSuper(p)
        or w.eq_is("fromUid", p.uid)
        or w.eq_is("toUid", p.uid)
        or (role(p) == "admin" and w.has("familyIds", fam(p)))
    )


def _list_contact_requests(p: Principal, w: _Filters) -> bool:
    return (
        isSuper(p)
        or w.eq_is("ownerUid", p.uid)
        or ("familyId" in w.eq and isFamAdmin(p, w.eq["familyId"]))
    )


def _list_settings(p: Principal, w: _Filters) -> bool:
    # A list rule cannot name the document id, so a collection query would return
    # `settings/federation`: denied.
    return False


_LIST: dict[tuple[str, ...], Callable[..., bool]] = {
    ("families",): _list_families,
    ("families", "*", "alerts"): _list_alerts,
    ("users",): _list_users,
    ("users", "*", "backends"): _list_backends,
    ("users", "*", "book"): _list_book,
    ("messages",): _list_messages,
    ("conversations",): _list_conversations,
    ("devices",): _list_devices,
    ("devices", "*", "locations"): _list_locations,
    ("devices", "*", "smsLog"): _list_sms_log,
    ("allow",): _list_allow,
    ("contactRequests",): _list_contact_requests,
    ("settings",): _list_settings,
}


def query_admissible(principal: Principal | None, query: Mapping[str, Any]) -> bool:
    """Whether the collection's rule is provable from the query's filters alone."""
    if principal is None:
        return False  # every rule needs `request.auth`
    if "doc" in query:
        path = query["doc"]
        if not isinstance(path, str):
            return False
        segs = path.split("/")
        if len(segs) % 2 or not all(segs):
            return False
        snap = get_db().collection("/".join(segs[:-1])).document(segs[-1]).get()
        data = snap.to_dict() if snap.exists else None
        return can_read(principal, path, data)
    path = query.get("collection")
    if not isinstance(path, str):
        return False
    if query.get("group"):
        # No recursive-wildcard read rule exists, so collection-group queries are refused.
        return False
    segs = path.split("/")
    if len(segs) % 2 == 0 or not all(segs):
        return False
    rule = _LIST.get(_shape(segs))
    if rule is None:
        return False
    try:
        return bool(rule(principal, _Filters(query.get("where") or []), *segs[1::2]))
    except (_Deny, ValueError, TypeError):
        return False
