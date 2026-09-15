#!/usr/bin/env python3
"""
End-to-end integration suite for the pager relay. Brings up the real `relay/docker-compose.yml`
stack (EMQX + Firebase emulators + relay, `DEV_MODE=1`), imports `tools/pager_client.py` as a
library (one process plays the pager device and every human in the scenario), and runs named
scenarios: bootstrap, text round-trip, allow-list and republish; location (periodic fixes, read
permission, on-demand `/locate` incl. coalescing, cached answer, `no_fix`, derived expiry);

Usage (needs the relay virtualenv -- imports `httpx`/`firebase-admin`):
    relay/.venv/bin/python tools/e2e_v2.py                 # all scenarios
    relay/.venv/bin/python tools/e2e_v2.py bootstrap allowlist
    relay/.venv/bin/python tools/e2e_v2.py location_periodic location_on_demand

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

logger = logging.getLogger("e2e_v2")

REPO_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = REPO_ROOT / "relay"
TOOLS_DIR = REPO_ROOT / "tools"
COMPOSE_FILE = RELAY_DIR / "docker-compose.yml"
ENV_PATH = RELAY_DIR / ".env"
EMQX_SETUP_SCRIPT = TOOLS_DIR / "emqx_setup.py"
RELAY_VENV_PYTHON = RELAY_DIR / ".venv" / "bin" / "python3"

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

sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(RELAY_DIR))
import httpx
import pager_client

_env_backup: str | None = None


def compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(
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


def write_env_file() -> None:
    global _env_backup


def restore_env_file() -> None:
    if _env_backup is not None:
    else:


def configure_emqx() -> None:
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


def _relay_reachable() -> bool:
    try:
        return resp.status_code == 200
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
    write_env_file()
    compose(*(["up", "-d", "--build"] if build else ["up", "-d"]))
    wait_until(_relay_reachable, timeout=120, interval=1, description="relay to become healthy")


def stop_stack() -> None:
    print("\n=== tearing down the compose stack ===")
    compose("down", "-v", check=False)
    restore_env_file()


def dump_logs() -> None:
        print(f"\n--- docker compose logs {service} --tail {tail} ---")
        result = compose("logs", service, "--tail", tail, check=False, capture=True)
        print(result.stdout)


def bootstrap_admin(alias: str = "admin", email: str = "admin@example.com") -> None:
    """Runs `python -m app.bootstrap` from the host against the published emulator ports; a
    home-auth relay uses the first-run API instead."""
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
    os.environ["FIRESTORE_EMULATOR_HOST"] = "localhost:8080"
    os.environ["FIREBASE_AUTH_EMULATOR_HOST"] = "localhost:9099"
    os.environ["GOOGLE_CLOUD_PROJECT"] = FIREBASE_PROJECT_ID
class Oracle:

    def __init__(self) -> None:

    def uid_for_alias(self, alias: str) -> str:

    def loc_req_exists(self, device_id: str) -> bool:

    def message(self, msg_id: str):

    def thread(self, alias_a: str, alias_b: str):
        key = self.messages_store.conv_key(self.uid_for_alias(alias_a), self.uid_for_alias(alias_b))


def _backdate_location(device_id: str, *, days_ago: float) -> str:
    from datetime import UTC, datetime, timedelta


    ts = int(time.time())
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



def scenario_text_roundtrip() -> None:
    """parent -> student via the API -> the pager device receives it; `shown`/`read` acks update
    delivery state. student `msg` -> parent's thread; student `msg @dad` to a second parent."""
    oracle = Oracle()

    parent = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    parent.login("parent")

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



def scenario_republish() -> None:
    """A device that has never connected still gets its queued message once it comes online
    (online-edge republish, PROTOCOL.md §5.3); a brand-new device avoids racing EMQX's own QoS 1
    queuing. Also: with the broker REST API unreachable a send stays 'queued' and `/internal/tick`
    publishes it once EMQX is back."""
    oracle = Oracle()
    admin = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    admin.login("admin")

    parent = pager_client.ServerClient(RELAY_URL, AUTH_URL)
    parent.login("parent")

    sent = parent.say("student", "device is brand new, message queues")
    msg_id = sent["id"]

    def _is_queued_or_sent() -> bool:
        msg = oracle.message(msg_id)
        return msg is not None and any(d.kind == "pager" for d in msg.deliveries.values())

    wait_until(_is_queued_or_sent, timeout=10, description="pager delivery to be created")

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



    wait_until(
        lambda: _delivery_state(msg_id_2) == "sent",
        timeout=15,
        description="delivery to reach 'sent' after tick retried it",
    )
    print("republish: /internal/tick delivered the previously-queued message")


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
    admin.admin_set_allow("parent", "locperiodic", message=True, locate=True, one_way=True)

    device.connect()
    wait_until(lambda: device.connected, timeout=10, description="loc-periodic device to connect")

    device.loc_auto(5)
    wait_until(
        timeout=25,
        description="at least 2 periodic fixes to land in devices/{d}/locations",
    )
    device.loc_stop_auto()
    print(f"location_periodic: {len(fixes)} periodic fixes landed in devices/{{d}}/locations")

    )

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
    admin.admin_set_allow("parent", "locdemo1", message=True, locate=True, one_way=True)
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
    admin.admin_set_allow("parent", "locdemo2", message=True, locate=True, one_way=True)
    admin.admin_set_allow("locrequester2", "locdemo2", message=True, locate=True, one_way=True)
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
    admin.admin_set_allow("parent", "locdemo3", message=True, locate=True, one_way=True)
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
    admin.admin_set_allow("parent", "locdemo4", message=True, locate=True, one_way=True)

    # Never connected: §5.3 excludes loc_req from the online-edge republish, so this request
    # can never be answered and is guaranteed to go stale.
    expiring = parent.locate("locdemo4")
    coalesced = parent.locate("locdemo4")
    assert coalesced["requestId"] == expiring["requestId"], "still within the TTL -> must coalesce"

    print(f"location_on_demand: waiting {LOC_REQ_TTL_S + 3}s for the loc_req to go stale...")
    time.sleep(LOC_REQ_TTL_S + 3)
    assert tick_json.get("locReqsCleared", 0) >= 1, tick_json
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
    admin.admin_set_retention(messages="4w", locations="1d")

    fresh_msg_id = parent.say("student", "retention: still here after a locations-only sweep")["id"]


    assert stale_loc_id not in remaining_ids, remaining_ids
    assert fresh_loc_id in remaining_ids, remaining_ids
    assert oracle.message(fresh_msg_id) is not None, "messages must be untouched by a locations-only sweep"
    print("retention: stale location fix swept; fresh fix + messages untouched")

    # --- messages: retention.messages=2w ---
    admin.admin_set_retention(messages="2w", locations="1d")
    stale_msg_id = _backdate_message(
        oracle.uid_for_alias("parent"), oracle.uid_for_alias("student"), days_ago=20
    )
    fresh_msg_id_2 = parent.say("student", "retention: fresh message survives a 2-week sweep")["id"]


    assert oracle.message(stale_msg_id) is None, "message older than the 2-week retention must be swept"
    assert oracle.message(fresh_msg_id_2) is not None
    print("retention: stale message swept; fresh message untouched")

    # --- idempotency: >1 batch's worth of stale docs, sweep() called twice ---
    stale_ids = [
        _backdate_message(oracle.uid_for_alias("parent"), oracle.uid_for_alias("student"), days_ago=20 + i)
        for i in range(3)
    ]
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
    # Two-way, so the device's up-message lands in the thread rather than tripping the
    # unknown-recipient path; this scenario wants representative traffic sizes.
    admin.admin_set_allow("parent", "bytesuser", message=True, locate=True, one_way=False)

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


SCENARIOS: dict[str, Callable[[], None]] = {
    "bootstrap": scenario_bootstrap,
    "text_roundtrip": scenario_text_roundtrip,
    "allowlist": scenario_allowlist,
    "republish": scenario_republish,
    "location_periodic": scenario_location_periodic,
    "location_on_demand": scenario_location_on_demand,
    "retention": scenario_retention,
    "bytes": scenario_bytes,
}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenarios", nargs="*", default=list(SCENARIOS), help="scenario names to run, in order"
    )
    parser.add_argument("--no-build", action="store_true", help="skip `docker compose build`")
    args = parser.parse_args(argv)

    unknown = [s for s in args.scenarios if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {unknown}; choices: {list(SCENARIOS)}")

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


if __name__ == "__main__":
    sys.exit(main())
