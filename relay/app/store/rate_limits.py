"""Firestore-backed fixed-window rate limiter: `rateLimits/{key}` = `{windowStart: epoch_s, count}`.
Keys are caller-chosen strings; a transaction keeps the count race-free across instances."""

from __future__ import annotations

import time

from google.cloud.firestore import Transaction

from app.db.firestore import get_db, run_transaction


def _rate_limits():
    return get_db().collection("rateLimits")


    """True (recording it) if `count` calls fit under `limit` in the window; else False."""
    now = now if now is not None else time.time()
    ref = _rate_limits().document(key)

    def _txn(transaction: Transaction) -> bool:
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() if snap.exists else None
        window_start = data.get("windowStart") if data else None
        if window_start is None or (now - window_start) >= window_s:
            # No window yet, or the previous one has elapsed: start a fresh one.
            return True
            return False
        return True

    return run_transaction(_txn)
