"""US Mobile: `BALANCE` and `Top-up text 100` to 888-878-1488; `parse` tolerates unknown wording."""

from __future__ import annotations

import re

from app.carriers.base import (
    Balance,
    Command,
    Event,
    LowWarning,
    Pack,
    SimInfo,
    TopupFailed,
    TopupOk,
    Unknown,
)

SERVICE_NUMBER = "+18888781488"

_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)"
_UNIT = r"(?:text(?:s| messages?)?|sms(?:es)?|messages?)"
_COUNT_THEN_REMAINING = re.compile(
    _NUM + r"\s*" + _UNIT + r"\b[^.\n]{0,40}?\b(?:remaining|remain|left|available)\b", re.IGNORECASE
)
_REMAINING_THEN_COUNT = re.compile(
    r"\b(?:remaining|left|available|balance)\b[^0-9\n]{0,40}" + _NUM + r"\s*" + _UNIT + r"\b", re.IGNORECASE
)
_UNLIMITED = re.compile(r"\bunlimited\b[^.\n]{0,20}\b" + _UNIT, re.IGNORECASE)
_OUT = re.compile(
    r"\b(?:no (?:more )?" + _UNIT + r"|out of " + _UNIT + r"|" + _UNIT
    + r" (?:are |have been |has been )?(?:used up|exhausted|depleted))\b",
    re.IGNORECASE,
)
_TOPUP = re.compile(r"top[\s-]?up", re.IGNORECASE)
_OK = re.compile(
    r"\b(?:success\w*|added|purchased|confirmed|completed?|activated|applied)\b", re.IGNORECASE
)
_FAIL = re.compile(
    r"\b(?:fail\w*|unable|declin\w*|insufficient|could not|couldn'?t|error|unsuccessful|invalid"
    r"|not\s+success\w*|not (?:available|eligible|allowed|enabled))\b",
    re.IGNORECASE,
)
_ADDED = re.compile(
    _NUM + r"\s*(?:more\s+)?" + _UNIT + r"\s+(?:have been |has been |were |was )?(?:added|purchased)"
    r"|(?:added|purchased)\s+" + _NUM + r"\s*" + _UNIT,
    re.IGNORECASE,
)
_LOW = re.compile(
    r"\b(?:running low|low on|almost (?:out|used|gone)|nearly (?:out|used))\b", re.IGNORECASE
)


def _int(raw: str) -> int:
    return int(raw.replace(",", ""))


def _first_group(m: re.Match[str]) -> int:
    return _int(next(g for g in m.groups() if g))


class UsMobile:
    id = "usmobile"
    name = "US Mobile"
    service_numbers = frozenset({SERVICE_NUMBER})

    def matches(self, sim: SimInfo) -> bool:
        """SIM name only: an MVNO SIM reports its host network's MCC/MNC."""
        name = (sim.simName or "").lower().replace(" ", "")
        return "usmobile" in name

    def balance_command(self) -> Command | None:
        return Command(method="sms", to=SERVICE_NUMBER, text="BALANCE")

    def topup_packs(self) -> list[Pack]:
        return [
            Pack(
                id="text100",
                label="100 texts",
                texts=100,
                command=Command(method="sms", to=SERVICE_NUMBER, text="Top-up text 100"),
            )
        ]

    def parse(self, sender: str, text: str) -> Event:
        if _TOPUP.search(text):
            if _FAIL.search(text):
                return TopupFailed(reason=" ".join(text.split())[:120])
            if _OK.search(text):
                added = _ADDED.search(text)
                left = _COUNT_THEN_REMAINING.search(text) or _REMAINING_THEN_COUNT.search(text)
                return TopupOk(
                    added=_first_group(added) if added else None,
                    balance=_first_group(left) if left else None,
                )
        if _UNLIMITED.search(text):
            return Balance(texts=None)
        m = _COUNT_THEN_REMAINING.search(text) or _REMAINING_THEN_COUNT.search(text)
        if m:
            return Balance(texts=_first_group(m))
        if _OUT.search(text):
            return Balance(texts=0)
        if _LOW.search(text):
            return LowWarning(texts=None)
        return Unknown()
