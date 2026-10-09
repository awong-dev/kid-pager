"""`/internal/tick`'s and `/internal/sweep`'s scheduled work: retries queued pager deliveries, fails
stale bridge outbox rows, and the retention sweep over every collection class."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta

from google.cloud.firestore import (
    Client,
    CollectionGroup,
    FieldFilter,
    Query,
    WriteBatch,
)

from app.db.firestore import get_db
from app.routing import Routing
from app.store import bridge_outbox
from app.store import bridges as bridges_store
from app.store import devices as devices_store
from app.store import messages as messages_store
from app.store import settings as settings_store
from app.tasks import TaskQueue, build_task_queue

logger = logging.getLogger("relay.jobs")

BRIDGE_OFFLINE_AFTER = timedelta(hours=24)
BRIDGE_ACKED_RETENTION = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class TickResult:
    devicesChecked: int
    retriesAttempted: int
    locReqsCleared: int = 0
    bridgeOutboxFailed: int = 0


def _fail_stale_bridge_outbox() -> int:
    """A `pending` outbox row older than 24 h on any bridge fails `bridge_offline`, with its delivery."""
    cutoff = datetime.now(UTC) - BRIDGE_OFFLINE_AFTER
    failed = 0
    return failed


    task_queue = task_queue if task_queue is not None else build_task_queue()
    devices_checked = 0
    retries_attempted = 0

    for device in devices_store.list_devices():
        if device.revokedAt is not None:
            continue
        devices_checked += 1

        for msg in messages_store.list_pending_for_device(device.id):
            bid = messages_store.find_pager_delivery(msg, device.id)
            if bid is None:
                continue
            delivery = msg.deliveries.get(bid)
            if delivery is None or delivery.state != "queued":
                # 'sent' (broker accepted it, no ack yet) is the online edge's job
                # (docs/PROTOCOL.md §5.3); only a publish that never reached the broker is ours.
                continue
            retries_attempted += 1
            task_queue.enqueue(
                lambda msg=msg, device_id=device.id: routing.redeliver_pager(msg, device_id),
                name=f"pager-retry:{msg.id}:{device.id}",
            )

    loc_reqs_cleared = location.clear_stale_loc_reqs()
    bridge_rows_failed = _fail_stale_bridge_outbox()

    return TickResult(
        bridgeOutboxFailed=bridge_rows_failed,
        devicesChecked=devices_checked,
        retriesAttempted=retries_attempted,
        locReqsCleared=loc_reqs_cleared,
    )


DEFAULT_SWEEP_BATCH = 500
# Firestore's own hard cap on writes in one `WriteBatch.commit()`.
MAX_BATCH_WRITES = 500


def _sweep_batch_size() -> int:
    """Query page size per collection per iteration; `SWEEP_BATCH` is read on every call."""
    return int(os.environ.get("SWEEP_BATCH", str(DEFAULT_SWEEP_BATCH)))


def _cutoff(seconds: int) -> datetime:
    return datetime.fromtimestamp(time.time() - seconds, tz=UTC)


class _AutoBatch:
    """Wraps a `WriteBatch`, committing every `MAX_BATCH_WRITES` deletes (Firestore's cap is 500 per
    batch), independent of the query page size."""

    def __init__(self, db: Client) -> None:
        self._db = db
        self._batch: WriteBatch = db.batch()
        self._count = 0

    def reserve(self, n: int) -> None:
        """Commit now if the next `n` deletes would not fit, so a group (a message and its `wireIds`
        companion) is never split across commits; a split could orphan the companion."""
        if self._count and self._count + n > MAX_BATCH_WRITES:
            self._batch.commit()
            self._batch = self._db.batch()
            self._count = 0

    def delete(self, ref) -> None:
        self._batch.delete(ref)
        self._count += 1
        if self._count >= MAX_BATCH_WRITES:
            self._batch.commit()
            self._batch = self._db.batch()
            self._count = 0

    def commit(self) -> None:
        if self._count:
            self._batch.commit()
            self._batch = self._db.batch()
            self._count = 0


def _sweep_by_created_at(
    query_factory,
    cutoff: datetime,
    batch_size: int,
    *,
    on_doc=None,
    created_at_field: str = "createdAt",
) -> int:
    """Delete every doc older than `cutoff`, one query page and `WriteBatch` per iteration.
    `on_doc(batch, snap)` stages extra deletes (the `wireIds` companion) in the same commit;
    `created_at_field` names the timestamp field compared."""
    db = get_db()
    deleted = 0
    while True:
        query: Query | CollectionGroup = (
            query_factory()
            .where(filter=FieldFilter(created_at_field, "<", cutoff))
            .order_by(created_at_field)
            .limit(batch_size)
        )
        docs = list(query.stream())
        if not docs:
            break
        batch = _AutoBatch(db)
        group = 1 if on_doc is None else 2
        for snap in docs:
            # Keep each doc and its companion in one commit (`_AutoBatch.reserve`).
            batch.reserve(group)
            batch.delete(snap.reference)
            deleted += 1
            if on_doc is not None:
                on_doc(batch, snap)
        batch.commit()
        if len(docs) < batch_size:
            # A short page means the collection is exhausted.
            break
    return deleted


def _sweep_alerts(cutoff: datetime, batch_size: int) -> int:


def _sweep_expired(
    query_factory, now_epoch_s: int, batch_size: int, *, expires_field: str = "expiresAt"
) -> int:
    """Like `_sweep_by_created_at` for collections with their own Unix-epoch-seconds expiry field:
    deletes every doc whose `expires_field` is `< now_epoch_s`. No companion deletes."""
    db = get_db()
    deleted = 0
    while True:
        query: Query | CollectionGroup = (
            query_factory()
            .where(filter=FieldFilter(expires_field, "<", now_epoch_s))
            .order_by(expires_field)
            .limit(batch_size)
        )
        docs = list(query.stream())
        if not docs:
            break
        batch = _AutoBatch(db)
        for snap in docs:
            batch.reserve(1)
            batch.delete(snap.reference)
            deleted += 1
        batch.commit()
        if len(docs) < batch_size:
            break
    return deleted


@dataclass(frozen=True, slots=True)
class SweepResult:
    messagesDeleted: int
    wireIdsDeleted: int
    locationsDeleted: int
    locWireIdsDeleted: int
    locReqsDeleted: int
    orphanedWireIdsDeleted: int = 0
    conversationsDeleted: int = 0
    gchatLinkCodesDeleted: int = 0
    alertsDeleted: int = 0
    batteryDeleted: int = 0
    heldSmsDeleted: int = 0
    bridgePairCodesDeleted: int = 0
    bridgeOutboxDeleted: int = 0
    heldChatDeleted: int = 0


def sweep() -> SweepResult:
    db = get_db()
    retention = settings_store.get_retention()
    batch_size = _sweep_batch_size()

    wire_ids_deleted = 0

    def _delete_companion_wire_id(batch: _AutoBatch, snap) -> None:
        nonlocal wire_ids_deleted
        data = snap.to_dict() or {}
        wire_id = data.get("wireId")
        recipient_uid = data.get("recipientUid")
        if wire_id and recipient_uid:
            batch.delete(db.collection("wireIds").document(f"{wire_id}_{recipient_uid}"))
            wire_ids_deleted += 1

    messages_deleted = _sweep_by_created_at(
        lambda: db.collection("messages"),
        msg_cutoff,
        batch_size,
        on_doc=_delete_companion_wire_id,
    )
    orphaned_wire_ids_deleted = _sweep_by_created_at(
        lambda: db.collection("wireIds"), msg_cutoff, batch_size
    )
    conversations_deleted = _sweep_by_created_at(
    )
    gchat_link_codes_deleted = _sweep_expired(
        lambda: db.collection("gchatLinkCodes"), int(time.time()), batch_size
    )

    # `devices/{id}/battery` samples.
    battery_deleted = _sweep_by_created_at(
        lambda: db.collection_group("battery"),
        _cutoff(retention.batteryDays * 86400),
        batch_size,
    )

    # Held inbound texts live for the message retention window (by `receivedAt`).
    held_sms_deleted = _sweep_by_created_at(
        lambda: db.collection("heldSms"), msg_cutoff, batch_size, created_at_field="receivedAt"
    )

    held_chat_deleted = _sweep_by_created_at(
        lambda: db.collection("heldChat"), msg_cutoff, batch_size, created_at_field="receivedAt"
    )

    # Expired pairing codes.
    bridge_codes_deleted = 0
    for code in bridges_store.list_expired_pair_codes(datetime.now(UTC)):
        bridges_store.delete_pair_code(code)
        bridge_codes_deleted += 1

    outbox_deleted = bridge_outbox.delete_acked_before(datetime.now(UTC) - BRIDGE_ACKED_RETENTION)

    )
        messagesDeleted=messages_deleted,
        wireIdsDeleted=wire_ids_deleted,
        orphanedWireIdsDeleted=orphaned_wire_ids_deleted,
        conversationsDeleted=conversations_deleted,
        gchatLinkCodesDeleted=gchat_link_codes_deleted,
        alertsDeleted=alerts_deleted,
        batteryDeleted=battery_deleted,
        heldSmsDeleted=held_sms_deleted,
        bridgePairCodesDeleted=bridge_codes_deleted,
        bridgeOutboxDeleted=outbox_deleted,
        heldChatDeleted=held_chat_deleted,
    )
