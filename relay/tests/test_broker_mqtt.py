"""`BROKER_MODE=mqtt`: `MqttBrokerClient` and `MosquittoDynsecAdmin` against `eclipse-mosquitto:2`
in Docker (skipped without Docker)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

from app import homeinit
from app.broker import BrokerClient, make_broker_client
from app.broker_admin import EmqxAdmin, MosquittoDynsecAdmin, make_broker_admin
from app.broker_mqtt import MqttBrokerClient
from app.config import Settings

IMAGE = "eclipse-mosquitto:2"
CONF = Path(__file__).resolve().parents[2] / "home" / "mosquitto" / "mosquitto.conf"
RELAY_PASSWORD = "relay-test-password"
WAIT_S = 5.0


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "broker_api_url": "http://unused.invalid/api/v5",
        "broker_api_key": None,
        "broker_api_secret": None,
        "webhook_key": "wk",
        "dev_mode": False,
        "google_cloud_project": None,
        "firestore_emulator_host": None,
        "firebase_auth_emulator_host": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_rest_mode_is_the_default_and_builds_the_rest_clients():
    settings = _settings()
    assert settings.broker_mode == "rest"
    broker = make_broker_client(settings)
    assert type(broker) is BrokerClient
    assert isinstance(make_broker_admin(settings, broker), EmqxAdmin)


def test_mqtt_mode_builds_the_mqtt_clients():
    settings = _settings(broker_mode="mqtt")
    broker = make_broker_client(settings)
    assert isinstance(broker, MqttBrokerClient)
    assert isinstance(make_broker_admin(settings, broker), MosquittoDynsecAdmin)


def test_broker_mode_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BROKER_MODE", "mqtt")
    monkeypatch.setenv("BROKER_MQTT_HOST", "mosquitto")
    monkeypatch.setenv("BROKER_MQTT_PORT", "1884")
    monkeypatch.setenv("BROKER_MQTT_PASSWORD", " pw \n")
    s = Settings.from_env()
    assert (s.broker_mode, s.broker_mqtt_host, s.broker_mqtt_port) == ("mqtt", "mosquitto", 1884)
    assert (s.broker_mqtt_username, s.broker_mqtt_password) == ("relay", "pw")
    monkeypatch.setenv("BROKER_MODE", "amqp")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_disconnected_client_fails_publish_control_and_health():
    """Nothing listens on port 1: publish/control fail fast and the admin reports "error"."""
    client = MqttBrokerClient(_settings(broker_mode="mqtt", broker_mqtt_port=1))
    client.start()
    try:
        assert client.healthcheck() is False
        assert client.publish("pager/x/down", b"x", qos=1, retain=False) is False
        assert client.control([{"command": "listClients"}], timeout=0.2) is None
        admin = MosquittoDynsecAdmin(client, manages_auth=True)
        assert admin.delete_user("x") == "error"
    finally:
        client.stop()


def test_manages_auth_off_is_a_noop():
    admin = MosquittoDynsecAdmin(None, manages_auth=False)  # type: ignore[arg-type]
    assert admin.ensure_device("d", "p", "d") == "manual"
    assert admin.ensure_boot_user("b", "p") == "manual"
    assert admin.delete_user("d") == "manual"


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(scope="module")
def mosquitto_port() -> Iterator[int]:
    if not _docker_ok():
        pytest.skip("Docker is not available")
    name = f"pager-test-mosquitto-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory() as tmp:
        dynsec = Path(tmp) / "dynamic-security.json"
        dynsec.write_text(json.dumps(homeinit.dynsec_config(RELAY_PASSWORD)))
        subprocess.run(
            ["docker", "create", "--name", name, "-p", "127.0.0.1::1883", IMAGE],
            check=True,
            capture_output=True,
        )
        try:
            subprocess.run(["docker", "cp", str(CONF), f"{name}:/mosquitto/config/mosquitto.conf"], check=True)
            subprocess.run(["docker", "cp", str(dynsec), f"{name}:/mosquitto/data/dynamic-security.json"], check=True)
            subprocess.run(["docker", "start", name], check=True, capture_output=True)
            out = subprocess.run(
                ["docker", "port", name, "1883/tcp"], check=True, capture_output=True, text=True
            ).stdout
            port = int(out.strip().splitlines()[0].rsplit(":", 1)[1])
            yield port
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@dataclass
class Device:
    """A bare paho client playing a pager."""

    client: mqtt.Client
    messages: list[mqtt.MQTTMessage] = field(default_factory=list)
    granted: dict[int, list] = field(default_factory=dict)
    connect_rc: list = field(default_factory=list)

    def subscribe(self, topic: str) -> bool:
        _rc, mid = self.client.subscribe(topic, 1)
        _wait(lambda: mid in self.granted)
        return not any(rc.is_failure for rc in self.granted[mid])

    def publish(self, topic: str, payload: bytes) -> None:
        self.client.publish(topic, payload, qos=1).wait_for_publish(WAIT_S)

    def close(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()


def _wait(cond, timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _device(port: int, username: str, password: str) -> Device:
    """Connected device, or one whose `connect_rc` says why not."""
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"{username}-{uuid.uuid4().hex[:6]}"
    )
    dev = Device(client)
    client.username_pw_set(username, password)
    client.on_connect = lambda c, u, f, rc, p: dev.connect_rc.append(rc)
    client.on_message = lambda c, u, m: dev.messages.append(m)
    client.on_subscribe = lambda c, u, mid, rcs, p: dev.granted.__setitem__(mid, rcs)
    client.connect("127.0.0.1", port)
    client.loop_start()
    _wait(lambda: dev.connect_rc)
    return dev


def _connects(port: int, username: str, password: str) -> bool:
    dev = _device(port, username, password)
    try:
        return bool(dev.connect_rc) and not dev.connect_rc[0].is_failure
    finally:
        dev.close()


@dataclass
class Relay:
    client: MqttBrokerClient
    received: list[tuple[str, bytes, int]]


def _relay(port: int, *, client_id: str | None = None, handler: bool = True) -> Relay:
    settings = _settings(
        broker_mode="mqtt",
        broker_mqtt_host="127.0.0.1",
        broker_mqtt_port=port,
        broker_mqtt_password=RELAY_PASSWORD,
    )
    client = MqttBrokerClient(settings, client_id=client_id or f"relay-{uuid.uuid4().hex[:6]}")
    received: list[tuple[str, bytes, int]] = []
    client.start((lambda t, p, q: received.append((t, p, q))) if handler else None)
    assert client.wait_until_ready(WAIT_S), "relay never logged in with BROKER_ADMIN_PASSWORD"
    return Relay(client, received)


@pytest.fixture
def relay(mosquitto_port: int) -> Iterator[Relay]:
    r = _relay(mosquitto_port)
    yield r
    r.client.stop()


@pytest.fixture
def admin(relay: Relay) -> MosquittoDynsecAdmin:
    return MosquittoDynsecAdmin(relay.client, manages_auth=True)


def _new_id() -> str:
    return f"pgr-{uuid.uuid4().hex[:8]}"


def test_relay_logs_in_with_the_homeinit_password_and_is_healthy(relay: Relay, mosquitto_port: int):
    assert relay.client.healthcheck() is True
    assert not _connects(mosquitto_port, "relay", "wrong")
    assert not _connects(mosquitto_port, "nobody", "x")


def test_device_acl_is_its_four_topics_and_nothing_else(
    relay: Relay, admin: MosquittoDynsecAdmin, mosquitto_port: int
):
    d, other = _new_id(), _new_id()
    assert admin.ensure_device(d, "pw1", d) == "ok"
    assert admin.ensure_device(other, "pw2", other) == "ok"
    dev = _device(mosquitto_port, d, "pw1")
    try:
        assert not dev.connect_rc[0].is_failure
        assert dev.subscribe(f"pager/{d}/down")
        assert not dev.subscribe(f"pager/{other}/down")
        assert not dev.subscribe("pager/#")
        assert not dev.subscribe(f"pager/{d}/up")

        body = bytes(range(256))  # binary, not UTF-8: the CBOR case
        for suffix in ("up", "status", "loc"):
            dev.publish(f"pager/{d}/{suffix}", body)
        # Denied publishes are acknowledged and dropped by the broker.
        dev.publish(f"pager/{other}/up", b"spoof")
        dev.publish(f"pager/{d}/down", b"self")
        assert _wait(lambda: len(relay.received) >= 3)
        time.sleep(0.3)
        assert relay.received == [(f"pager/{d}/{s}", body, 1) for s in ("up", "status", "loc")]

        assert relay.client.publish(f"pager/{d}/down", b"\x00down", qos=1, retain=False) is True
        assert _wait(lambda: dev.messages)
        assert [(m.topic, m.payload) for m in dev.messages] == [(f"pager/{d}/down", b"\x00down")]
    finally:
        dev.close()


def test_ensure_device_twice_rotates_the_password(admin: MosquittoDynsecAdmin, mosquitto_port: int):
    d = _new_id()
    assert admin.ensure_device(d, "old", d) == "ok"
    assert admin.ensure_device(d, "new", d) == "ok"
    assert not _connects(mosquitto_port, d, "old")
    dev = _device(mosquitto_port, d, "new")
    try:
        assert not dev.connect_rc[0].is_failure
        assert dev.subscribe(f"pager/{d}/down")
    finally:
        dev.close()


def test_delete_user_revokes_and_is_idempotent(admin: MosquittoDynsecAdmin, mosquitto_port: int):
    d = _new_id()
    assert admin.ensure_device(d, "pw", d) == "ok"
    assert _connects(mosquitto_port, d, "pw")
    assert admin.delete_user(d) == "ok"
    assert not _connects(mosquitto_port, d, "pw")
    assert admin.delete_user(d) == "ok"
    # The role went with it: a re-created credential starts clean.
    assert admin.ensure_device(d, "pw", d) == "ok"
    assert _connects(mosquitto_port, d, "pw")


def test_boot_user_reads_its_retained_bundle_and_acks(
    relay: Relay, admin: MosquittoDynsecAdmin, mosquitto_port: int
):
    bid = uuid.uuid4().hex[:16]
    assert admin.ensure_boot_user(bid, "bpw") == "ok"
    bundle = b"\x01encrypted-bundle\xff"
    assert relay.client.publish(f"pager/boot/{bid}/down", bundle, qos=1, retain=True)
    dev = _device(mosquitto_port, f"boot-{bid}", "bpw")
    try:
        assert not dev.connect_rc[0].is_failure
        assert dev.subscribe(f"pager/boot/{bid}/down")
        assert not dev.subscribe("pager/boot/+/down")
        assert _wait(lambda: dev.messages)
        assert dev.messages[0].payload == bundle and dev.messages[0].retain
        dev.publish(f"pager/boot/{bid}/up", b'{"v":1,"ok":1}')
        assert _wait(lambda: relay.received)
        assert relay.received == [(f"pager/boot/{bid}/up", b'{"v":1,"ok":1}', 1)]
    finally:
        dev.close()
    assert relay.client.publish(f"pager/boot/{bid}/down", b"", qos=1, retain=True)
    assert admin.delete_user(f"boot-{bid}") == "ok"
    assert not _connects(mosquitto_port, f"boot-{bid}", "bpw")


def test_persistent_session_keeps_messages_while_the_relay_is_away(
    admin: MosquittoDynsecAdmin, mosquitto_port: int
):
    """`clean_session=False`: a device message sent while the relay is
    disconnected arrives when it reconnects with the same client id."""
    client_id = f"relay-{uuid.uuid4().hex[:6]}"
    first = _relay(mosquitto_port, client_id=client_id)
    first.client.stop()
    d = _new_id()
    assert admin.ensure_device(d, "pw", d) == "ok"
    dev = _device(mosquitto_port, d, "pw")
    try:
        dev.publish(f"pager/{d}/status", b"while away")
    finally:
        dev.close()
    second = _relay(mosquitto_port, client_id=client_id)
    try:
        assert _wait(lambda: second.received)
        assert second.received == [(f"pager/{d}/status", b"while away", 1)]
    finally:
        second.client.stop()


def test_handler_errors_do_not_stop_dispatch(admin: MosquittoDynsecAdmin, mosquitto_port: int):
    settings = _settings(
        broker_mode="mqtt", broker_mqtt_host="127.0.0.1", broker_mqtt_port=mosquitto_port,
        broker_mqtt_password=RELAY_PASSWORD,
    )
    client = MqttBrokerClient(settings, client_id=f"relay-{uuid.uuid4().hex[:6]}")
    seen: list[bytes] = []

    def handler(topic: str, payload: bytes, qos: int) -> None:
        seen.append(payload)
        if payload == b"boom":
            raise RuntimeError("ingest bug")

    client.start(handler)
    assert client.wait_until_ready(WAIT_S)
    d = _new_id()
    assert admin.ensure_device(d, "pw", d) == "ok"
    dev = _device(mosquitto_port, d, "pw")
    try:
        dev.publish(f"pager/{d}/up", b"boom")
        dev.publish(f"pager/{d}/up", b"after")
        assert _wait(lambda: seen == [b"boom", b"after"])
    finally:
        dev.close()
        client.stop()


def test_concurrent_control_requests_get_their_own_answers(admin: MosquittoDynsecAdmin):
    ids = [_new_id() for _ in range(8)]
    results: dict[str, str] = {}

    def run(d: str) -> None:
        results[d] = admin.ensure_device(d, "pw", d)

    threads = [threading.Thread(target=run, args=(d,)) for d in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {d: "ok" for d in ids}


def test_create_app_dispatches_mqtt_messages_like_the_webhook(
    monkeypatch: pytest.MonkeyPatch, admin: MosquittoDynsecAdmin, mosquitto_port: int
):
    """The lifespan starts the client and each device message reaches `webhooks.dispatch`."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.routers import webhooks

    calls: list[tuple[object, str, bytes]] = []
    monkeypatch.setattr(webhooks, "dispatch", lambda ingest, t, p: calls.append((ingest, t, p)))
    settings = _settings(
        broker_mode="mqtt", broker_mqtt_host="127.0.0.1", broker_mqtt_port=mosquitto_port,
        broker_mqtt_password=RELAY_PASSWORD,
    )
    d = _new_id()
    assert admin.ensure_device(d, "pw", d) == "ok"
    with TestClient(create_app(settings=settings)) as tc:
        broker = tc.app.state.broker
        assert isinstance(broker, MqttBrokerClient)
        assert broker.wait_until_ready(WAIT_S)
        assert tc.get("/healthz").json()["broker"] is True
        dev = _device(mosquitto_port, d, "pw")
        try:
            dev.publish(f"pager/{d}/up", b"hello")
            assert _wait(lambda: calls)
        finally:
            dev.close()
        assert calls == [(tc.app.state.ingest, f"pager/{d}/up", b"hello")]
    assert broker.healthcheck() is True  # just stopped: still within 30 s
