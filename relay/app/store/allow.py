
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db
from app.store import devices as devices_store


class AllowEdge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fromUid: str
    toUid: str
    message: bool = False
    locate: bool = False


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


    db = get_db()
    incoming = {(e.from_uid, e.to_uid) for e in edges}

    batch = db.batch()
    for from_uid, to_uid in existing - incoming:
        batch.delete(_allow().document(edge_id(from_uid, to_uid)))
    for e in edges:
        batch.set(
            _allow().document(edge_id(e.from_uid, e.to_uid)),
        )
    batch.commit()

    affected_to_uids = {to for _, to in existing | incoming}
    for to_uid in affected_to_uids:
        _recompute_locatable_by(to_uid)

    return list_edges()
