"""US Mobile's reply parser (`app/carriers/usmobile.py`)."""

from __future__ import annotations

import pytest

from app.carriers import registry
from app.carriers.base import (
    Balance,
    LowWarning,
    SimInfo,
    TopupFailed,
    TopupOk,
    Unknown,
)
from app.carriers.usmobile import SERVICE_NUMBER, UsMobile

C = UsMobile()
SENDER = SERVICE_NUMBER

# These fixtures are shapes the tolerant parser accepts, not recorded replies.
PROVISIONAL_BALANCE = [
    ("You have 45 texts remaining.", 45),
    ("You have 12 text messages left this cycle.", 12),
    ("Remaining: 1,250 SMS", 1250),
    ("Texts left: 7 texts", 7),
    ("You have 0 texts remaining.", 0),
    ("You have no texts remaining on your plan.", 0),
    ("Top up anytime: reply 'Top-up text 100'. You have 30 texts left.", 30),
]
PROVISIONAL_UNLIMITED = ["Your plan includes unlimited texts."]
PROVISIONAL_TOPUP_OK = [
    ("Top-up successful! 100 texts have been added. You now have 145 texts remaining.", 100, 145),
    ("Your top-up is confirmed. 100 texts added.", 100, None),
    ("Top-up complete.", None, None),
]
PROVISIONAL_TOPUP_FAILED = [
    "Top-up failed: payment was declined.",
    "We were unable to complete your top-up.",
    "Your top-up was not successful.",
]
PROVISIONAL_LOW = ["Heads up: you're running low on texts."]
PROVISIONAL_UNKNOWN = [
    "Thanks for texting US Mobile. Visit usmobile.com for help.",
    "Reply STOP to unsubscribe.",
    "",
]


@pytest.mark.parametrize(("text", "texts"), PROVISIONAL_BALANCE)
def test_provisional_balance_shapes(text: str, texts: int):
    assert C.parse(SENDER, text) == Balance(texts=texts)


@pytest.mark.parametrize(("text", "added", "balance"), PROVISIONAL_TOPUP_OK)
def test_provisional_topup_ok(text: str, added: int | None, balance: int | None):
    assert C.parse(SENDER, text) == TopupOk(added=added, balance=balance)


@pytest.mark.parametrize("text", PROVISIONAL_TOPUP_FAILED)
def test_provisional_topup_failed(text: str):
    ev = C.parse(SENDER, text)
    assert isinstance(ev, TopupFailed) and ev.reason


@pytest.mark.parametrize(
    ("text", "expected"),
    [(t, Balance(texts=None)) for t in PROVISIONAL_UNLIMITED]
    + [(t, LowWarning(texts=None)) for t in PROVISIONAL_LOW],
)
def test_provisional_unlimited_and_low_shapes(text: str, expected: Balance | LowWarning):
    assert C.parse(SENDER, text) == expected


@pytest.mark.parametrize("text", PROVISIONAL_UNKNOWN)
def test_provisional_unknown_is_never_a_guess(text: str):
    assert C.parse(SENDER, text) == Unknown()


def test_service_number_and_commands():
    assert C.service_numbers == frozenset({"+18888781488"})
    cmd = C.balance_command()
    assert cmd is not None and (cmd.method, cmd.to, cmd.text) == ("sms", "+18888781488", "BALANCE")
    (pack,) = C.topup_packs()
    assert pack.id == "text100" and pack.texts == 100
    assert pack.command.text == "Top-up text 100" and pack.command.to == "+18888781488"


def test_matches_on_the_sim_name_only():
    assert C.matches(SimInfo(simName="US Mobile"))
    assert C.matches(SimInfo(simName="usmobile"))
    # An MVNO SIM reports its host network: MCC/MNC and network name prove nothing.
    assert not C.matches(SimInfo(simOperator="310260", netName="US Mobile"))
    assert not C.matches(SimInfo(simOperator="310260", simName="T-Mobile"))
    assert not C.matches(SimInfo())


def test_registry_matches_and_falls_back_to_generic():
    assert registry.match(SimInfo(simName="US Mobile")).id == "usmobile"
    assert registry.match(SimInfo(simName="Verizon")).id == "generic"
    assert registry.match(None).id == "generic"
    assert registry.get("nope").id == "generic" and registry.get(None).id == "generic"
    generic = registry.GENERIC
    assert generic.balance_command() is None and generic.topup_packs() == []
    assert generic.service_numbers == frozenset()
    assert [c.id for c in registry.all_carriers()] == ["usmobile", "generic"]
