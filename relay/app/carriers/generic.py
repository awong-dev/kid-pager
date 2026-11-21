"""The fallback carrier: no commands, no parsing, an estimate the admin types in."""

from __future__ import annotations

from app.carriers.base import Command, Event, Pack, SimInfo, Unknown


class Generic:
    id = "generic"
    name = "Other carrier"
    service_numbers: frozenset[str] = frozenset()

    def matches(self, sim: SimInfo) -> bool:
        return False

    def balance_command(self) -> Command | None:
        return None

    def topup_packs(self) -> list[Pack]:
        return []

    def parse(self, sender: str, text: str) -> Event:
        return Unknown()
