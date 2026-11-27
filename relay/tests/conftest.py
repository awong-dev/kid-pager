from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Iterator

import httpx
import pytest

# Must be set before the first get_db()/get_app() call: both cache a process-wide singleton.
os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:8080")
os.environ.setdefault("FIREBASE_AUTH_EMULATOR_HOST", "localhost:9099")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "demo-pager")

# DB_BACKEND=sqlite runs the suite against the SQLite store; AUTH_BACKEND=home signs in with relay
# sessions. With both set no emulator is needed.
DB_BACKEND = os.environ.get("DB_BACKEND", "firestore").strip().lower() or "firestore"
HOME_AUTH = (os.environ.get("AUTH_BACKEND", "").strip().lower() or "firebase") == "home"
FIREBASE_AUTH_SKIP_REASON = (
    "mints or reads Firebase Auth accounts or tokens; there is no Firebase Auth under "
    "AUTH_BACKEND=home"
)
if DB_BACKEND == "sqlite":
    os.environ["SQLITE_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="pager-sqlite-"), "pager.sqlite"
    )

from app.db.firestore import get_app, get_db
from app.ingest import Ingest
from tests.fake_transport import FakeBrokerClient
from tests.fed_world import make_relay, net  # noqa: F401 -- fixtures for the federation tests

FIRESTORE_HOST = os.environ["FIRESTORE_EMULATOR_HOST"]
AUTH_HOST = os.environ["FIREBASE_AUTH_EMULATOR_HOST"]
PROJECT_ID = os.environ["GOOGLE_CLOUD_PROJECT"]

FIRESTORE_WIPE_URL = (
    f"http://{FIRESTORE_HOST}/emulator/v1/projects/{PROJECT_ID}/databases/(default)/documents"
)
AUTH_WIPE_URL = f"http://{AUTH_HOST}/emulator/v1/projects/{PROJECT_ID}/accounts"


def _emulators_reachable() -> bool:
    try:
        if DB_BACKEND != "sqlite":
            httpx.get(f"http://{FIRESTORE_HOST}/", timeout=2.0)
        if not HOME_AUTH:
            httpx.get(f"http://{AUTH_HOST}/", timeout=2.0)
        return True
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="session", autouse=True)
def _require_emulators() -> None:
    """Fails the session up front if the emulators are down."""
    if not _emulators_reachable():
        pytest.exit(
            "Firestore/Auth emulators are not reachable at "
            f"{FIRESTORE_HOST} / {AUTH_HOST}. Start them first, e.g.:\n"
            "  docker compose -f relay/docker-compose.yml up -d firebase\n"
            "or:\n"
            "  firebase emulators:start --only firestore,auth --project demo-pager\n"
            "(see relay/README.md).",
            returncode=1,
        )


@pytest.fixture(scope="session", autouse=True)
def _init_firebase_app() -> None:
    """Creates the process-wide `firebase_admin.App` (and the sqlite schema) once."""
    if not HOME_AUTH:
        get_app()
    if DB_BACKEND == "sqlite":
        get_db()


# uid -> custom claims a test requested, under AUTH_BACKEND=home; compared with the user document.
HOME_CLAIMS: dict[str, dict] = {}


@pytest.fixture(autouse=True)
def _skip_firebase_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under `AUTH_BACKEND=home`: relay-session sign-in; other `firebase_admin.auth` calls skip."""
    if not HOME_AUTH:
        return
    from types import SimpleNamespace

    from firebase_admin import auth as fb_auth

    from app import authn_home

    HOME_CLAIMS.clear()

    def _skip(*_args, **_kwargs):
        pytest.skip(FIREBASE_AUTH_SKIP_REASON)

    def _create_user(**kwargs):
        return SimpleNamespace(uid=kwargs.get("uid") or authn_home.new_uid())

    def _set_claims(uid, claims):
        HOME_CLAIMS[uid] = dict(claims or {})

    for name in (
        "get_user",
        "get_user_by_email",
        "get_user_by_phone_number",
        "update_user",
        "delete_user",
        "create_custom_token",
        "verify_id_token",
    ):
        monkeypatch.setattr(fb_auth, name, _skip)
    monkeypatch.setattr(fb_auth, "create_user", _create_user)
    monkeypatch.setattr(fb_auth, "set_custom_user_claims", _set_claims)


@pytest.fixture(autouse=True)
def _clean_emulators() -> Iterator[None]:
    """Wipes both emulators (or the sqlite `docs` table) before every test."""
    if DB_BACKEND == "sqlite":
        conn = sqlite3.connect(os.environ["SQLITE_PATH"])
        with conn:
            conn.execute("DELETE FROM docs")
        conn.close()
    else:
        httpx.delete(FIRESTORE_WIPE_URL, timeout=10.0)
    if not HOME_AUTH:
        httpx.delete(AUTH_WIPE_URL, timeout=10.0)
    yield


@pytest.fixture(autouse=True)
def _reset_fed_state() -> Iterator[None]:
    """Drops the federation switch cache (30 s), the move record cache and any injected federation client."""
    from app import move
    from app.fed import client as fed_client
    from app.store import fed_state

    fed_state.invalidate_cache()
    move.invalidate_cache()
    fed_client.set_default_client(None)
    yield
    fed_state.invalidate_cache()
    move.invalidate_cache()
    fed_client.set_default_client(None)


@pytest.fixture(autouse=True)
def _reset_hooks_cache() -> Iterator[None]:
    """Drops the 30 s enabled-hook cache."""
    from app.store import hooks as hooks_store

    hooks_store.invalidate_cache()
    yield
    hooks_store.invalidate_cache()


@pytest.fixture(autouse=True)
def _inline_background() -> Iterator[None]:
    """Runs `app.hooks.spawn` inline and resets the federation sender's settings."""
    from app import hooks as hooks_module
    from app.fed import sender as fed_sender

    hooks_module.set_background(spawn=lambda fn: fn())
    fed_sender.configure(None)
    yield
    hooks_module.set_background()
    fed_sender.configure(None)


@pytest.fixture(autouse=True)
def _default_person_family(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gives each bare user its own one-person family, since `create_user` refuses none."""
    import functools

    from app.store import users as users_store

    original = users_store.create_user

    @functools.wraps(original)
    def _create_user(**kwargs):
        if kwargs.get("kind", "person") == "person" and kwargs.get("family_id") is None:
            kwargs["family_id"] = f"solo-{kwargs['uid']}"
        return original(**kwargs)

    monkeypatch.setattr(users_store, "create_user", _create_user)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "real_device_ids: do not honour a `deviceId` in create-device request bodies"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if DB_BACKEND != "sqlite":
        return
    skip = pytest.mark.skip(
        reason="firestore.rules has no meaning under DB_BACKEND=sqlite; the browser read "
        "rules are ported to Python and tested there"
    )
    for item in items:
        if item.path.name == "test_rules.py":
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _honour_test_device_ids(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Stages a `deviceId` from a create body so the next `new_device_id()` returns it."""
    if request.node.get_closest_marker("real_device_ids"):
        return
    from fastapi.testclient import TestClient

    from app.routers import admin as admin_router

    staged: list[str] = []
    original_request = TestClient.request
    original_new = admin_router.new_device_id

    def _request(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        body = kwargs.get("json")
        if (
            method.upper() == "POST"
            and str(url).split("?")[0] in ("/api/admin/devices", "/api/family/devices")
            and isinstance(body, dict)
            and "deviceId" in body
        ):
            staged.append(body["deviceId"])
        return original_request(self, method, url, *args, **kwargs)

    def _new() -> str:
        return staged.pop(0) if staged else original_new()

    monkeypatch.setattr(TestClient, "request", _request)
    monkeypatch.setattr(admin_router, "new_device_id", _new)


@pytest.fixture
def broker() -> FakeBrokerClient:
    return FakeBrokerClient()


@pytest.fixture
def ingest(broker: FakeBrokerClient) -> Ingest:
    return Ingest(broker)


def up_topic(device_id: str) -> str:
    return f"pager/{device_id}/up"


def status_topic(device_id: str) -> str:
    return f"pager/{device_id}/status"


def down_topic(device_id: str) -> str:
    return f"pager/{device_id}/down"


def loc_topic(device_id: str) -> str:
    return f"pager/{device_id}/loc"


def encode(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def ack_payload(msg_id: str, ack: str, ts: int | None = None) -> bytes:
    ts = ts if ts is not None else int(time.time())
    return encode({"v": 1, "id": msg_id, "ts": ts, "ack": ack})


def up_message_payload(
    msg_id: str, body: str, *, sender: str = "student", ts: int | None = None
) -> bytes:
    ts = ts if ts is not None else int(time.time())
    return encode({"v": 1, "id": msg_id, "ts": ts, "from": sender, "body": body, "ack": None})


def online_status_payload(session: str, **overrides) -> bytes:
    payload = {
        "v": 1,
        "state": "online",
        "mode": "sleep",
        "batt_mv": 3280,
        "rssi": -95,
        "session": session,
        "ts": int(time.time()),
        "fw": "0.1.0",
    }
    payload.update(overrides)
    return encode(payload)


def offline_status_payload(session: str) -> bytes:
    return encode({"v": 1, "state": "offline", "session": session})


def loc_payload(
    loc_id: str, *, lat: float = 37.7749, lon: float = -122.4194, req: str | None = None
) -> bytes:
    return encode(
        {
            "v": 1,
            "id": loc_id,
            "ts": int(time.time()),
            "loc": {"lat": lat, "lon": lon, "fix_ts": int(time.time())},
            "req": req,
        }
    )
