"""`bridges/{bridgeId}/topups/{topupId}`: server-only, kept 400 days.
`request` checks the policy and counts the month in one transaction; nothing is automatic."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from google.cloud.firestore import Transaction
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db, run_transaction

logger = logging.getLogger("relay.topups")

TopupState = Literal["requested", "confirmed", "failed", "unconfirmed"]
# Everything but `failed` may have cost money.
COUNTED = frozenset({"requested", "confirmed", "unconfirmed"})
# A new top-up is refused while one is waiting for its reply.
IN_FLIGHT = "requested"


class TopupDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    pack: str
    packLabel: str = ""
    texts: int = 0
    by: str = ""
    at: datetime
    state: TopupState = "requested"
    outboxId: str | None = None
    note: str | None = None


class Refused(Exception):
    """The policy transaction said no. `status` is the HTTP code for the route."""

    def __init__(self, status: int, detail: str, *, used: int = 0) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.used = used


def _col(bridge_id: str):
    return get_db().collection("bridges").document(bridge_id).collection("topups")


def _from_snap(snap) -> TopupDoc:
    return TopupDoc.model_validate({"id": snap.id, **(snap.to_dict() or {})})


def aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def get(bridge_id: str, topup_id: str) -> TopupDoc | None:
    snap = _col(bridge_id).document(topup_id).get()
    return _from_snap(snap) if snap.exists else None


def list_recent(bridge_id: str, limit: int = 100) -> list[TopupDoc]:
    """Newest first."""
    rows = [_from_snap(s) for s in _col(bridge_id).stream()]
    rows.sort(key=lambda r: aware(r.at), reverse=True)
    return rows[:limit]


def count_between(bridge_id: str, start: datetime, end: datetime) -> int:
    """Counted top-ups with `start <= at < end` (display only)."""
    return sum(
        1
        for r in list_recent(bridge_id, limit=10_000)
        if r.state in COUNTED and start <= aware(r.at) < end
    )


def request(
    bridge_id: str,
    topup_id: str,
    *,
    pack: str,
    pack_label: str,
    texts: int,
    by: str,
    now: datetime,
    month_start: datetime,
    month_end: datetime,
    limit: int | None,
) -> TopupDoc:
    """Counts and creates in one transaction; `Refused(403)` at the limit, `(409)` if one waits."""
    col = _col(bridge_id)
    ref = col.document(topup_id)

    def _txn(transaction: Transaction) -> None:
        rows = [_from_snap(s) for s in col.stream(transaction=transaction)]
        if any(r.state == IN_FLIGHT for r in rows):
            raise Refused(409, "a top-up is already waiting for the carrier's reply")
        used = sum(
            1 for r in rows if r.state in COUNTED and month_start <= aware(r.at) < month_end
        )
        if limit is not None and used >= limit:
            raise Refused(403, f"{used} of {limit} used this month", used=used)
        transaction.create(
            ref,
            {
                "pack": pack,
                "packLabel": pack_label,
                "texts": texts,
                "by": by,
                "at": now,
                "state": "requested",
                "outboxId": None,
                "note": None,
            },
        )

    run_transaction(_txn)
    created = get(bridge_id, topup_id)
    assert created is not None
    return created


def set_state(
    bridge_id: str,
    topup_id: str,
    state: TopupState,
    *,
    only_from: tuple[str, ...],
    note: str | None = None,
    outbox_id: str | None = None,
) -> bool:
    """Guarded move; True when this call made it. `failed` and `confirmed` are final."""
    ref = _col(bridge_id).document(topup_id)

    def _txn(transaction: Transaction) -> bool:
        snap = ref.get(transaction=transaction)
        if not snap.exists or (snap.to_dict() or {}).get("state") not in only_from:
            return False
        updates: dict[str, Any] = {"state": state}
        if note is not None:
            updates["note"] = note
        if outbox_id is not None:
            updates["outboxId"] = outbox_id
        transaction.update(ref, updates)
        return True

    return run_transaction(_txn)
