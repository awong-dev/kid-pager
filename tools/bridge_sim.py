#!/usr/bin/env python3
"""A stand-in for the bridge phone.

Speaks the exact `/bridge/*` contract the Android app does (pair, events,
outbox poll, ack, heartbeat) so the relay's bridge path can be exercised
end to end without a phone. Two things live here:

* `BridgeSim` -- the phone's side: pairs with a code, polls the outbox (with
  `wait`, which the real phone does not use), records each item and acks
  it `sent` (tier 1) unless a failure is armed, heartbeats, and forwards
  injected events. Standard library only (`urllib`), so `tools/e2e_v2.py` can
  import it, or drive a running sim through `BridgeSimClient`.
* A small control-plane HTTP server (FastAPI, imported lazily) on `:8020`:
  `POST /_pair {code, simNumber?, voiceNumber?}`, `POST /_inject {event}`
  (or `{events: [...]}`), `GET /_outbox`, `POST /_fail_next {times}`,
  `POST /_reset`, `GET /_status`, `POST /_pause` / `POST /_resume` (stop / restart
  heartbeating and outbox polling, to look like a dead phone),
  `POST /_set {smsDefault?, listenerBound?, accessibility?, battery?, charging?}` (heartbeat
  status overrides). The heartbeat also reports `charging`, `batteryExempt` and `carrier`
  (`BRIDGE_SIM_CARRIER_NAME`, default `Sim Carrier`) and a `sent` SMS ack carries `parts: 1`.
  The compose service `bridge-sim` runs it (`tools/mocks/bridge_sim.Dockerfile`).

Server moves (docs/MOVE_DESIGN.md §8): `Mover`/`MoveState` port the Android app's `Mover.kt`; a
`moved` answer triggers `GET {url}/bridge/hello` without the token, then a commit to the new URL.

Environment: `RELAY_URL` (default `http://relay:8000`), `BRIDGE_SIM_PAIR_CODE`
(pair on start), `BRIDGE_SIM_SIM_NUMBER` (default `+15550007777`),
`BRIDGE_SIM_VOICE_NUMBER` (default none), `BRIDGE_SIM_HEARTBEAT_S` (30),
`BRIDGE_SIM_POLL_WAIT_S` (5), `BRIDGE_SIM_WHATSAPP` (default on; `0` pairs
without `caps.whatsapp`).

WhatsApp: the sim reports `caps.whatsapp` at pair and
`status.whatsapp` on every heartbeat; `whatsapp_dm_event()` /
`whatsapp_group_event()` build JID-shaped events (`<digits>@s.whatsapp.net`,
`<id>@g.us`) for `inject`. A DM is keyed by its conversation id, LID or phone JID;
`whatsapp_dm_event(..., lid=True)` builds the `<digits>@lid` shape (no sender phone), the default
builds the phone-JID shape with a `wa.me` link. `POST /_pair` accepts `whatsapp: false` to pair
without the cap.
"""

import argparse
import dataclasses
import hashlib
import hmac
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_SIM_NUMBER = "+15550007777"


def whatsapp_dm_event(
    phone: str,
    text: str,
    *,
    name: str = "Contact",
    event_id: str | None = None,
    lid: bool = False,
) -> dict[str, Any]:
    """A WhatsApp DM as the phone reports it. Default: phone-JID shape, conversation id
    `<digits>@s.whatsapp.net`, sender phone set, `conversation.link` = `https://wa.me/<digits>`.
    `lid=True`: conversation id `<digits>@lid`, no sender phone or link, only the display name."""
    digits = phone.lstrip("+")
    event: dict[str, Any] = {"source": "whatsapp", "text": text}
    if lid:
        event["conversation"] = {"id": f"{digits}@lid", "isGroup": False}
        event["sender"] = {"name": name}
    else:
        event["conversation"] = {
            "id": f"{digits}@s.whatsapp.net",
            "isGroup": False,
            "link": f"https://wa.me/{digits}",
        }
        event["sender"] = {"name": name, "phone": phone}
    if event_id:
        event["id"] = event_id
    return event


def whatsapp_group_event(
    group_id: str, title: str, sender: str, text: str, *, event_id: str | None = None
) -> dict[str, Any]:
    """A WhatsApp group message: id = `<group_id>@g.us`, sender as shown
    (possibly `~ Name`)."""
    event: dict[str, Any] = {
        "source": "whatsapp",
        "conversation": {"id": f"{group_id}@g.us", "title": title, "isGroup": True},
        "sender": {"name": sender},
        "text": text,
    }
    if event_id:
        event["id"] = event_id
    return event
_UNSET: Any = object()


class RelayError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"relay returned {status}: {body}")
        self.status = status
        self.body = body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _request(
    method: str,
    url: str,
    *,
    body: Any = None,
    token: str | None = None,
    timeout: float = 30.0,
    ctx: ssl.SSLContext | None = None,
    follow: bool = True,
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    handlers: list[Any] = [urllib.request.HTTPSHandler(context=ctx)] if ctx else []
    if not follow:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode(errors="replace")
        return exc.code, parsed


@dataclass(frozen=True)
class MoveState:
    """Which relay URLs the phone knows and which it uses (`MoveState` in Mover.kt, times in ms).
    `relay_url` is home, preferred whenever it answers; `prev_url` the other one until `prev_until`;
    `on_other` means the phone works on `prev_url`. `pending_*` come from a `moved` answer and are
    untrusted until `hello` on that URL proves them."""

    relay_url: str
    pending_url: str | None = None
    pending_id: str | None = None
    last_try_at: float = 0
    prev_url: str | None = None
    prev_until: float = 0
    home_move_id: str | None = None
    on_other: bool = False
    fails: int = 0
    step: int = 0
    next_at: float = 0


@dataclass(frozen=True)
class HelloAnswer:
    bridge_id: str | None
    move_id: str | None
    sig: str | None


PROBE_OK, PROBE_MOVED, PROBE_UNAUTH, PROBE_FAIL = "ok", "moved", "unauth", "fail"


def parse_moved(body: Any) -> tuple[str, str] | None:
    """`(url, id)` of the `moved` object of an answer body; None when absent or unusable."""
    moved = body.get("moved") if isinstance(body, dict) else None
    if not isinstance(moved, dict):
        return None
    url, mid = moved.get("url"), moved.get("id")
    if not isinstance(url, str) or not isinstance(mid, str):
        return None
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.query or parts.fragment:
        return None
    return url.strip().rstrip("/"), mid


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hello_sig(token: str, nonce: str, bridge_id: str, move_id: str) -> str:
    """`hello`'s `sig`: HMAC-SHA256 keyed by the ASCII bytes of the token's hex hash (§8.4)."""
    msg = f"kp-bridge-hello|{nonce}|{bridge_id}|{move_id}".encode("ascii")
    return hmac.new(token_hash(token).encode("ascii"), msg, hashlib.sha256).hexdigest()


@dataclass
class MoveEnv:
    """What the decisions need from outside: identity, a nonce source and the two calls."""

    bridge_id: str | None
    token: str | None
    nonce: Callable[[], str]
    hello: Callable[[str, str, str], HelloAnswer | None]
    heartbeat: Callable[[str], str]


@dataclass(frozen=True)
class Mover:
    """The pure decisions of a server move (Mover.kt); the constants are overridable for a test run."""

    verify_retry_ms: float = 5 * 60_000
    fallback_ms: float = 7 * 24 * 3_600_000
    fails_before_alternating: int = 3
    backoff_ms: tuple[float, ...] = tuple(m * 60_000 for m in (1, 2, 5, 15, 30, 60))

    @staticmethod
    def active_url(s: MoveState) -> str:
        return s.prev_url if s.on_other and s.prev_url else s.relay_url

    @staticmethod
    def random_nonce() -> str:
        return secrets.token_hex(16)

    def hello_valid(
        self, token: str | None, own: str | None, nonce: str, expected: str | None, a: HelloAnswer | None
    ) -> bool:
        if a is None or token is None or own is None or expected is None:
            return False
        if a.bridge_id != own or a.move_id != expected or a.sig is None:
            return False
        return hmac.compare_digest(hello_sig(token, nonce, own, expected).encode(), a.sig.encode())

    def hello_verifies(self, env: MoveEnv, url: str, expected: str | None) -> bool:
        if env.bridge_id is None:
            return False
        nonce = env.nonce()
        return self.hello_valid(env.token, env.bridge_id, nonce, expected, env.hello(url, env.bridge_id, nonce))

    def expire(self, s: MoveState, now: float) -> MoveState:
        if s.prev_url is None or now < s.prev_until:
            return s
        return MoveState(
            relay_url=s.prev_url if s.on_other else s.relay_url,
            pending_url=s.pending_url,
            pending_id=s.pending_id,
            last_try_at=s.last_try_at,
        )

    def on_moved(self, s0: MoveState, url: str, mid: str, now: float) -> MoveState:
        s = self.expire(s0, now)
        if url == self.active_url(s) or (url == s.pending_url and mid == s.pending_id):
            return s
        return dataclasses.replace(s, pending_url=url, pending_id=mid, last_try_at=0)

    def should_verify(self, s: MoveState, now: float) -> bool:
        return s.pending_url is not None and now - s.last_try_at >= self.verify_retry_ms

    def begin_verify(self, s: MoveState, now: float) -> MoveState:
        return dataclasses.replace(s, last_try_at=now)

    def commit(self, s: MoveState, now: float) -> MoveState:
        if s.pending_url is None:
            return s
        return MoveState(
            relay_url=s.pending_url,
            prev_url=self.active_url(s),
            prev_until=now + self.fallback_ms,
            home_move_id=s.pending_id,
        )

    def verify(self, s: MoveState, env: MoveEnv, now: float) -> MoveState:
        if s.pending_url is None:
            return s
        return self.commit(s, now) if self.hello_verifies(env, s.pending_url, s.pending_id) else s

    def on_answer(self, s0: MoveState, now: float, moved: bool) -> MoveState:
        s = self.expire(s0, now)
        if not moved and s.pending_url is not None:
            s = dataclasses.replace(s, pending_url=None, pending_id=None)
        return dataclasses.replace(s, fails=0) if s.fails else s

    def on_failure(self, s0: MoveState, now: float) -> MoveState:
        s = self.expire(s0, now)
        return s if s.prev_url is None else dataclasses.replace(s, fails=s.fails + 1)

    def alternate_due(self, s: MoveState, now: float) -> bool:
        return (
            s.prev_url is not None
            and now >= s.next_at
            and (s.on_other or s.fails >= self.fails_before_alternating)
        )

    def _backed_off(self, s: MoveState, now: float) -> MoveState:
        wait = self.backoff_ms[min(s.step, len(self.backoff_ms) - 1)]
        return dataclasses.replace(s, step=s.step + 1, next_at=now + wait)

    def _back_home(self, s: MoveState) -> MoveState:
        return dataclasses.replace(s, on_other=False, fails=0, step=0, next_at=0)

    def alternate(self, s0: MoveState, env: MoveEnv, now: float) -> MoveState:
        """Tries the URL not in use: `hello` then a heartbeat on the other one (settle only on a 2xx
        without `moved`), a heartbeat only when probing home. A failed try backs off."""
        s = self.expire(s0, now)
        other = s.prev_url
        if other is None:
            return s
        if s.on_other:
            return self._back_home(s) if env.heartbeat(s.relay_url) == PROBE_OK else self._backed_off(s, now)
        if self.hello_verifies(env, other, s.home_move_id) and env.heartbeat(other) == PROBE_OK:
            return dataclasses.replace(s, on_other=True, fails=0, step=1, next_at=now + self.backoff_ms[0])
        return self._backed_off(s, now)

    def on_unauthorized(self, s0: MoveState, env: MoveEnv, now: float) -> tuple[MoveState, bool]:
        """A 401 from the URL in use: `(state, token stays)`."""
        s = self.expire(s0, now)
        other = s.prev_url
        if other is None:
            return s, False
        if s.on_other:
            probe = env.heartbeat(s.relay_url)
            if probe == PROBE_OK:
                return self._back_home(s), True
            return (s, False) if probe == PROBE_UNAUTH else (self._backed_off(s, now), True)
        failing = dataclasses.replace(s, fails=max(s.fails, self.fails_before_alternating))
        if not self.hello_verifies(env, other, s.home_move_id):
            return self._backed_off(failing, now), True
        probe = env.heartbeat(other)
        if probe == PROBE_OK:
            moved_home = MoveState(
                relay_url=other,
                prev_url=s.relay_url,
                prev_until=s.prev_until,
                home_move_id=s.home_move_id,
                pending_url=s.pending_url,
                pending_id=s.pending_id,
                last_try_at=s.last_try_at,
            )
            return moved_home, True
        return (s, False) if probe == PROBE_UNAUTH else (self._backed_off(failing, now), True)


class BridgeSim:
    """The phone. `start()` runs the poll and heartbeat loops in daemon threads."""

    def __init__(
        self,
        relay_url: str,
        *,
        sim_number: str | None = DEFAULT_SIM_NUMBER,
        voice_number: str | None = None,
        heartbeat_s: float = 30.0,
        poll_wait_s: int = 5,
        whatsapp: bool = True,
        ca_file: str | None = None,
        mover: Mover | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """`ca_file`: the CA the phone trusts for https relays (default: the system store)."""
        self.move = MoveState(relay_url=relay_url.rstrip("/"))
        self.mover = mover or Mover()
        self._clock = clock
        self._ctx = ssl.create_default_context(cafile=ca_file) if ca_file else None
        self._move_lock = threading.RLock()
        # Events and outbox acks the relay has not taken yet; the phone keeps them (EventQueue.kt).
        self.event_queue: list[dict[str, Any]] = []
        self.pending_acks: list[dict[str, Any]] = []
        self.move_log: list[str] = []
        self.whatsapp = whatsapp
        self.sim_number = sim_number
        self.voice_number = voice_number
        self.heartbeat_s = heartbeat_s
        self.poll_wait_s = poll_wait_s
        self.hinted_wait_s: int | None = None  # outboxWaitS from pair/heartbeat
        self.token: str | None = None
        self.bridge_id: str | None = None
        self.outbox: list[dict[str, Any]] = []
        self.fail_next = 0
        self.heartbeats = 0
        self.last_error: str | None = None
        self.paused = False
        self.overrides: dict[str, Any] = {}
        self.carrier_name = os.environ.get("BRIDGE_SIM_CARRIER_NAME", "Sim Carrier")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._counter = 0
        # Bumped by `pair` and `reset`: a long-poll in flight with the previous token drops its
        # result and exits, so it cannot hold up the new pairing.
        self._gen = 0
        self._started = False


    @property
    def relay_url(self) -> str:
        """The URL in use."""
        return Mover.active_url(self.move)

    # -- server move (Moves.kt)

    def _now_ms(self) -> float:
        return self._clock() * 1000

    def _env(self) -> MoveEnv:
        return MoveEnv(
            bridge_id=self.bridge_id,
            token=self.token,
            nonce=Mover.random_nonce,
            hello=self._hello,
            heartbeat=self._probe,
        )

    def _hello(self, url: str, bridge_id: str, nonce: str) -> HelloAnswer | None:
        """`GET {url}/bridge/hello`, no credentials; None unless a 2xx answer parses."""
        query = urllib.parse.urlencode({"bridge": bridge_id, "nonce": nonce})
        try:
            status, out = _request("GET", f"{url}/bridge/hello?{query}", timeout=10, ctx=self._ctx, follow=False)
        except OSError as exc:
            self.move_log.append(f"hello {url} failed: {type(exc).__name__}")
            return None
        if status != 200 or not isinstance(out, dict):
            self.move_log.append(f"hello {url} -> {status}")
            return None
        return HelloAnswer(out.get("bridgeId"), out.get("moveId"), out.get("sig"))

    def _probe(self, url: str) -> str:
        """An authenticated heartbeat to a URL not in use; accounted nowhere."""
        if not self.token:
            return PROBE_FAIL
        try:
            status, out = _request(
                "POST", f"{url}/bridge/heartbeat", body={"status": self.status_body()},
                token=self.token, timeout=10, ctx=self._ctx, follow=False,
            )
        except OSError:
            return PROBE_FAIL
        if status == 401:
            return PROBE_UNAUTH
        if parse_moved(out) is not None:
            return PROBE_MOVED
        return PROBE_OK if 200 <= status < 300 else PROBE_FAIL

    def _answered(self, moved: tuple[str, str] | None) -> None:
        """An HTTP answer from the URL in use that is not a failure."""
        now = self._now_ms()
        verify: MoveState | None = None
        alternate: MoveState | None = None
        with self._move_lock:
            s = self.mover.on_answer(self.move, now, moved is not None)
            if moved is not None:
                s = self.mover.on_moved(s, moved[0], moved[1], now)
                if self.mover.should_verify(s, now):
                    s = self.mover.begin_verify(s, now)
                    verify = s
            self.move = s
            if self.mover.alternate_due(s, now):
                alternate = s
        if verify is not None:
            self._check(verify)
        if alternate is not None:
            self._try_other(alternate)

    def _failed(self) -> None:
        """A network error, a timeout or a 5xx without `moved` from the URL in use."""
        now = self._now_ms()
        alternate: MoveState | None = None
        with self._move_lock:
            self.move = self.mover.on_failure(self.move, now)
            if self.mover.alternate_due(self.move, now):
                alternate = self.move
        if alternate is not None:
            self._try_other(alternate)

    def _check(self, started: MoveState) -> None:
        committed = self.mover.verify(started, self._env(), self._now_ms())
        if committed.relay_url == started.relay_url:
            self.move_log.append(f"move to {started.pending_url} not verified")
            return
        with self._move_lock:
            cur = self.move
            if (cur.relay_url, cur.on_other, cur.pending_url, cur.pending_id) != (
                started.relay_url, started.on_other, started.pending_url, started.pending_id,
            ):
                return
            self.move = self.mover.commit(cur, self._now_ms())
            self.move_log.append(f"moved to {self.move.relay_url}")

    def _try_other(self, before: MoveState) -> None:
        after = self.mover.alternate(before, self._env(), self._now_ms())
        with self._move_lock:
            cur = self.move
            if (cur.relay_url, cur.prev_url, cur.on_other) == (before.relay_url, before.prev_url, before.on_other):
                self.move = dataclasses.replace(
                    after, pending_url=cur.pending_url, pending_id=cur.pending_id, last_try_at=cur.last_try_at
                )
                if Mover.active_url(self.move) != Mover.active_url(before):
                    self.move_log.append(f"relay in use: {Mover.active_url(self.move)}")

    def _unauthorized(self) -> bool:
        """True when the token stays after a 401: the other URL took over or could not be reached."""
        with self._move_lock:
            before = self.move
        if before.prev_url is None:
            return False
        after, keep = self.mover.on_unauthorized(before, self._env(), self._now_ms())
        with self._move_lock:
            cur = self.move
            if (cur.relay_url, cur.prev_url, cur.on_other) == (before.relay_url, before.prev_url, before.on_other):
                self.move = dataclasses.replace(
                    after, pending_url=cur.pending_url, pending_id=cur.pending_id, last_try_at=cur.last_try_at
                )
        return keep

    def _send(self, method: str, path: str, *, body: Any = None, timeout: float = 30.0) -> tuple[int, Any]:
        """An authenticated call to the URL in use, feeding what came back to the move logic. A
        network error raises `RelayError(0, ...)`."""
        try:
            status, out = _request(
                method, f"{self.relay_url}{path}", body=body, token=self.token, timeout=timeout, ctx=self._ctx
            )
        except OSError as exc:
            self._failed()
            raise RelayError(0, f"{type(exc).__name__}: {exc}") from exc
        moved = parse_moved(out)
        if status == 401:
            if not self._unauthorized():
                self.token = None
        elif status < 500 or moved is not None:
            self._answered(moved)
        else:
            self._failed()
        return status, out

    @property
    def moved(self) -> bool:
        """True once the phone works on a URL it was redirected to."""
        return self.move.home_move_id is not None

    def status_body(self) -> dict[str, Any]:
        return {
            "battery": 90,
            "listenerBound": True,
            "smsDefault": True,
            "accessibility": True,
            "whatsapp": self.whatsapp,
            "accounts": ["kid@example.com"],
            "simNumber": self.sim_number,
            "voiceNumber": self.voice_number,
            "version": "sim-1",
            "charging": True,
            "batteryExempt": True,
            "carrier": {"simOperator": "310260", "simName": self.carrier_name, "netName": "Sim Net"},
            **self.overrides,
        }

    def pair(
        self,
        code: str,
        *,
        sim_number: Any = _UNSET,
        voice_number: Any = _UNSET,
        whatsapp: Any = _UNSET,
    ) -> dict[str, Any]:
        if whatsapp is not _UNSET:
            self.whatsapp = bool(whatsapp)
        if sim_number is not _UNSET:
            self.sim_number = sim_number
        if voice_number is not _UNSET:
            self.voice_number = voice_number
        body: dict[str, Any] = {
            "code": code,
            "version": "sim-1",
            "accounts": ["kid@example.com"],
            "caps": {"sms": True, "gchat": True, "gvoice": True, "whatsapp": self.whatsapp},
        }
        if self.sim_number:
            body["simNumber"] = self.sim_number
        if self.voice_number:
            body["voiceNumber"] = self.voice_number
        status, out = _request("POST", f"{self.relay_url}/bridge/pair", body=body, ctx=self._ctx)
        if status != 200:
            raise RelayError(status, out)
        with self._lock:
            self.token = out["token"]
            self.bridge_id = out["bridgeId"]
            self.last_error = None
            self._gen += 1
            gen = self._gen
        self._take_hint(out)
        if self._started:
            self._spawn_poll(gen)
        return out

    def _take_hint(self, out: Any) -> None:
        """Like the phone: cache `outboxWaitS` (clamped 0..25); a response
        without it resets the hint."""
        hint = out.get("outboxWaitS") if isinstance(out, dict) else None
        with self._lock:
            self.hinted_wait_s = max(0, min(25, hint)) if isinstance(hint, int) else None

    def heartbeat(self) -> dict[str, Any] | None:
        if not self.token:
            return None
        status, out = self._send("POST", "/bridge/heartbeat", body={"status": self.status_body()})
        if status == 200:
            self.heartbeats += 1
            self._take_hint(out)
            return out
        raise RelayError(status, out)

    def events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Queues `events` and sends everything queued. A row leaves the queue on a 2xx or when the
        relay rejects the batch (4xx other than 429); a 5xx, a `moved` or a network error keeps it."""
        if not self.token:
            raise RuntimeError("not paired")
        with self._lock:
            self.event_queue.extend(events)
        return self.flush_events()

    def flush_events(self) -> dict[str, Any]:
        with self._lock:
            batch = list(self.event_queue)
        if not batch or not self.token:
            return {"results": []}
        status, out = self._send("POST", "/bridge/events", body={"events": batch})
        if status == 200 or (400 <= status < 500 and status != 429 and status != 401):
            with self._lock:
                self.event_queue = [e for e in self.event_queue if e not in batch]
        if status != 200:
            raise RelayError(status, out)
        return out

    def inject(self, event: dict[str, Any]) -> dict[str, Any]:
        """One event, with a generated `id` if it has none."""
        event = dict(event)
        if "id" not in event:
            self._counter += 1
            event["id"] = f"sim{int(time.time())}x{self._counter}"
        event.setdefault("ts", int(time.time()))
        return self.events([event])

    def poll_outbox(self, wait: int = 0) -> list[dict[str, Any]]:
        if not self.token or self.paused:
            return []
        status, out = self._send("GET", f"/bridge/outbox?wait={wait}", timeout=wait + 15)
        if status != 200:
            raise RelayError(status, out)
        return out["items"]

    def ack(
        self,
        ob_id: str,
        state: str,
        reason: str | None = None,
        tier: int = 1,
        parts: int | None = None,
    ) -> None:
        """Acks an outbox item. The ack stays queued, to be sent again, when the relay answered with
        a 5xx or `moved` or could not be reached."""
        body: dict[str, Any] = {"state": state, "reason": reason, "tier": tier}
        if parts is not None and state == "sent":
            body["parts"] = parts
        entry = {"id": ob_id, "body": body}
        with self._lock:
            self.pending_acks.append(entry)
        self._send_ack(entry)

    def _send_ack(self, entry: dict[str, Any]) -> None:
        status, out = self._send("POST", f"/bridge/outbox/{entry['id']}/ack", body=entry["body"])
        if status == 204 or 400 <= status < 500:
            with self._lock:
                if entry in self.pending_acks:
                    self.pending_acks.remove(entry)
        if status != 204:
            raise RelayError(status, out)

    def flush_acks(self) -> None:
        for entry in list(self.pending_acks):
            try:
                self._send_ack(entry)
            except RelayError:
                return

    def handle_outbox_once(self, wait: int = 0, gen: int | None = None) -> int:
        """Polls once, records and acks every item. Returns how many. With
        `gen`, a poll that returns after a re-pair is dropped."""
        self.flush_acks()
        items = self.poll_outbox(wait)
        if gen is not None and gen != self._gen:
            return 0
        for item in items:
            with self._lock:
                fail = self.fail_next > 0
                if fail:
                    self.fail_next -= 1
            state, reason = ("failed", "sim_failed") if fail else ("sent", None)
            record = {**item, "ackState": state, "reason": reason, "tier": 1, "at": time.time()}
            with self._lock:
                self.outbox.append(record)
            sms_send = item.get("kind") == "send" and item.get("source") == "sms"
            try:
                self.ack(item["id"], state, reason, 1, parts=1 if sms_send else None)
            except RelayError as exc:
                self.last_error = str(exc)  # stays queued, `flush_acks` sends it again
        return len(items)


    def start(self) -> None:
        self._stop.clear()
        self._started = True
        thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        thread.start()
        self._threads.append(thread)
        self._spawn_poll(self._gen)

    def _spawn_poll(self, gen: int) -> None:
        thread = threading.Thread(target=self._poll_loop, args=(gen,), daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()

    def _poll_loop(self, gen: int) -> None:
        while not self._stop.is_set() and gen == self._gen:
            if not self.token or self.paused:
                self._stop.wait(0.5)
                continue
            try:
                hinted = self.hinted_wait_s
                started = time.monotonic()
                got = self.handle_outbox_once(self.poll_wait_s if hinted is None else hinted, gen)
                if not got and time.monotonic() - started < 0.2:
                    self._stop.wait(0.5)  # an answer that did not wait (a relay that has moved)
            except Exception as exc:  # noqa: BLE001 -- a sim keeps going
                self.last_error = str(exc)
                self._stop.wait(1.0)

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            if self.token and not self.paused:
                try:
                    self.heartbeat()
                    if self.event_queue:
                        self.flush_events()
                except Exception as exc:  # noqa: BLE001
                    self.last_error = str(exc)
            self._stop.wait(self.heartbeat_s)

    def reset(self) -> None:
        with self._lock:
            self.outbox.clear()
            self.fail_next = 0
            self.token = None
            self.bridge_id = None
            self.heartbeats = 0
            self.last_error = None
            self.paused = False
            self.overrides.clear()
            self.event_queue.clear()
            self.pending_acks.clear()
            self._gen += 1


class BridgeSimClient:
    """Drives a running sim's control plane (what `tools/e2e_v2.py` uses)."""

    def __init__(self, base_url: str = "http://localhost:8020") -> None:
        self.base_url = base_url.rstrip("/")

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        status, out = _request(method, f"{self.base_url}{path}", body=body, timeout=60)
        if status >= 400:
            raise RelayError(status, out)
        return out

    def pair(self, code: str, **numbers: Any) -> dict[str, Any]:
        """`simNumber=None` / `voiceNumber=None` mean "none"; omit to keep the defaults."""
        return self._call("POST", "/_pair", {"code": code, **numbers})

    def inject(self, event: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/_inject", {"event": event})

    def whatsapp_dm(self, phone: str, text: str, **kw: Any) -> dict[str, Any]:
        return self.inject(whatsapp_dm_event(phone, text, **kw))

    def whatsapp_group(
        self, group_id: str, title: str, sender: str, text: str, **kw: Any
    ) -> dict[str, Any]:
        return self.inject(whatsapp_group_event(group_id, title, sender, text, **kw))

    def outbox(self) -> list[dict[str, Any]]:
        return self._call("GET", "/_outbox")

    def fail_next(self, times: int = 1) -> None:
        self._call("POST", "/_fail_next", {"times": times})

    def status(self) -> dict[str, Any]:
        return self._call("GET", "/_status")

    def reset(self) -> None:
        self._call("POST", "/_reset", {})

    def pause(self) -> None:
        """Stop heartbeating and polling (a dead phone)."""
        self._call("POST", "/_pause", {})

    def resume(self) -> None:
        self._call("POST", "/_resume", {})

    def set_status(self, **overrides: Any) -> None:
        """Override heartbeat status keys (`smsDefault=False`, `battery=10`, ...)."""
        self._call("POST", "/_set", overrides)


def create_app(sim: BridgeSim):
    from fastapi import FastAPI, HTTPException, Request

    app = FastAPI(title="Bridge phone simulator")

    async def _json(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @app.post("/_pair")
    async def pair(request: Request) -> dict[str, Any]:
        body = await _json(request)
        kwargs: dict[str, Any] = {}
        if "simNumber" in body:
            kwargs["sim_number"] = body["simNumber"]
        if "voiceNumber" in body:
            kwargs["voice_number"] = body["voiceNumber"]
        if "whatsapp" in body:
            kwargs["whatsapp"] = body["whatsapp"]
        try:
            out = sim.pair(str(body.get("code", "")), **kwargs)
        except RelayError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.body) from exc
        try:
            sim.heartbeat()
        except RelayError:
            pass
        return {"bridgeId": out["bridgeId"]}

    @app.post("/_inject")
    async def inject(request: Request) -> dict[str, Any]:
        body = await _json(request)
        try:
            if "events" in body:
                return sim.events(body["events"])
            return sim.inject(body.get("event") or {})
        except RelayError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.body) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/_outbox")
    def outbox() -> list[dict[str, Any]]:
        with sim._lock:
            return list(sim.outbox)

    @app.post("/_fail_next")
    async def fail_next(request: Request) -> dict[str, int]:
        body = await _json(request)
        with sim._lock:
            sim.fail_next = int(body.get("times", 1))
        return {"failNext": sim.fail_next}

    @app.post("/_pause")
    def pause() -> dict[str, bool]:
        sim.paused = True
        return {"paused": True}

    @app.post("/_resume")
    def resume() -> dict[str, bool]:
        sim.paused = False
        try:
            sim.heartbeat()  # the phone says so at once, like a real wake-up
        except RelayError:
            pass
        return {"paused": False}

    @app.post("/_set")
    async def set_status(request: Request) -> dict[str, Any]:
        body = await _json(request)
        allowed = {"smsDefault", "listenerBound", "accessibility", "battery", "charging"}
        with sim._lock:
            for key, value in body.items():
                if key in allowed:
                    sim.overrides[key] = value
            current = dict(sim.overrides)
        try:
            sim.heartbeat()  # report the change now rather than in 30 s
        except RelayError:
            pass
        return current

    @app.post("/_reset")
    def reset() -> dict[str, bool]:
        sim.reset()
        return {"ok": True}

    @app.get("/_status")
    def status() -> dict[str, Any]:
        return {
            "paired": sim.token is not None,
            "bridgeId": sim.bridge_id,
            "simNumber": sim.sim_number,
            "voiceNumber": sim.voice_number,
            "whatsapp": sim.whatsapp,
            "heartbeats": sim.heartbeats,
            "paused": sim.paused,
            "lastError": sim.last_error,
            "relayUrl": sim.relay_url,
            "queuedEvents": len(sim.event_queue),
            "pendingAcks": len(sim.pending_acks),
            "moved": sim.moved,
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 -- container-local test double
    args = parser.parse_args()

    import uvicorn

    sim = BridgeSim(
        os.environ.get("RELAY_URL", "http://relay:8000"),
        sim_number=os.environ.get("BRIDGE_SIM_SIM_NUMBER", DEFAULT_SIM_NUMBER) or None,
        voice_number=os.environ.get("BRIDGE_SIM_VOICE_NUMBER") or None,
        heartbeat_s=float(os.environ.get("BRIDGE_SIM_HEARTBEAT_S", "30")),
        poll_wait_s=int(os.environ.get("BRIDGE_SIM_POLL_WAIT_S", "5")),
        whatsapp=os.environ.get("BRIDGE_SIM_WHATSAPP", "1") != "0",
    )
    sim.start()
    code = os.environ.get("BRIDGE_SIM_PAIR_CODE")
    if code:
        try:
            sim.pair(code)
        except RelayError as exc:
            print(f"bridge-sim: pairing on start failed: {exc}", flush=True)
    uvicorn.run(create_app(sim), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
