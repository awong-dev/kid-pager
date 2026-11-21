"""Cascading deletes for device, user, bridge and family, driven by the `app/datamap.py` rows."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from google.cloud.firestore import DELETE_FIELD, ArrayRemove, FieldFilter

from app import datamap
from app.broker_admin import BrokerAdmin
from app.db.firestore import get_db
from app.store import fed_links

logger = logging.getLogger("relay.purge")


@dataclass
class PurgeReport:
    """Documents deleted per collection name, and what the broker said."""

    deleted: Counter[str] = field(default_factory=Counter)
    broker: dict[str, str] = field(default_factory=dict)

    def merge(self, other: PurgeReport) -> None:
        self.deleted.update(other.deleted)
        self.broker.update(other.broker)

    @property
    def total(self) -> int:
        return sum(self.deleted.values())


def is_frozen(family_id: str) -> bool:
    """`families/{fid}.frozen`."""
    snap = get_db().collection("families").document(family_id).get()
    return bool(snap.exists and (snap.to_dict() or {}).get("frozen") is True)


def _children(parent: str) -> list[datamap.Coll]:
    return [c for c in datamap.REGISTRY if c.parent == parent]


def _delete_doc(ref, report: PurgeReport) -> None:
    """Delete one document and, depth first, every registered subcollection under it."""
    for child in _children(ref.parent.id):
        for snap in ref.collection(child.name).stream():
            _delete_doc(snap.reference, report)
    ref.delete()
    report.deleted[ref.parent.id] += 1


def _query(coll: datamap.Coll, field_name: str, op: str, value):
    db = get_db()
    base = db.collection_group(coll.name) if coll.parent else db.collection(coll.name)
    return base.where(filter=FieldFilter(field_name, op, value)).stream()


def _purgeable_rows() -> list[datamap.Coll]:
    """Registry rows a purge may touch: all but global rows and `users` itself."""
    return [c for c in datamap.REGISTRY if c.family != "global"]


def purge_device(device_id: str, emqx: BrokerAdmin | None = None) -> PurgeReport:
    report = PurgeReport()
    db = get_db()
    dev_ref = db.collection("devices").document(device_id)
    snap = dev_ref.get()
    username = (
        (snap.to_dict() or {}).get("mqttUsername") if snap.exists else None
    ) or device_id

    for coll in datamap.REGISTRY:
        for link in coll.links:
            kind, _, where = link.partition(":")
            if kind != "device":
                continue
            if where == "@id":
                ref = db.collection(coll.name).document(device_id)
                if ref.get().exists:
                    _delete_doc(ref, report)
            else:
                for s in _query(coll, where, "==", device_id):
                    _delete_doc(s.reference, report)

    # users/*/backends rows of kind "pager" naming this device.
    for s in (
        db.collection_group("backends")
        .where(filter=FieldFilter("kind", "==", "pager"))
        .stream()
    ):
        if ((s.to_dict() or {}).get("config") or {}).get("deviceId") == device_id:
            _delete_doc(s.reference, report)

    # messages stay but must stop targeting the device.
    for s in (
        db.collection("messages")
        .where(filter=FieldFilter("pendingDeviceIds", "array_contains", device_id))
        .stream()
    ):
        s.reference.update({"pendingDeviceIds": ArrayRemove([device_id])})

    if snap.exists:
        _delete_doc(dev_ref, report)

    if emqx is not None:
        try:
            result = emqx.delete_user(username)
        except Exception:
            logger.exception("broker user delete failed for device %s", device_id)
            result = "error"
        report.broker[device_id] = result
        if result == "error":
            logger.warning(
                "broker credential for device %s may remain (delete reported an error)",
                device_id,
            )
    return report


def purge_bridge(bridge_id: str) -> PurgeReport:
    report = PurgeReport()
    db = get_db()
    for coll in datamap.REGISTRY:
        for link in coll.links:
            kind, _, where = link.partition(":")
            if kind == "bridge" and where != "@id":
                for s in _query(coll, where, "==", bridge_id):
                    _delete_doc(s.reference, report)
    ref = db.collection("bridges").document(bridge_id)
    if ref.get().exists:
        _delete_doc(ref, report)
    return report


def _matching_uid(coll: datamap.Coll, uid: str):
    """Documents of `coll` naming `uid` in any `uid_fields` entry, scalar or list."""
    seen: set[str] = set()
    for fname in coll.uid_fields:
        for op in ("==", "array_contains"):
            for s in _query(coll, fname, op, uid):
                if s.reference.path in seen:
                    continue
                seen.add(s.reference.path)
                yield s


def _strip_from_group_conversation(snap, uid: str) -> bool:
    """Remove `uid` from a group conversation; False when fewer than two members remain."""
    data = snap.to_dict() or {}
    remaining = [u for u in data.get("uids", []) if u != uid]
    if len(remaining) < 2:
        return False
    snap.reference.update({"uids": remaining, f"participants.{uid}": DELETE_FIELD})
    return True


def purge_user(uid: str, emqx: BrokerAdmin | None = None) -> PurgeReport:
    report = PurgeReport()
    db = get_db()

    for coll in _purgeable_rows():
        if coll.name == "users" or not coll.uid_fields:
            continue
        for s in list(_matching_uid(coll, uid)):
            data = s.to_dict() or {}
            if coll.name == "devices":
                if data.get("ownerUid") == uid:
                    report.merge(purge_device(s.id, emqx))
                else:  # `defaultToUid` only: the device stays, minus the reference
                    s.reference.update({"defaultToUid": DELETE_FIELD})
                continue
            if coll.name == "bridges":
                report.merge(purge_bridge(s.id))
                continue
            if coll.name == "conversations" and _strip_from_group_conversation(s, uid):
                continue
            if coll.name == "messages":
                wire_id, recipient = data.get("wireId"), data.get("recipientUid")
                if wire_id and recipient:
                    # the dedup marker has no uid field
                    ref = db.collection("wireIds").document(f"{wire_id}_{recipient}")
                    if ref.get().exists:
                        _delete_doc(ref, report)
            _delete_doc(s.reference, report)

    # wireIds ids embed the uid; remove any left behind.
    for coll in datamap.REGISTRY:
        if coll.id_embeds_uid and coll.name == "wireIds":
            for s in db.collection("wireIds").stream():
                if s.id.endswith(f"_{uid}"):
                    _delete_doc(s.reference, report)

    fed_links.remove_shared_uid(uid)

    user_ref = db.collection("users").document(uid)
    if user_ref.get().exists:
        _delete_doc(user_ref, report)
    return report


def purge_family(family_id: str, emqx: BrokerAdmin | None = None) -> PurgeReport:
    report = PurgeReport()
    db = get_db()

    # 1. owners first; externals carry `ownerFamilyId` instead of `familyId`.
    uids: set[str] = set()
    for fname in ("familyId", "ownerFamilyId"):
        uids.update(
            s.id
            for s in db.collection("users")
            .where(filter=FieldFilter(fname, "==", family_id))
            .stream()
        )
    device_ids = [
        s.id
        for s in db.collection("devices")
        .where(filter=FieldFilter("familyId", "==", family_id))
        .stream()
    ]
    bridge_ids = [
        s.id
        for s in db.collection("bridges")
        .where(filter=FieldFilter("familyId", "==", family_id))
        .stream()
    ]
    for did in device_ids:
        report.merge(purge_device(did, emqx))
    for bid in bridge_ids:
        report.merge(purge_bridge(bid))
    for uid in sorted(uids):
        report.merge(purge_user(uid, emqx))

    # 2. rows still scoped to the family by a field.
    for coll in _purgeable_rows():
        if coll.parent or coll.family not in (
            "field:familyId",
            "field:familyIds",
            "field:ownerFamilyId",
        ):
            continue
        fname = coll.family.split(":", 1)[1]
        op = "array_contains" if fname == "familyIds" else "=="
        for s in list(_query(coll, fname, op, family_id)):
            ids = (s.to_dict() or {}).get("familyIds")
            if (
                fname == "familyIds"
                and isinstance(ids, list)
                and len(set(ids) - {family_id}) > 0
            ):
                s.reference.update({"familyIds": ArrayRemove([family_id])})
                continue
            _delete_doc(s.reference, report)

    # 3. the family document and its subcollections.
    ref = db.collection("families").document(family_id)
    if ref.get().exists:
        _delete_doc(ref, report)
    return report
