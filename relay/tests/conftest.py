from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator

import httpx
import pytest

# Must be set before the first get_db()/get_app() call: both cache a process-wide singleton.
os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:8080")
os.environ.setdefault("FIREBASE_AUTH_EMULATOR_HOST", "localhost:9099")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "demo-pager")

from app.ingest import Ingest
from tests.fake_transport import FakeBrokerClient

FIRESTORE_HOST = os.environ["FIRESTORE_EMULATOR_HOST"]
AUTH_HOST = os.environ["FIREBASE_AUTH_EMULATOR_HOST"]
PROJECT_ID = os.environ["GOOGLE_CLOUD_PROJECT"]

FIRESTORE_WIPE_URL = (
    f"http://{FIRESTORE_HOST}/emulator/v1/projects/{PROJECT_ID}/databases/(default)/documents"
)
AUTH_WIPE_URL = f"http://{AUTH_HOST}/emulator/v1/projects/{PROJECT_ID}/accounts"


def _emulators_reachable() -> bool:
    try:
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


@pytest.fixture(autouse=True)
def _clean_emulators() -> Iterator[None]:
    """Wipes both emulators (or the sqlite `docs` table) before every test."""
    yield


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
