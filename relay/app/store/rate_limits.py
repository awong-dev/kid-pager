"""Firestore-backed fixed-window rate limiter: `rateLimits/{key}` = `{windowStart: epoch_s, count}`.
Keys are caller-chosen strings; a transaction keeps the count race-free across instances."""

from __future__ import annotations

import time

from google.cloud.firestore import Transaction

from app.db.firestore import get_db, run_transaction


def _rate_limits():
    return get_db().collection("rateLimits")


def check_and_increment(
    key: str, *, limit: int, window_s: int, now: float | None = None, count: int = 1
) -> bool:
    """True (recording it) if `count` calls fit under `limit` in the window; else False."""
    now = now if now is not None else time.time()
    ref = _rate_limits().document(key)

    def _txn(transaction: Transaction) -> bool:
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() if snap.exists else None
        window_start = data.get("windowStart") if data else None
        used = data.get("count", 0) if data else 0
        if window_start is None or (now - window_start) >= window_s:
            # No window yet, or the previous one has elapsed: start a fresh one.
            if count > limit:
                return False
            transaction.set(ref, {"windowStart": now, "count": count})
            return True
        if used + count > limit:
            return False
        transaction.update(ref, {"count": used + count})
        return True

    return run_transaction(_txn)
