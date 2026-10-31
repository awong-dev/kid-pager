"""The relay as a persistent MQTT client of the broker (`BROKER_MODE=mqtt`, Mosquitto).
Handlers run on one worker thread; a message is acked after its handler returns (`manual_ack`)."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from app.broker import PUBLISH_TIMEOUT_S, BrokerClient
from app.config import Settings

logger = logging.getLogger("relay.broker_mqtt")

INGEST_TOPICS = ("pager/+/up", "pager/+/status", "pager/+/loc", "pager/boot/+/up")
CONTROL_TOPIC = "$CONTROL/dynamic-security/v1"
CONTROL_RESPONSE_TOPIC = "$CONTROL/dynamic-security/v1/response"
CONTROL_TIMEOUT_S = 5.0
HEALTHY_FOR_S = 30.0
KEEPALIVE_S = 60
RECONNECT_MIN_S, RECONNECT_MAX_S = 1, 30

MessageHandler = Callable[[str, bytes, int], None]


class _Pending:
    def __init__(self, n: int) -> None:
        self.responses: dict[int, dict[str, Any]] = {}
        self.n = n
        self.done = threading.Event()


class MqttBrokerClient(BrokerClient):
    def __init__(self, settings: Settings, *, client_id: str = "relay") -> None:
        super().__init__(settings)
        self._host = settings.broker_mqtt_host
        self._port = settings.broker_mqtt_port
        self._handler: MessageHandler | None = None
        self._inbox: queue.Queue[mqtt.MQTTMessage | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._connected = threading.Event()
        # Set once the control-response subscription is acknowledged on the current
        # connection; a `control()` request before that would lose its answer.
        self._control_ready = threading.Event()
        self._control_sub_mid: int | None = None
        self._disconnected_at: float | None = None
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
            manual_ack=True,
        )
        client.username_pw_set(settings.broker_mqtt_username, settings.broker_mqtt_password)
        client.reconnect_delay_set(min_delay=RECONNECT_MIN_S, max_delay=RECONNECT_MAX_S)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_subscribe = self._on_subscribe
        client.on_message = self._on_message
        self._client = client


    def start(self, handler: MessageHandler | None = None) -> None:
        """Connect in the background; with a `handler`, device traffic is dispatched to it."""
        self._handler = handler
        if handler is not None:
            self._worker = threading.Thread(target=self._work, name="mqtt-ingest", daemon=True)
            self._worker.start()
        self._client.connect_async(self._host, self._port, keepalive=KEEPALIVE_S)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()
        if self._worker is not None:
            self._inbox.put(None)
            self._worker.join(timeout=PUBLISH_TIMEOUT_S)

    def wait_until_ready(self, timeout: float) -> bool:
        """True once connected with the control-response subscription in place."""
        return self._control_ready.wait(timeout)


    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> bool:
        if not self._connected.is_set():
            logger.warning("mqtt publish skipped, not connected (topic=%s)", topic)
            return False
        try:
            info = self._client.publish(topic, payload, qos=qos, retain=retain)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.warning("mqtt publish failed (topic=%s rc=%s)", topic, info.rc)
                return False
            if qos == 0:
                return True
            info.wait_for_publish(timeout=PUBLISH_TIMEOUT_S)
        except (RuntimeError, ValueError):
            logger.warning("mqtt publish failed (topic=%s)", topic, exc_info=True)
            return False
        if not info.is_published():
            logger.warning("mqtt publish not acknowledged within %ss (topic=%s)", PUBLISH_TIMEOUT_S, topic)
            return False
        return True

    def healthcheck(self) -> bool:
        if self._connected.is_set():
            return True
        lost = self._disconnected_at
        return lost is not None and time.monotonic() - lost < HEALTHY_FOR_S


    def control(
        self, commands: list[dict[str, Any]], timeout: float = CONTROL_TIMEOUT_S
    ) -> list[dict[str, Any]] | None:
        """Send `commands` as one dynamic-security request; responses in order, or None."""
        deadline = time.monotonic() + timeout
        if not self._control_ready.wait(timeout):
            logger.warning("dynsec request skipped, not connected")
            return None
        key = uuid.uuid4().hex
        pending = _Pending(len(commands))
        tagged = [{**cmd, "correlationData": f"{key}:{i}"} for i, cmd in enumerate(commands)]
        with self._lock:
            self._pending[key] = pending
        try:
            info = self._client.publish(CONTROL_TOPIC, json.dumps({"commands": tagged}), qos=1)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.warning("dynsec request publish failed (rc=%s)", info.rc)
                return None
            if not pending.done.wait(max(0.0, deadline - time.monotonic())):
                logger.warning(
                    "dynsec request timed out: %s", [c["command"] for c in commands]
                )
                return None
            return [pending.responses[i] for i in range(len(commands))]
        finally:
            with self._lock:
                self._pending.pop(key, None)


    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            logger.warning("mqtt connect to %s:%s refused: %s", self._host, self._port, reason_code)
            return
        logger.info("mqtt connected to %s:%s", self._host, self._port)
        self._connected.set()
        if self._handler is not None:
            client.subscribe([(topic, 1) for topic in INGEST_TOPICS])
        _rc, self._control_sub_mid = client.subscribe(CONTROL_RESPONSE_TOPIC, 0)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties) -> None:
        if mid == self._control_sub_mid and not any(rc.is_failure for rc in reason_codes):
            self._control_ready.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        if self._connected.is_set():
            self._disconnected_at = time.monotonic()
            logger.warning("mqtt disconnected: %s", reason_code)
        self._connected.clear()
        self._control_ready.clear()

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        if msg.topic == CONTROL_RESPONSE_TOPIC:
            self._on_control_response(msg.payload)
        elif self._handler is not None:
            self._inbox.put(msg)
        elif msg.qos > 0:
            client.ack(msg.mid, msg.qos)

    def _on_control_response(self, payload: bytes) -> None:
        try:
            responses = json.loads(payload).get("responses", [])
        except (ValueError, AttributeError):
            logger.warning("unparsable dynsec response: %r", payload[:200])
            return
        with self._lock:
            for response in responses:
                key, _, index = str(response.get("correlationData", "")).rpartition(":")
                pending = self._pending.get(key)
                if pending is None or not index.isdigit():
                    continue  # another admin client's answer
                pending.responses[int(index)] = response
                if len(pending.responses) == pending.n:
                    pending.done.set()


    def _work(self) -> None:
        while (msg := self._inbox.get()) is not None:
            try:
                assert self._handler is not None
                self._handler(msg.topic, msg.payload, msg.qos)
            except Exception:
                # An ingest bug must not wedge the queue or make the broker redeliver
                # it forever.
                logger.exception("error handling mqtt message on %s", msg.topic)
            finally:
                if msg.qos > 0:
                    self._client.ack(msg.mid, msg.qos)
