#!/usr/bin/env python3
"""
`pager_client.py` -- the pager test client.

Plays both roles a real deployment splits across a physical device and a human's browser or
phone: a simulated pager device (real MQTT, exactly what firmware speaks) and a server driver
(HTTP against the relay's API plus the Firestore REST API with a real Firebase ID token).
`tools/e2e_v2.py` uses it for whole end-to-end runs.

Needs `httpx` and `paho-mqtt`, so run it with the relay virtualenv's interpreter.

Two ways to drive it:
  - stdlib `cmd` REPL:               relay/.venv/bin/python tools/pager_client.py
  - one-shot subcommand (scripting): relay/.venv/bin/python tools/pager_client.py msg "hi"
`--json` on either prints machine-readable output instead of prose.

Device-side flags: `--device-id --host --port --username --password`, and
`--cell <mcc,mnc,tac,ci[,rsrp]>` (docs/PROTOCOL.md §13.2): the device's serving cell, which also
starts it with `loc fail on` so it answers a `loc_req` with `no_fix` + `cell` (same shorthand as
the REPL's `loc cell`).
Server-side flags: `--api --auth-url --as <alias>` (`--as` runs `login <alias>` first).

`--bootstrap "<code>"` performs the setup-code bootstrap fetch instead of using
`--device-id`/`--host`/`--username`/`--password`/`--hmac-key`: parse the code, derive
`bid`/`bpw`/`bkey`, connect as `boot-{bid}`/`bpw` without server-cert validation, fetch and
decrypt the retained bundle, publish the ack, disconnect, then build the `DeviceClient` from the
bundle's `id`/`pw`/`k`/`host`/`port`.

Commands:

  Device: connect, disconnect, crash, inbox, msg, ack, autoack, status, bytes,
          loc <lat> <lon> [acc] | loc auto <period_s> [--walk] |
          loc min <s> | loc fail on|off | loc backoff <s> |
          loc cell <mcc,mnc,tac,ci[,rsrp]>|off |
          loc report <still|cell|move|gnss|stop> | loc track <period_s>,
          contactreq <name> [phone-or-alias],
          sms out <phone> <text...> | sms in <phone> <text...>
  Server: login, contacts, chat, say, watch, tick, sweep, locate <alias>,
          locations <alias> [n],
          admin user-add / allow / deny / device-add <label> / ca push|unpin <device_id> /
          alerts <family> [open|all] / approve <family> <alert_id> [link|create] [alias] /
          block <family> <alert_id> / dismiss <family> <alert_id> /
          cfg <device_id> [--auto <min>] [--clear],
          backend add <kind> <json-config>,
          smscontacts get <device_id> | smscontacts set <device_id> <name> <phone> [<name> <phone>...] |
          smslog <device_id> [limit]

`sms out`/`sms in` publish a signed `kind:"sms_log"` `/up` envelope: `out` always logs
`st:"sent"`; `in` checks the sender against the device's applied `cfg.sms` contact list and logs
`recv` for a listed number, `blocked` for an unlisted one. `smscontacts`/`smslog` drive the
device-owner `GET`/`PUT /api/devices/{id}/sms-contacts` and `GET /api/devices/{id}/sms-log` API.

`DeviceClient.book`/`.lock` hold the simulated device's applied state (docs/PROTOCOL.md §3.2:
`book`/`cfg` are not thread entries and are acked `shown` once applied).
"""

from __future__ import annotations

import argparse
import base64
import cmd
import json
import os
import queue
import re
import shlex
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
import paho.mqtt.client as mqtt

# The simulated device signs with the relay's own `app/devauth.py` and `app/wirecbor.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = REPO_ROOT / "relay"
if str(RELAY_DIR) not in sys.path:
    sys.path.insert(0, str(RELAY_DIR))

from app import devauth, devsetup, wirecbor

DEFAULT_MQTT_HOST = os.environ.get("MQTT_BROKER_HOST", "localhost")
DEFAULT_MQTT_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
DEFAULT_API_URL = os.environ.get("PAGER_API_URL", "http://localhost:8000")
DEFAULT_AUTH_URL = os.environ.get("PAGER_AUTH_URL", "http://localhost:9099")
# The Auth emulator accepts any non-empty key.
FAKE_API_KEY = "fake-api-key"
SIM_KEEPALIVE_S = 60
# CA file trusted for both MQTT (`tls_set`) and the relay API (`httpx` `verify`). Unset: plain
# MQTT and the system trust store.
TLS_CA_CERTS: str | None = os.environ.get("PAGER_CA_CERTS") or None


def _tls(client: mqtt.Client) -> None:
    if TLS_CA_CERTS:
        client.tls_set(ca_certs=TLS_CA_CERTS)


def _http_verify() -> ssl.SSLContext | bool:
    return ssl.create_default_context(cafile=TLS_CA_CERTS) if TLS_CA_CERTS else True


def new_id(prefix: str) -> str:
    """`prefix` + 8 lowercase hex chars, per docs/PROTOCOL.md §1."""
    return f"{prefix}{os.urandom(4).hex()}"


def now_ts() -> int:
    return int(time.time())


def parse_retention_shorthand(value: str) -> dict[str, Any]:
    """`4w` -> `{"n": 4, "unit": "weeks"}`, `10d` -> `{"n": 10, "unit": "days"}`."""
    m = re.fullmatch(r"(\d+)([dw])", value.strip().lower())
    if not m:
        raise ValueError(f"invalid retention shorthand {value!r} (expected e.g. '4w' or '10d')")
    n = int(m.group(1))
    unit = "weeks" if m.group(2) == "w" else "days"
    return {"n": n, "unit": unit}


def parse_cell_arg(value: str) -> dict[str, Any]:
    """`mcc,mnc,tac,ci[,rsrp]` -> the §13.2 `cell` map; mcc/mnc stay strings (leading zeros)."""
    parts = value.split(",")
    if len(parts) not in (4, 5):
        raise ValueError(f"--cell/loc cell must be mcc,mnc,tac,ci[,rsrp], got {value!r}")
    mcc, mnc, tac_s, ci_s = (p.strip() for p in parts[:4])
    try:
        cell: dict[str, Any] = {"mcc": mcc, "mnc": mnc, "tac": int(tac_s), "ci": int(ci_s)}
        if len(parts) == 5:
            cell["rsrp"] = int(parts[4].strip())
    except ValueError as exc:
        raise ValueError(f"--cell/loc cell: tac/ci/rsrp must be integers ({value!r})") from exc
    return cell


def _fs_value(field: dict[str, Any] | None) -> Any:
    """Unwraps one Firestore REST typed value (`{"integerValue": "3"}`) into a plain value."""
    if not field:
        return None
    if "doubleValue" in field:
        return field["doubleValue"]
    if "integerValue" in field:
        return int(field["integerValue"])
    if "booleanValue" in field:
        return field["booleanValue"]
    if "stringValue" in field:
        return field["stringValue"]
    if "nullValue" in field:
        return None
    return None


@dataclass
class ByteCounter:
    published: dict[str, int] = field(default_factory=dict)
    received: dict[str, int] = field(default_factory=dict)

    def add_published(self, topic: str, n: int) -> None:
        self.published[topic] = self.published.get(topic, 0) + n

    def add_received(self, topic: str, n: int) -> None:
        self.received[topic] = self.received.get(topic, 0) + n


@dataclass
class InboxEntry:
    topic: str
    data: dict[str, Any]
    received_at: float = field(default_factory=time.time)


# docs/PROTOCOL.md §14.2: n = (epoch << 20) | lo -- 20 bits of `lo` per
# epoch, 12 bits of epoch left over in the 32-bit counter.
_UP_LO_BITS = 20
_UP_LO_MASK = (1 << _UP_LO_BITS) - 1

# Device-side `/down` window; narrower than the relay's 64-wide `upBits`, same arithmetic.
_DOWN_WINDOW = 32
_DOWN_WINDOW_MASK = (1 << _DOWN_WINDOW) - 1


class DeviceClient:
    """A simulated pager device with the wire shapes of docs/PROTOCOL.md; `hmac_key` plays an
    `authMode: "hmac"` device (§14), `wire` selects the envelope encoding."""

    def __init__(
        self,
        device_id: str,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        *,
        hmac_key: bytes | None = None,
        wire: Literal["json", "cbor"] = "json",
        cell: dict[str, Any] | None = None,
    ):
        self.device_id = device_id
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.hmac_key = hmac_key
        self.wire = wire
        self.session_id = new_id("s_")
        self.connected = False
        self.inbox: list[InboxEntry] = []
        self.autoack: str = "off"  # off | on | shown-only
        self.bytes = ByteCounter()
        self._client: mqtt.Client | None = None
        self._loc_period_s = 0
        self._loc_min_s = 120
        # `loc track <period_s>` sets both: `_tracking` gates `/status`'s `loc_move_s`,
        # `_loc_move_s` is the reported value.
        self._tracking = False
        self._loc_move_s = 0
        # §14.2 `/up`, `/status`, `/loc` counter `n = (epoch << 20) | lo`; `crash()` bumps epoch.
        self._up_epoch = 0
        self._up_lo = 0
        # §14.2 `/down` replay window: highest accepted `n` plus a bitmap of the window below it.
        self._down_n = 0
        self._down_bits = 0
        # Last applied address book (`bv`/`d`/`c`/`p` as on the wire); `None` before the first.
        self.book: dict[str, Any] | None = None
        # `cfg` `lock` map applied cumulatively; `lock_cleared_count` counts applied `clear`s.
        self.lock_auto_min: int | None = None
        self.lock_cleared_count: int = 0
        # Applied `cfg.ca` state, reported in `/status`. A push is applied by trusting the given
        # `sha` (no HTTP fetch); `ca_fp` is its first 16 hex chars.
        self.tls: str = "unpinned"
        self.ca_fp: str | None = None
        # Applied `cfg.sms` list `[{"name", "phone"}]`; `sms in` checks the sender against it.
        self.sms_contacts: list[dict[str, str]] = []
        # Reported in `/status`; a test-only knob (`loc backoff <s>`), not a real backoff.
        self.loc_backoff_s: int = 0
        # §13.3 location state: `_lat`/`_lon` current position, `_last_fix` last successful fix,
        # `_last_attempt_ts` last attempt (successful or not; rule 1's window runs from it).
        self._lat = 37.7749
        self._lon = -122.4194
        self._loc_acc: int | None = None
        # §13.2 serving cell `{mcc, mnc, tac, ci, rsrp?}`, attached to no-fix `/loc` answers.
        # Starting with a cell also starts with `loc fail on`.
        self._cell: dict[str, Any] | None = cell
        self._loc_fail = cell is not None
        self._last_fix: dict[str, Any] | None = None
        self._last_attempt_ts: float | None = None
        # Rule 3 (one fix attempt in flight): serialises concurrent MQTT-thread callbacks.
        self._loc_lock = threading.Lock()
        self._loc_walk = False
        self._auto_stop: threading.Event | None = None
        self._auto_thread: threading.Thread | None = None
        # Test-only knob standing in for GNSS acquisition seconds (rule 2 bounds it at 60 s).
        self.loc_answer_delay_s: float = 0.0

    @property
    def down_topic(self) -> str:
        return f"pager/{self.device_id}/down"

    @property
    def up_topic(self) -> str:
        return f"pager/{self.device_id}/up"

    @property
    def status_topic(self) -> str:
        return f"pager/{self.device_id}/status"

    @property
    def loc_topic(self) -> str:
        return f"pager/{self.device_id}/loc"

    def _build_client(self) -> mqtt.Client:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.device_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
        )
        if self.username:
            client.username_pw_set(self.username, self.password)
        _tls(client)
        # §14.6: the broker-generated LWT carries no signature or `n`, so it bypasses `_publish`.
        lwt_obj = {"v": 1, "state": "offline", "session": self.session_id}
        lwt_payload = self._encode(lwt_obj)
        client.will_set(self.status_topic, lwt_payload, qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        return client

    def connect(self) -> None:
        if self.connected:
            print(f"already connected as {self.device_id} (session={self.session_id})")
            return
        self._client = self._build_client()
        self._client.connect(self.host, self.port, keepalive=SIM_KEEPALIVE_S)
        self._client.loop_start()
        # Let the connect handshake and subscribe land.
        deadline = time.monotonic() + 5.0
        while not self.connected and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.connected:
            print("warning: connect timed out waiting for CONNACK", file=sys.stderr)

    def disconnect(self) -> None:
        self.loc_stop_auto()
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
        self.connected = False
        print(f"disconnected {self.device_id}")

    def crash(self) -> None:
        """New cold-boot session id and reconnect (§1); bumps the `n` epoch (§14.2); see §5.3."""
        self.disconnect()
        self.session_id = new_id("s_")
        if self.hmac_key:
            self._up_epoch += 1
            self._up_lo = 0
        self.connect()

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties) -> None:
        self.connected = True
        client.subscribe(self.down_topic, qos=1)
        self.publish_status()


    def _encode(self, obj: dict[str, Any]) -> bytes:
        """Encodes `obj` unsigned; used only for the LWT (§14.6)."""
        if self.wire == "cbor":
            return wirecbor.encode(obj)
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _decode(self, payload: bytes) -> dict[str, Any] | None:
        """Inverse of `_encode` (first-byte dispatch, §3); `None` if it does not parse."""
        try:
            if wirecbor.is_cbor(payload):
                return wirecbor.decode(payload)
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, KeyError):
            return None

    def _next_up_n(self) -> int:
        """`n = (epoch << 20) | lo` (§14.2), pre-incremented: `upN=0` means nothing accepted yet."""
        self._up_lo += 1
        if self._up_lo > _UP_LO_MASK:
            self._up_lo = 1
            self._up_epoch += 1
        return (self._up_epoch << _UP_LO_BITS) | self._up_lo

    def _accept_down_n(self, n: int) -> bool:
        """Device-side `/down` replay window (§14.2), `_DOWN_WINDOW` wide."""
        if n > self._down_n:
            shift = n - self._down_n
            if shift >= _DOWN_WINDOW:
                self._down_bits = 0
            else:
                self._down_bits = ((self._down_bits << shift) | (1 << (shift - 1))) & _DOWN_WINDOW_MASK
            self._down_n = n
            return True
        gap = self._down_n - n
        if 0 < gap <= _DOWN_WINDOW:
            bit = 1 << (gap - 1)
            if self._down_bits & bit:
                return False
            self._down_bits |= bit
            return True
        return False

    def _on_message(self, client, userdata, msg) -> None:
        self.bytes.add_received(msg.topic, len(msg.payload))
        payload = msg.payload
        if self.hmac_key:
            ok, unsigned = devauth.verify(self.hmac_key, msg.topic, payload)
            if not ok:
                print(f"<- SECURITY dropped bad-sig/unsigned payload on {msg.topic}: {payload[:64]!r}")
                return
            data = self._decode(unsigned)
            if data is None:
                print(f"<- dropped unparsable (post-verify) payload on {msg.topic}: {unsigned[:64]!r}")
                return
            n = data.get("n")
            if not isinstance(n, int) or not self._accept_down_n(n):
                print(f"<- SECURITY dropped replayed/out-of-window /down n={n!r} on {msg.topic}")
                return
        else:
            data = self._decode(payload)
            if data is None:
                print(f"<- received unparsable payload on {msg.topic}: {payload[:64]!r}")
                return
        self.inbox.append(InboxEntry(topic=msg.topic, data=data))
        kind = data.get("kind", "msg")
        print(f"<- [{kind}] {data}")
        if kind == "loc_req":
            # PROTOCOL.md §3.2: MUST NOT ack, MUST NOT render -- answered on
            # /loc instead, subject to §13.3's device-side rate limit.
            self._handle_loc_req(data.get("id"))
            return
        if kind == "book":
            # §3.2 `kind:"book"`: not a thread entry; applied, then acked `shown` unconditionally.
            self._handle_book(data)
            return
        if kind == "cfg":
            self._handle_cfg(data)
            return
        msg_id = data.get("id")
        if not msg_id:
            return
        if self.autoack == "on":
            self.publish_ack(msg_id, "shown")
            self.publish_ack(msg_id, "read")
        elif self.autoack == "shown-only":
            self.publish_ack(msg_id, "shown")

    def _publish(self, topic: str, obj: dict[str, Any], qos: int, retain: bool = False) -> None:
        """§14.3: an `authMode: hmac` device signs every publish with a fresh `n` and `sig`."""
        assert self._client is not None, "not connected"
        if self.hmac_key:
            signed_obj = {**obj, "n": self._next_up_n()}
            if self.wire == "cbor":
                payload = devauth.sign_cbor(self.hmac_key, topic, signed_obj)
            else:
                payload = devauth.sign_json(self.hmac_key, topic, signed_obj)
        else:
            payload = self._encode(obj)
        self.bytes.add_published(topic, len(payload))
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def publish_status(
    ) -> None:
        obj: dict[str, Any] = {
            "v": 1,
            "state": "online",
            "mode": mode,
            "batt_mv": batt_mv,
            "rssi": rssi,
            "session": self.session_id,
            "ts": now_ts(),
            "fw": fw,
            "loc_period_s": self._loc_period_s,
            "loc_min_s": self._loc_min_s,
            "tls": self.tls,
            "loc_backoff_s": self.loc_backoff_s,
            # MQTT-session generation within this boot; constant (sessions are never resumed).
            "link": 1,
        }
        if self.ca_fp is not None:
            obj["ca_fp"] = self.ca_fp
        # Reported only while `loc track` is on.
        if self._tracking:
            obj["loc_move_s"] = self._loc_move_s
        self._publish(self.status_topic, obj, qos=1, retain=True)

    def publish_ack(self, msg_id: str, ack: str) -> None:
        self._publish(self.up_topic, {"v": 1, "id": msg_id, "ts": now_ts(), "ack": ack}, qos=1)
        print(f"-> acked {ack} for {msg_id}")

    def publish_msg(self, body: str, to: str | None = None) -> str:
        msg_id = new_id("u_")
        obj: dict[str, Any] = {"v": 1, "id": msg_id, "ts": now_ts(), "from": self.device_id}
        if to:
            obj["to"] = to
        obj["body"] = body
        obj["ack"] = None
        self._publish(self.up_topic, obj, qos=1)
        print(f"-> sent u-message {msg_id}{' to ' + to if to else ''}: {body!r}")
        return msg_id


    def publish_contact_req(self, name: str, ph: str | None = None) -> str:
        """`/up kind:"contact_req"` (§3.2, §4.2): `ph` is an E.164 number (leading `+`) or an alias,
        passed through unvalidated."""
        req_id = new_id("u_")
        obj: dict[str, Any] = {
            "v": 1,
            "id": req_id,
            "ts": now_ts(),
            "kind": "contact_req",
            "name": name,
            "ack": None,
        }
        if ph:
            obj["ph"] = ph
        self._publish(self.up_topic, obj, qos=1)
        print(f"-> contact_req {req_id}: name={name!r} ph={ph!r}")
        return req_id

    def _handle_book(self, data: dict[str, Any]) -> None:
        """`kind:"book"` (§3.2): assigns `self.book` synchronously, then acks `shown`."""
        self.book = {
            "bv": data.get("bv", 0),
            "d": data.get("d"),
            "c": data.get("c", []),
            "p": data.get("p", []),
        }
        msg_id = data.get("id")
        print(
            f"-> book applied: bv={self.book['bv']} d={self.book['d']!r} "
            f"contacts={[c.get('a') for c in self.book['c']]} "
            f"pending={[p.get('n') for p in self.book['p']]}"
        )
        if msg_id:
            self.publish_ack(msg_id, "shown")

    def _handle_cfg(self, data: dict[str, Any]) -> None:
        """`kind:"cfg"` (§3.2): applies `cfg.lock`, `cfg.ca` and `cfg.sms`, then acks `shown`.
        `cfg.ca` is applied without a real fetch; `lock.clear` is only counted."""
        cfg = data.get("cfg") or {}
        lock = cfg.get("lock") or {}
        if lock.get("clear"):
            self.lock_cleared_count += 1
        if lock.get("auto") is not None:
            self.lock_auto_min = lock["auto"]
        if "ca" in cfg:
            self._apply_ca(cfg["ca"] or {})
        if "sms" in cfg:
            self._apply_sms_contacts(cfg["sms"] or [])
        msg_id = data.get("id")
        print(
            f"-> cfg applied: lock={lock!r} (auto_min now {self.lock_auto_min}) "
            f"tls={self.tls} ca_fp={self.ca_fp} sms_contacts={self.sms_contacts}"
        )
        if msg_id:
            self.publish_ack(msg_id, "shown")

    def _apply_sms_contacts(self, contacts: list[dict[str, Any]]) -> None:
        """`cfg.sms = [{n, p}, ...]` (§10): replaces the whole list, stored under `name`/`phone`."""
        self.sms_contacts = [
            {"name": c.get("n", ""), "phone": c.get("p", "")} for c in contacts
        ]


    def publish_sms_log(self, *, peer: str, direction: Literal["out", "in"], st: str, body: str) -> str:
        """`/up kind:"sms_log"`: the pager's own modem SMS, audited back; not a thread entry."""
        log_id = new_id("s_")
        # No `ack` key: `sms_log` is `{id, ts, peer, dir, st, body, sms_ts}` plus `n`/`sig`.
        obj: dict[str, Any] = {
            "v": 1,
            "id": log_id,
            "ts": now_ts(),
            "kind": "sms_log",
            "peer": peer,
            "dir": direction,
            "st": st,
            "body": body,
            "sms_ts": now_ts(),
        }
        self._publish(self.up_topic, obj, qos=1)
        print(f"-> sms_log {log_id}: dir={direction} peer={peer} st={st} body={body!r}")
        return log_id

    def sms_send(self, peer: str, body: str) -> str:
        """`sms out <phone> <text>`: always succeeds, so `st` is `sent`."""
        return self.publish_sms_log(peer=peer, direction="out", st="sent", body=body)

    def sms_receive(self, peer: str, body: str) -> str:
        """`sms in <phone> <text>`: `st` is `recv` for a listed sender, `blocked` otherwise."""
        listed = any(c["phone"] == peer for c in self.sms_contacts)
        st = "recv" if listed else "blocked"
        return self.publish_sms_log(peer=peer, direction="in", st=st, body=body)

    def _apply_ca(self, ca: dict[str, Any]) -> None:
        url = ca.get("url")
        if not url:
            # `url = ''` un-pins.
            self.tls = "unpinned"
            self.ca_fp = None
            return
        sha = ca.get("sha")
        sha_hex: str | None
        if isinstance(sha, bytes):
            sha_hex = sha.hex()
        elif isinstance(sha, str):
            # JSON wire: base64url, no padding.
            padded = sha + "=" * (-len(sha) % 4)
            sha_hex = base64.urlsafe_b64decode(padded).hex()
        else:
            sha_hex = None
        self.tls = "pinned"
        self.ca_fp = sha_hex[:16] if sha_hex else None


    def _take_fix(self) -> dict[str, Any] | None:
        """One fix attempt: `None` iff `loc fail on`, else the current position."""
        if self._loc_fail:
            return None
        fix: dict[str, Any] = {"lat": self._lat, "lon": self._lon, "fix_ts": now_ts(), "src": "gnss"}
        if self._loc_acc is not None:
            fix["acc"] = self._loc_acc
        self._last_fix = fix
        return fix

    def publish_loc(
        self,
        *,
        req: str | None,
        loc: dict[str, Any] | None,
        cached: bool,
        err: str | None = None,
        why: str | None = None,
    ) -> str:
        loc_id = new_id("l_")
        obj: dict[str, Any] = {"v": 1, "id": loc_id, "ts": now_ts(), "loc": loc, "req": req}
        if cached:
            obj["cached"] = True
        if err:
            obj["err"] = err
        # §13.2: `cell` is attached whenever an answer has no fix and a cell is configured.
        if loc is None and self._cell is not None:
            obj["cell"] = self._cell
        # `why`: reason for an unsolicited report; absent unless set.
        if why is not None:
            obj["why"] = why
        # PROTOCOL.md §2: QoS 1 when `req` is non-null, QoS 0 for an unsolicited periodic fix.
        qos = 1 if req is not None else 0
        self._publish(self.loc_topic, obj, qos=qos)
        print(
            f"-> loc {loc_id} req={req} cached={cached} err={err} loc={loc} "
            f"cell={obj.get('cell')} why={why}"
        )
        return loc_id

    def loc_now(self, lat: float, lon: float, acc: int | None = None) -> None:
        """`loc <lat> <lon> [acc]`: one periodic fix now; also sets the current position."""
        with self._loc_lock:
            self._lat, self._lon, self._loc_acc = lat, lon, acc
            fix = self._take_fix()
            self._last_attempt_ts = time.time()
        if fix is None:
            self.publish_loc(req=None, loc=None, cached=False, err="no_fix")
        else:
            self.publish_loc(req=None, loc=fix, cached=False)

    def loc_report(self, why: str) -> None:
        """`loc report <why>`: one unsolicited cell-only `/loc` tagged `why` (no §13.3 attempt)."""
        self.publish_loc(req=None, loc=None, cached=False, err="no_fix", why=why)

    def loc_track(self, period_s: int) -> None:
        """`loc track <period_s>`: an unsolicited `why:"still"` report every `period_s` until
        `loc_stop_auto`; turns `_tracking` on so `/status` reports `loc_move_s`."""
        self.loc_stop_auto()
        self._loc_period_s = period_s
        self._tracking = True
        self._loc_move_s = period_s
        if period_s <= 0:
            self._tracking = False
            return
        self._auto_stop = threading.Event()
        stop_event = self._auto_stop

        def _loop() -> None:
            while not stop_event.wait(period_s):
                self.loc_report("still")

        self._auto_thread = threading.Thread(target=_loop, daemon=True)
        self._auto_thread.start()

    def loc_auto(self, period_s: int, walk: bool = False) -> None:
        """`loc auto <period_s> [--walk]`: periodic fixes until `loc_stop_auto()`, `disconnect()` or
        `crash()`; `--walk` drifts ~1 m/s northward."""
        self.loc_stop_auto()
        self._loc_period_s = period_s
        self._loc_walk = walk
        if period_s <= 0:
            return
        self._auto_stop = threading.Event()
        stop_event = self._auto_stop

        def _loop() -> None:
            while not stop_event.wait(period_s):
                with self._loc_lock:
                    if self._loc_walk:
                        self._lat += period_s / 111_000.0
                    fix = self._take_fix()
                    self._last_attempt_ts = time.time()
                if fix is None:
                    self.publish_loc(req=None, loc=None, cached=False, err="no_fix")
                else:
                    self.publish_loc(req=None, loc=fix, cached=False)

        self._auto_thread = threading.Thread(target=_loop, daemon=True)
        self._auto_thread.start()

    def loc_stop_auto(self) -> None:
        self._loc_period_s = 0
        self._tracking = False
        self._loc_move_s = 0
        if self._auto_stop is not None:
            self._auto_stop.set()
        if self._auto_thread is not None:
            self._auto_thread.join(timeout=2.0)
        self._auto_thread = None
        self._auto_stop = None

    def loc_set_min(self, seconds: int) -> None:
        """`loc min <s>`: min gap between on-demand attempts (§13.3 rule 1), default 120 s."""
        self._loc_min_s = seconds

    def loc_set_fail(self, on: bool) -> None:
        """`loc fail on|off`: every attempt answers `err:"no_fix"` while on (§13.2)."""
        self._loc_fail = on

    def loc_set_cell(self, cell: dict[str, Any] | None) -> None:
        """`loc cell <mcc,mnc,tac,ci[,rsrp]>|off` (and `--cell`): the cell attached to no-fix
        answers (§13.2). Setting one turns `loc fail on`; `off` does not turn it back off."""
        self._cell = cell
        if cell is not None:
            self._loc_fail = True

    def _handle_loc_req(self, req_id: str | None) -> None:
        """PROTOCOL.md §13.3 rules 1-3: answers a `loc_req` on `/loc` under the rate limit."""
        if not req_id:
            return
        with self._loc_lock:
            now = time.time()
            if (
                self._last_attempt_ts is not None
                and (now - self._last_attempt_ts) < self._loc_min_s
                and self._last_fix is not None
            ):
                # Rule 1: within loc_min_s of the last attempt, answer from the last fix
                # (cached:true).
                # With no last fix, fall through to a real attempt.
                fix = self._last_fix
                cached = True
            else:
                # Rules 2-3: attempt a fix; the lock above serialises concurrent attempts.
                fix = self._take_fix()
                self._last_attempt_ts = now
                cached = False

        def _answer(delay_s: float) -> None:
            if delay_s > 0:
                time.sleep(delay_s)
            if fix is None:
                self.publish_loc(req=req_id, loc=None, cached=False, err="no_fix")
            else:
                self.publish_loc(req=req_id, loc=fix, cached=cached)

        # The delay stands in for GNSS acquisition time; the cached path (rule 1) never delays.
        delay_s = self.loc_answer_delay_s if not cached else 0.0
        if delay_s > 0:
            threading.Thread(target=_answer, args=(delay_s,), daemon=True).start()
        else:
            _answer(0.0)


# Seconds to wait for the broker CONNACK and for the retained bundle.
BOOTSTRAP_TIMEOUT_S = 10.0


def bootstrap_device(
    code: str,
    *,
    port: int | None = None,
    wire: Literal["json", "cbor"] = "json",
    timeout: float = BOOTSTRAP_TIMEOUT_S,
) -> DeviceClient:
    """Fetches and decrypts the retained setup bundle as `boot-{bid}`, acks it, and returns an
    unconnected `DeviceClient`; `port` overrides the bundle's port, `timeout` bounds each wait."""
    print(f"[bootstrap] network: parsing code {code!r}...")
    token_bytes, host, code_port, apn = devsetup.parse(code)
    conn_port = port if port is not None else code_port
    bid, bpw, bkey = devsetup.derive(token_bytes)
    print(
        f"[bootstrap] network: derived bid={bid} host={host} port={conn_port} "
        f"(code said {code_port}) apn={apn!r}"
    )

    down_topic = devsetup.boot_topic_down(bid)
    up_topic = f"pager/boot/{bid}/up"
    username = devsetup.boot_username(bid)

    # PROTOCOL.md §6.1 bootstrap TLS row: no server-cert validation on this hop; plain MQTT here.
    received: queue.Queue[bytes] = queue.Queue()
    connected = threading.Event()

    def _on_connect(client, userdata, connect_flags, reason_code, properties) -> None:
        client.subscribe(down_topic, qos=1)
        connected.set()

    def _on_message(client, userdata, msg) -> None:
        received.put(msg.payload)

    boot_client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=username,
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )
    boot_client.username_pw_set(username, bpw)
    _tls(boot_client)
    boot_client.on_connect = _on_connect
    boot_client.on_message = _on_message
    boot_client.connect(host, conn_port, keepalive=30)
    boot_client.loop_start()
    try:
        if not connected.wait(timeout):
            raise RuntimeError(
                f"bootstrap connect to {host}:{conn_port} as {username!r} timed out after "
                f"{timeout}s (cannot reach broker)"
            )
        print(f"[bootstrap] broker: connected as {username}, subscribed {down_topic}")

        try:
            payload = received.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(
                f"timed out after {timeout}s waiting for the retained bundle on {down_topic} "
                "(no setup code waiting -- expired or already used?)"
            ) from None

        plain = devsetup.decrypt_bundle(bkey, payload)
        print(
            f"[bootstrap] bundle: decrypted {len(payload)} bytes -> id={plain['id']!r} "
            f"label={plain.get('label')!r} host={plain['host']}:{plain['port']}"
        )

        info = boot_client.publish(up_topic, wirecbor.encode({"v": 1, "ok": 1}), qos=1)
        info.wait_for_publish(timeout=timeout)
    finally:
        boot_client.loop_stop()
        boot_client.disconnect()
    print(f"[bootstrap] done: acked on {up_topic}, disconnected {username}")

    device = DeviceClient(
        plain["id"],
        plain["host"],
        conn_port,
        plain["id"],
        plain["pw"],
        hmac_key=plain["k"],
        wire=wire,
    )
    print(f"[bootstrap] continuing as {device.device_id} with the bundle's real credentials")
    return device


class ServerClient:
    """Signs in via a dev-mode custom token, then drives the relay API and Firestore REST API."""

    def __init__(
        self,
        api_url: str,
        auth_url: str,
        project_id: str = "demo-pager",
        firestore_url: str | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.auth_url = auth_url.rstrip("/")
        self.project_id = project_id
        # Firestore and Auth are separate emulators; the Firestore host comes from the auth URL.
        if firestore_url is None:
            from urllib.parse import urlsplit, urlunsplit

            parts = urlsplit(self.auth_url)
            host = parts.hostname or "localhost"
            firestore_url = urlunsplit((parts.scheme, f"{host}:8080", "", "", ""))
        self.firestore_url = firestore_url.rstrip("/")
        self.alias: str | None = None
        self.uid: str | None = None
        self._id_token: str | None = None
        # Filled lazily via `GET /api/me` for `_device_id_for_owner`'s family-scoping filter.
        self._family_id: str | None | Literal["unset"] = "unset"
        self._http = httpx.Client(timeout=10.0, verify=_http_verify())

    @property
    def firestore_base(self) -> str:
        return f"{self.firestore_url}/v1/projects/{self.project_id}/databases/(default)/documents"

    def _headers(self) -> dict[str, str]:
        if self._id_token is None:
            raise RuntimeError("not logged in -- run `login <alias>` first")
        return {"Authorization": f"Bearer {self._id_token}"}


    def login(self, alias: str) -> None:
        resp = self._http.post(f"{self.api_url}/api/dev/token", json={"alias": alias})
        if resp.status_code == 404:
            raise RuntimeError(
                f"login failed: unknown alias {alias!r} (or DEV_MODE is off on the relay)"
            )
        resp.raise_for_status()
        data = resp.json()
        self.uid = data["uid"]
        if data.get("kind") == "session":
            # AUTH_BACKEND=home returns a session token, used as the bearer as-is.
            self._id_token = data["token"]
        else:
            exchange = self._http.post(
                f"{self.auth_url}/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken",
                params={"key": FAKE_API_KEY},
                json={"token": data["token"], "returnSecureToken": True},
            )
            exchange.raise_for_status()
            self._id_token = exchange.json()["idToken"]
        self.alias = alias
        self._family_id = "unset"
        print(f"logged in as {alias} (uid={self.uid})")

    def _my_family_id(self) -> str | None:
        if self._family_id == "unset":
            self._family_id = self.me()["user"]["familyId"]
        return self._family_id


    def api_get(self, path: str) -> httpx.Response:
        return self._http.get(f"{self.api_url}{path}", headers=self._headers())

    def api_post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self._http.post(f"{self.api_url}{path}", json=body, headers=self._headers())

    def api_delete(self, path: str) -> httpx.Response:
        return self._http.delete(f"{self.api_url}{path}", headers=self._headers())

    def api_put(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self._http.put(f"{self.api_url}{path}", json=body, headers=self._headers())

    def say(self, alias: str, text: str) -> dict[str, Any]:
        resp = self.api_post(f"/api/conversations/{alias}/messages", {"body": text})
        if resp.status_code >= 400:
            raise RuntimeError(f"say failed: {resp.status_code} {resp.text}")
        return resp.json()

    def tick(self) -> dict[str, Any]:
        resp = self.api_post("/internal/tick", {})
        resp.raise_for_status()
        return resp.json()

    def sweep(self) -> dict[str, Any]:
        """`POST /internal/sweep`: the retention sweep."""
        resp = self.api_post("/internal/sweep", {})
        resp.raise_for_status()
        return resp.json()

    def add_backend(self, kind: str, config: dict[str, Any], *, enabled: bool = True) -> dict[str, Any]:
        """`POST /api/me/backends`. Only `gchat` is valid; the relay creates it `enabled=False`
        until `verify_backend()` succeeds."""
        resp = self.api_post("/api/me/backends", {"kind": kind, "config": config, "enabled": enabled})
        if resp.status_code >= 400:
            raise RuntimeError(f"add_backend failed: {resp.status_code} {resp.text}")
        return resp.json()

    def verify_backend(self, bid: str, code: str) -> dict[str, Any]:
        """`POST /api/me/backends/{id}/verify`: completes the `gchat` link/verify flow."""
        resp = self.api_post(f"/api/me/backends/{bid}/verify", {"code": code})
        if resp.status_code >= 400:
            raise RuntimeError(f"verify_backend failed: {resp.status_code} {resp.text}")
        return resp.json()

    def locate(self, alias: str) -> dict[str, Any]:
        resp = self.api_post(f"/api/conversations/{alias}/locate", {})
        if resp.status_code >= 400:
            raise RuntimeError(f"locate failed: {resp.status_code} {resp.text}")
        return resp.json()


    def admin_user_add(
        self, alias: str, name: str, *, email: str | None, phone: str | None, admin: bool = False
    ) -> dict[str, Any]:
        body = {
            "alias": alias,
            "displayName": name,
            "email": email,
            "phone": phone,
            "role": "admin" if admin else "member",
            "uid": alias,  # uid == alias for this tool's users
        }
        resp = self.api_post("/api/admin/users", body)
        if resp.status_code >= 400:
            raise RuntimeError(f"admin user-add failed: {resp.status_code} {resp.text}")
        return resp.json()

    def admin_device_add(
        self, label: str, owner_alias: str, *, default_to_alias: str | None = None
    ) -> dict[str, Any]:
        """The relay issues the device id (`pgr-` + 8 hex): `result["device"]["id"]`."""
        body = {"ownerAlias": owner_alias, "label": label}
        if default_to_alias:
            body["defaultToAlias"] = default_to_alias
        resp = self.api_post("/api/admin/devices", body)
        if resp.status_code >= 400:
            raise RuntimeError(f"admin device-add failed: {resp.status_code} {resp.text}")
        return resp.json()

    def admin_set_allow(
        self, from_alias: str, to_alias: str, *, message: bool, locate: bool, one_way: bool
    ) -> None:
        entries = self._current_allowlist_entries()
        entries = [
            e
            for e in entries
            if not (
                (e["fromAlias"] == from_alias and e["toAlias"] == to_alias)
                or (not one_way and e["fromAlias"] == to_alias and e["toAlias"] == from_alias)
            )
        ]
        entries.append(
            {"fromAlias": from_alias, "toAlias": to_alias, "message": message, "locate": locate}
        )
        if not one_way:
            entries.append(
                {"fromAlias": to_alias, "toAlias": from_alias, "message": message, "locate": locate}
            )
        resp = self.api_put("/api/admin/allowlist", {"entries": entries})
        if resp.status_code >= 400:
            raise RuntimeError(f"admin allow failed: {resp.status_code} {resp.text}")

    def admin_deny(self, from_alias: str, to_alias: str) -> None:
        entries = self._current_allowlist_entries()
        # An explicit `message: false` edge: same-family persons are implicitly approved and only a
        # stored deny beats that.
        entries = [
            e
            for e in entries
            if not (e["fromAlias"] == from_alias and e["toAlias"] == to_alias)
        ]
        entries.append(
            {"fromAlias": from_alias, "toAlias": to_alias, "message": False, "locate": False}
        )
        resp = self.api_put("/api/admin/allowlist", {"entries": entries})
        if resp.status_code >= 400:
            raise RuntimeError(f"admin deny failed: {resp.status_code} {resp.text}")

    def _current_allowlist_entries(self) -> list[dict[str, Any]]:
        """`PUT /api/admin/allowlist` replaces all edges, so current edges are read back first."""
        edges = self.api_get("/api/admin/allowlist").json()
        users = {u["uid"]: u["alias"] for u in self.api_get("/api/admin/users").json()}
        return [
            {
                "fromAlias": users.get(e["fromUid"], e["fromUid"]),
                "toAlias": users.get(e["toUid"], e["toUid"]),
                "message": e["message"],
                "locate": e["locate"],
            }
            for e in edges
        ]

    def admin_set_retention(
    ) -> dict[str, Any]:
        resp = self.api_put("/api/admin/settings", payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"admin settings retention failed: {resp.status_code} {resp.text}")
        return resp.json()

    def me(self) -> dict[str, Any]:
        """`GET /api/me`; used to look up the user's own `familyId` for the `family_*` methods."""
        resp = self.api_get("/api/me")
        resp.raise_for_status()
        return resp.json()


    def family_list_alerts(
        self, family: str, status: Literal["open", "all"] = "open"
    ) -> list[dict[str, Any]]:
        resp = self.api_get(f"/api/family/alerts?family={family}&status={status}")
        resp.raise_for_status()
        return resp.json()["alerts"]

    def family_approve_alert(
        self,
        family: str,
        alert_id: str,
        *,
        mode: Literal["link", "create"] | None = None,
        alias: str | None = None,
        name: str | None = None,
        for_alias: str | None = None,
    ) -> dict[str, Any]:
        """`POST /api/family/alerts/{id}/approve`: `contact_request` takes `mode`/`alias`,
        `sms_unknown` takes `name`/`forAlias`, `new_conversation` takes neither."""
        body: dict[str, Any] = {}
        if mode is not None:
            body["mode"] = mode
        if alias is not None:
            body["alias"] = alias
        if name is not None:
            body["name"] = name
        if for_alias is not None:
            body["forAlias"] = for_alias
        resp = self.api_post(f"/api/family/alerts/{alert_id}/approve?family={family}", body)
        if resp.status_code >= 400:
            raise RuntimeError(f"family approve_alert failed: {resp.status_code} {resp.text}")
        return resp.json()

    def family_block_alert(self, family: str, alert_id: str) -> dict[str, Any]:
        resp = self.api_post(f"/api/family/alerts/{alert_id}/block?family={family}", {})
        if resp.status_code >= 400:
            raise RuntimeError(f"family block_alert failed: {resp.status_code} {resp.text}")
        return resp.json()

    def family_dismiss_alert(self, family: str, alert_id: str) -> dict[str, Any]:
        resp = self.api_post(f"/api/family/alerts/{alert_id}/dismiss?family={family}", {})
        if resp.status_code >= 400:
            raise RuntimeError(f"family dismiss_alert failed: {resp.status_code} {resp.text}")
        return resp.json()

    def family_put_approved(
        self,
        family: str,
        uid: str,
        *,
        people: list[dict[str, Any]] | None = None,
        contacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """`PUT /api/family/members/{uid}/approved`: replaces the member's allow edges (`people`)
        and approved SMS contacts (`contacts`); never creates a contact."""
        body = {"people": people or [], "contacts": contacts or []}
        resp = self.api_put(f"/api/family/members/{uid}/approved?family={family}", body)
        if resp.status_code >= 400:
            raise RuntimeError(f"family put_approved failed: {resp.status_code} {resp.text}")
        return resp.json()

    def family_create_contact(self, family: str, phone: str, name: str) -> dict[str, Any]:
        """`POST /api/family/contacts?family=`: idempotent per number; 409 if `name` is taken."""
        resp = self.api_post(
            f"/api/family/contacts?family={family}", {"phone": phone, "name": name}
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"family create_contact failed: {resp.status_code} {resp.text}")
        return resp.json()

    def family_delete_contact(self, family: str, uid: str) -> dict[str, Any]:
        """`DELETE /api/family/contacts/{uid}?family=`."""
        resp = self.api_delete(f"/api/family/contacts/{uid}?family={family}")
        if resp.status_code >= 400:
            raise RuntimeError(f"family delete_contact failed: {resp.status_code} {resp.text}")
        return resp.json()


    def _checked(self, resp: httpx.Response, what: str) -> Any:
        if resp.status_code >= 400:
            raise RuntimeError(f"{what} failed: {resp.status_code} {resp.text}")
        return resp.json()

    def family_create_bridge(self, family: str, owner_uid: str, label: str) -> dict[str, Any]:
        """`POST /api/family/bridges?family=` -> `{bridge, code, expiresAt}`."""
        return self._checked(
            self.api_post(f"/api/family/bridges?family={family}", {"ownerUid": owner_uid, "label": label}),
            "family create_bridge",
        )

    def family_list_bridges(self, family: str) -> list[dict[str, Any]]:
        """`GET /api/family/bridges?family=`."""
        return self._checked(self.api_get(f"/api/family/bridges?family={family}"), "family list_bridges")

    def family_member_chat(self, family: str, uid: str) -> dict[str, Any]:
        """`GET /api/family/members/{uid}/chat?family=` -> `{subscribed, seen, bridges}`."""
        return self._checked(
            self.api_get(f"/api/family/members/{uid}/chat?family={family}"), "family member_chat"
        )

    def family_chat_subscribe(
        self,
        family: str,
        bridge_id: str,
        ref: str,
        *,
        pager_name: str,
        roster: list[dict[str, str]] | None = None,
        can_reply: bool = True,
    ) -> dict[str, Any]:
        """`POST /api/family/bridges/{b}/conversations/{ref}/subscribe`."""
        body = {"pagerName": pager_name, "canReply": can_reply, "roster": roster or []}
        return self._checked(
            self.api_post(
                f"/api/family/bridges/{bridge_id}/conversations/{ref}/subscribe?family={family}", body
            ),
            "family chat_subscribe",
        )

    def family_chat_ignore(self, family: str, bridge_id: str, ref: str) -> dict[str, Any]:
        return self._checked(
            self.api_post(
                f"/api/family/bridges/{bridge_id}/conversations/{ref}/ignore?family={family}", {}
            ),
            "family chat_ignore",
        )

    def family_bridge_inspect(self, family: str, bridge_id: str, link: str) -> dict[str, Any]:
        """`POST /api/family/bridges/{b}/inspect?family=` -> 202 `{outboxId}`."""
        resp = self.api_post(f"/api/family/bridges/{bridge_id}/inspect?family={family}", {"link": link})
        if resp.status_code != 202:
            raise RuntimeError(f"family bridge_inspect failed: {resp.status_code} {resp.text}")
        return resp.json()

    def family_list_contacts(self, family: str) -> list[dict[str, Any]]:
        return self._checked(self.api_get(f"/api/family/contacts?family={family}"), "family list_contacts")

    def admin_push_cfg(
        self, device_id: str, *, auto: int | None = None, clear: bool | None = None
    ) -> dict[str, Any]:
        """`POST /api/admin/devices/{id}/cfg`: remote lock (`cfg.lock.auto`/`cfg.lock.clear`)."""
        lock: dict[str, Any] = {}
        if auto is not None:
            lock["auto"] = auto
        if clear is not None:
            lock["clear"] = clear
        resp = self.api_post(f"/api/admin/devices/{device_id}/cfg", {"lock": lock})
        if resp.status_code >= 400:
            raise RuntimeError(f"admin push_cfg failed: {resp.status_code} {resp.text}")
        return resp.json()

    def admin_push_ca(self, device_id: str, action: Literal["push", "unpin"]) -> dict[str, Any]:
        """`POST /api/admin/devices/{id}/ca`: CA-pointer push or un-pin."""
        resp = self.api_post(f"/api/admin/devices/{device_id}/ca", {"action": action})
        if resp.status_code >= 400:
            raise RuntimeError(f"admin push_ca failed: {resp.status_code} {resp.text}")
        return resp.json()


    def my_devices(self) -> list[dict[str, Any]]:
        """`GET /api/devices`: the logged-in user's own devices (id, label, status)."""
        resp = self.api_get("/api/devices")
        resp.raise_for_status()
        return resp.json()

    def sms_contacts_get(self, device_id: str) -> dict[str, Any]:
        """`GET /api/devices/{id}/sms-contacts` -- owner or admin only."""
        resp = self.api_get(f"/api/devices/{device_id}/sms-contacts")
        if resp.status_code >= 400:
            raise RuntimeError(f"sms_contacts_get failed: {resp.status_code} {resp.text}")
        return resp.json()

    def sms_contacts_put(
        self, device_id: str, contacts: list[dict[str, str]]
    ) -> dict[str, Any]:
        """`PUT /api/devices/{id}/sms-contacts`: `contacts` is the whole list."""
        resp = self.api_put(f"/api/devices/{device_id}/sms-contacts", {"contacts": contacts})
        if resp.status_code >= 400:
            raise RuntimeError(f"sms_contacts_put failed: {resp.status_code} {resp.text}")
        return resp.json()

    def sms_log_get(
        self, device_id: str, *, limit: int = 100, before: int | None = None
    ) -> dict[str, Any]:
        """`GET /api/devices/{id}/sms-log?limit=&before=`: the audit log, newest first."""
        path = f"/api/devices/{device_id}/sms-log?limit={limit}"
        if before is not None:
            path += f"&before={before}"
        resp = self.api_get(path)
        if resp.status_code >= 400:
            raise RuntimeError(f"sms_log_get failed: {resp.status_code} {resp.text}")
        return resp.json()


    def firestore_get(self, path: str) -> httpx.Response:
        return self._http.get(f"{self.firestore_base}/{path}", headers=self._headers())

    def firestore_run_query(self, body: dict[str, Any]) -> httpx.Response:
        return self._http.post(
            f"{self.firestore_base}:runQuery", json=body, headers=self._headers()
        )

    def firestore_run_query_at(self, parent_path: str, body: dict[str, Any]) -> httpx.Response:
        """Like `firestore_run_query`, rooted at `parent_path` (e.g. `devices/{deviceId}`)."""
        return self._http.post(
            f"{self.firestore_base}/{parent_path}:runQuery", json=body, headers=self._headers()
        )

    def contacts(self) -> list[str]:
        """Best-effort: reads `allow/{self}_*` and resolves each `toUid` via `users/{toUid}`, which
        the rules allow only for your own uid or an admin; a non-admin may see raw uids."""
        assert self.uid is not None
        query = {
            "structuredQuery": {
                "from": [{"collectionId": "allow"}],
                "where": {
                    "fieldFilter": {
                        "field": {"fieldPath": "fromUid"},
                        "op": "EQUAL",
                        "value": {"stringValue": self.uid},
                    }
                },
            }
        }
        resp = self.firestore_run_query(query)
        resp.raise_for_status()
        to_uids = []
        for row in resp.json():
            doc = row.get("document")
            if not doc:
                continue
            to_uids.append(doc["fields"]["toUid"]["stringValue"])
        aliases = []
        for uid in to_uids:
            user_resp = self.firestore_get(f"users/{uid}")
            if user_resp.status_code == 200:
                aliases.append(user_resp.json()["fields"]["alias"]["stringValue"])
            else:
                aliases.append(uid)
        return aliases

    def chat(self, alias: str, n: int = 20) -> list[dict[str, Any]]:
        """The thread with `alias`, oldest N: a `messages` query on `convKey` ordered by `seq`."""
        peer_uid = self._resolve_alias_best_effort(alias)
        conv_key = "_".join(sorted([self.uid, peer_uid]))
        query = {
            "structuredQuery": {
                "from": [{"collectionId": "messages"}],
                # Both filters: Firestore authorises a `list` from the declared filters alone,
                # so `convKey` alone 403s under the messages rule (`uid in resource.data.uids`).
                "where": {
                    "compositeFilter": {
                        "op": "AND",
                        "filters": [
                            {
                                "fieldFilter": {
                                    "field": {"fieldPath": "convKey"},
                                    "op": "EQUAL",
                                    "value": {"stringValue": conv_key},
                                }
                            },
                            {
                                "fieldFilter": {
                                    "field": {"fieldPath": "uids"},
                                    "op": "ARRAY_CONTAINS",
                                    "value": {"stringValue": self.uid},
                                }
                            },
                        ],
                    }
                },
                "orderBy": [{"field": {"fieldPath": "seq"}}],
                "limit": n,
            }
        }
        resp = self.firestore_run_query(query)
        resp.raise_for_status()
        out = []
        for row in resp.json():
            doc = row.get("document")
            if not doc:
                continue
            fields = doc["fields"]
            out.append(
                {
                    "id": doc["name"].rsplit("/", 1)[-1],
                    "senderUid": fields.get("senderUid", {}).get("stringValue"),
                    "body": fields.get("body", {}).get("stringValue"),
                }
            )
        return out

    def _resolve_alias_best_effort(self, alias: str) -> str:
        # Dev mode only: users are created with uid == alias.
        return alias

    def _device_id_for_owner(self, owner_uid: str) -> str | None:
        """Device id owned by `owner_uid` that this caller may locate. Firestore authorises a `list`
        from its declared filters alone, so the query filters on `locatableBy array-contains` and
        `familyId` (when set) and matches `owner_uid` client-side."""
        family_id = self._my_family_id()
        locatable_by_filter = {
            "fieldFilter": {
                "field": {"fieldPath": "locatableBy"},
                "op": "ARRAY_CONTAINS",
                "value": {"stringValue": self.uid},
            }
        }
        if family_id is not None:
            where = {
                "compositeFilter": {
                    "op": "AND",
                    "filters": [
                        locatable_by_filter,
                        {
                            "fieldFilter": {
                                "field": {"fieldPath": "familyId"},
                                "op": "EQUAL",
                                "value": {"stringValue": family_id},
                            }
                        },
                    ],
                }
            }
        else:
            where = locatable_by_filter
        query = {
            "structuredQuery": {
                "from": [{"collectionId": "devices"}],
                "where": where,
            }
        }
        resp = self.firestore_run_query(query)
        resp.raise_for_status()
        for row in resp.json():
            doc = row.get("document")
            if not doc:
                continue
            fields = doc["fields"]
            if fields.get("ownerUid", {}).get("stringValue") == owner_uid:
                return doc["name"].rsplit("/", 1)[-1]
        return None

    def locations(self, alias: str, n: int = 20) -> list[dict[str, Any]]:
        """`locations <alias>`: the target's `n` newest `devices/{deviceId}/locations` fixes, read
        from Firestore so the `locatableBy` rule is exercised."""
        peer_uid = self._resolve_alias_best_effort(alias)
        device_id = self._device_id_for_owner(peer_uid)
        if device_id is None:
            raise RuntimeError(
                f"no device found for {alias!r} (either it has none, or this caller is not "
                "permitted to see it -- firestore.rules filtered it out)"
            )
        query = {
            "structuredQuery": {
                "from": [{"collectionId": "locations"}],
                "orderBy": [{"field": {"fieldPath": "createdAt"}, "direction": "DESCENDING"}],
                "limit": n,
            }
        }
        resp = self.firestore_run_query_at(f"devices/{device_id}", query)
        if resp.status_code == 403:
            raise RuntimeError(f"locations({alias!r}) denied by firestore.rules (403)")
        resp.raise_for_status()
        out = []
        for row in resp.json():
            doc = row.get("document")
            if not doc:
                continue
            fields = doc["fields"]
            out.append(
                {
                    "id": doc["name"].rsplit("/", 1)[-1],
                    "lat": _fs_value(fields.get("lat")),
                    "lon": _fs_value(fields.get("lon")),
                    "accM": _fs_value(fields.get("accM")),
                    "fixTs": _fs_value(fields.get("fixTs")),
                    "cached": _fs_value(fields.get("cached")),
                }
            )
        return out


class PagerShell(cmd.Cmd):
    intro = "pager_client.py -- type `help` for commands, Ctrl-D to exit."
    prompt = "(pager) "

    def __init__(self, device: DeviceClient, server: ServerClient, *, as_json: bool = False):
        super().__init__()
        self.device = device
        self.server = server
        self.as_json = as_json

    def _out(self, obj: Any) -> None:
        if self.as_json:
            print(json.dumps(obj, default=str))
        else:
            print(obj)


    def do_connect(self, arg: str) -> None:
        self.device.connect()

    def do_disconnect(self, arg: str) -> None:
        self.device.disconnect()

    def do_crash(self, arg: str) -> None:
        self.device.crash()

    def do_inbox(self, arg: str) -> None:
        rows = [
            {"topic": e.topic, "kind": e.data.get("kind", "msg"), **e.data} for e in self.device.inbox
        ]
        self._out(rows)

    def do_msg(self, arg: str) -> None:
        parts = shlex.split(arg)
        if not parts:
            print("usage: msg [@alias] <text>")
            return
        to = None
        if parts[0].startswith("@"):
            to = parts[0][1:]
            parts = parts[1:]
        body = " ".join(parts)
        self.device.publish_msg(body, to=to)

    def do_ack(self, arg: str) -> None:
        parts = arg.split()
        if len(parts) != 2 or parts[1] not in ("shown", "read"):
            print("usage: ack <id> shown|read")
            return
        self.device.publish_ack(parts[0], parts[1])

    def do_autoack(self, arg: str) -> None:
        mode = arg.strip()
        if mode not in ("on", "off", "shown-only"):
            print("usage: autoack on|off|shown-only")
            return
        self.device.autoack = mode
        print(f"autoack = {mode}")

    def do_status(self, arg: str) -> None:
        parser = argparse.ArgumentParser(prog="status", add_help=False)
        parser.add_argument("--batt", type=int, default=3280)
        parser.add_argument("--mode", choices=["sleep", "active"], default="sleep")
        parser.add_argument("--rssi", type=int, default=-85)
        try:
            ns = parser.parse_args(shlex.split(arg))
        except SystemExit:
            return
        self.device.publish_status(batt_mv=ns.batt, mode=ns.mode, rssi=ns.rssi)

    def do_bytes(self, arg: str) -> None:
        self._out({"published": self.device.bytes.published, "received": self.device.bytes.received})

    def do_loc(self, arg: str) -> None:
        parts = shlex.split(arg)
        if not parts:
            print(
                "usage: loc <lat> <lon> [acc] | loc auto <period_s> [--walk] | "
                "loc min <s> | loc fail on|off | loc backoff <s> | "
                "loc cell <mcc,mnc,tac,ci[,rsrp]>|off | "
                "loc report <still|cell|move|gnss|stop> | loc track <period_s>"
            )
            return
        if parts[0] == "report":
            if len(parts) != 2 or parts[1] not in ("still", "cell", "move", "gnss", "stop"):
                print("usage: loc report <still|cell|move|gnss|stop>")
                return
            self.device.loc_report(parts[1])
            return
        if parts[0] == "track":
            if len(parts) != 2:
                print("usage: loc track <period_s>")
                return
            try:
                period_s = int(parts[1])
            except ValueError:
                print("usage: loc track <period_s>")
                return
            self.device.loc_track(period_s)
            print(f"loc track: period={period_s}s")
            return
        if parts[0] == "cell":
            # Same shorthand as --cell.
            if len(parts) != 2:
                print("usage: loc cell <mcc,mnc,tac,ci[,rsrp]>|off")
                return
            if parts[1] == "off":
                self.device.loc_set_cell(None)
                print("loc cell = off")
                return
            try:
                cell = parse_cell_arg(parts[1])
            except ValueError as exc:
                print(f"usage: loc cell <mcc,mnc,tac,ci[,rsrp]>|off ({exc})")
                return
            self.device.loc_set_cell(cell)
            print(f"loc cell = {cell}")
            return
        if parts[0] == "backoff":
            # Test-only knob for `/status`'s `loc_backoff_s`.
            if len(parts) != 2:
                print("usage: loc backoff <s>")
                return
            self.device.loc_backoff_s = int(parts[1])
            print(f"loc backoff_s = {parts[1]}")
            return
        if parts[0] == "auto":
            parser = argparse.ArgumentParser(prog="loc auto", add_help=False)
            parser.add_argument("period_s", type=int)
            parser.add_argument("--walk", action="store_true")
            try:
                ns = parser.parse_args(parts[1:])
            except SystemExit:
                return
            self.device.loc_auto(ns.period_s, ns.walk)
            print(f"loc auto: period={ns.period_s}s walk={ns.walk}")
        elif parts[0] == "min":
            if len(parts) != 2:
                print("usage: loc min <s>")
                return
            self.device.loc_set_min(int(parts[1]))
            print(f"loc min = {parts[1]}s")
        elif parts[0] == "fail":
            if len(parts) != 2 or parts[1] not in ("on", "off"):
                print("usage: loc fail on|off")
                return
            self.device.loc_set_fail(parts[1] == "on")
            print(f"loc fail = {parts[1]}")
        else:
            try:
                lat = float(parts[0])
                lon = float(parts[1])
                acc = int(parts[2]) if len(parts) > 2 else None
            except (ValueError, IndexError):
                print("usage: loc <lat> <lon> [acc]")
                return
            self.device.loc_now(lat, lon, acc)

    def do_contactreq(self, arg: str) -> None:
        parts = shlex.split(arg)
        if not parts:
            print("usage: contactreq <name> [phone-or-alias]")
            return
        name = parts[0]
        ph = parts[1] if len(parts) > 1 else None
        self.device.publish_contact_req(name, ph)

    def do_sms(self, arg: str) -> None:
        """`sms out|in <phone> <text...>`: publishes a signed `kind:"sms_log"` `/up` envelope."""
        parts = shlex.split(arg)
        if len(parts) < 3 or parts[0] not in ("out", "in"):
            print("usage: sms out|in <phone> <text...>")
            return
        direction, phone, text = parts[0], parts[1], " ".join(parts[2:])
        if direction == "out":
            self.device.sms_send(phone, text)
        else:
            self.device.sms_receive(phone, text)

    def do_book(self, arg: str) -> None:
        """Shows the last-applied address book (`None` before one has arrived), PROTOCOL.md §3.2."""
        self._out(self.device.book)

    def do_lock(self, arg: str) -> None:
        """Shows the applied `cfg.lock` state (PROTOCOL.md §3.2 `kind:"cfg"`)."""
        self._out(
            {"auto_min": self.device.lock_auto_min, "cleared_count": self.device.lock_cleared_count}
        )


    def do_login(self, arg: str) -> None:
        alias = arg.strip()
        if not alias:
            print("usage: login <alias>")
            return
        self.server.login(alias)

    def do_contacts(self, arg: str) -> None:
        self._out(self.server.contacts())

    def do_chat(self, arg: str) -> None:
        parts = arg.split()
        if not parts:
            print("usage: chat <alias> [n]")
            return
        alias = parts[0]
        n = int(parts[1]) if len(parts) > 1 else 20
        self._out(self.server.chat(alias, n))

    def do_say(self, arg: str) -> None:
        parts = shlex.split(arg)
        if len(parts) < 2:
            print("usage: say <alias> <text>")
            return
        alias, text = parts[0], " ".join(parts[1:])
        self._out(self.server.say(alias, text))

    def do_locate(self, arg: str) -> None:
        alias = arg.strip()
        if not alias:
            print("usage: locate <alias>")
            return
        self._out(self.server.locate(alias))

    def do_locations(self, arg: str) -> None:
        parts = arg.split()
        if not parts:
            print("usage: locations <alias> [n]")
            return
        alias = parts[0]
        n = int(parts[1]) if len(parts) > 1 else 20
        self._out(self.server.locations(alias, n))

    def do_watch(self, arg: str) -> None:
        alias = arg.strip()
        if not alias:
            print("usage: watch <alias>")
            return
        print(f"watching thread with {alias} (Ctrl-C to stop)...")
        seen: set[str] = set()
        try:
            while True:
                for m in self.server.chat(alias, 50):
                    if m["id"] not in seen:
                        seen.add(m["id"])
                        print(f"[{m['senderUid']}] {m['body']}")
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("stopped watching")

    def do_tick(self, arg: str) -> None:
        self._out(self.server.tick())

    def do_sweep(self, arg: str) -> None:
        self._out(self.server.sweep())

    def do_backend(self, arg: str) -> None:
        parts = shlex.split(arg)
        if len(parts) < 1 or parts[0] != "add":
            print("usage: backend add <kind> <json-config> [--disabled]")
            return
        self._backend_add(parts[1:])

    def _backend_add(self, args: list[str]) -> None:
        parser = argparse.ArgumentParser(prog="backend add", add_help=False)
        parser.add_argument("kind")
        parser.add_argument("config", help="JSON object, e.g. '{\"phone\": \"+15551234567\"}'")
        parser.add_argument("--disabled", action="store_true")
        try:
            ns = parser.parse_args(args)
        except SystemExit:
            return
        try:
            config = json.loads(ns.config)
        except json.JSONDecodeError as exc:
            print(f"invalid JSON config: {exc}")
            return
        self._out(self.server.add_backend(ns.kind, config, enabled=not ns.disabled))

    def do_admin(self, arg: str) -> None:
        parts = shlex.split(arg)
        if not parts:
            print(
                "usage: admin user-add|device-add|allow|deny|settings|alerts|"
                "approve|block|dismiss|cfg|ca ..."
            )
            return
        sub, rest = parts[0], parts[1:]
        if sub == "user-add":
            self._admin_user_add(rest)
        elif sub == "device-add":
            self._admin_device_add(rest)
        elif sub == "allow":
            self._admin_allow(rest)
        elif sub == "deny":
            self._admin_deny(rest)
        elif sub == "settings":
            self._admin_settings(rest)
        elif sub == "alerts":
            self._admin_alerts(rest)
        elif sub == "approve":
            self._admin_approve(rest)
        elif sub == "block":
            self._admin_block(rest)
        elif sub == "dismiss":
            self._admin_dismiss(rest)
        elif sub == "cfg":
            self._admin_cfg(rest)
        elif sub == "ca":
            self._admin_ca(rest)
        else:
            print(f"unknown admin subcommand: {sub}")

    def _admin_user_add(self, args: list[str]) -> None:
        parser = argparse.ArgumentParser(prog="admin user-add", add_help=False)
        parser.add_argument("alias")
        parser.add_argument("name")
        parser.add_argument("--email")
        parser.add_argument("--phone")
        parser.add_argument("--admin", action="store_true")
        try:
            ns = parser.parse_args(args)
        except SystemExit:
            return
        self._out(
            self.server.admin_user_add(
                ns.alias, ns.name, email=ns.email, phone=ns.phone, admin=ns.admin
            )
        )

    def _admin_device_add(self, args: list[str]) -> None:
        parser = argparse.ArgumentParser(prog="admin device-add", add_help=False)
        parser.add_argument("label", help="display name; the relay issues the device id")
        parser.add_argument("--owner", required=True)
        parser.add_argument("--default-to")
        try:
            ns = parser.parse_args(args)
        except SystemExit:
            return
        result = self.server.admin_device_add(ns.label, ns.owner, default_to_alias=ns.default_to)
        print(f"device {result['device']['id']} created; setup code: {result['setupCode']}")
        self._out(result)

    def _admin_allow(self, args: list[str]) -> None:
        parser = argparse.ArgumentParser(prog="admin allow", add_help=False)
        parser.add_argument("a")
        parser.add_argument("b")
        parser.add_argument("--no-locate", action="store_true")
        parser.add_argument("--one-way", action="store_true")
        try:
            ns = parser.parse_args(args)
        except SystemExit:
            return
        self.server.admin_set_allow(
            ns.a, ns.b, message=True, locate=not ns.no_locate, one_way=ns.one_way
        )
        print(f"allowed {ns.a} -> {ns.b}{'' if ns.one_way else ' (and back)'}")

    def _admin_deny(self, args: list[str]) -> None:
        if len(args) != 2:
            print("usage: admin deny <a> <b>")
            return
        self.server.admin_deny(args[0], args[1])
        print(f"denied {args[0]} -> {args[1]}")

    def _admin_settings(self, args: list[str]) -> None:
        if not args or args[0] != "retention":
            return
        kv: dict[str, str] = {}
        for item in args[1:]:
            key, sep, value = item.partition("=")
            if not sep:
                return
            kv[key] = value
        try:
            result = self.server.admin_set_retention(
            )
        except ValueError as exc:
            print(f"invalid retention value: {exc}")
            return
        self._out(result)

    def _admin_alerts(self, args: list[str]) -> None:
        if not args:
            print("usage: admin alerts <family> [open|all]")
            return
        family = args[0]
        status = args[1] if len(args) > 1 else "open"
        if status not in ("open", "all"):
            print("usage: admin alerts <family> [open|all]")
            return
        self._out(self.server.family_list_alerts(family, status))

    def _admin_approve(self, args: list[str]) -> None:
        parser = argparse.ArgumentParser(prog="admin approve", add_help=False)
        parser.add_argument("family")
        parser.add_argument("alert_id")
        parser.add_argument("mode", nargs="?", choices=["link", "create"])
        parser.add_argument("alias", nargs="?")
        try:
            ns = parser.parse_args(args)
        except SystemExit:
            return
        self._out(
            self.server.family_approve_alert(
                ns.family, ns.alert_id, mode=ns.mode, alias=ns.alias
            )
        )

    def _admin_block(self, args: list[str]) -> None:
        if len(args) != 2:
            print("usage: admin block <family> <alert_id>")
            return
        self._out(self.server.family_block_alert(args[0], args[1]))

    def _admin_dismiss(self, args: list[str]) -> None:
        if len(args) != 2:
            print("usage: admin dismiss <family> <alert_id>")
            return
        self._out(self.server.family_dismiss_alert(args[0], args[1]))

    def _admin_cfg(self, args: list[str]) -> None:
        parser = argparse.ArgumentParser(prog="admin cfg", add_help=False)
        parser.add_argument("device_id")
        parser.add_argument("--auto", type=int)
        parser.add_argument("--clear", action="store_true")
        try:
            ns = parser.parse_args(args)
        except SystemExit:
            return
        self._out(
            self.server.admin_push_cfg(
                ns.device_id, auto=ns.auto, clear=ns.clear if ns.clear else None
            )
        )

    def _admin_ca(self, args: list[str]) -> None:
        parser = argparse.ArgumentParser(prog="admin ca", add_help=False)
        parser.add_argument("action", choices=["push", "unpin"])
        parser.add_argument("device_id")
        try:
            ns = parser.parse_args(args)
        except SystemExit:
            return
        self._out(self.server.admin_push_ca(ns.device_id, ns.action))


    def do_devices(self, arg: str) -> None:
        """`GET /api/devices` -- the logged-in user's own devices."""
        self._out(self.server.my_devices())

    def do_smscontacts(self, arg: str) -> None:
        """`smscontacts get <device_id>` / `smscontacts set <device_id> <name> <phone> [...]`."""
        parts = shlex.split(arg)
        if len(parts) >= 2 and parts[0] == "get":
            self._out(self.server.sms_contacts_get(parts[1]))
            return
        if len(parts) >= 2 and parts[0] == "set":
            device_id = parts[1]
            rest = parts[2:]
            if len(rest) % 2 != 0:
                print("usage: smscontacts set <device_id> <name> <phone> [<name> <phone> ...]")
                return
            contacts = [
                {"name": rest[i], "phone": rest[i + 1]} for i in range(0, len(rest), 2)
            ]
            self._out(self.server.sms_contacts_put(device_id, contacts))
            return
        print("usage: smscontacts get|set <device_id> ...")

    def do_smslog(self, arg: str) -> None:
        """`smslog <device_id> [limit]` -- `GET /api/devices/{id}/sms-log`."""
        parts = shlex.split(arg)
        if not parts:
            print("usage: smslog <device_id> [limit]")
            return
        device_id = parts[0]
        limit = int(parts[1]) if len(parts) > 1 else 100
        self._out(self.server.sms_log_get(device_id, limit=limit))


    def do_exit(self, arg: str) -> bool:
        return True

    def do_EOF(self, arg: str) -> bool:
        print()
        return True


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device-id", default=os.environ.get("PAGER_DEVICE_ID", "pgr-cli"))
    parser.add_argument("--host", default=DEFAULT_MQTT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_MQTT_PORT)
    parser.add_argument("--username", default=os.environ.get("MQTT_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"))
    parser.add_argument(
        "--hmac-key",
        default=os.environ.get("PAGER_HMAC_KEY", ""),
        help="base64 device HMAC key (docs/PROTOCOL.md §14) -- every publish is signed "
        "with it and every /down is verified and dropped on failure. Omit or pass '' to "
        "play an unsigned authMode:password device (the default).",
    )
    parser.add_argument(
        "--wire",
        choices=["json", "cbor"],
        default=os.environ.get("PAGER_WIRE", "json"),
        help="wire encoding for this device's publishes (default: json)",
    )
    parser.add_argument(
        "--bootstrap",
        metavar="CODE",
        help="setup-code bootstrap fetch: derives and fetches this "
        "device's real credentials from the broker first, then continues as that device -- "
        "overrides --device-id/--username/--password/--hmac-key (the bundle supplies all four); "
        "--host stays the code's own host, but --port (still 1883 by default) overrides both the "
        "bootstrap hop and the continuation, since this tool never speaks TLS",
    )
    parser.add_argument(
        "--cell",
        metavar="MCC,MNC,TAC,CI[,RSRP]",
        help="PROTOCOL.md §13.2 cell-tower location fallback: this device's serving "
        "cell, attached to every /loc answer that has no fix -- e.g. --cell 310,410,12345,87654321,-95 "
        "makes this simulated pager answer a loc_req with no_fix + cell instead of plain no_fix "
        "(same effect as 'loc fail on' plus this option, or simply before any real fix has been set)",
    )
    parser.add_argument("--api", default=DEFAULT_API_URL, help="relay API base URL")
    parser.add_argument("--auth-url", default=DEFAULT_AUTH_URL, help="Auth emulator base URL")
    parser.add_argument("--as", dest="as_alias", help="log in as this alias before running")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="one-shot command (omit for the REPL)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    cell = parse_cell_arg(args.cell) if args.cell else None

    if args.bootstrap:
        device = bootstrap_device(args.bootstrap, port=args.port, wire=args.wire)
        if cell is not None:
            device.loc_set_cell(cell)
    else:
        hmac_key = base64.b64decode(args.hmac_key) if args.hmac_key else None
        device = DeviceClient(
            args.device_id,
            args.host,
            args.port,
            args.username,
            args.password,
            hmac_key=hmac_key,
            wire=args.wire,
            cell=cell,
        )
    server = ServerClient(args.api, args.auth_url)
    shell = PagerShell(device, server, as_json=args.json)

    if args.as_alias:
        server.login(args.as_alias)

    if args.command:
        line = shlex.join(args.command)
        shell.onecmd(line)
        return 0

    shell.cmdloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
