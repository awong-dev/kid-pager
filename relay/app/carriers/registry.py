"""The carriers the relay knows: one file per carrier and one line here."""

from __future__ import annotations

from app.carriers.base import Carrier, SimInfo
from app.carriers.generic import Generic
from app.carriers.usmobile import UsMobile

GENERIC = Generic()
CARRIERS: tuple[Carrier, ...] = (UsMobile(),)


def all_carriers() -> list[Carrier]:
    """Every carrier an admin may pick, the fallback last."""
    return [*CARRIERS, GENERIC]


def get(carrier_id: str | None) -> Carrier:
    """The carrier with this id; an unknown or missing id is the generic fallback."""
    for carrier in CARRIERS:
        if carrier.id == carrier_id:
            return carrier
    return GENERIC


def known(carrier_id: str) -> bool:
    return any(c.id == carrier_id for c in all_carriers())


def match(sim: SimInfo | None) -> Carrier:
    """Auto-match from what the phone reports; the generic carrier when none claims it."""
    if sim is not None:
        for carrier in CARRIERS:
            if carrier.matches(sim):
                return carrier
    return GENERIC
