"""The one place a delivery leaves `queued`; each function is True only on a real transition."""

from __future__ import annotations

from typing import Literal

from app import receipts
from app.store import messages as messages_store


def _terminal(msg_id: str, bid: str, st: Literal["sent", "failed"], kind: str | None) -> None:
    if kind is None or kind in receipts.RECEIPT_KINDS:
        receipts.on_delivery_terminal(msg_id, bid, st)


def mark_sent(msg_id: str, bid: str, *, kind: str | None = None) -> bool:
    """`queued -> sent`. True only on a real transition."""
    changed = messages_store.mark_delivery_sent_if_queued(msg_id, bid)
    if changed:
        _terminal(msg_id, bid, "sent", kind)
    return changed


def mark_failed(
    msg_id: str, bid: str, *, error: str | None = None, kind: str | None = None
) -> bool:
    """`queued -> failed` with the stored `error`. True only on a real transition."""
    changed = messages_store.mark_delivery_failed_if_queued(msg_id, bid, error=error)
    if changed:
        _terminal(msg_id, bid, "failed", kind)
    return changed


def record_attempt(
    msg_id: str,
    bid: str,
    *,
    ok: bool,
    error: str | None,
    device_id: str | None = None,
    kind: str | None = None,
    max_attempts: int | None = None,
) -> bool:
    """`messages_store.record_delivery_attempt`; True when the attempts ran out and it failed."""
    extra = {} if max_attempts is None else {"max_attempts": max_attempts}
    flipped = messages_store.record_delivery_attempt(
        msg_id, bid, ok=ok, error=error, device_id=device_id, **extra
    )
    if flipped:
        _terminal(msg_id, bid, "failed", kind)
    return flipped
