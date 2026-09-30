"""`allow/{fromUid}_{toUid}` directed edges: `message` gates routing, `locate` gates /locate."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db
from app.store import devices as devices_store
from app.store import users as users_store


class AllowEdge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fromUid: str
    toUid: str
    message: bool = False
    locate: bool = False
    # Non-null `familyId`s of both ends, deduped and sorted; empty on edges that predate it.
    familyIds: list[str] = []


class LocateCrossFamily(ValueError):
    """A `locate: true` edge across families; `str(exc) == "locate_cross_family"`."""


def _family_id(uid: str) -> str | None:
    user = users_store.get_user(uid)
    return user.familyId if user is not None else None


def family_ids_for(from_uid: str, to_uid: str) -> list[str]:
    """The non-null `familyId`s of both ends of an edge, deduped and sorted."""
    ids = {fid for fid in (_family_id(from_uid), _family_id(to_uid)) if fid is not None}
    return sorted(ids)


def check_locate_family(from_uid: str, to_uid: str, locate: bool) -> None:
    """Raises `LocateCrossFamily` if `locate` is true and the ends do not share a `familyId`."""
    if not locate:
        return
    from_fam = _family_id(from_uid)
    to_fam = _family_id(to_uid)
    if from_fam is None or to_fam is None or from_fam != to_fam:
        raise LocateCrossFamily("locate_cross_family")


@dataclass(frozen=True, slots=True)
class EdgeInput:
    from_uid: str
    to_uid: str
    message: bool = True
    locate: bool = True


def edge_id(from_uid: str, to_uid: str) -> str:
    return f"{from_uid}_{to_uid}"


def _allow():
    return get_db().collection("allow")


def get_edge(from_uid: str, to_uid: str) -> AllowEdge | None:
    snap = _allow().document(edge_id(from_uid, to_uid)).get()
    if not snap.exists:
        return None
    return AllowEdge.model_validate(snap.to_dict() or {})


def list_edges() -> list[AllowEdge]:
    return [AllowEdge.model_validate(snap.to_dict() or {}) for snap in _allow().stream()]


def is_message_allowed(from_uid: str, to_uid: str) -> bool:
    edge = get_edge(from_uid, to_uid)
    return edge is not None and edge.message


def allowed_recipients(from_uid: str) -> list[str]:
    """Every `toUid` with a `message` edge from `from_uid`."""
    return sorted(e.toUid for e in list_edges() if e.fromUid == from_uid and e.message)


def set_edge(from_uid: str, to_uid: str, *, message: bool, locate: bool) -> AllowEdge:
    """Upserts one edge; callers passing `locate=True` must call `check_locate_family` first."""
    _allow().document(edge_id(from_uid, to_uid)).set(
        {
            "fromUid": from_uid,
            "toUid": to_uid,
            "message": message,
            "locate": locate,
            "familyIds": family_ids_for(from_uid, to_uid),
        }
    )
    _recompute_locatable_by(to_uid)
    edge = get_edge(from_uid, to_uid)
    assert edge is not None
    return edge


def delete_edge(from_uid: str, to_uid: str) -> None:
    _allow().document(edge_id(from_uid, to_uid)).delete()
    _recompute_locatable_by(to_uid)


def recompute_locatable_by_for_owner(uid: str) -> None:
    """Recomputes `locatableBy` on `to_uid`'s devices, e.g. one created after its edges."""
    _recompute_locatable_by(uid)


def _recompute_locatable_by(to_uid: str) -> None:
    """`locatableBy` on each device owned by `to_uid` = every `fromUid` with a `locate` edge."""
    locators = sorted(
        e.fromUid
        for e in list_edges()
        if e.toUid == to_uid and e.locate
    )
    for device in devices_store.list_devices(owner_uid=to_uid):
        devices_store.set_locatable_by(device.id, locators)


def replace_all(edges: list[EdgeInput], *, family_id: str | None = None) -> list[AllowEdge]:
    """Replaces all edges (or those touching `family_id`); checks every edge before any write."""
    for e in edges:
        check_locate_family(e.from_uid, e.to_uid, e.locate)

    db = get_db()
    all_existing = list_edges()
    if family_id is None:
        existing = {(e.fromUid, e.toUid) for e in all_existing}
    else:
        existing = {
            (e.fromUid, e.toUid)
            for e in all_existing
            if family_id in (_family_id(e.fromUid), _family_id(e.toUid))
        }
    incoming = {(e.from_uid, e.to_uid) for e in edges}

    batch = db.batch()
    for from_uid, to_uid in existing - incoming:
        batch.delete(_allow().document(edge_id(from_uid, to_uid)))
    for e in edges:
        batch.set(
            _allow().document(edge_id(e.from_uid, e.to_uid)),
            {
                "fromUid": e.from_uid,
                "toUid": e.to_uid,
                "message": e.message,
                "locate": e.locate,
                "familyIds": family_ids_for(e.from_uid, e.to_uid),
            },
        )
    batch.commit()

    affected_to_uids = {to for _, to in existing | incoming}
    for to_uid in affected_to_uids:
        _recompute_locatable_by(to_uid)

    return list_edges()
