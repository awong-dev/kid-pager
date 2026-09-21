"""Device location (docs/PROTOCOL.md §13): `/loc` ingest, deduped by `locWireIds/{deviceId}_{id}` in

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from google.cloud.firestore import (
    SERVER_TIMESTAMP,
    ArrayRemove,
    ArrayUnion,
    DocumentReference,
    DocumentSnapshot,
    FieldFilter,
    Transaction,
)

from app import cellgeo
from app.db.firestore import get_db, run_transaction
from app.ids import new_id
from app.routing import Routing
from app.store import backends as backends_store
from app.store import devices as devices_store
from app.store import locations as locations_store
from app.store import messages as messages_store
from app.store.backends import Backend as BackendRow
from app.store.locations import LocationFix
from app.store.messages import Message
from app.wire import CellInfo, LocEnvelope, LocFix, resolve_ts

logger = logging.getLogger("relay.location")

# docs/PROTOCOL.md §13.4: the expiry is 15 minutes after creation.
DEFAULT_LOC_REQ_TTL_S = 15 * 60
# docs/PROTOCOL.md §13.3 rule 6: a fix younger than this answers a `/locate` without wire traffic.
CACHED_ANSWER_WINDOW_S = 60


def loc_req_ttl_s() -> int:
    """15 minutes in production (docs/PROTOCOL.md §13.4); `LOC_REQ_TTL_S` overrides it, read on every call."""
    return int(os.environ.get("LOC_REQ_TTL_S", str(DEFAULT_LOC_REQ_TTL_S)))


def _age_seconds(dt: datetime | None) -> float:
    """`inf` for no timestamp, so a missing `createdAt` counts as infinitely stale."""
    if dt is None:
        return float("inf")
    return (datetime.now(UTC) - dt).total_seconds()


def _loc_reqs():
    return get_db().collection("locReqs")


def _loc_wire_ids():
    return get_db().collection("locWireIds")


def _fix_doc(env: LocEnvelope, loc: LocFix) -> dict:
    return {
        "ts": env.ts,
        "fixTs": loc.fix_ts,
        "lat": loc.lat,
        "lon": loc.lon,
        "accM": loc.acc,
        "src": loc.src,
        "cached": env.cached,
        "reqId": env.req,
        "createdAt": SERVER_TIMESTAMP,
    }


def _resolve_cell_fallback(cell: CellInfo, fix_ts: int) -> LocFix | None:
    """Asks `app/cellgeo.py` to turn a `cell` into a position, called only when there is no GNSS fix
    (docs/PROTOCOL.md §13.2). `fix_ts` is the envelope's `ts`, or the receive time when 0. Returns
    `None` when resolution fails; nothing here may turn into a 500 on `POST /webhooks/mqtt`."""
    try:
        resolved = cellgeo.resolve(cell)
    except Exception as exc:  # noqa: BLE001 -- must never break /webhooks/mqtt ingest
        resolved = None
    if resolved is None:
        return None
    try:
        return LocFix(lat=resolved.lat, lon=resolved.lon, acc=resolved.acc_m, fix_ts=fix_ts, src="cell")
    except Exception as exc:  # noqa: BLE001 -- a malformed provider response must not break ingest
        return None


def ingest_loc(device_id: str, env: LocEnvelope) -> None:
    """Dedup on `id`, store the fix (if any) and resolve the matching `loc_req`, all in one
    transaction (docs/PROTOCOL.md §13.2). Split across two, a failure after the first commit would
    make the redelivered QoS-1 message dedup away and leave the `loc_req` stuck `sent`.

    `_resolve_cell_fallback` (an HTTP call, so before the transaction) may produce a `src:"cell"` fix
    treated like a real one; a GNSS fix wins. `req_ref`/`loc_field` are built only when
    `env.req is not None`.

    # The device lookup is not part of the transaction: an unknown device is dropped either way.
    # Needed when there is a `req` (for `owner_uid`) or a `cell` (for `set_last_cell`).
    owner_uid: str | None = None
    if env.req is not None or env.cell is not None:
        device = devices_store.get_device(device_id)
        if device is None:
            if env.req is not None:
                logger.warning("/loc req=%s for unknown device=%s dropped", env.req, device_id)
        else:
            owner_uid = device.ownerUid

    effective_loc = env.loc
    if env.cell is not None and owner_uid is not None:
        cell_fix_ts = resolve_ts(env.ts)
        if effective_loc is None:

    fix_ref = locations_store.new_location_ref(device_id) if effective_loc is not None else None
    fix_data = _fix_doc(env, effective_loc) if effective_loc is not None else None
    ttl = loc_req_ttl_s()

    def _txn(transaction: Transaction) -> None:
        # every read this transaction needs, before any write is staged
        fulfil: _FulfilPlan | None = None
        drop_reason: str | None = None
        delete_stale_req = False

        if req_ref is not None:
            req_snap = req_ref.get(transaction=transaction)
            if not req_snap.exists:
                # docs/PROTOCOL.md §13.4: a late /loc for an expired or already fulfilled request
                # (fulfilment deletes this doc) is logged and dropped.
                drop_reason = "no live loc_req"
            else:
                req_data = req_snap.to_dict() or {}
                loc_req_msg_id = req_data.get("messageId")
                if loc_req_msg_id != env.req:
                    drop_reason = f"does not match the live loc_req {loc_req_msg_id}"
                elif _age_seconds(req_data.get("createdAt")) >= ttl:
                    # The derived expiry (docs/PROTOCOL.md §13.4) beat this answer: drop it, never
                    # mark 'fulfilled', and delete the stale doc.
                    drop_reason = f"request already past its {ttl}s TTL"
                    delete_stale_req = True
                else:
                    loc_req_ref = messages_store.messages_ref(loc_req_msg_id)
                    loc_req_snap = loc_req_ref.get(transaction=transaction)
                    meta_ref = messages_store.meta_ref()
                    meta_snap = meta_ref.get(transaction=transaction)
                    conv_refs: dict[str, DocumentReference] = {}
                    conv_snaps: dict[str, DocumentSnapshot] = {}
                    for uid in requester_uids:
                        key = messages_store.conv_key(owner_uid, uid)
                        ref = messages_store.conversation_ref(key)
                        conv_refs[uid] = ref
                        conv_snaps[uid] = ref.get(transaction=transaction)
                    fulfil = _FulfilPlan(
                        loc_req_msg_id=loc_req_msg_id,
                        loc_req_ref=loc_req_ref,
                        loc_req_snap=loc_req_snap,
                        meta_ref=meta_ref,
                        meta_snap=meta_snap,
                        requester_uids=requester_uids,
                        conv_refs=conv_refs,
                        conv_snaps=conv_snaps,
                    )
        # -- every write from here on --


        # `transaction.create` raises AlreadyExists (caught below, outside the transaction) if this
        # `/loc` id was already processed.
        transaction.create(dedup_ref, {"deviceId": device_id, "createdAt": SERVER_TIMESTAMP})
            transaction.set(fix_ref, fix_data)

        if req_ref is None:
            return

        if drop_reason is not None:
            logger.info(
                "late /loc id=%s req=%s for device=%s: %s, dropped",
                env.id,
                env.req,
                device_id,
                drop_reason,
            )
            if delete_stale_req:
                transaction.delete(req_ref)
            return

        assert fulfil is not None
        assert owner_uid is not None
        _fulfil_loc_req_in_txn(
            transaction,
            device_id=device_id,
            owner_uid=owner_uid,
            env=env,
            loc_field=loc_field,
            req_ref=req_ref,
            plan=fulfil,
        )

    try:
        run_transaction(_txn)
    except AlreadyExists:
        logger.debug("duplicate /loc id=%s from device=%s dropped", env.id, device_id)
        return


def _loc_message_field(env: LocEnvelope, loc: LocFix | None) -> dict:
    """The `loc` field of the `kind='loc'` message posted to each requester: the fix's shape, or
    `{"err": ...}` when no position was available."""
    if loc is not None:
        return {
            "lat": loc.lat,
            "lon": loc.lon,
            "accM": loc.acc,
            "fixTs": loc.fix_ts,
            "src": loc.src,
            "cached": env.cached,
        }
    return {"err": env.err}


def _loc_preview(loc_field: dict) -> str:
    """`loc_field` is `_loc_message_field`'s output; a usable position (cell-resolved included) has no
    `err` key."""
    return "location" if "err" not in loc_field else f"location unavailable ({loc_field['err']})"


@dataclass(frozen=True, slots=True)
class _FulfilPlan:
    """Every read `ingest_loc`'s `_txn` needs to fulfil a `loc_req`, gathered in its read phase so
    the write phase only stages writes (Firestore requires every read before any write)."""

    loc_req_msg_id: str
    loc_req_ref: DocumentReference
    loc_req_snap: DocumentSnapshot
    meta_ref: DocumentReference
    meta_snap: DocumentSnapshot
    requester_uids: list[str]
    conv_refs: dict[str, DocumentReference]
    conv_snaps: dict[str, DocumentSnapshot]


def _fulfil_loc_req_in_txn(
    transaction: Transaction,
    *,
    device_id: str,
    owner_uid: str,
    env: LocEnvelope,
    loc_field: dict,
    req_ref: DocumentReference,
    plan: _FulfilPlan,
) -> None:
    """Write-only half of `loc_req` fulfilment, inside `ingest_loc`'s transaction: delete
    `locReqs/{deviceId}`, mark the `loc_req` delivery `fulfilled`, and add one `kind='loc'` message to
    each coalesced requester's thread with the owner (docs/PROTOCOL.md §13.3). All reads are in `plan`."""
    transaction.delete(req_ref)

    if plan.loc_req_snap.exists:
        loc_req_data = plan.loc_req_snap.to_dict() or {}
        loc_req_msg = Message.model_validate({"id": plan.loc_req_msg_id, **loc_req_data})
        bid = messages_store.find_pager_delivery(loc_req_msg, device_id)
        if bid is not None and loc_req_msg.deliveries[bid].state != "fulfilled":
            transaction.update(
                plan.loc_req_ref,
                {
                    f"deliveries.{bid}.state": "fulfilled",
                    "pendingDeviceIds": ArrayRemove([device_id]),
                },
            )

    seq = (plan.meta_snap.get("seqCounter") if plan.meta_snap.exists else 0) or 0
    for uid in plan.requester_uids:
        seq += 1
        key = messages_store.conv_key(owner_uid, uid)
        uids_sorted = sorted([owner_uid, uid])
        new_msg_ref = messages_store.messages_ref(new_id("m_"))
        transaction.set(
            new_msg_ref,
            {
                "seq": seq,
                "convKey": key,
                "uids": uids_sorted,
                "senderUid": owner_uid,
                "recipientUid": uid,
                "kind": "loc",
                "body": None,
                "loc": loc_field,
                "wireId": None,
                "originBackendKind": "pager",
                "originBackendId": None,
                "ts": env.ts,
                "createdAt": SERVER_TIMESTAMP,
                "deliveries": {},
                "pendingDeviceIds": [],
            },
        )
        conv_snap = plan.conv_snaps[uid]
        unread = dict(conv_snap.get("unread") or {}) if conv_snap.exists else {}
        unread[uid] = unread.get(uid, 0) + 1
        conv_data = {
            "uids": uids_sorted,
            "lastMessageAt": SERVER_TIMESTAMP,
            "lastPreview": _loc_preview(loc_field),
            "unread": unread,
        }
        if conv_snap.exists:
            transaction.update(plan.conv_refs[uid], conv_data)
        else:
            transaction.set(plan.conv_refs[uid], conv_data)

    if plan.meta_snap.exists:
        transaction.update(plan.meta_ref, {"seqCounter": seq})
    else:
        transaction.set(
            plan.meta_ref, {"schemaVersion": 2, "lastSweepAt": None, "seqCounter": seq}
        )


def effective_loc_req_state(msg: Message, delivery_state: str) -> str:
    """docs/PROTOCOL.md §13.4: a `loc_req` delivery still `sent` `loc_req_ttl_s()` after the message's
    `createdAt` is `expired`, derived at read time and never written."""
    if msg.kind != "loc_req" or delivery_state != "sent" or msg.createdAt is None:
        return delivery_state
    if _age_seconds(msg.createdAt) >= loc_req_ttl_s():
        return "expired"
    return delivery_state


def clear_stale_loc_reqs() -> int:
    """`/internal/tick`: delete every `locReqs/{deviceId}` doc older than `loc_req_ttl_s()` so a fresh
    `/locate` does not coalesce onto a dead request. Not transactional; a racing `/locate` re-reads
    the doc and decides independently."""
    ttl = loc_req_ttl_s()
    cutoff = datetime.fromtimestamp(time.time() - ttl, tz=UTC)
    cleared = 0
    for snap in _loc_reqs().where(filter=FieldFilter("createdAt", "<", cutoff)).stream():
        snap.reference.delete()
        cleared += 1
    return cleared


class NoLocatableDevice(Exception):
    """The target device is revoked or has no enabled `pager` backend to deliver a fresh `loc_req` to."""


@dataclass(frozen=True, slots=True)
class LocateOutcome:
    request_id: str | None
    cached: bool
    fix: LocationFix | None = None


def _find_pager_backend(device: devices_store.Device) -> BackendRow | None:
    for b in backends_store.list_backends(device.ownerUid):
        if b.enabled and b.kind == "pager" and b.config.get("deviceId") == device.id:
            return b
    return None


class Location:
    """Holds a `Routing` so `locate()` can deliver a new `loc_req` inline via `Routing.redeliver_pager`."""

    def __init__(self, routing: Routing) -> None:
        self._routing = routing

    def locate(
        self,
        *,
        requester_uid: str,
        device: devices_store.Device,
        ts: int | None = None,
        _retry_on_conflict: bool = True,
    ) -> LocateOutcome:
        """`_retry_on_conflict` is an internal recursion guard; callers never pass it."""
        if device.revokedAt is not None:
            raise NoLocatableDevice(device.id)
        ts = ts if ts is not None else int(time.time())
        req_ref = _loc_reqs().document(device.id)
        ttl = loc_req_ttl_s()
        # Looked up outside `_txn`: its `fn` must read only via `transaction.get(...)`.
        pager_backend = _find_pager_backend(device)

        def _txn(transaction: Transaction) -> tuple[str, str | None, LocationFix | None]:
            req_snap = req_ref.get(transaction=transaction)
            if req_snap.exists:
                req_data = req_snap.to_dict() or {}
                if _age_seconds(req_data.get("createdAt")) < ttl:
                    # docs/PROTOCOL.md §13.3 rule 5: coalesce onto the existing request.
                    requesters = list(req_data.get("requesterUids") or [])
                    if requester_uid not in requesters:
                        transaction.update(
                            req_ref, {"requesterUids": ArrayUnion([requester_uid])}
                        )
                    return ("coalesced", req_data.get("messageId"), None)

            # No live request to coalesce onto: docs/PROTOCOL.md §13.3 rule 6, a fix younger than
            # CACHED_ANSWER_WINDOW_S answers immediately with no wire trip.
            fix_query = (
                locations_store.locations_collection(device.id)
                .order_by("createdAt", direction="DESCENDING")
                .limit(1)
            )
            fix_snaps = fix_query.get(transaction=transaction)
            if fix_snaps:
                fix_snap = fix_snaps[0]
                fix_data = fix_snap.to_dict() or {}
                    if req_snap.exists:
                        # Stale locReqs row superseded by this cached answer
                        # -- clean it up now rather than waiting for tick.
                        transaction.delete(req_ref)
                    fix = LocationFix.model_validate({"id": fix_snap.id, **fix_data})
                    return ("cached", None, fix)

            # Neither a live request nor a fresh fix: claim the slot with a new device-targeted
            # `loc_req` (not `Routing.send()`).
            if pager_backend is None:
                raise NoLocatableDevice(device.id)

            msg_id = new_id("m_")
            msg_ref = messages_store.messages_ref(msg_id)
            conv_key = messages_store.conv_key(requester_uid, device.ownerUid)
            meta_ref = messages_store.meta_ref()
            meta_snap = meta_ref.get(transaction=transaction)

            seq = ((meta_snap.get("seqCounter") if meta_snap.exists else 0) or 0) + 1
            if meta_snap.exists:
                transaction.update(meta_ref, {"seqCounter": seq})
            else:
                transaction.set(
                    meta_ref, {"schemaVersion": 2, "lastSweepAt": None, "seqCounter": seq}
                )

            uids_sorted = sorted([requester_uid, device.ownerUid])
            transaction.set(
                msg_ref,
                {
                    "seq": seq,
                    "convKey": conv_key,
                    "uids": uids_sorted,
                    "senderUid": requester_uid,
                    "recipientUid": device.ownerUid,
                    "kind": "loc_req",
                    "body": None,
                    "loc": None,
                    "wireId": None,
                    "originBackendKind": "webapp",
                    "originBackendId": None,
                    "ts": ts,
                    "createdAt": SERVER_TIMESTAMP,
                    "deliveries": {
                        pager_backend.id: {
                            "kind": "pager",
                            "state": "queued",
                            "attempts": 0,
                            "externalId": device.id,
                        }
                    },
                    "pendingDeviceIds": [device.id],
                },
            )

            # docs/PROTOCOL.md §3.2: a loc_req is not a thread entry; `conversations/{convKey}` is
            # untouched, only the `kind='loc'` answer updates it.
            req_payload = {
                "messageId": msg_id,
                "requesterUids": [requester_uid],
                "createdAt": SERVER_TIMESTAMP,
            }
            if req_snap.exists:
                # Superseding a stale request: an ordinary overwrite, not a uniqueness claim.
                transaction.set(req_ref, req_payload)
            else:
                # docs/PROTOCOL.md §13.3 rule 5 (one in-flight loc_req per device) uses
                # `transaction.create()`; `AlreadyExists` is retried once outside, then coalesces.
                transaction.create(req_ref, req_payload)
            return ("claimed", msg_id, None)

        try:
            kind, request_id, fix = run_transaction(_txn)
        except AlreadyExists:
            if not _retry_on_conflict:
                raise
            return self.locate(
                requester_uid=requester_uid, device=device, ts=ts, _retry_on_conflict=False
            )

        if kind == "claimed":
            assert request_id is not None
            msg = messages_store.get_message(request_id)
            assert msg is not None
            self._routing.redeliver_pager(msg, device.id)
            # docs/PROTOCOL.md §13.3 rule 5 coalesces onto an in-flight request only: if the inline
            # delivery just failed, clear the row and report no locatable device.
            refreshed = messages_store.get_message(request_id)
            assert refreshed is not None
            bid = messages_store.find_pager_delivery(refreshed, device.id)
            if bid is not None and refreshed.deliveries[bid].state == "failed":
                req_ref.delete()
                raise NoLocatableDevice(device.id)
            return LocateOutcome(request_id=request_id, cached=False)
        if kind == "coalesced":
            return LocateOutcome(request_id=request_id, cached=False)
        return LocateOutcome(request_id=None, cached=True, fix=fix)
