"""The bridge phone simulator's server-move rules (tools/bridge_sim.py, docs/MOVE_DESIGN.md §8) against
two fake relays: `hello` without the token, commit, queued events and acks across the redirect, the
failover, and a `401`."""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge_sim
from bridge_sim import BridgeSim, Mover, hello_sig

SRC = "https://old.example.org"
DST = "https://new.example.org/kid-pager"
MOVE = "mv_0123456789ab"
TOKEN = "b_0123456789.test-secret"
BRIDGE = "b_0123456789"
MIN = 60_000


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000


class Relay:
    """What a relay does for a bridge, in the states of docs/MOVE_DESIGN.md §8.3."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.down = False
        self.redirect: str | None = None  # `bridgeRedirect`
        self.move_id = MOVE
        self.known = True  # holds this bridge's token hash
        self.hello_ok = True
        self.requests: list[tuple[str, str, str | None]] = []
        self.events: list[dict[str, Any]] = []
        self.acks: list[str] = []
        self.outbox: list[dict[str, Any]] = []

    @property
    def moved(self) -> dict[str, str] | None:
        return {"url": self.redirect, "id": self.move_id} if self.redirect else None

    def handle(self, method: str, path: str, body: Any, token: str | None) -> tuple[int, Any]:
        self.requests.append((method, path, token))
        route, _, query = path.partition("?")
        if route == "/bridge/hello":
            if not self.known or not self.hello_ok:
                return 404, {"detail": "unavailable"}
            q = dict(p.split("=") for p in query.split("&"))
            return 200, {"bridgeId": BRIDGE, "moveId": self.move_id, "sig": hello_sig(TOKEN, q["nonce"], BRIDGE, self.move_id)}
        if token != TOKEN or not self.known:
            return 401, {"detail": "unauthorized"}
        moved = self.moved
        if route == "/bridge/heartbeat":
            return 200, {"pending": 0, **({"moved": moved} if moved else {})}
        if route == "/bridge/outbox":
            return 200, {"items": [] if moved else self.outbox, **({"moved": moved} if moved else {})}
        if route == "/bridge/events":
            if moved:
                return 503, {"detail": "moved", "moved": moved}
            self.events.extend(body["events"])
            return 200, {"results": [{"id": e["id"], "outcome": "delivered"} for e in body["events"]]}
        if route.startswith("/bridge/outbox/") and route.endswith("/ack"):
            if moved:
                return 503, {"detail": "moved", "moved": moved}
            self.acks.append(route.split("/")[3])
            return 204, None
        return 404, None

    def tokens_seen(self) -> list[str | None]:
        return [t for _, _, t in self.requests]


@pytest.fixture
def world(monkeypatch):
    clock = Clock()
    relays = {SRC: Relay(SRC), DST: Relay(DST)}

    def fake_request(method, url, *, body=None, token=None, timeout=30.0, ctx=None, follow=True):
        for base in sorted(relays, key=len, reverse=True):
            if url.startswith(base + "/") and not relays[base].down:
                return relays[base].handle(method, url[len(base):], body, token)
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(bridge_sim, "_request", fake_request)
    sim = BridgeSim(SRC, clock=clock)
    sim.token, sim.bridge_id = TOKEN, BRIDGE
    return sim, relays[SRC], relays[DST], clock


def freeze(src: Relay) -> None:
    src.redirect = DST


def test_hello_signature_matches_the_design_vector():
    assert hello_sig(
        "b_0123456789.test-secret", "00112233445566778899aabbccddeeff", "b_0123456789", "mv_0123456789ab"
    ) == "c987c28916b52dc0add4d1bbcd6fa99983b765d36944a0aec29d7ab709f5566d"
    assert bridge_sim.token_hash("b_0123456789.test-secret") == (
        "3f7a3cbb91d0d0017834072b515b9b3bf46761e5ed05063cea0db33fdef35b32"
    )


def test_a_moved_answer_is_followed_after_hello_without_the_token(world):
    sim, src, dst, _ = world
    sim.heartbeat()
    assert sim.relay_url == SRC
    freeze(src)
    sim.heartbeat()
    assert sim.relay_url == DST and sim.move.prev_url == SRC and sim.move.home_move_id == MOVE
    assert sim.move.prev_until > sim.move.last_try_at
    # the target saw `hello` (no token) first, and no authenticated request before it
    assert dst.requests[0][1].startswith("/bridge/hello") and dst.requests[0][2] is None
    sim.heartbeat()
    assert ("POST", "/bridge/heartbeat", TOKEN) in dst.requests


@pytest.mark.parametrize("fault", ["wrong_sig", "wrong_move", "unknown", "down", "not_https"])
def test_a_target_that_does_not_prove_itself_gets_no_token_and_is_retried_in_five_minutes(world, fault):
    sim, src, dst, clock = world
    freeze(src)
    if fault == "wrong_move":
        dst.move_id = "mv_ffffffffffff"
    elif fault == "unknown":
        dst.known = False
    elif fault == "down":
        dst.down = True
    elif fault == "wrong_sig":
        real = dst.handle

        def bad(method, path, body, token):
            status, out = real(method, path, body, token)
            if status == 200 and "sig" in (out or {}):
                out = {**out, "sig": "0" * 64}
            return status, out

        dst.handle = bad  # type: ignore[method-assign]
    elif fault == "not_https":
        src.redirect = "http://new.example.org"
    sim.heartbeat()
    assert sim.relay_url == SRC
    assert TOKEN not in dst.tokens_seen()
    n = len(dst.requests)
    sim.heartbeat()  # within five minutes: no second try
    assert len(dst.requests) == n
    clock.advance_ms(5 * MIN + 1)
    dst.down, dst.known, dst.move_id = False, True, MOVE
    dst.handle = Relay.handle.__get__(dst)  # type: ignore[method-assign]
    if fault != "not_https":
        sim.heartbeat()
        assert sim.relay_url == DST


def test_events_stay_queued_through_the_redirect_and_reach_the_target(world):
    sim, src, dst, _ = world
    freeze(src)
    with pytest.raises(bridge_sim.RelayError) as err:
        sim.inject({"source": "sms", "text": "hi", "id": "e1"})
    assert err.value.status == 503
    assert sim.relay_url == DST  # the 503 carried `moved`: followed
    assert [e["id"] for e in sim.event_queue] == ["e1"] and src.events == []
    out = sim.flush_events()
    assert [r["id"] for r in out["results"]] == ["e1"]
    assert sim.event_queue == [] and [e["id"] for e in dst.events] == ["e1"]


def test_events_stay_queued_while_the_target_cannot_prove_itself(world):
    sim, src, dst, clock = world
    freeze(src)
    dst.down = True
    with pytest.raises(bridge_sim.RelayError):
        sim.inject({"source": "sms", "text": "hi", "id": "e1"})
    assert sim.relay_url == SRC and len(sim.event_queue) == 1
    dst.down = False
    clock.advance_ms(5 * MIN + 1)
    with pytest.raises(bridge_sim.RelayError):
        sim.flush_events()  # this one asks the source, which answers `moved` again, then follows it
    assert sim.relay_url == DST
    sim.flush_events()
    assert [e["id"] for e in dst.events] == ["e1"] and sim.event_queue == []


def test_an_event_the_relay_rejects_is_dropped_but_a_5xx_is_kept(world, monkeypatch):
    sim, src, _dst, _ = world
    real = src.handle
    answers = iter([(400, {"detail": "bad"}), (500, {"detail": "boom"})])

    def flaky(method, path, body, token):
        if path.startswith("/bridge/events"):
            return next(answers)
        return real(method, path, body, token)

    src.handle = flaky  # type: ignore[method-assign]
    with pytest.raises(bridge_sim.RelayError):
        sim.inject({"source": "sms", "text": "a", "id": "e1"})
    assert sim.event_queue == []
    with pytest.raises(bridge_sim.RelayError):
        sim.inject({"source": "sms", "text": "b", "id": "e2"})
    assert [e["id"] for e in sim.event_queue] == ["e2"]


def test_an_ack_refused_with_moved_is_sent_again_to_the_target_once(world):
    sim, src, dst, _ = world
    freeze(src)
    with pytest.raises(bridge_sim.RelayError):
        sim.ack("ob_1", "sent")
    assert sim.relay_url == DST and [a["id"] for a in sim.pending_acks] == ["ob_1"]
    assert src.acks == []
    sim.flush_acks()
    sim.flush_acks()
    assert dst.acks == ["ob_1"] and sim.pending_acks == []


def test_the_outbox_poll_follows_the_redirect(world):
    sim, src, _dst, _ = world
    freeze(src)
    assert sim.poll_outbox() == []
    assert sim.relay_url == DST


# ---- failover (§8.6) ----------------------------------------------------------------------------


def moved_phone(world):
    sim, src, dst, clock = world
    freeze(src)
    sim.heartbeat()
    assert sim.relay_url == DST
    return sim, src, dst, clock


def test_after_three_failures_the_phone_tries_the_source_and_settles_only_on_a_plain_answer(world):
    sim, src, dst, clock = moved_phone(world)
    dst.down = True
    for _ in range(3):
        with pytest.raises(bridge_sim.RelayError):
            sim.heartbeat()
    # the source still redirects: its `hello` is valid but its heartbeat answers `moved`, so no settling
    assert sim.relay_url == DST and not sim.move.on_other
    clock.advance_ms(1 * MIN + 1)
    with pytest.raises(bridge_sim.RelayError):
        sim.heartbeat()
    assert not sim.move.on_other
    # the move is called off on the source: it answers plainly
    src.redirect = None
    clock.advance_ms(5 * MIN)
    with pytest.raises(bridge_sim.RelayError):
        sim.heartbeat()
    assert sim.move.on_other and sim.relay_url == SRC
    sim.heartbeat()  # works on the other URL
    assert sim.heartbeats >= 1
    # home comes back: probed on the back-off, and the phone returns
    dst.down = False
    clock.advance_ms(1 * MIN + 1)
    sim.heartbeat()
    assert not sim.move.on_other and sim.relay_url == DST


def test_an_other_url_that_does_not_prove_itself_is_never_sent_the_token(world):
    sim, src, dst, _clock = moved_phone(world)
    dst.down = True
    src.redirect = None
    src.hello_ok = False
    tokens_before = src.tokens_seen().count(TOKEN)
    for _ in range(4):
        with pytest.raises(bridge_sim.RelayError):
            sim.heartbeat()
    assert src.tokens_seen().count(TOKEN) == tokens_before
    assert not sim.move.on_other


def test_a_401_from_home_within_the_window_tries_the_other_url_and_keeps_the_token(world):
    sim, src, dst, _ = moved_phone(world)
    src.redirect = None
    dst.known = False  # the row is gone on the new home
    with pytest.raises(bridge_sim.RelayError) as err:
        sim.heartbeat()
    assert err.value.status == 401 and sim.token == TOKEN
    assert sim.relay_url == SRC and sim.move.prev_url == DST  # the other URL became home


def test_a_401_from_both_clears_the_token(world):
    sim, src, dst, _ = moved_phone(world)
    src.redirect = None
    dst.known = False
    real = src.handle

    def refuses_the_token(method, path, body, token):
        return (401, {"detail": "unauthorized"}) if path.startswith("/bridge/heartbeat") else real(method, path, body, token)

    src.handle = refuses_the_token  # type: ignore[method-assign]
    with pytest.raises(bridge_sim.RelayError):
        sim.heartbeat()
    assert sim.token is None


def test_after_the_window_a_401_clears_the_token_and_the_other_url_is_forgotten(world):
    sim, _src, dst, clock = moved_phone(world)
    clock.advance_ms(8 * 24 * 60 * MIN)
    dst.known = False
    with pytest.raises(bridge_sim.RelayError):
        sim.heartbeat()
    assert sim.token is None and sim.move.prev_url is None


def test_a_5xx_without_moved_is_a_failure_not_a_redirect(world):
    sim, _src, dst, _ = moved_phone(world)
    real = dst.handle

    def broken(method, path, body, token):
        return (503, {"detail": "starting"}) if path.startswith("/bridge/heartbeat") else real(method, path, body, token)

    dst.handle = broken  # type: ignore[method-assign]
    for _ in range(2):
        with pytest.raises(bridge_sim.RelayError):
            sim.heartbeat()
    assert sim.move.fails == 2 and sim.relay_url == DST


def test_a_mover_with_short_timers_for_an_e2e_run():
    m = Mover(verify_retry_ms=1000, backoff_ms=(10, 20))
    s = bridge_sim.MoveState(relay_url=SRC)
    s = m.on_moved(s, DST, MOVE, 5000)
    assert m.should_verify(s, 5000)
    tried = m.begin_verify(s, 5000)
    assert not m.should_verify(tried, 5500) and m.should_verify(tried, 6000)
