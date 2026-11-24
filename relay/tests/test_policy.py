"""Tests for `app.policy`."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app import policy


@dataclass
class _FakePolicy:
    out: str
    in_: str


@dataclass
class _FakeUser:
    kind: str
    policy: _FakePolicy


def _person(out: str, in_: str) -> _FakeUser:
    return _FakeUser(kind="person", policy=_FakePolicy(out=out, in_=in_))


def _external() -> _FakeUser:
    # Externals have no policy of their own; check() skips an external side entirely.
    return _FakeUser(kind="external", policy=_FakePolicy(out="", in_=""))


# The (people-column, numbers-column) rule for each out ("open") and in ("any") code.
RULE_TABLE: dict[str, tuple[policy.Rule, policy.Rule]] = {
    "open": ("any", "any"),
    "any": ("any", "any"),
    "people": ("approved", "none"),
    "people_sms": ("approved", "approved"),
    "sms": ("none", "approved"),
    "any_sms": ("none", "any"),
}


@pytest.mark.parametrize("code,expected", sorted(RULE_TABLE.items()))
def test_rule_table(code: str, expected: tuple[policy.Rule, policy.Rule]) -> None:
    people_rule, numbers_rule = expected
    assert policy.rule(code, "person") == people_rule
    assert policy.rule(code, "external") == numbers_rule


def test_check_open_any_passes_with_no_edges_at_all() -> None:
    sender = _person("open", "any")
    recipient = _person("open", "any")
    assert policy.check(sender, recipient, has_edge_out=False, has_edge_in=False) is None


def test_check_people_people_requires_both_directed_edges() -> None:
    sender = _person("people", "people")
    recipient = _person("people", "people")

    assert policy.check(sender, recipient, has_edge_out=True, has_edge_in=True) is None
    assert (
        policy.check(sender, recipient, has_edge_out=False, has_edge_in=True) == "not_allowed"
    )
    assert (
        policy.check(sender, recipient, has_edge_out=True, has_edge_in=False) == "not_allowed"
    )


@pytest.mark.parametrize(
    ("sender", "recipient", "has_edge_out", "has_edge_in", "expected"),
    [
        pytest.param(
            _person("sms", "any"), _person("open", "any"), True, True, "policy_out", id="sender-none-with-edges"
        ),
        pytest.param(
            _person("open", "any"), _person("open", "sms"), True, True, "policy_in", id="recipient-none"
        ),
        pytest.param(
            _person("sms", "any"),
            _person("open", "sms"),
            False,
            False,
            "policy_out",
            id="sender-none-checked-before-recipient-in",
        ),
    ],
)
def test_check_refuses_a_none_column_with_the_matching_policy_code(
    sender, recipient, has_edge_out, has_edge_in, expected
) -> None:
    assert (
        policy.check(sender, recipient, has_edge_out=has_edge_out, has_edge_in=has_edge_in)
        == expected
    )


@pytest.mark.parametrize(
    ("sender", "recipient", "expected"),
    [
        pytest.param(_external(), _person("open", "any"), None, id="external-sender-open-recipient"),
        pytest.param(_external(), _person("open", "people"), "policy_in", id="external-sender-blocked"),
        pytest.param(_person("any_sms", "any"), _external(), None, id="external-recipient-open-sender"),
        pytest.param(_person("people", "any"), _external(), "policy_out", id="external-recipient-blocked"),
        pytest.param(_external(), _external(), None, id="both-external"),
    ],
)
def test_check_skips_an_external_sides_own_policy(sender, recipient, expected) -> None:
    assert policy.check(sender, recipient, has_edge_out=False, has_edge_in=False) == expected


def test_chat_external_is_a_person_peer():
    @dataclass
    class _ChatExternal(_FakeUser):
        chat: dict | None = None

    chat_ext = _ChatExternal(kind="external", policy=_FakePolicy(out="", in_=""), chat={"bridgeId": "b"})
    owner = _person("people", "people")
    # people column: approved -> needs the edge; the numbers column would be `none`.
    assert policy.check(owner, chat_ext, False, False) == "not_allowed"
    assert policy.check(owner, chat_ext, True, True) is None
    assert policy.check(chat_ext, owner, False, True) is None
    assert policy.check(chat_ext, owner, False, False) == "not_allowed"
    plain = _external()
    assert policy.check(owner, plain, True, True) == "policy_out"
