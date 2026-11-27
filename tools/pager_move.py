"""The pager's side of an over-the-air server move, mixed into `DeviceClient` (docs/MOVE_FIRMWARE_DESIGN.md
§5, §6.2; wire contract docs/MOVE_DESIGN.md §12). State changes run on one worker thread (`_move_loop`);
the MQTT thread only classifies a `cfg.mv`, hands the proof to a waiting trial and files a `moving` receipt.
Not modelled: the Beam bearer, login alternation (§5.7), the Device-screen text, NVS atomicity."""

from __future__ import annotations

import base64
import hashlib
import hmac
import queue
import re
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import paho.mqtt.client as mqtt

MOVE_ID_RE = re.compile(r"^[a-z0-9_]{3,16}$")
HOST_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,127}$")
CA_URL_MAX = 200
MAX_PROOF_N = 2**53 - 1
LEDGER_N = 4
PENDING_UP_SLOTS = 2
MAX_RE_ACKS = 10

# `mv` report states and reasons, MOVE_DESIGN.md §12.2.
PEND, TRY, OK, FAIL, CXL = "pend", "try", "ok", "fail", "cxl"


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 with SHA-256."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, block = b"", b""
    for i in range(1, -(-length // 32) + 1):
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
    return out[:length]


def derive(k: bytes, move_id: str, dev_id: str) -> tuple[bytes, str]:
    """`(k', pw')` of MOVE_DESIGN.md §6.1."""
    salt = move_id.encode("ascii")
    k_prime = hkdf_sha256(k, salt, b"kp-move-key|" + dev_id.encode("ascii"))
    pw = hkdf_sha256(k, salt, b"kp-move-pw|" + dev_id.encode("ascii"))
    return k_prime, base64.urlsafe_b64encode(pw).decode("ascii").rstrip("=")[:43]


@dataclass(frozen=True)
class Timers:
    """Firmware constants (MOVE_FIRMWARE_DESIGN.md §5.6), in seconds; a test or the e2e shrinks them."""

    schedule_s: tuple[float, ...] = (0, 600, 3600, 21600)
    background_s: float = 86400
    ack_wait_s: float = 30
    guard_s: float = 60
    proof_wait_s: float = 180
    ca_tries: int = 3
    ca_retry_s: float = 60
    moving_hold_s: float = 86400


@dataclass
class Order:
    id: str
    host: str
    port: int
    ca_url: str | None = None
    ca_sha: bytes | None = None


@dataclass
class Record:
    """The move record: what `/status` `mv` reports, plus the pending identity."""

    id: str
    st: str
    rsn: str = ""
    at: int = 0
    sched: int = 0
    next_try: float = 0.0
    t0: float = 0.0
    order: Order | None = None
    k_prime: bytes | None = None
    pw_prime: str | None = None
    ca_pem: str | None = None

    @property
    def unsettled(self) -> bool:
        return self.order is not None and self.st in (PEND, TRY, FAIL)

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "st": self.st}
        if self.rsn:
            out["rsn"] = self.rsn
        out["at"] = min(self.at, 255)
        return out


class Trial:
    """One session on the pending identity: the first `n` it drew and the proof it waits for.
    `ready` is set when the session is usable or has failed, `over` when the proof arrived or the
    session was lost."""

    def __init__(self, move_id: str) -> None:
        self.move_id = move_id
        self.n_first: int | None = None
        self.subscribed = False
        self.refused: str | None = None
        self.dropped = False
        self.proof = False
        self.proof_msg_id: str | None = None
        self.ready = threading.Event()
        self.over = threading.Event()


@dataclass
class Held:
    """A reply the pager keeps instead of sending (`pending_up` with its body)."""

    id: str
    to: str | None
    body: str
    ts: int


def fetch_ca_pem(url: str) -> str:
    """The CA download: plain GET, no certificate validation (the pager checks the SHA-256)."""
    resp = httpx.get(url, verify=False, timeout=10.0)
    resp.raise_for_status()
    return resp.text


def _sha_bytes(sha: Any) -> bytes | None:
    if isinstance(sha, bytes):
        return sha if len(sha) == 32 else None
    if isinstance(sha, str):
        try:
            raw = base64.urlsafe_b64decode(sha + "=" * (-len(sha) % 4))
        except ValueError:
            return None
        return raw if len(raw) == 32 else None
    return None


def classify(mv: Any) -> tuple[str, Any] | None:
    """`("order", Order)`, `("cancel", id)`, `("proof", (id, N))` or None for a malformed `cfg.mv`
    (dropped, not acked). §12.1: an order has host and port and neither cancel nor ok."""
    if not isinstance(mv, dict) or not isinstance(mv.get("id"), str) or not MOVE_ID_RE.match(mv["id"]):
        return None
    mid = mv["id"]
    keys = set(mv)
    if keys == {"id", "cancel"} and mv["cancel"] is True:
        return "cancel", mid
    if keys == {"id", "ok"}:
        n = mv["ok"]
        if isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= MAX_PROOF_N:
            return "proof", (mid, n)
        return None
    if {"host", "port"} <= keys <= {"id", "host", "port", "ca"}:
        host, port = mv["host"], mv["port"]
        if not isinstance(host, str) or not isinstance(port, int) or isinstance(port, bool):
            return None
        order = Order(mid, host, port)
        if "ca" in mv:
            ca = mv["ca"]
            sha = _sha_bytes(ca.get("sha")) if isinstance(ca, dict) else None
            url = ca.get("url") if isinstance(ca, dict) else None
            if sha is None or not isinstance(url, str) or not 0 < len(url.encode()) <= CA_URL_MAX:
                return None
            order.ca_url, order.ca_sha = url, sha
        return "order", order
    return None


class MoveMixin:
    """Needs `DeviceClient`'s `_publish`, `_build_client`, identity fields and receive path."""

    # -- set up by `_move_init`
    move_capable: bool
    move_timers: Timers
    move_auto: bool
    move_ca_fetch: Callable[[str], str]

    def _move_init(
        self,
        *,
        move_capable: bool,
        timers: Timers | None,
        auto: bool,
        ca_fetch: Callable[[str], str] | None,
    ) -> None:
        self.move_capable = move_capable
        self.move_timers = timers or Timers()
        self.move_auto = auto
        self.move_ca_fetch = ca_fetch or fetch_ca_pem
        self.move_rec: Record | None = None
        self.move_ledger: list[tuple[str, str]] = []
        self.move_events: list[str] = []
        self._trial: Trial | None = None
        self._mv_lock = threading.RLock()
        self._mv_q: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._mv_thread: threading.Thread | None = None
        self._mv_stop = threading.Event()
        self._sent_texts: dict[str, Held] = {}
        self.held_up: list[Held] = []
        self.held_sms: list[dict[str, Any]] = []
        self.failed_moving: list[str] = []
        self._hold_until = 0.0
        self.ca_pem: str | None = None

    # -- worker

    def _move_start(self) -> None:
        if not self.move_capable or (self._mv_thread and self._mv_thread.is_alive()):
            return
        self._mv_stop.clear()
        self._mv_thread = threading.Thread(target=self._move_loop, daemon=True)
        self._mv_thread.start()

    def _move_halt(self) -> None:
        self._mv_stop.set()
        self._mv_q.put(None)

    def _move_loop(self) -> None:
        while not self._mv_stop.is_set():
            try:
                task = self._mv_q.get(timeout=self._move_wake_in())
            except queue.Empty:
                task = None
            else:
                if task is None:
                    self._mv_q.task_done()
                    continue
            try:
                if task is not None:
                    task()
                else:
                    self._move_tick()
            except Exception as exc:  # noqa: BLE001 -- a simulator keeps going
                self.move_events.append(f"error: {exc!r}")
            finally:
                if task is not None:
                    self._mv_q.task_done()

    def _move_wake_in(self) -> float:
        """Seconds to the next due timer, at most 1."""
        due = [t for t in (self._rec_due(), self._hold_due()) if t is not None]
        return max(0.0, min([1.0, *(t - time.monotonic() for t in due)]))

    def _rec_due(self) -> float | None:
        rec = self.move_rec
        return rec.next_try if self.move_auto and rec is not None and rec.unsettled and rec.next_try else None

    def _hold_due(self) -> float | None:
        return self._hold_until if self._hold_until and not self._order_held() else None

    def _move_tick(self) -> None:
        now = time.monotonic()
        rec = self.move_rec
        if self.move_auto and rec is not None and rec.unsettled and rec.next_try and now >= rec.next_try:
            self._move_trial(manual=False)
        if self._hold_until and now >= self._hold_until and not self._order_held():
            self._hold_until = 0.0
            self._release_held()

    def move_wait_idle(self, timeout: float = 30.0) -> None:
        """Waits until every queued move task has run (tests and the harness)."""
        deadline = time.monotonic() + timeout
        while self._mv_q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)
        if self._mv_q.unfinished_tasks:
            raise TimeoutError("move tasks still running")

    def _move_run(self, fn: Callable[[], Any], timeout: float = 120.0) -> Any:
        """Runs `fn` on the worker and returns its result."""
        box: dict[str, Any] = {}
        done = threading.Event()

        def task() -> None:
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        self._mv_q.put(task)
        if not done.wait(timeout):
            raise TimeoutError("move task did not finish")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    # -- what /status carries

    def move_status_fields(self) -> dict[str, Any]:
        if not self.move_capable:
            return {}
        out: dict[str, Any] = {"mvc": 1, "mb": 1024}
        rec = self.move_rec
        if rec is not None:
            out["mv"] = rec.report()
        return out

    # -- the receive path (MQTT thread)

    def _on_move_cfg(self, data: dict[str, Any]) -> None:
        """A verified `cfg` carrying sub-key `mv`."""
        msg_id = data.get("id")
        parsed = classify((data.get("cfg") or {}).get("mv"))
        if parsed is None:
            self.move_events.append("malformed cfg.mv dropped")
            return
        kind, body = parsed
        if kind == "proof":
            self._on_proof(msg_id, *body)
        elif kind == "cancel":
            self._mv_q.put(lambda: self._do_cancel(msg_id, body))
        else:
            self._mv_q.put(lambda: self._do_order(msg_id, body))

    def _ack(self, msg_id: str | None) -> None:
        if msg_id:
            self.publish_ack(msg_id, "shown")  # type: ignore[attr-defined]

    def _on_proof(self, msg_id: str | None, move_id: str, n: int) -> None:
        trial = self._trial
        if (move_id, "ok") in self.move_ledger:
            self._ack(msg_id)  # the ack of a commit can be lost; the target re-sends the proof
            return
        if trial is None or trial.move_id != move_id or trial.n_first is None or n < trial.n_first:
            self.move_events.append(f"proof dropped: id={move_id} N={n}")
            return
        trial.proof_msg_id = msg_id
        trial.proof = True
        trial.over.set()

    # -- order, cancel (worker)

    def _ledger_add(self, move_id: str, outcome: str) -> None:
        self.move_ledger = [e for e in self.move_ledger if e[0] != move_id][-(LEDGER_N - 1):]
        self.move_ledger.append((move_id, outcome))

    def _report(self) -> None:
        self.publish_status()  # type: ignore[attr-defined]

    def _do_order(self, msg_id: str | None, order: Order) -> None:
        rec = self.move_rec
        if any(e[0] == order.id for e in self.move_ledger):
            self._ack(msg_id)
            return
        if rec is not None and rec.id == order.id:
            self._ack(msg_id)  # M12: the current id again, in any state
            return
        if self._trial is not None:
            return  # a trial of another id is running: deferred, not acked
        refusal = self._refusal(order)
        if refusal:
            self.move_rec = Record(order.id, FAIL, refusal)
            self._ack(msg_id)
            self._report()
            return
        if rec is not None and rec.unsettled:
            self._ledger_add(rec.id, CXL)
        new = Record(order.id, PEND, order=order, t0=time.monotonic())
        new.k_prime, new.pw_prime = derive(self.hmac_key or b"", order.id, self.device_id)  # type: ignore[attr-defined]
        if order.ca_url:
            new.ca_pem = self._fetch_ca(order)
            if new.ca_pem is None:
                new.st, new.rsn = FAIL, "ca"
                new.next_try = time.monotonic() + self.move_timers.background_s
        self._schedule_next(new)
        self.move_rec = new
        self._hold_until = 0.0
        self._ack(msg_id)
        self._wait_acked()
        self._report()
        self.move_events.append(f"order {order.id} stored ({new.st}{'/' + new.rsn if new.rsn else ''})")

    def _refusal(self, order: Order) -> str:
        if not self.hmac_key:
            return "rej"
        if not HOST_RE.match(order.host) or not 1 <= order.port <= 65535:
            return "rej"
        if order.ca_url is None and self.tls == "pinned":  # type: ignore[attr-defined]
            return "down"
        return ""

    def _fetch_ca(self, order: Order) -> str | None:
        assert order.ca_url and order.ca_sha
        for attempt in range(self.move_timers.ca_tries):
            if attempt:
                time.sleep(self.move_timers.ca_retry_s)
            try:
                pem = self.move_ca_fetch(order.ca_url)
            except (httpx.HTTPError, OSError, ValueError):
                continue
            if pem.count("-----BEGIN CERTIFICATE-----") == 1 and hashlib.sha256(pem.encode()).digest() == order.ca_sha:
                return pem
        return None

    def _wait_acked(self) -> None:
        info = getattr(self, "_last_pub_info", None)
        if info is not None:
            try:
                info.wait_for_publish(timeout=self.move_timers.ack_wait_s)
            except (RuntimeError, ValueError):
                pass

    def _do_cancel(self, msg_id: str | None, move_id: str) -> None:
        rec = self.move_rec
        if rec is not None and rec.id == move_id and rec.st in (PEND, FAIL):
            self._ledger_add(move_id, CXL)
            rec.st, rec.rsn, rec.order, rec.k_prime, rec.pw_prime, rec.ca_pem = CXL, "", None, None, None, None
            rec.next_try = 0.0
            self._ack(msg_id)
            self._release_held()
            self._report()
        else:
            self._ack(msg_id)

    # -- schedule

    def _schedule_next(self, rec: Record) -> None:
        """Arms the next trial: the M6 steps after the first, then the background period."""
        t = self.move_timers
        if rec.st == FAIL and rec.rsn == "ca":
            rec.next_try = time.monotonic() + t.background_s
        elif rec.sched < len(t.schedule_s):
            rec.next_try = rec.t0 + t.schedule_s[rec.sched]
        else:
            rec.next_try = time.monotonic() + t.background_s

    # -- trial (worker)

    def move_switch_now(self) -> str:
        """The Device-screen action: a trial now, whatever the budget. Returns `ok` or the reason."""
        return self._move_run(lambda: self._move_trial(manual=True)) or ""

    def move_run_trial(self) -> str:
        """The next scheduled trial now (for tests that do not wait for the timers)."""
        return self._move_run(lambda: self._move_trial(manual=False)) or ""

    def _move_trial(self, *, manual: bool) -> str | None:
        rec = self.move_rec
        if rec is None or not rec.unsettled or self._trial is not None:
            return "none"
        order = rec.order
        assert order is not None
        if order.ca_url and rec.ca_pem is None:  # a slot without its PEM is never connected with
            rec.ca_pem = self._fetch_ca(order)
            if rec.ca_pem is None:
                rec.st, rec.rsn = FAIL, "ca"
                self._schedule_next(rec)
                self._report()
                return "ca"
        rec.at += 1
        if not manual and rec.sched < len(self.move_timers.schedule_s):
            rec.sched += 1
        return self._session(rec)

    def _note_failure(self, rec: Record, rsn: str) -> None:
        """`pend` with the reason while scheduled trials remain, `fail/budget` after the last one."""
        spent = rec.sched >= len(self.move_timers.schedule_s)
        rec.st, rec.rsn = (FAIL, "budget") if spent else (PEND, rsn)
        self._schedule_next(rec)
        self.move_events.append(f"trial {rec.at} failed: {rsn}")

    def _drop_session(self) -> None:
        client = self._client  # type: ignore[attr-defined]
        self.connected = False  # type: ignore[attr-defined]
        if client is not None:
            client.loop_stop()
            client.disconnect()
        self._client = None  # type: ignore[attr-defined]

    def _session(self, rec: Record) -> str | None:
        """Connects with the pending identity, reports `try`, waits for the proof. None: committed."""
        order = rec.order
        assert order is not None and rec.k_prime is not None and rec.pw_prime is not None
        saved = (
            self.host, self.port, self.username, self.password,  # type: ignore[attr-defined]
            self.hmac_key, self.ca_pem, self._down_n, self._down_bits,  # type: ignore[attr-defined]
        )
        trial = Trial(rec.id)
        self._drop_session()
        self.host, self.port = order.host, order.port  # type: ignore[attr-defined]
        self.username, self.password = self.device_id, rec.pw_prime  # type: ignore[attr-defined]
        self.hmac_key, self.ca_pem = rec.k_prime, rec.ca_pem  # type: ignore[attr-defined]
        self._down_n = self._down_bits = 0  # type: ignore[attr-defined]
        self._trial = trial
        rec.st = TRY
        try:
            rsn = self._connect_trial(trial)
            if rsn is None:
                trial.over.wait(self.move_timers.proof_wait_s)
                if trial.proof:
                    self._commit(rec, trial)
                    return None
                rsn = "conn" if trial.dropped else "proof"
        finally:
            self._trial = None
        self._drop_session()
        (
            self.host, self.port, self.username, self.password,  # type: ignore[attr-defined]
            self.hmac_key, self.ca_pem, self._down_n, self._down_bits,  # type: ignore[attr-defined]
        ) = saved
        self._note_failure(rec, rsn)
        self.connect()  # type: ignore[attr-defined]
        return rsn

    def _connect_trial(self, trial: Trial) -> str | None:
        """CONNACK and SUBACK on the pending identity; the failure reason or None."""
        client = self._build_client(trial=trial)  # type: ignore[attr-defined]
        self._client = client  # type: ignore[attr-defined]
        try:
            client.connect(self.host, self.port, keepalive=60)  # type: ignore[attr-defined]
        except ssl.SSLError:
            return "tls"
        except OSError:
            return "conn"
        client.loop_start()
        trial.ready.wait(self.move_timers.guard_s)
        return None if trial.subscribed else (trial.refused or "conn")

    def _commit(self, rec: Record, trial: Trial) -> None:
        """The one commit point: the pending identity becomes the active one."""
        order = rec.order
        assert order is not None
        self._ledger_add(rec.id, OK)
        rec.st, rec.rsn, rec.next_try = OK, "", 0.0
        rec.order, rec.k_prime, rec.pw_prime = None, None, None
        self.tls = "pinned" if rec.ca_pem else self.tls  # type: ignore[attr-defined]
        if rec.ca_pem:
            self.ca_fp = order.ca_sha.hex()[:16] if order.ca_sha else None  # type: ignore[attr-defined]
        self.connected = True  # type: ignore[attr-defined]
        self.move_events.append(f"committed {rec.id}")
        self._ack(trial.proof_msg_id)
        self._re_ack()
        self._release_held()
        self._report()

    def _re_ack(self) -> None:
        """The current ack state of the newest thread entries, at most 10."""
        seen = [e.data.get("id") for e in reversed(self.inbox) if e.data.get("kind", "msg") == "msg"]  # type: ignore[attr-defined]
        for msg_id in [i for i in dict.fromkeys(seen) if i][:MAX_RE_ACKS]:
            self.publish_ack(msg_id, "shown")  # type: ignore[attr-defined]

    # -- the trial's MQTT callbacks (installed by `_build_client(trial=...)`)

    def _trial_on_connect(self, trial: Trial, client: mqtt.Client, reason_code: Any) -> None:
        if reason_code.is_failure:
            text = str(reason_code).lower()
            trial.refused = "auth" if "authoriz" in text or "password" in text else "conn"
            trial.ready.set()
            return
        client.subscribe(self.down_topic, qos=1)  # type: ignore[attr-defined]

    def _trial_on_subscribe(self, trial: Trial, reason_codes: Any) -> None:
        if any(getattr(rc, "is_failure", False) for rc in reason_codes):
            trial.refused = "auth"
        else:
            trial.n_first = self.publish_status()  # type: ignore[attr-defined]  # reports `try`
            trial.subscribed = True
        trial.ready.set()

    def _trial_on_disconnect(self, trial: Trial) -> None:
        trial.dropped = True
        trial.ready.set()
        trial.over.set()

    # -- held replies (M9, §6.2)

    def _order_held(self) -> bool:
        rec = self.move_rec
        return rec is not None and rec.unsettled

    def move_hold_up(self) -> bool:
        return self.move_capable and (self._order_held() or time.monotonic() < self._hold_until)

    def _remember_text(self, held: Held) -> None:
        self._sent_texts[held.id] = held

    def hold_text(self, held: Held) -> bool:
        """Files a reply instead of sending it. False when `pending_up` has no free slot."""
        with self._mv_lock:
            if len(self.held_up) >= PENDING_UP_SLOTS:
                return False
            self.held_up.append(held)
            return True

    def _on_moving_receipt(self, ref: str) -> None:
        """`rcpt` `moving`: re-queue the text under the same id, or show it failed."""
        with self._mv_lock:
            text = self._sent_texts.get(ref)
            if text is None or any(h.id == ref for h in self.held_up) or len(self.held_up) >= PENDING_UP_SLOTS:
                self.failed_moving.append(ref)
                return
            self.held_up.append(text)
            if not self._order_held():
                self._hold_until = time.monotonic() + self.move_timers.moving_hold_s
        self._mv_q.put(lambda: None)  # wakes the worker to arm its timer

    def _release_held(self) -> None:
        """Sends what was held, with the original ids, on whatever server is active."""
        with self._mv_lock:
            texts, self.held_up = self.held_up, []
            sms, self.held_sms = self.held_sms, []
        for h in texts:
            self._send_text(h)  # type: ignore[attr-defined]
        for entry in sms:
            self._send_sms_log(entry)  # type: ignore[attr-defined]
