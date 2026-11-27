"""The pager simulator's move rules (tools/pager_move.py) against two fake brokers: no network, no
relay. The fake "source" delivers orders and receipts under `k`; the fake "target" answers a
`/status` that reports `mv` try with a proof under `k'`."""

from __future__ import annotations

import hashlib
import json
import ssl
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import pager_client
import pager_move
from app import devauth, wire
from pager_client import DeviceClient
from pager_move import Timers

K = bytes(range(32))
DEV = "pgr-3fa9c01e"
MOVE_ID = "mv_0123456789ab"


def _test_ca() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "move test ca")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


PEM = _test_ca()
PEM_SHA = hashlib.sha256(PEM.encode()).digest()
TARGET = ("target.test", 8883)
SOURCE = ("source.test", 1883)
VECTORS = json.loads((TOOLS / "move_vectors.json").read_text())["vectors"]
K_PRIME, PW_PRIME = pager_move.derive(K, MOVE_ID, DEV)


class RC:
    def __init__(self, text: str, failure: bool) -> None:
        self.text, self.is_failure = text, failure

    def __str__(self) -> str:
        return self.text


class Info:
    def wait_for_publish(self, timeout: float | None = None) -> None:
        return None


class Server:
    """One broker: the logins it accepts, what it received, and who is connected to it."""

    def __init__(self, user: str, pw: str, key: bytes) -> None:
        self.user, self.pw, self.key = user, pw, key
        self.received: list[tuple[str, dict[str, Any]]] = []
        self.clients: list[FakeClient] = []
        self.down_n = 0
        self.tls_error = False
        self.answer_proof = False
        self.proof_offset = 0
        self.refuse_all = False

    def send(self, topic: str, obj: dict[str, Any]) -> None:
        self.down_n += 1
        payload = devauth.sign_cbor(self.key, topic, {"v": 1, "ts": int(time.time()), "n": self.down_n, **obj})
        for client in list(self.clients):
            client.on_message(client, None, type("M", (), {"topic": topic, "payload": payload})())

    def cfg_mv(self, mv: dict[str, Any], msg_id: str = "m_00000001") -> None:
        self.send(f"pager/{DEV}/down", {"id": msg_id, "kind": "cfg", "cfg": {"mv": mv}, "ack": None})


class Net:
    def __init__(self) -> None:
        self.servers: dict[tuple[str, int], Server] = {}
        self.clients: list[FakeClient] = []

    def factory(self, **kw: Any) -> FakeClient:
        client = FakeClient(self)
        self.clients.append(client)
        return client

    def decoded(self, srv: Server) -> list[tuple[str, dict[str, Any]]]:
        return srv.received


class FakeClient:
    def __init__(self, net: Net) -> None:
        self.net = net
        self.on_connect = self.on_message = self.on_subscribe = self.on_disconnect = None
        self.creds: tuple[str | None, str | None] = (None, None)
        self.tls: str | None = None
        self.server: Server | None = None
        self.closed = False

    def username_pw_set(self, user: str | None, pw: str | None) -> None:
        self.creds = (user, pw)

    def tls_set(self, ca_certs: str | None = None) -> None:
        self.tls = "file"

    def tls_set_context(self, ctx: ssl.SSLContext) -> None:
        self.tls = "context"

    def will_set(self, *a: Any, **kw: Any) -> None:
        return None

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        srv = self.net.servers.get((host, port))
        if srv is None:
            raise ConnectionRefusedError(f"{host}:{port}")
        if srv.tls_error:
            raise ssl.SSLError("certificate verify failed")
        self.server = srv
        self.addr = (host, port)

    def loop_start(self) -> None:
        srv = self.server
        assert srv is not None and self.on_connect is not None
        if srv.refuse_all or self.creds != (srv.user, srv.pw):
            self.on_connect(self, None, {}, RC("Not authorized", True), None)
            return
        srv.clients.append(self)
        self.on_connect(self, None, {}, RC("Success", False), None)

    def loop_stop(self) -> None:
        return None

    def disconnect(self) -> None:
        self.closed = True
        if self.server and self in self.server.clients:
            self.server.clients.remove(self)

    def subscribe(self, topic: str, qos: int = 0) -> None:
        if self.on_subscribe is not None:
            self.on_subscribe(self, None, 1, [RC("Granted QoS 1", False)], None)

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> Info:
        srv = self.server
        assert srv is not None
        ok, raw = devauth.verify(srv.key, topic, payload)
        assert ok, f"{self.addr} got an envelope that does not verify under its key"
        env = wire.decode_envelope_bytes(raw)
        assert env is not None
        data = env[0]
        srv.received.append((topic, data))
        if srv.answer_proof and topic.endswith("/status") and (data.get("mv") or {}).get("st") == "try":
            proof = {"id": data["mv"]["id"], "ok": data["n"] + srv.proof_offset}
            threading.Timer(0.02, srv.cfg_mv, (proof, "m_0000f00d")).start()  # a broker answers later
        return Info()


def device(net: Net, **kw: Any) -> DeviceClient:
    timers = kw.pop("timers", Timers(schedule_s=(0, 0, 0), guard_s=1, proof_wait_s=1, ca_retry_s=0, background_s=3600))
    dev = DeviceClient(
        DEV, *SOURCE, DEV, "pw-old", hmac_key=K, wire="cbor", ca_file=None, move_capable=True,
        move_timers=timers, move_auto=kw.pop("auto", False), move_ca_fetch=kw.pop("ca", lambda url: PEM),
        client_factory=net.factory,
    )
    dev.connect()
    return dev


@pytest.fixture
def net():
    return Net()


@pytest.fixture
def src(net):
    srv = net.servers[SOURCE] = Server(DEV, "pw-old", K)
    return srv


@pytest.fixture
def dst(net):
    srv = net.servers[TARGET] = Server(DEV, PW_PRIME, K_PRIME)
    return srv


@pytest.fixture
def make(net, src, dst):
    devs: list[DeviceClient] = []

    def build(**kw: Any) -> DeviceClient:
        d = device(net, **kw)
        devs.append(d)
        return d

    yield build
    for d in devs:
        d.disconnect()


ORDER = {"id": MOVE_ID, "host": TARGET[0], "port": TARGET[1], "ca": {"url": "https://target.test/kid-pager/ca/x.pem", "sha": PEM_SHA}}


def statuses(srv: Server) -> list[dict[str, Any]]:
    return [d for t, d in srv.received if t.endswith("/status")]


def acks(srv: Server) -> list[str]:
    return [d["id"] for t, d in srv.received if t.endswith("/up") and d.get("ack") == "shown"]


def take_order(dev: DeviceClient, src: Server, order: dict[str, Any] = ORDER, msg_id: str = "m_00000001") -> None:
    src.cfg_mv(order, msg_id)
    dev.move_wait_idle()


# ---- keys and shapes ----------------------------------------------------------------------------


@pytest.mark.parametrize("v", VECTORS, ids=[v["label"] for v in VECTORS])
def test_derivation_matches_the_shared_vectors(v):
    k_prime, pw = pager_move.derive(bytes.fromhex(v["k"]), v["move_id"], v["dev_id"])
    assert (k_prime.hex(), pw) == (v["k_prime"], v["pw_prime"])


def test_shapes():
    c = pager_move.classify
    kind, order = c({"id": MOVE_ID, "host": "h", "port": 8883})
    assert kind == "order" and order.ca_sha is None
    assert c({"id": MOVE_ID, "cancel": True}) == ("cancel", MOVE_ID)
    assert c({"id": MOVE_ID, "ok": 7}) == ("proof", (MOVE_ID, 7))
    sha_b64 = "A" * 43
    assert c({"id": MOVE_ID, "host": "h", "port": 1, "ca": {"url": "https://x/y", "sha": sha_b64}})[1].ca_sha
    for bad in (
        {"id": "XX", "host": "h", "port": 1},
        {"id": MOVE_ID, "host": "h", "port": 1, "ok": 3},
        {"id": MOVE_ID, "host": "h"},
        {"id": MOVE_ID, "cancel": False},
        {"id": MOVE_ID, "cancel": True, "ok": 1},
        {"id": MOVE_ID, "ok": True},
        {"id": MOVE_ID, "host": "h", "port": 1, "ca": {"url": "http://x" * 30, "sha": sha_b64}},
        {"id": MOVE_ID, "host": "h", "port": 1, "ca": {"url": "https://x", "sha": "short"}},
    ):
        assert c(bad) is None, bad


# ---- accepting an order -------------------------------------------------------------------------


def test_status_advertises_the_capability_and_reports_the_order(src, make):
    dev = make()
    assert statuses(src)[0]["mvc"] == 1 and statuses(src)[0]["mb"] == 1024 and "mv" not in statuses(src)[0]
    take_order(dev, src)
    assert acks(src) == ["m_00000001"]
    assert statuses(src)[-1]["mv"] == {"id": MOVE_ID, "st": "pend", "at": 0}
    assert dev.move_hold_up()


def test_the_same_order_again_is_acked_and_changes_nothing(src, make):
    fetched: list[str] = []
    dev = make(ca=lambda url: fetched.append(url) or PEM)
    take_order(dev, src)
    before = (dev.move_rec.at, dev.move_rec.k_prime, len(fetched))
    take_order(dev, src, msg_id="m_00000002")
    assert acks(src) == ["m_00000001", "m_00000002"]
    assert (dev.move_rec.at, dev.move_rec.k_prime, len(fetched)) == before


def test_a_malformed_order_is_dropped_without_an_ack(src, make):
    dev = make()
    take_order(dev, src, {"id": MOVE_ID, "host": TARGET[0]})
    assert acks(src) == [] and dev.move_rec is None


def test_an_order_without_ca_is_refused_while_the_active_identity_is_pinned(src, make):
    dev = make()
    dev.tls = "pinned"
    take_order(dev, src, {"id": MOVE_ID, "host": TARGET[0], "port": 8883})
    assert acks(src) == ["m_00000001"]
    assert statuses(src)[-1]["mv"] == {"id": MOVE_ID, "st": "fail", "rsn": "down", "at": 0}
    assert not dev.move_hold_up()


def test_a_bad_host_or_port_is_refused(src, make):
    dev = make()
    take_order(dev, src, {"id": MOVE_ID, "host": "bad host!", "port": 8883, "ca": ORDER["ca"]})
    assert statuses(src)[-1]["mv"]["rsn"] == "rej"


def test_ca_that_cannot_be_fetched_stores_the_order_as_fail_ca(src, dst, make):
    tries: list[str] = []
    dev = make(ca=lambda url: tries.append(url) or "-----BEGIN CERTIFICATE-----\nother\n-----END CERTIFICATE-----\n")
    take_order(dev, src)
    assert len(tries) == 3
    assert statuses(src)[-1]["mv"] == {"id": MOVE_ID, "st": "fail", "rsn": "ca", "at": 0}
    assert dev.move_rec.order is not None and dev.move_rec.ca_pem is None
    dev.move_ca_fetch = lambda url: PEM  # "Switch now" fetches the CA again before it connects
    dst.answer_proof = True
    assert dev.move_switch_now() == ""
    assert dev.move_rec.st == "ok"


# ---- the trial ----------------------------------------------------------------------------------


def test_a_valid_proof_commits_to_the_target(src, dst, make):
    dev = make()
    dst.answer_proof = True
    held = dev.publish_msg("typed after the order", "mom")
    assert held  # sent before the order: not held
    take_order(dev, src)
    late = dev.publish_msg("held reply", "mom")
    assert [h.id for h in dev.held_up] == [late]
    assert dev.move_run_trial() == ""

    tries = [d for t, d in dst.received if t.endswith("/status")]
    assert tries[0]["mv"] == {"id": MOVE_ID, "st": "try", "at": 1} and tries[0]["mvc"] == 1
    assert (dev.host, dev.port, dev.password, dev.hmac_key) == (*TARGET, PW_PRIME, K_PRIME)
    assert dev.tls == "pinned" and dev.ca_fp == PEM_SHA.hex()[:16]
    assert dev.move_ledger == [(MOVE_ID, "ok")]
    assert dst.received[-1][1]["mv"] == {"id": MOVE_ID, "st": "ok", "at": 1}
    assert "m_0000f00d" in acks(dst)
    # the held reply goes to the target under its original id, and nothing went to the source
    assert [d["id"] for t, d in dst.received if t.endswith("/up") and d.get("body")] == [late]
    assert late not in [d.get("id") for _, d in src.received]
    assert not dev.move_hold_up()


def test_a_proof_for_an_older_status_is_ignored(src, dst, make):
    dev = make()
    take_order(dev, src)
    dst.answer_proof, dst.proof_offset = True, -1
    assert dev.move_run_trial() == "proof"
    assert dev.move_ledger == []
    assert (dev.host, dev.port, dev.password, dev.hmac_key) == (*SOURCE, "pw-old", K)
    assert dev.move_rec.st == "pend" and dev.move_rec.rsn == "proof" and dev.move_rec.at == 1
    assert statuses(src)[-1]["mv"] == {"id": MOVE_ID, "st": "pend", "rsn": "proof", "at": 1}


def test_no_proof_reverts_to_the_source_with_its_down_window(net, src, dst, make):
    dev = make()
    take_order(dev, src)
    src_window = (dev._down_n, dev._down_bits)
    assert dev.move_run_trial() == "proof"
    assert (dev._down_n, dev._down_bits) == src_window
    assert dev.connected and dev.move_hold_up()
    src.cfg_mv(ORDER, "m_00000009")  # the source re-sends on the online edge: a no-op
    dev.move_wait_idle()
    assert dev.move_rec.at == 1


@pytest.mark.parametrize(
    ("setup", "rsn"),
    [
        (lambda net, dst: net.servers.pop(TARGET), "conn"),
        (lambda net, dst: setattr(dst, "tls_error", True), "tls"),
        (lambda net, dst: setattr(dst, "refuse_all", True), "auth"),
    ],
    ids=["unreachable", "tls", "login refused"],
)
def test_a_trial_that_cannot_connect_reports_why(net, src, dst, make, setup, rsn):
    dev = make()
    take_order(dev, src)
    setup(net, dst)
    assert dev.move_run_trial() == rsn
    assert statuses(src)[-1]["mv"]["rsn"] == rsn and dev.connected
    assert dev.host == SOURCE[0]


def test_the_budget_ends_in_fail_budget_and_switch_now_still_works(net, src, dst, make):
    dev = make(timers=Timers(schedule_s=(0, 0), guard_s=1, proof_wait_s=1, ca_retry_s=0))
    take_order(dev, src)
    net.servers.pop(TARGET)
    assert dev.move_run_trial() == "conn"
    assert statuses(src)[-1]["mv"]["st"] == "pend"
    assert dev.move_run_trial() == "conn"
    assert statuses(src)[-1]["mv"] == {"id": MOVE_ID, "st": "fail", "rsn": "budget", "at": 2}
    # the target comes back; the Device-screen action retries at once
    net.servers[TARGET] = dst
    dst.answer_proof = True
    assert dev.move_switch_now() == ""
    assert dev.move_rec.st == "ok" and dev.move_rec.at == 3


def test_scheduled_trials_run_by_themselves(src, dst, make):
    dev = make(auto=True, timers=Timers(schedule_s=(0,), guard_s=1, proof_wait_s=2, ca_retry_s=0))
    dst.answer_proof = True
    src.cfg_mv(ORDER)
    deadline = time.time() + 10
    while dev.move_rec is None or dev.move_rec.st != "ok":
        assert time.time() < deadline, dev.move_events
        time.sleep(0.05)


# ---- cancel -------------------------------------------------------------------------------------


def test_cancel_drops_the_order_and_releases_held_replies(src, make):
    dev = make()
    take_order(dev, src)
    held = dev.publish_msg("waiting", "mom")
    src.cfg_mv({"id": MOVE_ID, "cancel": True}, "m_0000c0de")
    dev.move_wait_idle()
    assert "m_0000c0de" in acks(src)
    assert statuses(src)[-1]["mv"] == {"id": MOVE_ID, "st": "cxl", "at": 0}
    assert dev.move_ledger == [(MOVE_ID, "cxl")] and not dev.move_hold_up()
    assert held in [d.get("id") for _, d in src.received]
    take_order(dev, src, ORDER, "m_00000003")  # a finished id: acked, ignored
    assert dev.move_rec.st == "cxl" and "m_00000003" in acks(src)


def test_a_cancel_after_the_commit_changes_nothing(src, dst, make):
    dev = make()
    take_order(dev, src)
    dst.answer_proof = True
    dev.move_run_trial()
    dst.cfg_mv({"id": MOVE_ID, "cancel": True}, "m_0000c0de")
    dev.move_wait_idle()
    assert dev.move_rec.st == "ok" and "m_0000c0de" in acks(dst)


def test_the_proof_of_a_committed_move_is_only_acked(src, dst, make):
    dev = make()
    take_order(dev, src)
    dst.answer_proof = True
    dev.move_run_trial()
    dst.cfg_mv({"id": MOVE_ID, "ok": 1}, "m_0000beef")
    assert "m_0000beef" in acks(dst)
    stray = {"id": "mv_other", "ok": 99}
    dst.cfg_mv(stray, "m_0000dead")
    assert "m_0000dead" not in acks(dst)


# ---- texts the frozen source refused ------------------------------------------------------------


def rcpt(srv: Server, ref: str) -> None:
    srv.send(f"pager/{DEV}/down", {"id": "m_00c0ffee", "kind": "rcpt", "ref": ref, "st": "failed", "rsn": "moving", "ack": None})


def test_a_moving_receipt_requeues_the_text_under_its_id_and_sends_it_after_the_commit(src, dst, make):
    dev = make()
    ref = dev.publish_msg("typed while frozen", "mom")
    rcpt(src, ref)
    assert [h.id for h in dev.held_up] == [ref] and dev.failed_moving == []
    take_order(dev, src)  # the order arrives: still held, now until the commit
    dst.answer_proof = True
    dev.move_run_trial()
    sent = [d for t, d in dst.received if t.endswith("/up") and d.get("id") == ref]
    assert len(sent) == 1 and sent[0]["body"] == "typed while frozen"
    assert dev.held_up == []


def test_a_moving_receipt_with_no_free_slot_shows_failed(src, make):
    dev = make()
    take_order(dev, src)
    refs = [dev.publish_msg(f"reply {i}", "mom") for i in range(2)]
    with pytest.raises(RuntimeError):
        dev.publish_msg("third", "mom")
    unknown = "u_00000000"
    rcpt(src, unknown)
    assert dev.failed_moving == [unknown]
    rcpt(src, refs[0])  # already held: nothing to re-queue
    assert dev.failed_moving == [unknown, refs[0]]


def test_a_moving_receipt_with_no_order_holds_the_text_for_a_while_then_tries_again(src, make):
    dev = make(timers=Timers(schedule_s=(0,), guard_s=1, proof_wait_s=1, ca_retry_s=0, moving_hold_s=0.3))
    ref = dev.publish_msg("typed while frozen", "mom")
    before = len([1 for _, d in src.received if d.get("id") == ref])
    rcpt(src, ref)
    assert dev.move_hold_up()
    deadline = time.time() + 5
    while len([1 for _, d in src.received if d.get("id") == ref]) == before:
        assert time.time() < deadline
        time.sleep(0.05)
    assert not dev.move_hold_up() and dev.held_up == []


def test_sms_log_entries_wait_for_the_commit_too(src, dst, make):
    dev = make()
    take_order(dev, src)
    log_id = dev.sms_send("+15550001111", "hello")
    assert log_id not in [d.get("id") for _, d in src.received]
    dst.answer_proof = True
    dev.move_run_trial()
    assert log_id in [d.get("id") for _, d in dst.received]


def test_a_device_that_is_not_move_capable_acks_a_cfg_mv_like_any_unknown_sub_key(net, src):
    dev = DeviceClient(DEV, *SOURCE, DEV, "pw-old", hmac_key=K, wire="cbor", ca_file=None, client_factory=net.factory)
    dev.connect()
    try:
        assert "mvc" not in statuses(src)[0]
        src.cfg_mv(ORDER)
        assert dev.move_rec is None
    finally:
        dev.disconnect()


def test_pager_client_exports_the_move_hooks():
    assert pager_client.DeviceClient.move_switch_now is pager_move.MoveMixin.move_switch_now
