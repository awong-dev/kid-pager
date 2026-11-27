#!/usr/bin/env python3
"""
Two-stack end-to-end run of the over-the-air deployment move (docs/MOVE_DESIGN.md §14).

The SOURCE is the cloud-style stack (`relay/docker-compose.yml`: relay, EMQX, Firebase emulators), the
TARGET the home stack (`home/docker-compose.yml`: relay, Mosquitto, nginx with TLS, mounted at the
default `/kid-pager`). Both run at once as their own compose projects (`kp-move-src`, `kp-move-tgt`),
so a run never touches `relay-*` containers, `relay/.env`, `home/data` or `home/pager.env`; the
target's data directory and env file live in `tools/.e2e-move/` (self-ignoring).

How the two stacks see each other:
- The source refuses loopback and private addresses (`app/fed/addr.py`), so the target gets the
  hostname `home.kp-move.test` and, on a Docker network `kp-move-e2e` with a non-private subnet
  (11.0.0.0/24), the address 11.0.0.10. That address is a TCP gateway (`move-gw`) in front of the
  target's nginx rather than nginx itself: both stacks name their web service `relay`, and a shared
  network would make each stack's nginx and relay resolve the other's. The source relay maps the
  name to the gateway (`extra_hosts`) and trusts the target's CA through `SSL_CERT_FILE`.
- This process maps the same name to 127.0.0.1 (a `getaddrinfo` patch), so the simulated pagers and
  the bridge phone reach the target's published 443 and 8883 under the name the order carries.

Usage (needs the relay virtualenv):
    relay/.venv/bin/python tools/e2e_move.py [--no-build] [--keep-stack]
    relay/.venv/bin/python tools/e2e_v2.py --stack move       # the same
Exit code 0 if the scenario passes; both stacks are torn down unless `--keep-stack`.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("e2e_move")

REPO_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = REPO_ROOT / "relay"
HOME_DIR = REPO_ROOT / "home"
TOOLS_DIR = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(RELAY_DIR))
import bridge_sim
import httpx
import pager_client
import pager_move

WORK = TOOLS_DIR / ".e2e-move"
DATA = WORK / "data"
TARGET_ENV = WORK / "target.env"
TARGET_OVERRIDE = WORK / "target.override.yml"
GATEWAY_CONF = WORK / "gateway.conf"
SOURCE_OVERRIDE = WORK / "source.override.yml"
TARGET_CA = DATA / "certs" / "ca" / "ca.crt"

# The target's broker: "mosquitto" (`--stack home`) or "emqx" (`--stack home-emqx`).
BROKER = "mosquitto"
SRC_PROJECT = "kp-move-src"
TGT_PROJECT = "kp-move-tgt"
NETWORK = "kp-move-e2e"
SUBNET = "11.0.0.0/24"
TARGET_IP = "11.0.0.10"
TARGET_HOST = "home.kp-move.test"
TARGET_URL = f"https://{TARGET_HOST}/kid-pager"

SRC_URL = "http://localhost:8000"
SRC_MQTT_PORT = 1883
# The emulators are published off their usual ports so the stack never meets another one's.
FIRESTORE_PORT, AUTH_PORT, UI_PORT = 18080, 19099, 14000
AUTH_URL = f"http://localhost:{AUTH_PORT}"
EMQX_API_URL = "http://localhost:18083"
PROJECT_ID = "demo-pager"
BROKER_API_KEY, BROKER_API_SECRET = "dev-broker-api-key", "dev-broker-api-secret"
WEBHOOK_KEY = "dev-webhook-key"
PASSPHRASE = "e2e move passphrase"

PAGER_TIMERS = pager_move.Timers(
    schedule_s=(0, 5, 10), background_s=3600, ack_wait_s=10, guard_s=15, proof_wait_s=30, ca_retry_s=2,
    moving_hold_s=3600,
)
# The second pager gets two trials so the log shows `pend/conn` and then `fail/budget`.
FAILING_TIMERS = pager_move.Timers(
    schedule_s=(0, 4), background_s=3600, ack_wait_s=10, guard_s=15, proof_wait_s=30, ca_retry_s=2,
    moving_hold_s=3600,
)

_real_getaddrinfo = socket.getaddrinfo


def _map_target_host(host: Any, *args: Any, **kwargs: Any) -> Any:
    return _real_getaddrinfo("127.0.0.1" if host == TARGET_HOST else host, *args, **kwargs)


def wait_until(
    predicate: Callable[[], bool], *, timeout: float, interval: float = 0.5, description: str = ""
) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # noqa: BLE001 -- reported in the failure
            last_exc = exc
        time.sleep(interval)
    extra = f" (last error: {last_exc!r})" if last_exc else ""
    raise AssertionError(f"timed out after {timeout}s waiting for: {description}{extra}")


# ---- the two stacks ---------------------------------------------------------------------------


def run(cmd: list[str], *, check: bool = True, capture: bool = False, env: dict[str, str] | None = None,
        cwd: Path | None = None, timeout: float = 1200) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(
        cmd, check=check, capture_output=capture, text=True, timeout=timeout, cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
    )


def source_compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    files = ["-f", str(RELAY_DIR / "docker-compose.yml"), "-f", str(SOURCE_OVERRIDE)]
    return run(["docker", "compose", "-p", SRC_PROJECT, *files, *args], check=check, capture=capture, cwd=RELAY_DIR)


def target_compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    files = ["-f", str(HOME_DIR / "docker-compose.yml")]
    if BROKER == "emqx":
        files += ["-f", str(HOME_DIR / "docker-compose.emqx.yml")]
    files += ["-f", str(TARGET_OVERRIDE)]
    env = {
        "PAGER_WEB": "none",  # a relay image without the web export
        "SCHEDULER_MODE": "none",
        "PAGER_DATA_DIR": str(DATA),
        "PAGER_ENV_FILE": str(TARGET_ENV),
        # Published on loopback only; 443 and 8883 are what the target advertises.
        "PAGER_HTTP_BIND": "127.0.0.1:8081",
        "PAGER_HTTPS_BIND": "127.0.0.1:443",
        "PAGER_MQTTS_BIND": "127.0.0.1:8883",
        "PAGER_BEAM_BIND": "127.0.0.1:8884",
    }
    return run(["docker", "compose", "-p", TGT_PROJECT, *files, *args], check=check, capture=capture, env=env, cwd=HOME_DIR)


def write_target_files() -> None:
    WORK.mkdir(exist_ok=True)
    (WORK / ".gitignore").write_text("*\n")
    DATA.mkdir(exist_ok=True)
    TARGET_ENV.write_text(
        "\n".join(
            [
                f"PAGER_HOST={TARGET_HOST}",
                "TLS_MODE=selfsigned",
                # PAGER_PATH is left unset: the app is served under the default /kid-pager.
                "DEV_MODE=1",
                "RATE_LIMIT_BACKEND_CREATE_LIMIT=1000",
                "RATE_LIMIT_ADMIN_WRITE_LIMIT=1000",
                "RATE_LIMIT_WEBHOOK_IP_LIMIT=1000",
                "RATE_LIMIT_AUTH_IP_LIMIT=1000",
                "",
            ]
        )
    )
    GATEWAY_CONF.write_text(
        "events {}\nstream {\n  server {\n    listen 443;\n    proxy_pass nginx:443;\n  }\n}\n"
    )
    TARGET_OVERRIDE.write_text(
        "\n".join(
            [
                "services:",
                "  move-gw:",
                "    image: nginx:stable-alpine",
                "    depends_on:",
                "      - nginx",
                "    volumes:",
                f"      - {GATEWAY_CONF}:/etc/nginx/nginx.conf:ro",
                "    networks:",
                "      default: {}",
                f"      {NETWORK}:",
                f"        ipv4_address: {TARGET_IP}",
                *(
                    # the source's EMQX holds 127.0.0.1:18083
                    ["  emqx:", "    ports: !override", '      - "127.0.0.1:18084:18083"']
                    if BROKER == "emqx"
                    else []
                ),
                "networks:",
                f"  {NETWORK}:",
                "    external: true",
                "",
            ]
        )
    )


def write_source_override() -> None:
    SOURCE_OVERRIDE.write_text(
        "\n".join(
            [
                "services:",
                "  relay:",
                "    environment:",
                "      BROKER_API_URL: http://emqx:18083/api/v5",
                f"      BROKER_API_KEY: {BROKER_API_KEY}",
                f"      BROKER_API_SECRET: {BROKER_API_SECRET}",
                f"      WEBHOOK_KEY: {WEBHOOK_KEY}",
                "      SSL_CERT_FILE: /e2e/target-ca.crt",
                "    extra_hosts:",
                f'      - "{TARGET_HOST}:{TARGET_IP}"',
                "    volumes:",
                f"      - {TARGET_CA}:/e2e/target-ca.crt:ro",
                "    networks: !override",
                "      - pager-net",
                f"      - {NETWORK}",
                "  firebase:",
                "    ports: !override",
                f'      - "{FIRESTORE_PORT}:8080"',
                f'      - "{AUTH_PORT}:9099"',
                f'      - "{UI_PORT}:4000"',
                "networks:",
                f"  {NETWORK}:",
                "    external: true",
                "",
            ]
        )
    )


def target_reachable() -> bool:
    try:
        return httpx.get(f"{TARGET_URL}/healthz", timeout=3.0, verify=target_ctx()).status_code == 200
    except (httpx.HTTPError, OSError):
        return False


def target_ctx() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(TARGET_CA))


def start_stacks(*, build: bool) -> None:
    socket.getaddrinfo = _map_target_host  # type: ignore[assignment]
    if DATA.exists() and any(DATA.iterdir()):
        print(f"{DATA} is left over from an earlier run; clearing it")
        stop_stacks()
    write_target_files()
    run(["docker", "network", "create", "--subnet", SUBNET, NETWORK], check=False, capture=True)
    # The target first: its CA is what the source relay trusts.
    target_compose("up", "-d", *(["--build"] if build else []))
    wait_until(target_reachable, timeout=180, interval=2, description="the target relay behind nginx")
    write_source_override()
    source_compose("up", "-d", *(["--build"] if build else []))
    wait_until(
        lambda: httpx.get(f"{SRC_URL}/healthz", timeout=3.0).status_code == 200,
        timeout=180, interval=2, description="the source relay",
    )
    wait_until(
        lambda: httpx.get(
            f"{AUTH_URL}/emulator/v1/projects/{PROJECT_ID}/accounts", timeout=3.0
        ).status_code == 405,
        timeout=60, interval=1, description="the Auth emulator",
    )
    result = subprocess.run(
        [
            sys.executable, str(TOOLS_DIR / "emqx_setup.py"), "--emqx-url", EMQX_API_URL,
            "--relay-url", "http://relay:8000", "--webhook-key", WEBHOOK_KEY,
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, end="", file=sys.stderr)
        raise AssertionError("tools/emqx_setup.py failed")


def stop_stacks() -> None:
    print("\n=== tearing down both stacks ===")
    if SOURCE_OVERRIDE.exists():
        source_compose("down", "-v", check=False)
    if TARGET_OVERRIDE.exists():
        # The containers wrote the data directory as root; clear it from inside one.
        target_compose(
            "run", "--rm", "--no-deps", "--entrypoint", "sh", "init", "-c", "rm -rf /data/* /data/.[!.]*",
            check=False,
        )
        target_compose("down", "-v", check=False)
    run(["docker", "network", "rm", NETWORK], check=False, capture=True)
    shutil.rmtree(WORK, ignore_errors=True)


def dump_logs() -> None:
    for name, compose, services in (
        ("source", source_compose, ("relay", "emqx")),
        ("target", target_compose, ("relay", BROKER, "nginx")),
    ):
        for service in services:
            print(f"\n--- {name} {service} logs (tail 80) ---")
            print(compose("logs", service, "--tail", "80", check=False, capture=True).stdout)


# ---- helpers the scenario uses ----------------------------------------------------------------


class Store:
    """The target's document store through `/api/dev/db` (`DEV_MODE=1`), for whitebox checks."""

    def __init__(self) -> None:
        from app.db import sqlitestore

        self._wire = sqlitestore
        self._http = httpx.Client(base_url=f"{TARGET_URL}/api/dev/db", timeout=10.0, verify=target_ctx())

    def get(self, path: str) -> dict[str, Any] | None:
        resp = self._http.get("/doc", params={"path": path})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return self._wire.from_wire(body["data"]) if body["exists"] else None

    def list(self, parent: str) -> list[tuple[str, dict[str, Any]]]:
        resp = self._http.get("/list", params={"parent": parent})
        assert resp.status_code == 200, resp.text
        return [(row["id"], self._wire.from_wire(row["data"])) for row in resp.json()["docs"]]


def source_db() -> Any:
    os.environ["FIRESTORE_EMULATOR_HOST"] = f"localhost:{FIRESTORE_PORT}"
    os.environ["FIREBASE_AUTH_EMULATOR_HOST"] = f"localhost:{AUTH_PORT}"
    os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
    from app.db.firestore import get_db

    return get_db()


def bootstrap_source_admin() -> None:
    env = {
        "FIRESTORE_EMULATOR_HOST": f"localhost:{FIRESTORE_PORT}",
        "FIREBASE_AUTH_EMULATOR_HOST": f"localhost:{AUTH_PORT}",
        "GOOGLE_CLOUD_PROJECT": PROJECT_ID,
    }
    run([sys.executable, "-m", "app.bootstrap", "--admin-email", "admin@example.com", "--alias", "admin"],
        env=env, cwd=RELAY_DIR, timeout=120)


def checked(resp: httpx.Response, what: str, *ok: int) -> Any:
    assert resp.status_code in (ok or (200,)), f"{what}: {resp.status_code} {resp.text[:400]}"
    return resp.json() if resp.content else None


def provision(admin: pager_client.ServerClient, owner: str, label: str, wire: str,
              timers: pager_move.Timers) -> pager_client.DeviceClient:
    """A move-capable pager on the source, set up with a real setup code, connected and online."""
    info = admin.admin_device_add(label, owner, default_to_alias="mom")
    device = pager_client.bootstrap_device(
        info["setupCode"], port=SRC_MQTT_PORT, wire=wire, ca_file=None, move_capable=True  # type: ignore[arg-type]
    )
    device.move_timers = timers
    device.connect()
    wait_until(lambda: device.connected, timeout=10, description=f"{label} to connect")
    return device


def has_id(device: pager_client.DeviceClient, msg_id: str) -> int:
    return sum(1 for e in device.inbox if e.data.get("id") == msg_id)


def move_page(admin: pager_client.ServerClient) -> dict[str, Any]:
    return checked(admin.api_get("/api/admin/move"), "GET /api/admin/move")


def device_row(page: dict[str, Any], device_id: str) -> dict[str, Any]:
    return next(d for d in page["devices"] if d["deviceId"] == device_id)


def target_logs(service: str) -> str:
    return target_compose("logs", service, "--no-log-prefix", check=False, capture=True).stdout


# ---- the scenario -----------------------------------------------------------------------------


def scenario_move(wire: str) -> None:
    store = Store()
    sdb = source_db()

    # -- the target is empty and answers move-info under /kid-pager
    info = checked(httpx.get(f"{TARGET_URL}/api/public/move-info", verify=target_ctx()), "move-info")
    assert info["empty"] is True and info["move"] is None, info
    assert info["baseUrl"] == TARGET_URL and info["broker"] == {"host": TARGET_HOST, "port": 8883}, info
    assert info["ca"]["url"].startswith(f"{TARGET_URL}/ca/"), info
    print("move: the empty target answers move-info with the /kid-pager base URL and CA pointer")

    # -- source: people, two move-capable pagers, a bridge phone
    bootstrap_source_admin()
    admin = pager_client.ServerClient(SRC_URL, AUTH_URL)
    admin.login("admin")
    family_id = admin.me()["user"]["familyId"] or "default"
    for alias in ("mom", "kid1", "kid2"):
        admin.admin_user_add(alias, alias.title(), email=f"{alias}@example.com", phone=None)
    admin.admin_set_allow("mom", "kid1", message=True, locate=False, one_way=False)
    admin.admin_set_allow("mom", "kid2", message=True, locate=False, one_way=False)
    mom = pager_client.ServerClient(SRC_URL, AUTH_URL)
    mom.login("mom")
    pager1 = provision(admin, "kid1", "pager-one", wire, PAGER_TIMERS)
    pager2 = provision(admin, "kid2", "pager-two", wire, FAILING_TIMERS)
    d1, d2 = pager1.device_id, pager2.device_id

    created = admin.family_create_bridge(family_id, "mom", "Kitchen phone")
    phone = bridge_sim.BridgeSim(
        SRC_URL, mover=bridge_sim.Mover(verify_retry_ms=2000, backoff_ms=(2000, 4000, 8000)),
        ca_file=str(TARGET_CA),
    )
    phone.pair(created["code"])
    phone.heartbeat()
    print(f"move: source has {d1}, {d2} online and bridge {phone.bridge_id} paired")

    # -- a page the pager receives but never shows, then Freeze
    page = mom.say("kid1", "a page nobody has read yet")
    wait_until(lambda: has_id(pager1, page["id"]) == 1, timeout=15, description="the page to reach pager one")
    pre = checked(admin.api_post("/api/admin/move/preflight", {"targetBaseUrl": TARGET_URL}), "preflight")
    assert pre["baseUrl"] == TARGET_URL and pre["caFingerprint"], pre
    freeze = checked(
        admin.api_post(
            "/api/admin/move/freeze",
            {
                "targetBaseUrl": TARGET_URL,
                "confirm": {"host": pre["broker"]["host"], "port": pre["broker"]["port"], "caSha": pre["caFingerprint"]},
                "accounts": [],
            },
        ),
        "freeze",
    )
    move_id = freeze["moveId"]
    print(f"move: frozen, move id {move_id}")
    refused = mom.api_post("/api/conversations/kid1/messages", {"body": "sent while frozen"})
    assert refused.status_code >= 400, refused.text
    banner = checked(mom.api_get("/api/me/move"), "GET /api/me/move")
    assert banner["targetBaseUrl"] == TARGET_URL, banner
    claim = (banner.get("claim") or {}).get("url", "")
    assert claim.startswith(f"{TARGET_URL}/login#invite="), banner
    print("move: web send refused; the person's banner carries the /kid-pager claim link")

    # -- a pager text on the frozen source: the `moving` receipt, re-queued, nothing stored
    text_id = pager1.publish_msg("typed while frozen", "mom")
    wait_until(
        lambda: any(r.get("rsn") == "moving" and r.get("ref") == text_id for r in pager1.receipts),
        timeout=15, description="the moving receipt",
    )
    assert [h.id for h in pager1.held_up] == [text_id] and pager1.failed_moving == []
    assert not list(sdb.collection("messages").where("wireId", "==", text_id).stream())
    print("move: the frozen source answered the pager's text with a moving receipt; it is re-queued, not stored")

    # -- the bridge follows `moved`, but the target is still empty: it stays and sends no token
    try:
        phone.inject({"source": "sms", "conversation": {"id": "sms-1", "isGroup": False},
                      "sender": {"name": "", "phone": "+15550006666"}, "text": "who dis", "id": "mv-e1"})
        raise AssertionError("the frozen source took a bridge event")
    except bridge_sim.RelayError as exc:
        assert exc.status == 503 and exc.body["moved"] == {"url": TARGET_URL, "id": move_id}, exc.body
    assert phone.relay_url == SRC_URL and len(phone.event_queue) == 1
    log = target_logs("relay")
    assert "/bridge/hello" in log and "/bridge/heartbeat" not in log and "/bridge/events" not in log, log[-800:]
    print("move: the bridge saw moved, called hello on the empty target, got no proof and kept its event queued")

    # -- bundle, dry run, import (the target holds nothing, so the passphrase authorises it)
    bundle = admin._http.get(
        f"{SRC_URL}/api/admin/move/bundle", headers={**admin._headers(), "X-Export-Passphrase": PASSPHRASE}
    )
    assert bundle.status_code == 200, bundle.text[:300]
    tgt_http = httpx.Client(verify=target_ctx(), timeout=300.0)
    headers = {"X-Export-Passphrase": PASSPHRASE}
    dry = checked(tgt_http.post(f"{TARGET_URL}/api/admin/move/import?dryRun=1", content=bundle.content, headers=headers), "dry run")
    assert dry["ok"] is True, dry
    real = tgt_http.post(f"{TARGET_URL}/api/admin/move/import", content=bundle.content, headers=headers)
    checked(real, "import", 201)
    info = checked(httpx.get(f"{TARGET_URL}/api/public/move-info", verify=target_ctx()), "move-info")
    assert info["move"] == {"id": move_id, "imported": True}, info
    print(f"move: bundle {len(bundle.content)} bytes imported; move-info reports {move_id} imported")

    tgt = pager_client.ServerClient(TARGET_URL, "http://unused", ca_file=str(TARGET_CA))
    tgt.login("admin")
    tgt_mom = pager_client.ServerClient(TARGET_URL, "http://unused", ca_file=str(TARGET_CA))
    tgt_mom.login("mom")
    waiting = move_page(tgt)
    assert device_row(waiting, d1)["state"] == "waiting" and device_row(waiting, d2)["state"] == "waiting"

    # -- a page written on the target while the pager is still on the source waits
    target_page = tgt_mom.say("kid1", "written on the target")
    msg = store.get(f"messages/{target_page['id']}")
    assert msg is not None
    assert all(d["state"] == "queued" for d in msg["deliveries"].values() if d["kind"] == "pager"), msg["deliveries"]

    # -- the bridge: hello proves the target, it commits, and its queued event lands there once
    time.sleep(2.5)
    phone.heartbeat()
    assert phone.relay_url == TARGET_URL and phone.move.prev_url == SRC_URL, phone.move_log
    out = phone.flush_events()
    assert [r["id"] for r in out["results"]] == ["mv-e1"] and phone.event_queue == [], out
    assert len(sdb.collection("alerts").get()) == 0, "the source took the event"
    wait_until(lambda: move_page(tgt)["bridges"][0]["arrived"] is True, timeout=15, description="the bridge row to arrive")
    print(f"move: the bridge committed to {phone.relay_url} (other URL kept) and its queued event reached the target")

    # -- cutover for pager one: it moves, commits on the proof, and gets what it was owed
    assert checked(admin.api_post("/api/admin/move/cutover", {"deviceIds": [d1]}), "cutover")["ordered"] == 1
    wait_until(lambda: pager1.move_rec is not None and pager1.move_rec.st == "ok", timeout=90,
               description=f"pager one to commit; events: {pager1.move_events}")
    assert (pager1.host, pager1.port) == (TARGET_HOST, 8883) and pager1.tls == "pinned"
    assert pager1.move_ledger == [(move_id, "ok")], pager1.move_ledger
    wait_until(lambda: has_id(pager1, page["id"]) == 2 and has_id(pager1, target_page["id"]) == 1, timeout=60,
               description="the held pages to arrive on the target under their original ids")
    row = device_row(move_page(tgt), d1)
    assert row["state"] == "moved" and row["remedy"] == "none", row
    dev1 = store.get(f"devices/{d1}")
    assert dev1 is not None and dev1["provisionState"] == "provisioned", dev1
    # the re-queued text reaches the target once, under the id the source never stored
    wait_until(
        lambda: sum(1 for _, m in store.list("messages") if m.get("wireId") == text_id) == 1,
        timeout=30, description="the re-queued text to arrive on the target",
    )
    time.sleep(2)
    assert sum(1 for _, m in store.list("messages") if m.get("wireId") == text_id) == 1
    assert pager1.held_up == []
    for mid in (page["id"], target_page["id"]):
        pager1.publish_ack(mid, "shown")

    def shown(msg_id: str) -> bool:
        m = store.get(f"messages/{msg_id}")
        return m is not None and any(d["kind"] == "pager" and d["state"] == "shown" for d in m["deliveries"].values())

    wait_until(lambda: shown(page["id"]) and shown(target_page["id"]), timeout=30,
               description="the target to record both pages shown")
    print("move: pager one committed; the pages arrived under their original ids, the held text once")

    # -- pager two: the target's broker is down, so it reverts and reports failure on the source page
    target_compose("stop", BROKER)
    assert checked(admin.api_post("/api/admin/move/cutover", {"deviceIds": [d2]}), "cutover")["ordered"] == 1

    def last_report() -> dict[str, Any] | None:
        return device_row(move_page(admin), d2)["last"]

    wait_until(lambda: (last_report() or {}).get("rsn") == "conn", timeout=60,
               description=f"pend/conn on the source page; events: {pager2.move_events}")
    wait_until(lambda: (last_report() or {}).get("st") == "fail", timeout=60,
               description=f"fail on the source page; events: {pager2.move_events}")
    last = last_report()
    assert last is not None and last["rsn"] == "budget" and last["at"] == 2, last
    assert pager2.connected and pager2.host != TARGET_HOST and pager2.hmac_key is not None
    assert pager2.move_rec is not None and pager2.move_rec.st == "fail"
    print(f"move: pager two reverted to the source and reported {last}")

    # -- the target is back; pager two is fixed with a setup code from the target
    target_compose("up", "-d", "--no-deps", "--wait", BROKER)
    code: dict[str, Any] = {}

    def rotated() -> bool:
        resp = tgt.api_post(f"/api/admin/devices/{d2}/rotate-credentials", {})
        if resp.status_code != 200 or resp.json().get("brokerPush") != "pushed":
            return False
        code.update(resp.json())
        return True

    wait_until(rotated, timeout=90, interval=3, description="the target to issue a setup code for pager two")
    pager2.disconnect()
    fresh = pager_client.bootstrap_device(
        code["setupCode"], port=8883, wire=wire, ca_file=str(TARGET_CA), move_capable=True  # type: ignore[arg-type]
    )
    assert fresh.device_id == d2
    fresh.connect()
    wait_until(lambda: fresh.connected, timeout=15, description="pager two to connect to the target")
    after = tgt_mom.say("kid2", "welcome to the new server")
    wait_until(lambda: has_id(fresh, after["id"]) >= 1, timeout=30, description="a page to reach pager two on the target")
    wait_until(lambda: device_row(move_page(tgt), d2)["state"] == "reprovisioned", timeout=30,
               description="the target page to show pager two as set up again")
    dev2 = store.get(f"devices/{d2}")
    assert dev2 is not None and dev2["provisionState"] == "provisioned", dev2
    print("move: pager two reprovisioned from the target and receives pages there")

    for d in (pager1, fresh):
        d.disconnect()
    phone.stop()
    for name, logs in (
        ("source relay", source_compose("logs", "relay", "--no-log-prefix", check=False, capture=True).stdout),
        ("target relay", target_logs("relay")),
    ):
        bad = [ln for ln in logs.splitlines() if "Traceback" in ln or " ERROR " in ln or "ERROR:" in ln]
        assert not bad, f"{name} logged errors: {bad[:5]}"
    print("\nmove: PASSED")


SCENARIOS: dict[str, Callable[[str], None]] = {"move": scenario_move}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="*", default=list(SCENARIOS))
    parser.add_argument("--no-build", action="store_true", help="skip `docker compose --build`")
    parser.add_argument("--keep-stack", action="store_true", help="leave both stacks up for inspection")
    parser.add_argument("--target-broker", choices=["mosquitto", "emqx"], default="mosquitto",
                        help="the home stack's broker (--stack home / home-emqx in e2e_v2.py)")
    parser.add_argument("--wire", choices=["json", "cbor"], default="cbor",
                        help="encoding of the simulated pagers (a move's `mv` report is CBOR-only; default cbor)")
    args = parser.parse_args(argv)
    unknown = [s for s in args.scenarios if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {unknown}; choices: {list(SCENARIOS)}")
    global BROKER
    BROKER = args.target_broker
    try:
        start_stacks(build=not args.no_build)
        for name in args.scenarios:
            print(f"\n=== scenario: {name} ===")
            SCENARIOS[name](args.wire)
        return 0
    except Exception:
        logger.exception("scenario failed")
        dump_logs()
        return 1
    finally:
        if args.keep_stack:
            print("\n=== --keep-stack: both stacks left up (the next run clears the target's data) ===")
        else:
            stop_stacks()


if __name__ == "__main__":
    sys.exit(main())
