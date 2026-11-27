#!/usr/bin/env python3
"""
End-to-end integration suite for the pager relay. Brings up the real `relay/docker-compose.yml`
stack (EMQX + Firebase emulators + relay, `DEV_MODE=1`), imports `tools/pager_client.py` as a
library (one process plays the pager device and every human in the scenario), and runs named
scenarios: bootstrap, text round-trip, allow-list and republish; location (periodic fixes, read
permission, on-demand `/locate` incl. coalescing, cached answer, `no_fix`, derived expiry);
retention and byte accounting.

Usage (needs the relay virtualenv -- imports `httpx`/`firebase-admin`):
    relay/.venv/bin/python tools/e2e_v2.py                 # all scenarios
    relay/.venv/bin/python tools/e2e_v2.py bootstrap allowlist
    relay/.venv/bin/python tools/e2e_v2.py location_periodic location_on_demand
    relay/.venv/bin/python tools/e2e_v2.py --stack home     # the home stack (Mosquitto), through nginx/TLS
    relay/.venv/bin/python tools/e2e_v2.py --stack home-emqx  # the same with the EMQX override
    relay/.venv/bin/python tools/e2e_v2.py --stack move       # cloud stack -> home stack, both at once
    relay/.venv/bin/python tools/e2e_v2.py --stack move-emqx  # the same, the home stack on EMQX

Exit code 0 if every requested scenario passes, 1 otherwise. Always tears
down the compose stack it started.

Known gap: EMQX device credentials are not provisioned. `tools/emqx_setup.py` and
`relay/docker-compose.yml` configure only the rule-engine webhook and the relay's REST API key, so
the local EMQX accepts anonymous MQTT connections and enforces no topic ACLs; the device in this
suite connects without presenting its credentials. A real deployment sets them up in the broker.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("e2e_v2")

REPO_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = REPO_ROOT / "relay"
TOOLS_DIR = REPO_ROOT / "tools"
COMPOSE_FILE = RELAY_DIR / "docker-compose.yml"
# `--override FILE` appends compose files.
COMPOSE_FILES: list[Path] = [COMPOSE_FILE]
ENV_PATH = RELAY_DIR / ".env"
EMQX_SETUP_SCRIPT = TOOLS_DIR / "emqx_setup.py"
RELAY_VENV_PYTHON = RELAY_DIR / ".venv" / "bin" / "python3"

# `--stack home` uses home/docker-compose.yml (the relay as an MQTT client of Mosquitto,
# BROKER_MODE=mqtt) plus the `test` profile (bridge-sim), reached only through its nginx edge
# with the stack's own CA. `--stack home-emqx` adds the EMQX override (BROKER_MODE=rest).
STACK = "cloud"
HOME_STACK = False  # STACK is "home" or "home-emqx"
BROKER = "emqx"  # the broker's compose service: "emqx" or "mosquitto"
BROKER_ADMIN_PASSWORD = ""  # home stack: the dynsec `relay` client's password
HOME_DIR = REPO_ROOT / "home"
# The home stacks run as their own compose project with their own gitignored data directory and
# env file, so a test run never touches a live deployment's files.
HOME_PROJECT = "pager-e2e"
HOME_DATA = HOME_DIR / ".e2e-data"
HOME_ENV = HOME_DIR / ".e2e.env"
HOME_CA = HOME_DATA / "certs" / "ca" / "ca.crt"

RELAY_URL = "http://localhost:8000"
AUTH_URL = "http://localhost:9099"
MQTT_HOST = "localhost"
MQTT_PORT = 1883
EMQX_API_URL = "http://localhost:18083"
FIREBASE_PROJECT_ID = "demo-pager"
BROKER_API_KEY = "dev-broker-api-key"
BROKER_API_SECRET = "dev-broker-api-secret"
WEBHOOK_KEY = "dev-webhook-key"
# docs/PROTOCOL.md §13.4: 900 s in production. Shortened so the derived-expiry sub-case does not
# wait 15 minutes, yet longer than every other sub-case (incl. the 3 s answer delay).
LOC_REQ_TTL_S = 10
# A bridge is offline after this many seconds of silence (production: 900); above the sim's
# 60 s `lastSeenAt` touch. The `bridge_health` scenario backdates `lastSeenAt` instead.
BRIDGE_OFFLINE_AFTER_S = 120

sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(RELAY_DIR))
import httpx
import pager_client

_env_backup: str | None = None

# True when the relay under test answers `GET /api/setup/status`, i.e. runs `AUTH_BACKEND=home`.
# The admin is then created through the first-run API and `store()` goes through `/api/dev/db`.
HOME_AUTH = False

# Wire encoding every `make_device()` plays; set once in `main()` from `--wire`.
WIRE_MODE: str = "json"

# `make_device` caches one `DeviceClient` per device id: §14.2's `n` is per-device state, so a
# second client for the same device would restart at `n=0` and be dropped as a replay.
# `disconnect()`/`connect()` models a warm reconnect; only `crash()` (§1) models a cold boot.
_DEVICE_CLIENTS: dict[str, pager_client.DeviceClient] = {}

# The relay issues device ids (`pgr-` + 8 hex); scenarios name devices by a stable label
# (e.g. "pgr-e2e-1") and look the issued id up here. `_DEVICE_CLIENTS` is keyed by issued id.
_ISSUED_IDS: dict[str, str] = {}


def issued_id(label: str) -> str:
    """The relay-issued device id for a device this run created under `label`."""
    return _ISSUED_IDS[label]


def create_device_with_secret(
    admin: pager_client.ServerClient,
    device_id: str,
    owner_alias: str,
    *,
    default_to_alias: str | None = None,
) -> dict:
    """Creates a device via `POST /api/admin/devices` and runs the real bootstrap fetch
    (`pager_client.bootstrap_device`); caches the signed `DeviceClient` in `_DEVICE_CLIENTS`."""
    label = device_id  # the scenario's name for the device; the relay issues the real id
    info = admin.admin_device_add(label, owner_alias, default_to_alias=default_to_alias)
    issued = info["device"]["id"]
    _ISSUED_IDS[label] = issued
    print(f"device created: label={label!r} issued id={issued}")
    device = pager_client.bootstrap_device(info["setupCode"], port=MQTT_PORT, wire=WIRE_MODE)
    assert device.device_id == issued, (device.device_id, issued)
    _DEVICE_CLIENTS[issued] = device
    return info


def make_device(device_id: str) -> pager_client.DeviceClient:
    """Returns the cached `DeviceClient` for `device_id` (see `_DEVICE_CLIENTS`); an unprovisioned
    id gets an unsigned device speaking this run's `--wire` encoding."""
    device_id = _ISSUED_IDS.get(device_id, device_id)  # accept a label or an issued id
    existing = _DEVICE_CLIENTS.get(device_id)
    if existing is not None:
        return existing
    device = pager_client.DeviceClient(
        device_id,
        MQTT_HOST,
        MQTT_PORT,
        None,
        None,
        hmac_key=None,
        wire=WIRE_MODE,
    )
    _DEVICE_CLIENTS[device_id] = device
    return device


def compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    files = [arg for path in COMPOSE_FILES for arg in ("-f", str(path))]
    profiles = ["-p", HOME_PROJECT, "--profile", "test"] if HOME_STACK else []
    cmd = ["docker", "compose", *files, *profiles, *args]
    print(f"$ {' '.join(cmd)}")
    env = dict(os.environ)
    if HOME_STACK:
        # A relay image without the web export, and no background scheduler racing scenarios that
        # call /internal/tick themselves.
        env.update(
            PAGER_WEB="none",
            SCHEDULER_MODE="none",
            PAGER_DATA_DIR=str(HOME_DATA),
            PAGER_ENV_FILE=str(HOME_ENV),
        )
    return subprocess.run(
        cmd,
        cwd=str(COMPOSE_FILES[0].parent),
        check=check,
        capture_output=capture,
        text=True,
        timeout=600,
        env=env,
    )


def wait_until(
    predicate: Callable[[], bool], *, timeout: float, interval: float = 0.5, description: str = ""
) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # noqa: BLE001 -- collected for the failure message
            last_exc = exc
        time.sleep(interval)
    extra = f" (last error: {last_exc!r})" if last_exc else ""
    raise AssertionError(f"timed out after {timeout}s waiting for: {description}{extra}")


def _env_path() -> Path:
    return HOME_ENV if HOME_STACK else ENV_PATH


def write_env_file() -> None:
    global _env_backup
    path = _env_path()
    if path.exists() and not HOME_STACK:  # home/.e2e.env is the suite's own file
        _env_backup = path.read_text()
    if HOME_STACK:
        # The suite's own env file: pager.env's two values plus test-only switches (DEV_MODE gates
        # /api/dev/token and /api/dev/db, and the rate limits are raised).
        lines = [
            "PAGER_HOST=localhost",
            "TLS_MODE=selfsigned",
            "PAGER_PATH=/",
            "DEV_MODE=1",
            "RATE_LIMIT_BACKEND_CREATE_LIMIT=1000",
            "RATE_LIMIT_ADMIN_WRITE_LIMIT=1000",
            "RATE_LIMIT_WEBHOOK_IP_LIMIT=1000",
            "RATE_LIMIT_AUTH_IP_LIMIT=1000",
        ]
    else:
        lines = [
            "BROKER_API_URL=http://emqx:18083/api/v5",
            f"BROKER_API_KEY={BROKER_API_KEY}",
            f"BROKER_API_SECRET={BROKER_API_SECRET}",
            f"WEBHOOK_KEY={WEBHOOK_KEY}",
        ]
    path.write_text(
        "\n".join(
            [
                *lines,
                f"LOC_REQ_TTL_S={LOC_REQ_TTL_S}",
                f"BRIDGE_OFFLINE_AFTER_S={BRIDGE_OFFLINE_AFTER_S}",
                "",
            ]
        )
    )


def restore_env_file() -> None:
    path = _env_path()
    if _env_backup is not None:
        path.write_text(_env_backup)
    else:
        path.unlink(missing_ok=True)


def _home_secrets() -> dict[str, str]:
    try:
        text = (HOME_DATA / "secrets.env").read_text()
    except PermissionError:
        # On Linux the file is root's, mode 0600; read it from inside a container.
        text = compose(
            "run", "--rm", "--no-deps", "--entrypoint", "cat", "init", "/data/secrets.env",
            capture=True,
        ).stdout
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def configure_emqx() -> None:
    if HOME_STACK:
        # A one-shot compose service runs the same script inside the compose network.
        compose("run", "--rm", "emqx-setup")
        return
    result = subprocess.run(
        [
            sys.executable,
            str(EMQX_SETUP_SCRIPT),
            "--emqx-url",
            EMQX_API_URL,
            "--relay-url",
            "http://relay:8000",
            "--webhook-key",
            WEBHOOK_KEY,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, end="", file=sys.stderr)
        raise AssertionError(f"tools/emqx_setup.py failed (exit {result.returncode})")


def _verify() -> Any:
    """httpx `verify` for RELAY_URL: the home stack's own CA (nginx on 443)."""
    if HOME_STACK:
        import ssl

        return ssl.create_default_context(cafile=str(HOME_CA))
    return True


def _relay_reachable() -> bool:
    try:
        resp = httpx.get(f"{RELAY_URL}/healthz", timeout=3.0, verify=_verify())
        return resp.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


def _emqx_reachable() -> bool:
    try:
        resp = httpx.get(f"{EMQX_API_URL}/api/v5/status", timeout=3.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _auth_emulator_reachable() -> bool:
    # The compose healthcheck and `/healthz` probe only Firestore, not the Auth emulator (9099) in
    # the same container, which can still be starting; wait for it explicitly.
    try:
        # Any HTTP response means the emulator is up and routing (only DELETE is valid here, hence
        # 405); a connection error or reset means it is not.
        resp = httpx.get(
            f"{AUTH_URL}/emulator/v1/projects/{FIREBASE_PROJECT_ID}/accounts", timeout=3.0
        )
        return resp.status_code == 405
    except httpx.HTTPError:
        return False


def start_stack(*, build: bool) -> None:
    global HOME_AUTH, BROKER_API_KEY, BROKER_API_SECRET, WEBHOOK_KEY, BROKER_ADMIN_PASSWORD
    write_env_file()
    if HOME_STACK and HOME_DATA.exists() and any(HOME_DATA.iterdir()):
        print(f"{HOME_DATA} is left over from an earlier run; clearing it and its stack")
        _clear_home_data()
        compose("down", "-v", check=False)
    compose(*(["up", "-d", "--build"] if build else ["up", "-d"]))
    wait_until(_relay_reachable, timeout=120, interval=1, description="relay to become healthy")
    if HOME_STACK:
        secrets = _home_secrets()
        BROKER_API_KEY = secrets["BROKER_API_KEY"]
        BROKER_API_SECRET = secrets["BROKER_API_SECRET"]
        WEBHOOK_KEY = secrets["WEBHOOK_KEY"]
        BROKER_ADMIN_PASSWORD = secrets["BROKER_ADMIN_PASSWORD"]
    else:
        wait_until(
            _auth_emulator_reachable, timeout=60, interval=1, description="Auth emulator to become reachable"
        )
    HOME_AUTH = httpx.get(f"{RELAY_URL}/api/setup/status", timeout=5.0, verify=_verify()).status_code == 200
    if HOME_AUTH:
        print("relay runs AUTH_BACKEND=home: first-run admin, store oracle over /api/dev/db")
    if STACK == "cloud":
        configure_emqx()  # the home stack's emqx-setup service already ran on `up`


def stop_stack() -> None:
    print("\n=== tearing down the compose stack ===")
    if _HOST_MQTT is not None:
        _HOST_MQTT.stop()
    if HOME_STACK and HOME_DATA.exists():
        _clear_home_data()
    compose("down", "-v", check=False)
    if HOME_STACK and HOME_DATA.exists():
        HOME_DATA.rmdir()
    restore_env_file()


def _clear_home_data() -> None:
    # The containers wrote the data directory as root; clear it from inside one.
    compose(
        "run", "--rm", "--no-deps", "--entrypoint", "sh", "init", "-c",
        "rm -rf /data/* /data/.[!.]*", check=False,
    )


def dump_logs() -> None:
    services = ("relay", BROKER, "nginx") if HOME_STACK else ("relay", "emqx", "firebase")
    for service, tail in zip(services, ("120", "60", "40"), strict=True):
        print(f"\n--- docker compose logs {service} --tail {tail} ---")
        result = compose("logs", service, "--tail", tail, check=False, capture=True)
        print(result.stdout)


def internal(path: str) -> dict[str, Any]:
    """`POST /internal/{tick,sweep}`. The home stack's nginx refuses `/internal/` from outside, so
    there the call is made from inside the compose network with `X-Relay-Webhook-Key`."""
    if not HOME_STACK:
        resp = httpx.post(f"{RELAY_URL}{path}", timeout=60.0, verify=_verify())
        resp.raise_for_status()
        return resp.json()
    script = (
        "import httpx,sys;"
        f"r=httpx.post('http://localhost:8000{path}',headers={{'X-Relay-Webhook-Key':sys.argv[1]}},timeout=60);"
        "r.raise_for_status();print(r.text)"
    )
    import json

    result = compose("exec", "-T", "relay", "python", "-c", script, WEBHOOK_KEY, capture=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


def bootstrap_admin(alias: str = "admin", email: str = "admin@example.com") -> None:
    """Runs `python -m app.bootstrap` from the host against the published emulator ports; a
    home-auth relay uses the first-run API instead."""
    if HOME_AUTH:
        # Idempotent like app.bootstrap: later scenarios call this again.
        status = httpx.get(f"{RELAY_URL}/api/setup/status", timeout=10.0, verify=_verify())
        if not status.json()["needsSetup"]:
            return
        resp = httpx.post(
            f"{RELAY_URL}/api/setup/first-admin",
            json={"username": alias, "password": os.urandom(12).hex()},
            headers={"X-Requested-With": "pager"},
            timeout=10.0,
            verify=_verify(),
        )
        assert resp.status_code == 200, resp.text
        print(f"admin created through /api/setup/first-admin: uid={resp.json()['uid']}")
        return
    python = str(RELAY_VENV_PYTHON) if RELAY_VENV_PYTHON.exists() else sys.executable
    env = dict(os.environ)
    env.update(
        FIRESTORE_EMULATOR_HOST="localhost:8080",
        FIREBASE_AUTH_EMULATOR_HOST="localhost:9099",
        GOOGLE_CLOUD_PROJECT=FIREBASE_PROJECT_ID,
    )
    result = subprocess.run(
        [python, "-m", "app.bootstrap", "--admin-email", email, "--alias", alias],
        cwd=str(RELAY_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, end="", file=sys.stderr)
        raise AssertionError("app.bootstrap failed")


def _use_relay_store() -> None:
    """Sets the emulator env vars `app.db.firestore` reads (before its first use in this process)
    and makes `import app.*` resolve against `relay/`. Cloud stack only."""
    assert not HOME_AUTH, "the Firestore emulator is not part of a home-auth stack"
    os.environ["FIRESTORE_EMULATOR_HOST"] = "localhost:8080"
    os.environ["FIREBASE_AUTH_EMULATOR_HOST"] = "localhost:9099"
    os.environ["GOOGLE_CLOUD_PROJECT"] = FIREBASE_PROJECT_ID


class StoreTransport:
    def get(self, path: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def set(self, path: str, data: dict[str, Any]) -> None:
        raise NotImplementedError

    def merge(self, path: str, data: dict[str, Any]) -> None:
        raise NotImplementedError

    def delete(self, path: str) -> None:
        raise NotImplementedError

    def list(self, *, parent: str | None = None, group: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        """`(document id, data)` for every document in collection `parent` or group `group`."""
        raise NotImplementedError


class FirestoreTransport(StoreTransport):
    def __init__(self) -> None:
        _use_relay_store()
        from app.db.firestore import get_db

        self._db = get_db()

    def _ref(self, path: str):
        parts = path.split("/")
        return self._db.collection(*parts[:-1]).document(parts[-1])

    def get(self, path: str) -> dict[str, Any] | None:
        snap = self._ref(path).get()
        return (snap.to_dict() or {}) if snap.exists else None

    def set(self, path: str, data: dict[str, Any]) -> None:
        self._ref(path).set(data)

    def merge(self, path: str, data: dict[str, Any]) -> None:
        self._ref(path).set(data, merge=True)

    def delete(self, path: str) -> None:
        self._ref(path).delete()

    def list(self, *, parent: str | None = None, group: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        query = self._db.collection(*parent.split("/")) if parent else self._db.collection_group(group)
        return [(snap.id, snap.to_dict() or {}) for snap in query.stream()]


class HttpTransport(StoreTransport):
    def __init__(self) -> None:
        from app.db import sqlitestore

        self._wire = sqlitestore
        self._http = httpx.Client(base_url=f"{RELAY_URL}/api/dev/db", timeout=10.0, verify=_verify())

    def _ok(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code != 200:
            raise AssertionError(f"/api/dev/db: {resp.status_code} {resp.text}")
        return resp.json()

    def get(self, path: str) -> dict[str, Any] | None:
        body = self._ok(self._http.get("/doc", params={"path": path}))
        return self._wire.from_wire(body["data"]) if body["exists"] else None

    def set(self, path: str, data: dict[str, Any]) -> None:
        self._ok(self._http.put("/doc", json={"path": path, "data": self._wire.to_wire(data)}))

    def merge(self, path: str, data: dict[str, Any]) -> None:
        self._ok(self._http.patch("/doc", json={"path": path, "data": self._wire.to_wire(data)}))

    def delete(self, path: str) -> None:
        self._ok(self._http.delete("/doc", params={"path": path}))

    def list(self, *, parent: str | None = None, group: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        params = {"parent": parent} if parent else {"group": group}
        rows = self._ok(self._http.get("/list", params=params))["docs"]
        return [(row["id"], self._wire.from_wire(row["data"])) for row in rows]


_store: StoreTransport | None = None


def store() -> StoreTransport:
    global _store
    if _store is None:
        _store = HttpTransport() if HOME_AUTH else FirestoreTransport()
    return _store


class Oracle:
    """Whitebox store reads through `store()`, used only to verify what blackbox actions produced.
    Bypasses `firestore.rules`; documents are decoded with the relay's own models."""

    def __init__(self) -> None:
        from app.store import contacts, locations, messages, users

        self.messages_store = messages
        self._locations = locations
        self._users = users
        self._contacts = contacts

    def uid_for_alias(self, alias: str) -> str:
        data = store().get(f"aliases/{alias}")
        assert data is not None and data.get("uid"), f"no such alias: {alias!r}"
        return data["uid"]

    def loc_req_exists(self, device_id: str) -> bool:
        return store().get(f"locReqs/{device_id}") is not None

    def message(self, msg_id: str):
        data = store().get(f"messages/{msg_id}")
        return None if data is None else self.messages_store.Message.model_validate({"id": msg_id, **data})

    def all_messages(self) -> list:
        return [
            self.messages_store.Message.model_validate({"id": doc_id, **data})
            for doc_id, data in store().list(parent="messages")
        ]

    def thread(self, alias_a: str, alias_b: str):
        """`messages_store.list_thread`: the conversation's messages by `seq`."""
        key = self.messages_store.conv_key(self.uid_for_alias(alias_a), self.uid_for_alias(alias_b))
        return sorted((m for m in self.all_messages() if m.convKey == key), key=lambda m: m.seq)

    def conversation_exists(self, alias_a: str, alias_b: str) -> bool:
        key = self.messages_store.conv_key(self.uid_for_alias(alias_a), self.uid_for_alias(alias_b))
        return store().get(f"conversations/{key}") is not None

    def locations(self, device_id: str) -> list:
        fixes = [
            self._locations.LocationFix.model_validate({"id": doc_id, **data})
            for doc_id, data in store().list(parent=f"devices/{device_id}/locations")
        ]
        return sorted(fixes, key=lambda f: f.createdAt, reverse=True)

    def user(self, uid: str):
        data = store().get(f"users/{uid}")
        return None if data is None else self._users.User.model_validate({"uid": uid, **data})

    def device(self, device_id: str) -> dict[str, Any] | None:
        return store().get(f"devices/{device_id}")

    def setup_code_exists(self, bid: str) -> bool:
        return store().get(f"setupCodes/{bid}") is not None

    def contact_request(self, device_id: str, req_id: str):
        key = self._contacts.key(device_id, req_id)
        data = store().get(f"contactRequests/{key}")
        return None if data is None else self._contacts.ContactRequest.model_validate({"key": key, **data})


def _backdate_location(device_id: str, *, days_ago: float) -> str:
    from datetime import UTC, datetime, timedelta

    from app.ids import new_id

    ts = int(time.time())
    loc_id = new_id("l_")
    doc = {
        "ts": ts,
        "fixTs": ts,
        "lat": 37.0,
        "lon": -122.0,
        "accM": 5,
        "src": "gnss",
        "cached": False,
        "reqId": None,
        "createdAt": datetime.now(UTC) - timedelta(days=days_ago),
    }
    store().set(f"devices/{device_id}/locations/{loc_id}", doc)
    return loc_id


def _backdate_message(sender_uid: str, recipient_uid: str, *, days_ago: float) -> str:
    from datetime import UTC, datetime, timedelta

    from app.ids import new_id
    from app.store import messages as messages_store

    msg_id = new_id("m_")
    key = messages_store.conv_key(sender_uid, recipient_uid)
    doc = {
        "seq": 1,
        "convKey": key,
        "uids": sorted([sender_uid, recipient_uid]),
        "senderUid": sender_uid,
        "recipientUid": recipient_uid,
        "kind": "text",
        "body": "stale (backdated by tools/e2e_v2.py's scenario_retention)",
        "loc": None,
        "wireId": None,
        "originBackendKind": "webapp",
        "originBackendId": None,
        "ts": int(time.time()),
        "createdAt": datetime.now(UTC) - timedelta(days=days_ago),
        "deliveries": {},
        "pendingDeviceIds": [],
    }
    store().set(f"messages/{msg_id}", doc)
    return msg_id


def scenario_bootstrap() -> None:
    """Admin sign-in, parent + student users, an allow edge, and a student-owned pager with
    `parent` as default recipient."""
    bootstrap_admin()

    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")

    admin.admin_user_add("parent", "Parent", email="parent@example.com", phone=None)
    admin.admin_user_add("student", "Student", email="student@example.com", phone=None)
    admin.admin_set_allow("parent", "student", message=True, locate=True, one_way=False)

    device_info = create_device_with_secret(admin, "pgr-e2e-1", "student", default_to_alias="parent")
    assert device_info["device"]["mqttUsername"] == device_info["device"]["id"] == issued_id("pgr-e2e-1")
    assert device_info["setupCode"]
    print(f"bootstrap: admin, parent, student, allow-edge and device pgr-e2e-1 created as {issued_id('pgr-e2e-1')}")


def scenario_text_roundtrip() -> None:
    """parent -> student via the API -> the pager device receives it; `shown`/`read` acks update
    delivery state. student `msg` -> parent's thread; student `msg @dad` to a second parent."""
    oracle = Oracle()

    parent = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    parent.login("parent")

    device = make_device("pgr-e2e-1")
    device.connect()
    wait_until(lambda: device.connected, timeout=10, description="device to connect")

    sent = parent.say("student", "pickup at 3:15 by the gym")
    msg_id = sent["id"]

    wait_until(
        lambda: any(e.data.get("id") == msg_id for e in device.inbox),
        timeout=10,
        description="device to receive the down message",
    )
    device.publish_ack(msg_id, "shown")
    device.publish_ack(msg_id, "read")

    def _is_read() -> bool:
        msg = oracle.message(msg_id)
        if msg is None:
            return False
        return any(d.kind == "pager" and d.state == "read" for d in msg.deliveries.values())

    wait_until(_is_read, timeout=10, description="pager delivery to reach 'read'")
    print("text_roundtrip: parent -> student -> device, shown+read acked")

    # student -> parent's thread (no `to`, so the device's default recipient). The stored message
    # has a fresh `m_` doc id; the device's `u_` wire id survives only in `wireId` (§1 dedup key).
    up_id = device.publish_msg("ok coming")
    wait_until(
        lambda: any(m.wireId == up_id for m in oracle.thread("parent", "student")),
        timeout=15,
        description="student's reply to land in the parent<->student thread",
    )

    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    admin.admin_user_add("dad", "Dad", email="dad@example.com", phone=None)
    admin.admin_set_allow("dad", "student", message=True, locate=True, one_way=False)

    up_id_2 = device.publish_msg("ok coming", to="dad")
    wait_until(
        lambda: any(m.wireId == up_id_2 for m in oracle.thread("dad", "student")),
        timeout=10,
        description="student's @dad reply to land in the dad<->student thread",
    )
    device.disconnect()
    print("text_roundtrip: student -> parent (default) and student -> @dad both landed")


def scenario_allowlist() -> None:
    """student `msg @stranger` (unregistered alias) is dropped with a `system` reply; admin denies
    parent<->student -> send returns 403; an unregistered signed-in UID gets 403 and cannot read."""
    device = make_device("pgr-e2e-1")
    device.connect()
    wait_until(lambda: device.connected, timeout=10, description="device to connect")
    device.inbox.clear()

    device.publish_msg("hi", to="stranger")
    wait_until(
        lambda: any(e.data.get("from") == "system" for e in device.inbox),
        timeout=10,
        description="a system 'unknown recipient' reply",
    )
    system_reply = next(e.data for e in device.inbox if e.data.get("from") == "system")
    assert system_reply["body"] == "unknown recipient"
    print("allowlist: unknown @stranger -> dropped + system reply received")

    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    admin.admin_deny("parent", "student")

    parent = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    parent.login("parent")
    try:
        parent.say("student", "are you there?")
        raise AssertionError("expected say() to fail with 403 after admin_deny")
    except RuntimeError as exc:
        assert "403" in str(exc), f"expected a 403, got: {exc}"
    print("allowlist: admin deny -> send returns 403")

    if HOME_AUTH:
        _allowlist_unregistered_home()
    else:
        _allowlist_unregistered_cloud()
    device.disconnect()

    # Restore parent<->student so later scenarios see the allow-list state `bootstrap` set up.
    admin.admin_set_allow("parent", "student", message=True, locate=True, one_way=False)


def _allowlist_unregistered_cloud() -> None:
    """An unregistered signed-in UID (a real Auth account with no users/{uid} doc) is rejected by
    the API and by a direct Firestore read."""
    from firebase_admin import auth as fb_auth

    _use_relay_store()
    from app.db.firestore import get_app

    get_app()
    stranger_uid = "e2e-stranger"
    try:
        fb_auth.get_user(stranger_uid)
    except fb_auth.UserNotFoundError:
        fb_auth.create_user(uid=stranger_uid, email="stranger@example.com")

    custom_token = fb_auth.create_custom_token(stranger_uid).decode("utf-8")
    exchange = httpx.post(
        f"{AUTH_URL}/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken",
        params={"key": pager_client.FAKE_API_KEY},
        json={"token": custom_token, "returnSecureToken": True},
        timeout=10.0,
    )
    exchange.raise_for_status()
    id_token = exchange.json()["idToken"]

    api_resp = httpx.get(f"{RELAY_URL}/api/me", headers={"Authorization": f"Bearer {id_token}"})
    assert api_resp.status_code == 403, api_resp.text

    fs_resp = httpx.get(
        f"http://localhost:8080/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/settings/retention",
        headers={"Authorization": f"Bearer {id_token}"},
        timeout=10.0,
    )
    assert fs_resp.status_code == 403, fs_resp.text
    print("allowlist: unregistered signed-in uid -> 403 from both the API and Firestore")


def _allowlist_unregistered_home() -> None:
    """The same check against home sign-in: a relay session for a uid with no users/{uid} doc is
    refused by the API. The browser's rules-bounded reads at home are the SSE stream."""
    minted = httpx.post(
        f"{RELAY_URL}/api/dev/token", json={"uid": "e2e-stranger"}, timeout=10.0, verify=_verify()
    )
    minted.raise_for_status()
    assert minted.json()["kind"] == "session", minted.json()
    api_resp = httpx.get(
        f"{RELAY_URL}/api/me",
        headers={"Authorization": f"Bearer {minted.json()['token']}"},
        timeout=10.0,
        verify=_verify(),
    )
    assert api_resp.status_code == 403, api_resp.text
    print("allowlist: unregistered signed-in uid -> 403 from the API (home session)")

def scenario_republish() -> None:
    """A device that has never connected still gets its queued message once it comes online
    (online-edge republish, PROTOCOL.md §5.3); a brand-new device avoids racing EMQX's own QoS 1
    queuing. Also: with the broker REST API unreachable a send stays 'queued' and `/internal/tick`
    publishes it once EMQX is back."""
    oracle = Oracle()
    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    create_device_with_secret(admin, "pgr-e2e-2", "student", default_to_alias="parent")

    parent = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    parent.login("parent")

    sent = parent.say("student", "device is brand new, message queues")
    msg_id = sent["id"]

    def _is_queued_or_sent() -> bool:
        msg = oracle.message(msg_id)
        return msg is not None and any(d.kind == "pager" for d in msg.deliveries.values())

    wait_until(_is_queued_or_sent, timeout=10, description="pager delivery to be created")

    device = make_device("pgr-e2e-2")
    device.connect()
    wait_until(
        lambda: any(e.data.get("id") == msg_id for e in device.inbox),
        timeout=10,
        description="the never-connected device's first connect to trigger the online-edge republish",
    )
    matching = [e for e in device.inbox if e.data.get("id") == msg_id]
    assert len(matching) == 1, f"expected exactly one delivery of {msg_id}, got {len(matching)}"
    device.disconnect()
    print("republish: message queued before the device ever connected arrived exactly once")

    # --- broker REST API down -> tick recovers it ---
    print(f"republish: stopping {BROKER} to simulate the broker being down...")
    connects_before = _relay_mqtt_connects()
    compose("stop", BROKER)

    sent2 = parent.say("student", "broker is down for this one")
    msg_id_2 = sent2["id"]

    def _delivery_state(mid: str) -> str | None:
        msg = oracle.message(mid)
        if msg is None:
            return None
        for d in msg.deliveries.values():
            if d.kind == "pager":
                return d.state
        return None

    wait_until(
        lambda: _delivery_state(msg_id_2) == "queued",
        timeout=15,
        description="delivery to stay 'queued' while the broker is unreachable",
    )
    print(f"republish: delivery stayed 'queued' while {BROKER} was down")

    compose("start", BROKER)
    if BROKER == "mosquitto":
        # The relay reconnects by itself (backoff up to 30 s); only its log line shows it.
        wait_until(
            lambda: _relay_mqtt_connects() > connects_before,
            timeout=90,
            interval=2,
            description="the relay to reconnect to mosquitto",
        )
    else:
        wait_until(_emqx_reachable, timeout=90, interval=2, description="emqx's REST API to come back")
        configure_emqx()  # restarting the container loses its dynamic rule-engine config

    print(f"republish: /internal/tick -> {internal('/internal/tick')}")

    wait_until(
        lambda: _delivery_state(msg_id_2) == "sent",
        timeout=15,
        description="delivery to reach 'sent' after tick retried it",
    )
    print("republish: /internal/tick delivered the previously-queued message")


def _relay_mqtt_connects() -> int:
    """How many times the relay has logged an MQTT (re)connect; always 0 under BROKER_MODE=rest."""
    out = compose("logs", "relay", check=False, capture=True).stdout
    return out.count("mqtt connected to")


def scenario_location_periodic() -> None:
    """`loc auto 5` fills `devices/{d}/locations` over a few cycles (checked via `Oracle` and the
    API); periodic fixes never create a thread message (§3.2); a user without `locate` permission
    cannot read them (`locatableBy` rule, §5.6)."""
    oracle = Oracle()
    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    admin.admin_user_add(
        "locperiodic", "LocPeriodic", email="locperiodic@example.com", phone=None
    )
    # Device before allow-edge: `create_device` starts `locatableBy` at `[]`, and only a later
    # allow-list write recomputes it, so the opposite order would leave it empty.
    create_device_with_secret(admin, "pgr-e2e-locp", "locperiodic", default_to_alias="parent")
    admin.admin_set_allow("parent", "locperiodic", message=True, locate=True, one_way=True)

    device = make_device("pgr-e2e-locp")
    device.connect()
    wait_until(lambda: device.connected, timeout=10, description="loc-periodic device to connect")

    device.loc_auto(5)
    wait_until(
        lambda: len(oracle.locations(issued_id("pgr-e2e-locp"))) >= 2,
        timeout=25,
        description="at least 2 periodic fixes to land in devices/{d}/locations",
    )
    device.loc_stop_auto()
    fixes = oracle.locations(issued_id("pgr-e2e-locp"))
    print(f"location_periodic: {len(fixes)} periodic fixes landed in devices/{{d}}/locations")

    assert not oracle.conversation_exists("parent", "locperiodic"), (
        "periodic fixes must never create a thread message (PROTOCOL.md §3.2)"
    )

    if HOME_AUTH:
        # The parent's read is a Firestore REST query bounded by firestore.rules; at home it is the
        # SSE stream with the rules ported to Python.
        print("location_periodic: rules-bounded location reads not checked (home)")
        device.disconnect()
        return
    parent = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    parent.login("parent")
    via_api = parent.locations("locperiodic")
    assert len(via_api) >= 2, via_api
    print("location_periodic: a user with locate permission reads locations via Firestore REST")

    admin.admin_user_add(
        "locoutsider", "LocOutsider", email="locoutsider@example.com", phone=None
    )
    outsider = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    outsider.login("locoutsider")
    try:
        outsider.locations("locperiodic")
        raise AssertionError("expected locations() to be denied for a non-locate user")
    except RuntimeError as exc:
        print(f"location_periodic: user without locate permission denied: {exc}")

    device.disconnect()


def scenario_location_on_demand() -> None:
    """`locate` -> real fix, request fulfilled; a second `locate` within 60 s answers `cached:true`;
    two requesters within the coalescing window share one `loc_req` and both get a `kind='loc'`
    message; `loc fail on` -> `err:"no_fix"` still resolves to `fulfilled`; with the shortened
    `LOC_REQ_TTL_S`, `tick()` clears a stale `locReqs` doc and the next `/locate` starts fresh."""
    oracle = Oracle()
    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    parent = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    parent.login("parent")

    # --- a real fix, then a <60s cached re-ask ---
    # Device before allow-edge in every sub-case (see scenario_location_periodic).
    admin.admin_user_add("locdemo1", "LocDemo1", email="locdemo1@example.com", phone=None)
    create_device_with_secret(admin, "pgr-e2e-locd1", "locdemo1", default_to_alias="parent")
    admin.admin_set_allow("parent", "locdemo1", message=True, locate=True, one_way=True)
    device1 = make_device("pgr-e2e-locd1")
    device1.connect()
    wait_until(lambda: device1.connected, timeout=10, description="locdemo1 device to connect")

    outcome1 = parent.locate("locdemo1")
    assert outcome1["requestId"], outcome1
    assert outcome1["cached"] is False, outcome1

    def _fulfilled() -> bool:
        msg = oracle.message(outcome1["requestId"])
        if msg is None:
            return False
        return any(d.kind == "pager" and d.state == "fulfilled" for d in msg.deliveries.values())

    wait_until(_fulfilled, timeout=10, description="loc_req to be fulfilled by the device's answer")
    print("location_on_demand: locate -> device answered, loc_req fulfilled")

    loc_req_count_before = sum(1 for e in device1.inbox if e.data.get("kind") == "loc_req")
    outcome2 = parent.locate("locdemo1")
    assert outcome2["cached"] is True, outcome2
    assert outcome2["requestId"] is None, outcome2
    loc_req_count_after = sum(1 for e in device1.inbox if e.data.get("kind") == "loc_req")
    assert loc_req_count_after == loc_req_count_before, "cached answer must not re-ask the device"
    print("location_on_demand: second locate() within 60s answered cached:true, no new wire message")
    device1.disconnect()

    # --- coalescing: two requesters, one live loc_req ---
    admin.admin_user_add("locdemo2", "LocDemo2", email="locdemo2@example.com", phone=None)
    admin.admin_user_add(
        "locrequester2", "LocRequester2", email="locrequester2@example.com", phone=None
    )
    create_device_with_secret(admin, "pgr-e2e-locd2", "locdemo2", default_to_alias="parent")
    admin.admin_set_allow("parent", "locdemo2", message=True, locate=True, one_way=True)
    admin.admin_set_allow("locrequester2", "locdemo2", message=True, locate=True, one_way=True)
    device2 = make_device("pgr-e2e-locd2")
    # A real device takes seconds to attempt a fix (§13.3 rule 2 bounds it at 60 s); a few
    # seconds of delay make the coalescing window deterministic.
    device2.loc_answer_delay_s = 3.0
    device2.connect()
    wait_until(lambda: device2.connected, timeout=10, description="locdemo2 device to connect")

    requester2 = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    requester2.login("locrequester2")

    outcomeA = parent.locate("locdemo2")
    outcomeB = requester2.locate("locdemo2")
    assert outcomeA["requestId"] == outcomeB["requestId"], (outcomeA, outcomeB)
    print("location_on_demand: two requesters coalesced onto the same loc_req")

    def _both_notified() -> bool:
        mom_thread = oracle.thread("parent", "locdemo2")
        req_thread = oracle.thread("locrequester2", "locdemo2")
        return any(m.kind == "loc" for m in mom_thread) and any(
            m.kind == "loc" for m in req_thread
        )

    wait_until(
        _both_notified,
        timeout=15,
        description="both coalesced requesters to get a kind='loc' message",
    )
    print("location_on_demand: both coalesced requesters received their own kind='loc' message")
    device2.disconnect()

    # --- loc fail on -> no_fix, still resolves the request ---
    admin.admin_user_add("locdemo3", "LocDemo3", email="locdemo3@example.com", phone=None)
    create_device_with_secret(admin, "pgr-e2e-locd3", "locdemo3", default_to_alias="parent")
    admin.admin_set_allow("parent", "locdemo3", message=True, locate=True, one_way=True)
    device3 = make_device("pgr-e2e-locd3")
    device3.connect()
    wait_until(lambda: device3.connected, timeout=10, description="locdemo3 device to connect")
    device3.loc_set_fail(True)

    outcome3 = parent.locate("locdemo3")

    def _no_fix_fulfilled() -> bool:
        msg = oracle.message(outcome3["requestId"])
        if msg is None:
            return False
        return any(d.kind == "pager" and d.state == "fulfilled" for d in msg.deliveries.values())

    wait_until(_no_fix_fulfilled, timeout=10, description="no_fix answer to still fulfil the request")
    thread3 = oracle.thread("parent", "locdemo3")
    loc_msgs3 = [m for m in thread3 if m.kind == "loc"]
    assert len(loc_msgs3) == 1 and loc_msgs3[0].loc == {"err": "no_fix"}, loc_msgs3
    print("location_on_demand: loc fail on -> err:no_fix, request resolved to fulfilled (not stuck)")
    device3.disconnect()

    # --- derived expiry: shortened LOC_REQ_TTL_S + tick() ---
    admin.admin_user_add("locdemo4", "LocDemo4", email="locdemo4@example.com", phone=None)
    create_device_with_secret(admin, "pgr-e2e-locd4", "locdemo4", default_to_alias="parent")
    admin.admin_set_allow("parent", "locdemo4", message=True, locate=True, one_way=True)

    # Never connected: §5.3 excludes loc_req from the online-edge republish, so this request
    # can never be answered and is guaranteed to go stale.
    expiring = parent.locate("locdemo4")
    coalesced = parent.locate("locdemo4")
    assert coalesced["requestId"] == expiring["requestId"], "still within the TTL -> must coalesce"

    print(f"location_on_demand: waiting {LOC_REQ_TTL_S + 3}s for the loc_req to go stale...")
    time.sleep(LOC_REQ_TTL_S + 3)
    tick_json = internal("/internal/tick")
    assert tick_json.get("locReqsCleared", 0) >= 1, tick_json
    assert not oracle.loc_req_exists(issued_id("pgr-e2e-locd4"))
    print(f"location_on_demand: tick() cleared the stale locReqs row -- {tick_json}")

    fresh = parent.locate("locdemo4")
    assert fresh["requestId"] != expiring["requestId"], "must not coalesce onto the expired request"
    print("location_on_demand: a locate() after expiry starts a fresh loc_req, not a coalesce")


def scenario_retention() -> None:
    """Retention sweep. There is no API to send a fix or message dated in the past, so documents are
    inserted with an explicit past `createdAt`; the third sub-case calls `sweep` twice to check the
    second call deletes nothing."""
    oracle = Oracle()
    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    parent = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    parent.login("parent")

    # --- locations: retention.locations=1d, messages untouched ---
    admin.admin_user_add("retloc", "RetLoc", email="retloc@example.com", phone=None)
    create_device_with_secret(admin, "pgr-e2e-retloc", "retloc")
    admin.admin_set_retention(messages="4w", locations="1d")

    fresh_loc_id = _backdate_location(issued_id("pgr-e2e-retloc"), days_ago=0.02)  # ~30 min old
    stale_loc_id = _backdate_location(issued_id("pgr-e2e-retloc"), days_ago=3)  # well past 1 day
    fresh_msg_id = parent.say("student", "retention: still here after a locations-only sweep")["id"]

    print(f"retention: sweep (locations=1d) -> {internal('/internal/sweep')}")

    remaining_ids = {f.id for f in oracle.locations(issued_id("pgr-e2e-retloc"))}
    assert stale_loc_id not in remaining_ids, remaining_ids
    assert fresh_loc_id in remaining_ids, remaining_ids
    assert oracle.message(fresh_msg_id) is not None, "messages must be untouched by a locations-only sweep"
    from datetime import UTC, datetime, timedelta

    # The sweep reports the oldest survivor per class.
    sweep_report = internal("/internal/sweep")
    oldest_loc = (sweep_report.get("oldest") or {}).get("locations")
    assert oldest_loc is not None, sweep_report
    assert (
        datetime.now(UTC) - datetime.fromisoformat(oldest_loc)
    ) < timedelta(days=1, hours=1), f"oldest surviving location past the 1-day limit: {oldest_loc}"
    print("retention: stale location fix swept; fresh fix + messages untouched")

    # --- messages: retention.messages=2w ---
    admin.admin_set_retention(messages="2w", locations="1d")
    stale_msg_id = _backdate_message(
        oracle.uid_for_alias("parent"), oracle.uid_for_alias("student"), days_ago=20
    )
    fresh_msg_id_2 = parent.say("student", "retention: fresh message survives a 2-week sweep")["id"]

    print(f"retention: sweep (messages=2w) -> {internal('/internal/sweep')}")

    assert oracle.message(stale_msg_id) is None, "message older than the 2-week retention must be swept"
    assert oracle.message(fresh_msg_id_2) is not None
    print("retention: stale message swept; fresh message untouched")

    # --- idempotency: >1 batch's worth of stale docs, sweep() called twice ---
    stale_ids = [
        _backdate_message(oracle.uid_for_alias("parent"), oracle.uid_for_alias("student"), days_ago=20 + i)
        for i in range(3)
    ]
    result3 = internal("/internal/sweep")
    result4 = internal("/internal/sweep")
    print(f"retention: two sweep() calls in a row -> first {result3}, second {result4}")

    for mid in stale_ids:
        assert oracle.message(mid) is None
    assert result3["messagesDeleted"] >= len(stale_ids)
    assert result4["messagesDeleted"] == 0, "a second sweep() right after the first must be a no-op"
    print("retention: sweep() is idempotent -- the second call deleted nothing")


def scenario_bytes() -> None:
    """Informational (no hard assertions): exercises a mix of up/down/status/loc traffic and prints
    the per-topic byte counters next to PROTOCOL.md §7.2's payload-byte figures."""
    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    admin.admin_user_add("bytesuser", "BytesUser", email="bytesuser@example.com", phone=None)
    create_device_with_secret(admin, "pgr-e2e-bytes", "bytesuser", default_to_alias="parent")
    # Two-way, so the device's up-message lands in the thread rather than tripping the
    # unknown-recipient path; this scenario wants representative traffic sizes.
    admin.admin_set_allow("parent", "bytesuser", message=True, locate=True, one_way=False)

    device = make_device("pgr-e2e-bytes")
    device.connect()
    wait_until(lambda: device.connected, timeout=10, description="bytes device to connect")

    parent = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    parent.login("parent")
    sent = parent.say("bytesuser", "how are things going today?")
    wait_until(
        lambda: any(e.data.get("id") == sent["id"] for e in device.inbox),
        timeout=10,
        description="bytes device to receive the down message",
    )
    device.publish_ack(sent["id"], "shown")
    device.publish_ack(sent["id"], "read")
    device.publish_status(batt_mv=3150, mode="active", rssi=-80)
    device.loc_now(37.7749, -122.4194, acc=12)
    device.publish_msg("ok", to="parent")

    time.sleep(0.5)  # let the last up-message land before reading counters
    device.disconnect()

    published = device.bytes.published
    received = device.bytes.received
    assert sum(published.values()) > 0 and sum(received.values()) > 0, "expected some traffic counted"

    print("bytes: per-topic published bytes:", published)
    print("bytes: per-topic received bytes:", received)
    print(
        "bytes: PROTOCOL.md §7.2 reference payload sizes (JSON only, no TLS/TCP -- "
        "this compose stack has neither) -- /down 'msg' ~99 B, /down 'loc_req' ~78 B, "
        "/up ack ~51 B, /up reply ~84 B, /status ~117 B, /loc periodic ~160 B, "
        "/loc answering a loc_req ~160 B"
    )


def _pending_book_or_cfg_acked(device_id: str, field: str) -> bool:
    """Whitebox check that `devcfg.ack()` recorded the device's latest `book`/`cfg` `shown` ack
    (`devices/{d}.pendingBook`/`pendingCfg`); no client-facing read exists (§3.2)."""
    data = store().get(f"devices/{device_id}")
    if data is None:
        return False
    pending = data.get(field)
    return isinstance(pending, dict) and pending.get("acked") is True


def _relay_settings():
    """A `Settings` this host process can use against the compose stack's published ports
    (`Settings.from_env()` would read this process's own environment, not `relay/.env`). The home
    stack's EMQX API is on 127.0.0.1:18083; its Mosquitto is reached over TLS on 8883 as `relay`."""
    from app.config import Settings

    return Settings(
        broker_api_url=f"{EMQX_API_URL}/api/v5",
        broker_api_key=BROKER_API_KEY,
        broker_api_secret=BROKER_API_SECRET,
        webhook_key=WEBHOOK_KEY,
        dev_mode=True,
        google_cloud_project=FIREBASE_PROJECT_ID,
        firestore_emulator_host=None if HOME_AUTH else "localhost:8080",
        firebase_auth_emulator_host=None if HOME_AUTH else "localhost:9099",
        broker_mode="mqtt" if BROKER == "mosquitto" else "rest",
        broker_mqtt_host=MQTT_HOST,
        broker_mqtt_port=MQTT_PORT,
        broker_mqtt_username="relay",
        broker_mqtt_password=BROKER_ADMIN_PASSWORD or None,
    )


_HOST_MQTT = None


def _host_mqtt():
    """Mosquitto stack: one `app.broker_mqtt.MqttBrokerClient` from this host,
    over nginx's TLS 8883 with the stack's CA, logged in as `relay` under its
    own client id (never the relay's), with no device subscriptions."""
    global _HOST_MQTT
    if _HOST_MQTT is None:
        from app.broker_mqtt import MqttBrokerClient

        client = MqttBrokerClient(_relay_settings(), client_id="e2e-host")
        client._client.tls_set(ca_certs=str(HOME_CA))  # the relay itself speaks plain 1883
        client.start()
        assert client.wait_until_ready(15), "could not log in to mosquitto as relay over 8883"
        _HOST_MQTT = client
    return _HOST_MQTT


def _expire_setup_codes() -> int:
    """`devsetup.expire()`, which no scheduler calls. A home relay's store is reachable only through
    `store()`, so there the same steps run through it and `devsetup._clear_and_revoke`."""
    from datetime import UTC, datetime

    from app import devsetup
    from app.broker import BrokerClient
    from app.broker_admin import make_broker_admin

    settings = _relay_settings()
    broker = _host_mqtt() if BROKER == "mosquitto" else BrokerClient(settings)
    emqx = make_broker_admin(settings, broker)
    if not HOME_AUTH:
        return devsetup.expire(settings=settings, broker=broker, emqx=emqx)
    now = datetime.now(UTC)
    expired = [bid for bid, data in store().list(parent="setupCodes") if data["expiresAt"] < now]
    for bid in expired:
        devsetup._clear_and_revoke(bid, broker, emqx)
        store().delete(f"setupCodes/{bid}")
    return len(expired)


def _broker_user_exists(username: str) -> bool:
    """EMQX: raw `GET .../authentication/{mechanism}/users/{username}` (`EmqxAdmin` has no exists
    method). Mosquitto: the dynsec `getClient` command."""
    if BROKER == "mosquitto":
        responses = _host_mqtt().control([{"command": "getClient", "username": username}])
        assert responses is not None, "dynsec getClient timed out"
        return "error" not in responses[0]
    resp = httpx.get(
        f"{EMQX_API_URL}/api/v5/authentication/password_based:built_in_database/users/{username}",
        auth=(BROKER_API_KEY, BROKER_API_SECRET),
        timeout=5.0,
    )
    return resp.status_code == 200


def _broker_retained_message_exists(topic: str) -> bool:
    """EMQX 5 retainer HTTP API (`GET /api/v5/mqtt/retainer/message/{topic}`: 200 or 404). Used
    because the cleanup revokes the bootstrap credential. Mosquitto: subscribe as `relay` and see
    whether a retained message arrives."""
    if BROKER == "mosquitto":
        return _mosquitto_retained(topic)
    import urllib.parse

    encoded = urllib.parse.quote(topic, safe="")
    resp = httpx.get(
        f"{EMQX_API_URL}/api/v5/mqtt/retainer/message/{encoded}",
        auth=(BROKER_API_KEY, BROKER_API_SECRET),
        timeout=5.0,
    )
    return resp.status_code == 200


def _mosquitto_retained(topic: str, wait_s: float = 2.0) -> bool:
    import threading

    import paho.mqtt.client as mqtt

    got = threading.Event()
    subscribed = threading.Event()
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"e2e-retained-{os.getpid()}"
    )
    client.username_pw_set("relay", BROKER_ADMIN_PASSWORD)
    client.tls_set(ca_certs=str(HOME_CA))
    client.on_connect = lambda c, u, f, rc, p: c.subscribe(topic, 1)
    client.on_subscribe = lambda c, u, mid, rcs, p: subscribed.set()
    client.on_message = lambda c, u, m: got.set() if m.retain and m.payload else None
    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    try:
        assert subscribed.wait(10), f"could not subscribe to {topic} as relay"
        return got.wait(wait_s)
    finally:
        client.disconnect()
        client.loop_stop()


def scenario_setup_code() -> None:
    """Admin creates a device, a simulated device performs the real bootstrap fetch,
    `provisionState` becomes `"provisioned"` on the first signed `/status`, and `boot-{bid}` is gone
    from EMQX. A second code is expired by patching `setupCodes/{bid}.expiresAt` and calling
    `devsetup.expire()`, which leaves no credential or retained bundle; a fresh `bootstrap_device()`
    then fails. `BROKER_MANAGES_AUTH=0` (a shared `boot` user) is not exercised."""
    bootstrap_admin()
    from datetime import UTC, datetime, timedelta

    from app import devsetup

    oracle = Oracle()

    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    admin.admin_user_add("scowner", "SCOwner", email="scowner@example.com", phone=None)

    # --- happy path: create -> code -> real bootstrap -> provisioned -> boot user gone ---
    result = admin.admin_device_add("pgr-e2e-setup", "scowner")
    setup_id = result["device"]["id"]
    assert result["brokerPush"] == "pushed", result  # BROKER_MANAGES_AUTH=1 (default) in this stack
    code = result["setupCode"]
    print(f"setup_code: admin created pgr-e2e-setup as {setup_id}, brokerPush={result['brokerPush']}, code={code!r}")

    token_bytes, _host, _port, _apn = devsetup.parse(code)
    bid, _bpw, _bkey = devsetup.derive(token_bytes)
    boot_username = devsetup.boot_username(bid)
    assert _broker_user_exists(boot_username), f"{boot_username} should exist right after issue()"
    print(f"setup_code: bootstrap credential {boot_username} is live on {BROKER} before the device fetches it")

    device = pager_client.bootstrap_device(code, port=MQTT_PORT, wire=WIRE_MODE)
    assert device.device_id == setup_id, (device.device_id, setup_id)
    print(f"setup_code: real §3.2 bootstrap fetch complete, now holding {setup_id}'s real credentials")

    device.connect()
    wait_until(lambda: device.connected, timeout=10, description="bootstrapped device to connect signed")
    device.publish_status(batt_mv=3800, mode="active", rssi=-70)

    def _provisioned() -> bool:
        d = oracle.device(setup_id)
        return d is not None and d.get("provisionState") == "provisioned"

    wait_until(
        _provisioned,
        timeout=10,
        description="provisionState to flip to 'provisioned' on the first signed /status",
    )
    print("setup_code: provisionState == 'provisioned' after the device's first signed /status")

    assert not _broker_user_exists(boot_username), f"{boot_username} should be gone after bootstrap completes"
    assert not oracle.setup_code_exists(bid), "setupCodes/{bid} should be deleted by complete()"
    print(f"setup_code: boot user {boot_username} and setupCodes/{bid} are both gone -- the boot user is gone")
    device.disconnect()

    # --- expired code: patch expiresAt, call devsetup.expire() directly, confirm no retained bundle ---
    result2 = admin.admin_device_add("pgr-e2e-setup-exp", "scowner")
    setup_exp_id = result2["device"]["id"]
    code2 = result2["setupCode"]
    token_bytes2, _h2, _p2, _a2 = devsetup.parse(code2)
    bid2, _bpw2, _bkey2 = devsetup.derive(token_bytes2)
    boot_username2 = devsetup.boot_username(bid2)
    down_topic2 = devsetup.boot_topic_down(bid2)

    assert _broker_user_exists(boot_username2), "boot user should exist before expiry"
    assert _broker_retained_message_exists(down_topic2), "retained bundle should exist before expiry"
    print(f"setup_code: pgr-e2e-setup-exp ({setup_exp_id})'s bootstrap credential and retained bundle both exist before expiry")

    store().merge(f"setupCodes/{bid2}", {"expiresAt": datetime.now(UTC) - timedelta(minutes=1)})
    expired_count = _expire_setup_codes()
    assert expired_count == 1, expired_count
    print(
        "setup_code: devsetup.expire() (standalone) "
        f"cleaned up {expired_count} expired code"
    )

    assert not _broker_retained_message_exists(down_topic2), "retained bundle must be cleared after expiry"
    assert not _broker_user_exists(boot_username2), "boot credential must be revoked after expiry"
    assert not oracle.setup_code_exists(bid2)
    print("setup_code: expired code left no retained message on pager/boot/{bid}/down and no live bootstrap credential")

    try:
        pager_client.bootstrap_device(code2, port=MQTT_PORT, wire=WIRE_MODE, timeout=5.0)
        raise AssertionError("bootstrap_device() must fail against an expired code")
    except RuntimeError as exc:
        print(f"setup_code: a real bootstrap attempt against the expired code fails, as a real device would: {exc}")


def scenario_address_book() -> None:
    """Device `contact_req` for `+15550001111 Grandma` -> admin approves (creates the family SMS
    contact, never a person) -> device gets the updated `book` and acks `shown`; the contact is
    not in `c[]` and reaches the pager only as `cfg.sms`; a device `up` to its alias gets
    `unknown recipient` and no thread; admin pushes `cfg` `lock.auto=2` and the device acks."""
    bootstrap_admin()
    oracle = Oracle()

    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    family_id = admin.me()["user"]["familyId"] or "default"
    abstudent = admin.admin_user_add(
        "abstudent", "ABStudent", email="abstudent@example.com", phone=None
    )
    create_device_with_secret(admin, "pgr-e2e-book", "abstudent")

    device = make_device("pgr-e2e-book")
    device.connect()
    wait_until(lambda: device.connected, timeout=10, description="address_book device to connect")

    # A `contact_req` adds the entry at once (no pending row). The default policy refuses numbers
    # outright; with `out: people_sms` the add replies "added; needs a parent's OK to text" and
    # raises one alert with the deterministic id `cr_{owner}_{peer}`.
    resp = admin._http.patch(
        f"{admin.api_url}/api/family/members/{abstudent['uid']}?family={family_id}",
        json={"policy": {"out": "people_sms", "in": "people_sms"}},
        headers=admin._headers(),
    )
    assert resp.status_code == 200, resp.text
    device.inbox.clear()
    req_id = device.publish_contact_req("Grandma", "+15550001111")

    wait_until(
        lambda: any(
            e.data.get("from") == "system" and e.data.get("body") == "Grandma: added; needs a parent's OK to text"
            for e in device.inbox
        ),
        timeout=10,
        description="the 'Grandma: added; needs a parent's OK to text' system reply",
    )
    row = oracle.contact_request(issued_id("pgr-e2e-book"), req_id)
    assert row is not None and row.status == "added", row
    grandma = next(c for c in admin.family_list_contacts(family_id) if c["phone"] == "+15550001111")
    contact_uid, contact_alias = grandma["uid"], grandma["alias"]
    expected_alert_id = f"cr_{abstudent['uid']}_{contact_uid}"

    def _pending_alert() -> dict | None:
        alerts = admin.family_list_alerts(family_id, status="open")
        return next(
            (a for a in alerts if a["kind"] == "contact_request" and a["id"] == expected_alert_id),
            None,
        )

    wait_until(
        lambda: _pending_alert() is not None,
        timeout=10,
        description="the refused add to raise the open contact_request alert cr_{owner}_{peer}",
    )
    alert = _pending_alert()
    assert alert is not None
    assert alert["peerPhone"] == "+15550001111", alert
    assert not alert.get("contactRequestKey"), alert
    print(f"address_book: contact_req added the entry and raised one open alert (id={alert['id']})")

    # Not on cfg.sms until a parent approves: permission is policy, not membership.
    assert {"name": "Grandma", "phone": "+15550001111"} not in device.sms_contacts, device.sms_contacts

    approved = admin.family_approve_alert(family_id, alert["id"])
    assert approved["status"] == "handled", approved
    print(f"address_book: admin approved in one click (edge written); SMS contact alias={contact_alias}")

    # The contact reaches the pager as `cfg.sms`.
    wait_until(
        lambda: {"name": "Grandma", "phone": "+15550001111"} in device.sms_contacts,
        timeout=10,
        description="device to receive and apply cfg.sms containing Grandma",
    )
    wait_until(
        lambda: device.book is not None,
        timeout=10,
        description="device to receive and apply a book",
    )
    assert device.book is not None
    assert device.book["p"] == [], device.book  # p[] retired
    print(
        f"address_book: device applied sms_contacts={device.sms_contacts}; book bv={device.book['bv']} "
        f"contacts={[c['a'] for c in device.book['c']]}"
    )

    wait_until(
        lambda: _pending_book_or_cfg_acked(issued_id("pgr-e2e-book"), "pendingBook"),
        timeout=10,
        description="device's book 'shown' ack to land at the relay",
    )
    print("address_book: device's book ack ('shown') landed at the relay")

    # The student has no relay SMS number: an `up` to the contact's alias takes the §4.2 case-3
    # path with the `sms not set up` system reply, nothing stored.
    device.inbox.clear()
    device.publish_msg("hi grandma", to=contact_alias)
    wait_until(
        lambda: any(e.data.get("from") == "system" for e in device.inbox),
        timeout=10,
        description="a system 'sms not set up' reply for the SMS contact's alias",
    )
    system_reply = next(e.data for e in device.inbox if e.data.get("from") == "system")
    assert system_reply["body"] == "sms not set up; ask your admin", system_reply
    assert oracle.thread("abstudent", contact_alias) == [], "no relay message to an SMS contact"
    print("address_book: message to the SMS contact -> 'sms not set up', no thread written")

    admin.admin_push_cfg(issued_id("pgr-e2e-book"), auto=2)
    wait_until(
        lambda: device.lock_auto_min == 2,
        timeout=10,
        description="device to receive and apply cfg lock.auto=2",
    )
    print("address_book: device applied cfg lock.auto=2")

    wait_until(
        lambda: _pending_book_or_cfg_acked(issued_id("pgr-e2e-book"), "pendingCfg"),
        timeout=10,
        description="device's cfg 'shown' ack to land at the relay",
    )
    print("address_book: device's cfg ack ('shown') landed at the relay")
    device.disconnect()


def scenario_sms_log() -> None:
    """Admin creates the SMS contact Mom and picks her for the student; the derived
    `devices.smsContacts` pushes `/down cfg.sms` and the device acks `shown`; `sms out` to the
    listed contact and `sms in` from an unlisted number land in `devices/{id}/smsLog` with the
    right `st`, readable via `GET /api/devices/{id}/sms-log` (name resolved only when listed)."""
    bootstrap_admin()

    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    family_id = admin.me()["user"]["familyId"] or "default"
    smsstudent = admin.admin_user_add(
        "smsstudent", "SMSStudent", email="smsstudent@example.com", phone=None
    )
    create_device_with_secret(admin, "pgr-e2e-sms", "smsstudent")

    device = make_device("pgr-e2e-sms")
    device.connect()
    wait_until(lambda: device.connected, timeout=10, description="sms_log device to connect")

    mom_phone = "+15550002222"
    stranger_phone = "+15550003333"
    mom = admin.family_create_contact(family_id, mom_phone, "Mom")
    pushed = admin.family_put_approved(
        family_id, smsstudent["uid"], contacts=[{"uid": mom["uid"]}]
    )
    assert pushed["contacts"] == [{"uid": mom["uid"], "message": True}], pushed
    print("sms_log: admin created contact Mom and approved it for the student")

    wait_until(
        lambda: any(c.get("phone") == mom_phone for c in device.sms_contacts),
        timeout=10,
        description="device to receive and apply cfg.sms containing Mom",
    )
    print(f"sms_log: device applied sms_contacts={device.sms_contacts}")

    wait_until(
        lambda: _pending_book_or_cfg_acked(issued_id("pgr-e2e-sms"), "pendingCfgSms"),
        timeout=10,
        description="device's cfg.sms 'shown' ack to land at the relay",
    )
    print("sms_log: device's cfg.sms ack ('shown') landed at the relay")

    out_id = device.sms_send(mom_phone, "on my way")
    in_id = device.sms_receive(stranger_phone, "who is this")

    def _log_entries() -> list[dict]:
        return admin.sms_log_get(issued_id("pgr-e2e-sms"))["entries"]

    wait_until(
        lambda: len(_log_entries()) >= 2,
        timeout=10,
        description="both sms_log entries to land at the relay",
    )
    entries = {e["id"]: e for e in _log_entries()}
    assert entries[out_id]["dir"] == "out" and entries[out_id]["st"] == "sent", entries[out_id]
    assert entries[out_id]["name"] == "Mom", entries[out_id]
    assert entries[in_id]["dir"] == "in" and entries[in_id]["st"] == "blocked", entries[in_id]
    assert entries[in_id]["name"] is None, entries[in_id]
    print(
        "sms_log: out->Mom logged 'sent', in from an unlisted number logged 'blocked' "
        "with no name resolved"
    )
    device.disconnect()


BRIDGE_SIM_URL = "http://localhost:8020"
BRIDGE_SIM_SIM_NUMBER = "+15550007777"  # relay/docker-compose.yml's BRIDGE_SIM_SIM_NUMBER


def _bridge_member(admin: pager_client.ServerClient, family_id: str, alias: str, device_id: str):
    """A member with a pager that is connected, whose policy lets numbers in and out."""
    member = admin.admin_user_add(alias, alias.title(), email=f"{alias}@example.com", phone=None)
    create_device_with_secret(admin, device_id, alias)
    device = make_device(device_id)
    device.connect()
    wait_until(lambda: device.connected, timeout=10, description=f"{device_id} to connect")
    resp = admin._http.patch(
        f"{admin.api_url}/api/family/members/{member['uid']}?family={family_id}",
        json={"policy": {"out": "people_sms", "in": "people_sms"}},
        headers=admin._headers(),
    )
    assert resp.status_code == 200, resp.text
    return member, device


def _pair_sim(
    admin: pager_client.ServerClient,
    sim: Any,
    family_id: str,
    owner_uid: str,
    label: str,
    **numbers: Any,
) -> dict[str, Any]:
    """Creates the bridge row and code through the family API, pairs the simulator, and waits for a
    heartbeat and the member's `smsNumber`."""
    created = admin.family_create_bridge(family_id, owner_uid, label)
    bridge_id = created["bridge"]["id"]
    sim.reset()
    sim.pair(created["code"], **numbers)

    def _paired() -> bool:
        row = next(b for b in admin.family_list_bridges(family_id) if b["id"] == bridge_id)
        return bool(row["paired"] and row["lastSeenAt"])

    wait_until(_paired, timeout=15, description="the bridge row to show paired with lastSeenAt")
    return next(b for b in admin.family_list_bridges(family_id) if b["id"] == bridge_id)


def _sim_sent(sim: Any, since: int = 0) -> list[dict[str, Any]]:
    return [i for i in sim.outbox()[since:] if i.get("kind") == "send"]


def scenario_bridge() -> None:
    """Against the `bridge-sim` service: pair; an SMS from an unknown number is held (`sms_unknown`
    alert) and approval delivers it; the pager's reply reaches the sim on `sms` and is acked
    `sent`; a Voice text from the same number is the same contact and replies go on `gvoice`; an
    unknown Google Chat group is held behind one `chat_unknown` alert, subscribe delivers the
    backlog with `sndr`, a reply reaches the sim, inspect by link works, an ignored one drops."""
    import bridge_sim

    bootstrap_admin()
    oracle = Oracle()
    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    family_id = admin.me()["user"]["familyId"] or "default"
    sim = bridge_sim.BridgeSimClient(BRIDGE_SIM_URL)

    kid, device = _bridge_member(admin, family_id, "bkid", "pgr-e2e-brg")
    voice_number = "+15550009999"
    bridge = _pair_sim(
        admin, sim, family_id, kid["uid"], "Kitchen phone", voiceNumber=voice_number
    )
    assert bridge["simNumber"] == BRIDGE_SIM_SIM_NUMBER and bridge["voiceNumber"] == voice_number, bridge
    assert bridge["caps"]["sms"] and bridge["caps"]["gvoice"] and "tokenHash" not in bridge, bridge
    wait_until(
        lambda: oracle.user(kid["uid"]).smsNumber == BRIDGE_SIM_SIM_NUMBER,
        timeout=10,
        description="the bridge SIM to become the member's smsNumber",
    )
    bridge_id = bridge["id"]
    print("bridge: paired; the member's smsNumber is the SIM")

    # SMS from an unknown number -> held, then approved.
    neighbor = "+15550006666"
    device.inbox.clear()
    out = sim.inject(
        {
            "source": "sms",
            "conversation": {"id": "sms-1", "isGroup": False},
            "sender": {"name": "", "phone": neighbor},
            "text": "who dis",
        }
    )
    assert out["results"][0]["outcome"] == "held", out
    alert = next(
        a
        for a in admin.family_list_alerts(family_id)
        if a["kind"] == "sms_unknown" and a.get("peerPhone") == neighbor
    )
    assert not any(e.data.get("body") == "who dis" for e in device.inbox), "held text leaked"
    decision = admin.family_approve_alert(family_id, alert["id"], name="BridgeNeighbor")
    assert decision["delivered"] == 1 and decision["undelivered"] == 0, decision
    wait_until(
        lambda: any(e.data.get("body") == "who dis" for e in device.inbox),
        timeout=10,
        description="the approved held SMS to reach the pager",
    )
    print("bridge: SMS held, approved, delivered")

    contact = next(
        c for c in admin.family_list_contacts(family_id) if c["phone"] == neighbor
    )
    # Pager reply -> outbox (sms) -> acked sent.
    t_publish = time.monotonic()
    device.publish_msg("on my way", to=contact["alias"])
    wait_until(
        lambda: any(i["text"] == "on my way" for i in _sim_sent(sim)),
        timeout=15,
        interval=0.05,
        description="the pager reply to reach the bridge outbox",
    )
    print(f"bridge: pager -> bridge ack latency {time.monotonic() - t_publish:.2f} s")
    item = next(i for i in _sim_sent(sim) if i["text"] == "on my way")
    assert item["source"] == "sms" and item["to"]["phone"] == neighbor, item
    assert item["ackState"] == "sent", item

    def _delivery_state(text: str) -> str | None:
        for _doc_id, data in store().list(parent="messages"):
            if data.get("body") == text and data.get("senderUid") == kid["uid"]:
                return (data.get("deliveries") or {}).get("sms", {}).get("state")
        return None

    wait_until(
        lambda: _delivery_state("on my way") == "sent",
        timeout=15,
        description="the ack to mark the delivery sent",
    )
    print("bridge: pager reply -> outbox (sms) -> acked sent -> delivery sent")

    # Voice text from the same number -> same contact; replies now go on gvoice.
    out = sim.inject(
        {
            "source": "gvoice",
            "conversation": {"id": "voice-thread-1", "isGroup": False},
            "sender": {"name": "BridgeNeighbor", "phone": neighbor},
            "text": "texting from voice",
        }
    )
    assert out["results"][0]["outcome"] == "delivered", out
    assert len([c for c in admin.family_list_contacts(family_id) if c["phone"] == neighbor]) == 1
    device.publish_msg("got it", to=contact["alias"])
    wait_until(
        lambda: any(i["text"] == "got it" for i in _sim_sent(sim)),
        timeout=15,
        description="the second pager reply to reach the bridge outbox",
    )
    item = next(i for i in _sim_sent(sim) if i["text"] == "got it")
    assert item["source"] == "gvoice" and item["to"]["conversationId"] == "voice-thread-1", item
    assert item["to"]["link"].endswith(f"t.{neighbor}"), item
    print("bridge: Voice text -> same contact, reply went out on gvoice with the thread")

    # Unknown Google Chat group -> one chat_unknown alert, nothing on the pager.
    group_id = "spaces/AAA|soccer"
    link = "https://chat.google.com/room/AAA"
    device.inbox.clear()
    for sender, text in (("Devon P", "who is driving?"), ("Logan", "I can")):
        out = sim.inject(
            {
                "source": "gchat",
                "conversation": {
                    "id": group_id,
                    "title": "Soccer carpool",
                    "isGroup": True,
                    "link": link,
                },
                "sender": {"name": sender},
                "text": text,
            }
        )
        assert out["results"][0]["outcome"] == "held", out
    chat_alerts = [a for a in admin.family_list_alerts(family_id) if a["kind"] == "chat_unknown"]
    assert len(chat_alerts) == 1 and chat_alerts[0]["heldCount"] == 2, chat_alerts
    assert chat_alerts[0]["people"] == ["Devon P", "Logan"], chat_alerts[0]
    time.sleep(1)
    assert not any(e.data.get("body") == "who is driving?" for e in device.inbox), "held chat leaked"
    tab = admin.family_member_chat(family_id, kid["uid"])
    (seen,) = [r for r in tab["seen"] if r["conversationId"] == group_id]
    result = admin.family_chat_subscribe(
        family_id,
        bridge_id,
        seen["ref"],
        pager_name="Soccer",
        roster=[{"name": "Devon P", "nick": "devon"}, {"name": "Logan", "nick": "logan"}],
    )
    assert result["delivered"] == 2 and result["undelivered"] == 0, result
    group_alias = result["alias"]
    wait_until(
        lambda: sum(1 for e in device.inbox if e.data.get("sndr")) >= 2,
        timeout=10,
        description="the Chat backlog to reach the pager with sndr",
    )
    pages = [e.data for e in device.inbox if e.data.get("sndr")]
    assert [(p["from"], p["sndr"], p["body"]) for p in pages[:2]] == [
        (group_alias, "devon", "who is driving?"),
        (group_alias, "logan", "I can"),
    ], pages
    assert not [a for a in admin.family_list_alerts(family_id) if a["kind"] == "chat_unknown"]
    print("bridge: Chat group held behind one alert, subscribe delivered the backlog with sndr")

    # Pager reply to the group -> outbox with the conversation.
    device.publish_msg("I will drive", to=group_alias)
    wait_until(
        lambda: any(i["text"] == "I will drive" for i in _sim_sent(sim)),
        timeout=15,
        description="the group reply to reach the bridge outbox",
    )
    item = next(i for i in _sim_sent(sim) if i["text"] == "I will drive")
    assert item["source"] == "gchat" and item["to"]["conversationId"] == group_id, item
    assert item["to"]["link"] == link, item
    print("bridge: pager reply to the group reached the outbox with conversationId and link")

    # Inspect by link -> the phone reports the conversation.
    inspect_link = "https://chat.google.com/dm/grandma-xyz"
    ob = admin.family_bridge_inspect(family_id, bridge_id, inspect_link)["outboxId"]
    wait_until(
        lambda: any(i["id"] == ob for i in sim.outbox()),
        timeout=15,
        description="the inspect request to reach the phone",
    )
    out = sim.inject(
        {
            "source": "gchat",
            "kind": "inspect",
            "conversation": {"id": "dm/grandma-xyz", "title": "Grandma", "isGroup": False, "link": inspect_link},
            "sender": {"name": ""},
            "people": ["Grandma"],
        }
    )
    assert out["results"][0]["outcome"] == "inspected", out
    tab = admin.family_member_chat(family_id, kid["uid"])
    assert any(r["link"] == inspect_link and r["status"] == "seen" for r in tab["seen"]), tab["seen"]
    print("bridge: inspect by link produced a seen row")

    # Ignore another conversation -> later events drop.
    other = {
        "source": "gchat",
        "conversation": {"id": "spaces/ZZZ", "title": "Spam room", "isGroup": True},
        "sender": {"name": "Bot"},
        "text": "buy now",
    }
    assert sim.inject(other)["results"][0]["outcome"] == "held"
    tab = admin.family_member_chat(family_id, kid["uid"])
    (spam,) = [r for r in tab["seen"] if r["conversationId"] == "spaces/ZZZ"]
    admin.family_chat_ignore(family_id, bridge_id, spam["ref"])
    assert sim.inject(other)["results"][0]["outcome"] == "dropped_ignored"
    print("bridge: ignored conversation drops")
    device.disconnect()


def scenario_bridge_voice() -> None:
    """A Google-Voice-only bridge (no SIM): the member's `smsNumber` is the Voice number and a pager
    text to a contact reaches the phone as `gvoice` with `to.link`, no thread needed."""
    import bridge_sim

    bootstrap_admin()
    oracle = Oracle()
    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    family_id = admin.me()["user"]["familyId"] or "default"
    sim = bridge_sim.BridgeSimClient(BRIDGE_SIM_URL)

    kid, device = _bridge_member(admin, family_id, "vkid", "pgr-e2e-brv")
    voice_number = "+15550001111"
    bridge = _pair_sim(
        admin, sim, family_id, kid["uid"], "Voice only", simNumber=None, voiceNumber=voice_number
    )
    assert bridge["simNumber"] is None and bridge["voiceNumber"] == voice_number, bridge
    assert bridge["caps"]["gvoice"] and not bridge["caps"]["sms"], bridge
    wait_until(
        lambda: oracle.user(kid["uid"]).smsNumber == voice_number,
        timeout=10,
        description="the Voice number to become the member's smsNumber",
    )
    gran_number = "+15550002222"
    gran = admin.family_create_contact(family_id, gran_number, "VoiceGran")
    admin.family_put_approved(family_id, kid["uid"], contacts=[{"uid": gran["uid"]}])

    # Cold outbound: no thread has ever been seen for this number.
    alias = oracle.user(gran["uid"]).alias
    device.publish_msg("hello grandma", to=alias)
    wait_until(
        lambda: any(i["text"] == "hello grandma" for i in _sim_sent(sim)),
        timeout=15,
        description="the cold Voice text to reach the bridge outbox",
    )
    item = next(i for i in _sim_sent(sim) if i["text"] == "hello grandma")
    assert item["source"] == "gvoice" and item["to"]["phone"] == gran_number, item
    assert item["to"]["link"] == f"https://voice.google.com/u/0/messages?itemId=t.{gran_number}", item
    assert item["ackState"] == "sent", item

    # Inbound Voice text from the contact is delivered to the pager.
    device.inbox.clear()
    out = sim.inject(
        {
            "source": "gvoice",
            "conversation": {"id": "vt-gran", "isGroup": False},
            "sender": {"name": "VoiceGran", "phone": gran_number},
            "text": "hi dear",
        }
    )
    assert out["results"][0]["outcome"] == "delivered", out
    wait_until(
        lambda: any(e.data.get("body") == "hi dear" for e in device.inbox),
        timeout=10,
        description="the Voice text to reach the pager",
    )
    # A SIM-sourced event is refused: this phone has no SIM.
    out = sim.inject(
        {
            "source": "sms",
            "conversation": {"id": "sms-x", "isGroup": False},
            "sender": {"name": "", "phone": gran_number},
            "text": "sim?",
        }
    )
    assert out["results"][0]["outcome"] == "dropped_cap", out
    print("bridge_voice: Voice-only bridge: cold outbound on gvoice with link, inbound delivered")
    device.disconnect()


def scenario_bridge_whatsapp() -> None:
    """WhatsApp as a bridge source. A LID DM (`<digits>@lid`, name only) is held behind a
    `chat_unknown` alert; subscribe delivers the backlog; the pager's reply carries
    `to.conversationId` + `to.title` and no phone. A phone-JID DM takes the same path and its
    reply also carries `to.link`. A group is held behind one alert, subscribe delivers the
    backlog with `sndr` (`~ ` stripped), and the reply carries conversationId and title.
    Inspect by a wa.me link is a 400."""
    import bridge_sim

    bootstrap_admin()
    oracle = Oracle()
    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    family_id = admin.me()["user"]["familyId"] or "default"
    sim = bridge_sim.BridgeSimClient(BRIDGE_SIM_URL)

    kid, device = _bridge_member(admin, family_id, "wkid", "pgr-e2e-brw")
    wa_sim_number = "+15550003333"  # the default SIM number belongs to the `bridge` scenario's member
    bridge = _pair_sim(
        admin, sim, family_id, kid["uid"], "WhatsApp phone",
        simNumber=wa_sim_number, voiceNumber=None, whatsapp=True,  # the sim keeps bridge_voice's Voice number otherwise
    )
    assert bridge["caps"]["whatsapp"] is True, bridge
    bridge_id = bridge["id"]
    wait_until(
        lambda: oracle.user(kid["uid"]).smsNumber == wa_sim_number,
        timeout=10,
        description="the bridge SIM to become the member's smsNumber",
    )

    def dm_round(digits_phone: str, name: str, pager_name: str, *, lid: bool) -> tuple[str, dict[str, Any]]:
        """Hold one DM, subscribe it, check the backlog, send a pager reply; return (conv id, outbox item)."""
        conv = digits_phone.lstrip("+") + ("@lid" if lid else "@s.whatsapp.net")
        text, reply = f"hey from {name}", f"hi back {name}"
        device.inbox.clear()
        out = sim.inject(bridge_sim.whatsapp_dm_event(digits_phone, text, name=name, lid=lid))
        assert out["results"][0]["outcome"] == "held", out
        alerts = [
            a
            for a in admin.family_list_alerts(family_id)
            if a["kind"] == "chat_unknown" and a.get("source") == "whatsapp" and not a.get("isGroup")
        ]
        assert len(alerts) == 1, alerts
        assert not any(
            a["kind"] == "sms_unknown" and a.get("subjectUid") == kid["uid"]
            for a in admin.family_list_alerts(family_id)
        ), "DM took the SMS path"  # scoped to this kid: earlier scenarios leave their own sms_unknown alerts
        assert not any(e.data.get("body") == text for e in device.inbox), "held text leaked"
        tab = admin.family_member_chat(family_id, kid["uid"])
        (seen,) = [r for r in tab["seen"] if r["conversationId"] == conv]
        assert seen["source"] == "whatsapp" and seen["title"] == name and not seen.get("isGroup"), seen
        result = admin.family_chat_subscribe(family_id, bridge_id, seen["ref"], pager_name=pager_name)
        assert result["delivered"] == 1 and result["undelivered"] == 0, result
        wait_until(
            lambda: any(e.data.get("body") == text for e in device.inbox),
            timeout=10,
            description="the subscribed WhatsApp DM backlog to reach the pager",
        )
        device.publish_msg(reply, to=result["alias"])
        wait_until(
            lambda: any(i["text"] == reply for i in _sim_sent(sim)),
            timeout=15,
            description="the pager reply to reach the bridge outbox",
        )
        item = next(i for i in _sim_sent(sim) if i["text"] == reply)
        assert item["source"] == "whatsapp" and item["ackState"] == "sent", item
        assert item["to"]["conversationId"] == conv and item["to"]["title"] == name, item
        assert "phone" not in item["to"], item
        return conv, item

    # LID DM (no number): held as chat_unknown -> subscribe -> backlog -> reply by conversation id + title.
    _, item = dm_round("+9876543210", "WaLid", "WaLid WA", lid=True)
    assert item["to"].get("link") is None, item
    print("bridge_whatsapp: LID DM held as chat_unknown, subscribed, reply carried conversationId and title")

    # Phone-JID DM: the same path; the reply also carries the wa.me link from the event.
    friend = "+15550004444"
    _, item = dm_round(friend, "WaFriend", "WaFriend WA", lid=False)
    assert item["to"]["link"] == f"https://wa.me/{friend.lstrip('+')}", item
    print("bridge_whatsapp: phone-JID DM took the same path, reply carried the wa.me link")

    # Group -> one chat_unknown alert -> subscribe -> backlog with sndr.
    group_jid = "120363999111@g.us"
    device.inbox.clear()
    for sender, text in (("~ Devon P", "who is driving?"), ("~Logan", "I can")):
        out = sim.inject(
            bridge_sim.whatsapp_group_event("120363999111", "Soccer WA", sender, text)
        )
        assert out["results"][0]["outcome"] == "held", out
    chat_alerts = [
        a for a in admin.family_list_alerts(family_id) if a["kind"] == "chat_unknown" and a.get("isGroup")
    ]
    assert len(chat_alerts) == 1 and chat_alerts[0]["people"] == ["Devon P", "Logan"], chat_alerts
    assert chat_alerts[0]["source"] == "whatsapp", chat_alerts[0]
    tab = admin.family_member_chat(family_id, kid["uid"])
    (seen,) = [r for r in tab["seen"] if r["conversationId"] == group_jid]
    assert seen["source"] == "whatsapp" and seen["title"] == "Soccer WA", seen
    result = admin.family_chat_subscribe(
        family_id,
        bridge_id,
        seen["ref"],
        pager_name="SoccerWA",
        roster=[{"name": "Devon P", "nick": "devon"}, {"name": "Logan", "nick": "logan"}],
    )
    assert result["delivered"] == 2 and result["undelivered"] == 0, result
    group_alias = result["alias"]
    wait_until(
        lambda: sum(1 for e in device.inbox if e.data.get("sndr")) >= 2,
        timeout=10,
        description="the WhatsApp group backlog to reach the pager with sndr",
    )
    pages = [e.data for e in device.inbox if e.data.get("sndr")]
    assert [(p["from"], p["sndr"], p["body"]) for p in pages[:2]] == [
        (group_alias, "devon", "who is driving?"),
        (group_alias, "logan", "I can"),
    ], pages
    print("bridge_whatsapp: group held behind one alert, subscribe delivered the backlog with sndr")

    device.publish_msg("I will drive", to=group_alias)
    wait_until(
        lambda: any(i["text"] == "I will drive" for i in _sim_sent(sim)),
        timeout=15,
        description="the group reply to reach the bridge outbox",
    )
    item = next(i for i in _sim_sent(sim) if i["text"] == "I will drive")
    assert item["source"] == "whatsapp" and item["to"]["conversationId"] == group_jid, item
    assert item["to"]["title"] == "Soccer WA", item
    print("bridge_whatsapp: group reply carries conversationId and title")

    # Inspect by a wa.me link is refused.
    resp = admin.api_post(
        f"/api/family/bridges/{bridge_id}/inspect?family={family_id}",
        {"link": "https://wa.me/15550004444"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "whatsapp links cannot be inspected; wait for a message", resp.text
    print("bridge_whatsapp: inspect of a wa.me link is a 400")
    device.disconnect()


def scenario_receipts() -> None:
    """A pager page to a bridged SMS contact; the simulator acks `failed sim_failed`
    (`/_fail_next`). A device that never reported `mb` gets one `system` row; once
    `devices.status.mb` is set (whitebox) it gets `kind:"rcpt"` for the failure, then a success."""
    import bridge_sim

    bootstrap_admin()
    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    family_id = admin.me()["user"]["familyId"] or "default"
    sim = bridge_sim.BridgeSimClient(BRIDGE_SIM_URL)

    kid, device = _bridge_member(admin, family_id, "rkid", "pgr-e2e-rcp")
    # The sim keeps the last scenario's numbers across `/_reset`, and those belong to its member.
    _pair_sim(
        admin, sim, family_id, kid["uid"], "Receipt phone",
        simNumber="+15550010001", voiceNumber=None,
    )
    gran = admin.family_create_contact(family_id, "+15550010002", "ReceiptGran")
    admin.family_put_approved(family_id, kid["uid"], contacts=[{"uid": gran["uid"]}])
    alias = Oracle().user(gran["uid"]).alias

    # Phase 1: no `mb` -> one `system` row on failure, nothing on success.
    device.inbox.clear()
    sim.fail_next(1)
    device.publish_msg("hello one", to=alias)

    def _system_rows() -> list[Any]:
        return [e for e in device.inbox if e.data.get("from") == "system"]

    wait_until(lambda: bool(_system_rows()), timeout=15, description="the phase-1 system row")
    time.sleep(1.0)
    (row,) = _system_rows()
    body = row.data["body"]
    assert body.startswith("to ReceiptGran ") and body.endswith(": not delivered (sim_failed)"), body
    assert not device.receipts, device.receipts
    device.publish_msg("hello two", to=alias)
    wait_until(
        lambda: any(i["text"] == "hello two" for i in _sim_sent(sim)),
        timeout=15,
        description="the second page to reach the bridge outbox",
    )
    time.sleep(1.0)
    assert len(_system_rows()) == 1 and not device.receipts, "a success must stay silent on phase 1"
    print(f"receipts: phase 1 system row: {body!r}")

    # Phase 2: `mb` reported -> rcpt for both outcomes.
    store().merge(f"devices/{device.device_id}", {"status": {"mb": 1024}})
    device.inbox.clear()
    sim.fail_next(1)
    failed_id = device.publish_msg("hello three", to=alias)
    wait_until(lambda: len(device.receipts) >= 1, timeout=15, description="the failed rcpt")
    assert device.receipts[0]["ref"] == failed_id, device.receipts
    assert device.receipts[0]["st"] == "failed" and device.receipts[0]["rsn"] == "sim_failed"
    ok_id = device.publish_msg("hello four", to=alias)
    wait_until(lambda: len(device.receipts) >= 2, timeout=15, description="the sent rcpt")
    assert device.receipts[1]["ref"] == ok_id and device.receipts[1]["st"] == "sent", device.receipts
    assert not _system_rows(), "an mb device must not get the phase-1 row"
    print("receipts: phase 2 rcpt failed(sim_failed) then sent")
    device.disconnect()


def scenario_bridge_health() -> None:
    """A silent bridge is announced once (a `system` alert and one pager `system` text) however
    many ticks follow; its next heartbeat closes the alert; losing the SMS role is a `degraded`
    notice and regaining it a "fully working" one. Silence is `/_pause` + stale `lastSeenAt`."""
    from datetime import UTC, datetime, timedelta

    import bridge_sim

    bootstrap_admin()
    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")
    family_id = admin.me()["user"]["familyId"] or "default"
    sim = bridge_sim.BridgeSimClient(BRIDGE_SIM_URL)

    kid, device = _bridge_member(admin, family_id, "hkid", "pgr-e2e-hlt")
    bridge = _pair_sim(
        admin, sim, family_id, kid["uid"], "Health phone",
        simNumber="+15550010003", voiceNumber=None,  # not the previous scenario's number
    )
    bridge_id = bridge["id"]
    gran = admin.family_create_contact(family_id, "+15550004444", "HealthGran")
    admin.family_put_approved(family_id, kid["uid"], contacts=[{"uid": gran["uid"]}])

    def _system_texts() -> list[str]:
        return [e.data["body"] for e in device.inbox if e.data.get("from") == "system"]

    def _alert(code: str) -> dict[str, Any] | None:
        return next(
            (
                a
                for a in admin.family_list_alerts(family_id, status="all")
                if a["kind"] == "system" and a.get("code") == code and a.get("key") == bridge_id
            ),
            None,
        )

    def _health_state() -> str | None:
        row = next(b for b in admin.family_list_bridges(family_id) if b["id"] == bridge_id)
        return (row.get("health") or {}).get("state")

    # Offline: silent phone + stale lastSeenAt -> one notice over three ticks.
    sim.pause()
    time.sleep(6)  # an outbox long-poll already in flight has returned
    store().merge(
        f"bridges/{bridge_id}", {"lastSeenAt": datetime.now(UTC) - timedelta(minutes=10)}
    )
    device.inbox.clear()
    for _ in range(3):
        internal("/internal/tick")
    wait_until(
        lambda: any(t.startswith("Bridge phone offline since") for t in _system_texts()),
        timeout=15,
        description="the offline system text",
    )
    time.sleep(1.0)
    assert len([t for t in _system_texts() if t.startswith("Bridge phone offline")]) == 1, _system_texts()
    assert _health_state() == "offline"
    alert = _alert("bridge_offline")
    assert alert is not None and alert["status"] == "open", alert
    print(f"bridge_health: offline -> {_system_texts()[0]!r}")

    # Back: the heartbeat closes it at once.
    device.inbox.clear()
    sim.resume()
    wait_until(
        lambda: "Bridge phone is back." in _system_texts(),
        timeout=15,
        description="the restored system text",
    )
    assert _health_state() == "ok"
    alert = _alert("bridge_offline")
    assert alert is not None and alert["status"] == "handled", alert
    print("bridge_health: restored")

    # Degraded: the SMS role is lost, then regained.
    device.inbox.clear()
    sim.set_status(smsDefault=False)
    wait_until(
        lambda: "Bridge phone lost SMS role." in _system_texts(),
        timeout=15,
        description="the degraded system text",
    )
    assert _health_state() == "degraded"
    sim.set_status(smsDefault=True)
    wait_until(
        lambda: "Bridge phone is fully working again." in _system_texts(),
        timeout=15,
        description="the fully-working system text",
    )
    assert _health_state() == "ok"
    print("bridge_health: degraded then fully working")
    device.disconnect()


SCENARIOS: dict[str, Callable[[], None]] = {
    "bootstrap": scenario_bootstrap,
    "text_roundtrip": scenario_text_roundtrip,
    "allowlist": scenario_allowlist,
    "republish": scenario_republish,
    "location_periodic": scenario_location_periodic,
    "location_on_demand": scenario_location_on_demand,
    "retention": scenario_retention,
    "bytes": scenario_bytes,
    "address_book": scenario_address_book,
    "setup_code": scenario_setup_code,
    "sms_log": scenario_sms_log,
    "bridge": scenario_bridge,
    "bridge_voice": scenario_bridge_voice,
    "bridge_whatsapp": scenario_bridge_whatsapp,
    "receipts": scenario_receipts,
    "bridge_health": scenario_bridge_health,
}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenarios", nargs="*", default=list(SCENARIOS), help="scenario names to run, in order"
    )
    parser.add_argument("--no-build", action="store_true", help="skip `docker compose build`")
    parser.add_argument(
        "--keep-stack",
        action="store_true",
        help="leave the stack (and, for home stacks, home/.e2e-data and home/.e2e.env) up "
        "after the run, for inspection; the next run clears it",
    )
    parser.add_argument(
        "--wire",
        choices=["json", "cbor"],
        default="json",
        help="wire encoding every simulated device in this run speaks "
        "-- run the whole suite once per value to cover both (default: json)",
    )
    parser.add_argument(
        "--stack",
        choices=["cloud", "home", "home-emqx", "move", "move-emqx"],
        default="cloud",
        help="cloud: relay/docker-compose.yml (emulators, EMQX, plain ports). home: "
        "home/docker-compose.yml (Mosquitto, the relay as its MQTT client) + the test profile, "
        "through nginx's TLS edge with the stack's own CA. "
        "home-emqx: the same plus home/docker-compose.emqx.yml (EMQX, BROKER_MODE=rest). Both "
        "home stacks run as compose project pager-e2e with home/.e2e-data and home/.e2e.env "
        "(never home/data or home/pager.env), removed afterwards. "
        "move: the over-the-air move between two stacks at once (tools/e2e_move.py): the cloud "
        "stack as the source, the home stack as the target (move-emqx: with EMQX as its broker); only --no-build and --keep-stack apply "
        "(the move pagers speak CBOR)",
    )
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        type=Path,
        metavar="FILE",
        help="extra compose file layered over relay/docker-compose.yml, e.g. "
        "relay/docker-compose.home-auth.yml (AUTH_BACKEND=home DB_BACKEND=sqlite); repeatable",
    )
    args = parser.parse_args(argv)
    if args.stack in ("move", "move-emqx"):
        import e2e_move

        return e2e_move.main(
            [*(["--no-build"] if args.no_build else []), *(["--keep-stack"] if args.keep_stack else []),
             "--wire", "cbor" if args.wire == "json" else args.wire,
             "--target-broker", "emqx" if args.stack == "move-emqx" else "mosquitto"]
        )
    global STACK, HOME_STACK, BROKER, RELAY_URL, MQTT_PORT
    STACK = args.stack
    HOME_STACK = STACK in ("home", "home-emqx")
    if STACK == "home":
        BROKER = "mosquitto"
        COMPOSE_FILES[:] = [HOME_DIR / "docker-compose.yml"]
    elif STACK == "home-emqx":
        COMPOSE_FILES[:] = [HOME_DIR / "docker-compose.yml", HOME_DIR / "docker-compose.emqx.yml"]
    if HOME_STACK:
        RELAY_URL = "https://localhost"
        MQTT_PORT = 8883
        pager_client.TLS_CA_CERTS = str(HOME_CA)
    COMPOSE_FILES.extend(p.resolve() for p in args.override)

    unknown = [s for s in args.scenarios if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {unknown}; choices: {list(SCENARIOS)}")

    global WIRE_MODE
    WIRE_MODE = args.wire

    start_stack(build=not args.no_build)
    passed: list[str] = []
    try:
        for name in args.scenarios:
            print(f"\n=== scenario: {name} ===")
            SCENARIOS[name]()
            passed.append(name)
        print(f"\nALL PASSED: {passed}")
        return 0
    except Exception:
        logger.exception("scenario failed after passing: %s", passed)
        dump_logs()
        return 1
    finally:
        if args.keep_stack:
            print("\n=== --keep-stack: leaving the compose stack up ===")
        else:
            stop_stack()


if __name__ == "__main__":
    sys.exit(main())
