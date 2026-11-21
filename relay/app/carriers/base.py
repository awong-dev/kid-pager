"""Carrier types."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class SimInfo(BaseModel):
    """What the phone's TelephonyManager reports in its heartbeat (`status.carrier`)."""

    model_config = ConfigDict(extra="ignore")

    simOperator: str | None = Field(default=None, max_length=16)
    simName: str | None = Field(default=None, max_length=80)
    netName: str | None = Field(default=None, max_length=80)


class BalanceDoc(BaseModel):
    """`texts` is the allowance as of `at` (None: an unlimited plan, never warns)."""

    model_config = ConfigDict(extra="ignore")

    texts: int | None = None
    at: int = 0
    source: Literal["carrier", "manual", "estimate"] = "carrier"


class CarrierState(BaseModel):
    """`bridges/{id}.carrier`. All times are epoch seconds."""

    model_config = ConfigDict(extra="ignore")

    id: str = "generic"
    # True when an admin chose `id` (a heartbeat then never changes it).
    manual: bool = False
    balance: BalanceDoc | None = None
    # SMS parts sent since `balance` was taken (acks' `parts`, else the relay's own count).
    sentSince: int = 0
    lastCheckAt: int | None = None
    unknownReplies: int = 0
    # Check bookkeeping.
    partsSinceCheck: int = 0
    checkDue: bool = False
    awaiting: bool = False
    misses: int = 0
    # Low / out notice track; `level` is ok, low or out.
    level: str = "ok"
    levelSince: int = 0
    levelNotifiedAt: int | None = None


class Command(BaseModel):
    """Something to send the carrier; only `method="sms"` is built, `"ussd"` is rejected."""

    model_config = ConfigDict(frozen=True)

    method: Literal["sms", "ussd"] = "sms"
    to: str
    text: str


class Pack(BaseModel):
    """One purchasable top-up; `texts` is how many it adds."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    texts: int
    command: Command


class Balance(BaseModel):
    """The carrier's own statement of the allowance. `texts=None`: unlimited."""

    kind: Literal["balance"] = "balance"
    texts: int | None = None


class TopupOk(BaseModel):
    """A top-up went through; `added` and `balance` are set when the reply states them."""

    kind: Literal["topup_ok"] = "topup_ok"
    added: int | None = None
    balance: int | None = None


class TopupFailed(BaseModel):
    kind: Literal["topup_failed"] = "topup_failed"
    reason: str = ""


class LowWarning(BaseModel):
    """ "You are running low". `texts` when the text says how many are left."""

    kind: Literal["low"] = "low"
    texts: int | None = None


class Unknown(BaseModel):
    kind: Literal["unknown"] = "unknown"


Event = Balance | TopupOk | TopupFailed | LowWarning | Unknown


class Carrier(Protocol):
    id: str
    name: str
    # Normalized (E.164) senders that are the carrier itself, never a person.
    service_numbers: frozenset[str]

    def matches(self, sim: SimInfo) -> bool: ...

    def balance_command(self) -> Command | None: ...

    def topup_packs(self) -> list[Pack]: ...

    def parse(self, sender: str, text: str) -> Event: ...
