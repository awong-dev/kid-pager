"""Relay-issued device ids: `pgr-` + 8 lowercase hex (docs/PROTOCOL.md §1)."""

from __future__ import annotations

import secrets

DEVICE_ID_ATTEMPTS = 5


def new_device_id() -> str:
    return "pgr-" + secrets.token_hex(4)
