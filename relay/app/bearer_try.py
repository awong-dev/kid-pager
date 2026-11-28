"""Telling pagers to try Soracom Beam again (`cfg.br`, docs/PROTOCOL.md §3.2).

A pager that moved here did so over the direct bearer. Once the admin has pointed a Beam group at
this server, `start()` sends each pager a try; the pager connects through Beam with its login and
reports `br{st: "try"}`, which `note_report()` answers with the proof. A pager that gets no proof
goes back to the direct bearer by itself."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from app import devcfg
from app.broker import BrokerClient
from app.db.firestore import get_db
from app.store import devices as devices_store
from app.wire import StatusEnvelope

logger = logging.getLogger("relay.bearer_try")

# Why a pager was not sent a try.
SKIP_FIRMWARE = "firmware"  # its last status did not carry `brc`
SKIP_ON_BEAM = "on_beam"
SKIP_MOVING = "moving"  # a server move is not settled for it
SKIP_UNKNOWN = "unknown"


def _ref(device_id: str) -> Any:
    return get_db().collection("devices").document(device_id)


def _skip_reason(dev: devices_store.Device) -> str | None:
    if dev.status.brc != 1:
        return SKIP_FIRMWARE
    if dev.status.tls == "proxy":
        return SKIP_ON_BEAM
    if dev.move is not None and dev.move.state in ("waiting", "trying"):
        return SKIP_MOVING
    return None


def start(broker: BrokerClient, device_ids: list[str] | None = None) -> dict[str, Any]:
    """Sends a try to every pager, or to `device_ids`. A pager still working on an earlier try is
    sent that same request again."""
    devices = {d.id: d for d in devices_store.list_devices() if d.revokedAt is None}
    wanted = list(devices) if device_ids is None else device_ids
    sent: list[str] = []
    skipped: list[dict[str, str]] = []
    for device_id in wanted:
        dev = devices.get(device_id)
        reason = SKIP_UNKNOWN if dev is None else _skip_reason(dev)
        if dev is None or reason is not None:
            skipped.append({"id": device_id, "reason": reason or SKIP_UNKNOWN})
            continue
        current = dev.bearerTry
        if current is not None and current.st in ("sent", "try"):
            request_id = current.id
        else:
            request_id = "br_" + secrets.token_hex(6)
            _ref(device_id).update(
                {"bearerTry": {"id": request_id, "st": "sent", "sentAt": datetime.now(UTC)}}
            )
        devcfg.push_bearer_try(device_id, request_id, broker)
        sent.append(device_id)
    return {"sent": sent, "skipped": skipped}


def note_report(device: devices_store.Device, env: StatusEnvelope, broker: BrokerClient) -> None:
    """A `/status` `br` for the request this relay issued: record it, and answer each `try` that
    arrives through Beam with the proof, since a proof can be lost."""
    current = device.bearerTry
    report = env.br
    if current is None or report is None or report.id != current.id:
        return
    # Between attempts the pager reports `try` from the direct bearer, where it drops a proof.
    if report.st == "try" and env.tls == "proxy":
        devcfg.push_bearer_proof(device.id, current.id, broker)
    if (report.st, report.rsn) != (current.st, current.rsn):
        _ref(device.id).update(
            {
                "bearerTry.st": report.st,
                "bearerTry.rsn": report.rsn,
                "bearerTry.reportedAt": datetime.now(UTC),
            }
        )
        logger.info("bearer try %s on %s: %s %s", current.id, device.id, report.st, report.rsn or "")
