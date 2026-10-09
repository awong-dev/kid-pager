"""Carrier APN presets offered when a device is created or edited.
The APN travels in the typed setup code (`;apn=<apn>`) and the encrypted bundle (`apn`)."""

from __future__ import annotations

import re

from pydantic import BaseModel

# ident.h: IDENT_APN_MAX is 32 including the NUL. 3GPP TS 23.003 §9.1: labels
# of letters, digits and hyphens separated by dots.
APN_MAX_LEN = 31
_APN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


class ApnPreset(BaseModel):
    id: str
    label: str
    apn: str
    note: str | None = None


PRESETS: list[ApnPreset] = [
    ApnPreset(
        id="us-mobile-dark-star",
        label="US Mobile - Dark Star (AT&T)",
        apn="ereseller",
        note="Required: with the carrier default, TLS never connects on this SIM.",
    ),
    ApnPreset(
        id="soracom",
        label="Soracom (Air for Cellular)",
        apn="soracom.io",
        note="The pager authenticates PAP/CHAP itself (user sora).",
    ),
]


def validate_apn(apn: str | None) -> str | None:
    """Returns the normalised APN, or None for "carrier default"; raises ValueError otherwise."""
    if apn is None:
        return None
    apn = apn.strip()
    if apn == "":
        return None
    if len(apn) > APN_MAX_LEN:
        raise ValueError(f"APN is longer than {APN_MAX_LEN} characters")
    if not _APN_RE.match(apn) or ".." in apn:
        raise ValueError("APN may contain only letters, digits, dots and hyphens")
    return apn
