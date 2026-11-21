"""Conversation policy: the out/in pickers on `users/{uid}.policy` and the DM gate `check()`."""

from __future__ import annotations

from typing import Literal, Protocol

# Machine codes each picker stores.
OUT = {"open", "people", "people_sms", "sms", "any_sms"}
IN = {"any", "people", "people_sms", "sms", "any_sms"}

PeerKind = Literal["person", "external"]
Rule = Literal["any", "approved", "none"]
Reason = Literal["policy_out", "policy_in", "not_allowed"]

# Per code: (people-column, numbers-column) rule.
_RULES: dict[str, tuple[Rule, Rule]] = {
    "open": ("any", "any"),
    "any": ("any", "any"),
    "people": ("approved", "none"),
    "people_sms": ("approved", "approved"),
    "sms": ("none", "approved"),
    "any_sms": ("none", "any"),
}


def rule(policy_code: str, peer_kind: PeerKind) -> Rule:
    """Rule for `policy_code` against a peer of `peer_kind` (the other party's `users.kind`)."""
    people_rule, numbers_rule = _RULES[policy_code]
    return people_rule if peer_kind == "person" else numbers_rule


def _peer_kind(user: _UserLike) -> PeerKind:
    """Externals standing for a Chat subscription or a federated person count as people."""
    if user.kind == "external" and (getattr(user, "chat", None) or getattr(user, "fed", None)):
        return "person"
    return user.kind  # type: ignore[return-value]


class _PolicyLike(Protocol):
    out: str
    in_: str


class _UserLike(Protocol):
    kind: str
    policy: _PolicyLike


def check(
    sender: _UserLike, recipient: _UserLike, has_edge_out: bool, has_edge_in: bool
) -> Reason | None:
    """Reason this DM is refused, or None. The sender's outbound rule is checked first.

    `has_edge_out`/`has_edge_in` are the `allow` edges sender->recipient and recipient->sender.
    """
    if sender.kind != "external":
        out_rule = rule(sender.policy.out, _peer_kind(recipient))
        if out_rule == "none":
            return "policy_out"
        if out_rule == "approved" and not has_edge_out:
            return "not_allowed"

    if recipient.kind != "external":
        in_rule = rule(recipient.policy.in_, _peer_kind(sender))
        if in_rule == "none":
            return "policy_in"
        if in_rule == "approved" and not has_edge_in:
            return "not_allowed"

    return None
