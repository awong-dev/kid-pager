"""Runtime configuration from environment variables (see relay/.env.example).
WEBHOOK_KEY defaults to "" so a misconfigured deployment fails closed."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

DbBackend = Literal["firestore", "sqlite"]
AuthBackend = Literal["firebase", "home"]
SchedulerMode = Literal["none", "internal"]
BrokerMode = Literal["rest", "mqtt"]
FwCache = Literal["gcs", "local"]


_BASE_PATH_RE = re.compile(r"(/[A-Za-z0-9_-]+)*")
BASE_PATH_MAX = 200


def valid_base_path(value: str) -> bool:
    """`""` (served at the root) or `/a/b`: letters, digits, `-` and `_` in each segment, no
    trailing slash, no dots, query, fragment, `%` or whitespace. The one rule for `PAGER_PATH` and
    for a federation peer's path, which is appended to a URL whose host is an address the relay
    already checked."""
    return len(value) <= BASE_PATH_MAX and _BASE_PATH_RE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class Settings:
    # `broker_api_url` is the broker's REST publish API (e.g. EMQX's `/api/v5`), with
    # `broker_api_key`/`broker_api_secret` as HTTP basic auth.
    broker_api_url: str
    broker_api_key: str | None
    broker_api_secret: str | None
    # Shared secret the broker's rule-engine HTTP action sends on every webhook call
    # (`X-Relay-Webhook-Key`), so nobody else can post "device traffic".
    webhook_key: str
    # Gates `POST /api/dev/token`, the dev-only custom-token mint. Never true in a real
    # deployment.
    dev_mode: bool
    # The Google libraries read these environment variables themselves; surfaced here only
    # so `/healthz` and logs can report them.
    google_cloud_project: str | None
    firestore_emulator_host: str | None
    firebase_auth_emulator_host: str | None
    # Where a pager reaches the broker over TLS (`BROKER_HOST`, `BROKER_PORT`): written into setup
    # codes and move orders. `app/ca_resolve.py` connects there to resolve the CA when
    # BROKER_CA_PEM is unset.
    broker_host: str = "localhost"
    broker_port: int = 8883
    # The broker CA PEM; wins over auto-resolve. Empty: `app/ca_resolve.py` looks it up;
    # if that fails, bundles carry no `ca`.
    broker_ca_pem: str | None = None
    # The relay's public base URL, for `GET /ca/{sha256hex}.pem` pointers. Empty with a CA
    # configured makes `app/devsetup.py` and the CA-push route refuse.
    public_base_url: str | None = None
    # "fcm" injects `app.backends.fcm.FirebaseFCMClient` into the `webapp` backend; "null"
    # (default) keeps the no-op `NullFCMClient`. Any other value is treated as "null".
    push_backend: str = "null"
    # Public OTA bucket: `fw_index_url` is `fw/index.json`, `fw_bucket_base` its base URL with
    # a trailing slash. Either unset = every OTA route answers 503.
    fw_index_url: str | None = None
    fw_bucket_base: str | None = None
    # Where on-demand deltas are written. "gcs" (default) is `fw-cache/` in the bucket above;
    # "local" is `fw_cache_dir`, served by the relay at `PUBLIC_BASE_URL/fw-cache/...`.
    fw_cache: FwCache = "gcs"
    fw_cache_dir: str = "data/fw-cache"
    # A Soracom SAM user's auth key for enrolling SIMs into the Beam group from the admin UI.
    # Unset = the enroll routes answer 503 and the list route reports `configured: false`.
    soracom_auth_key_id: str | None = None
    soracom_auth_key: str | None = None
    soracom_beam_group: str = "pager-beam"
    soracom_beam_destination: str = "mqtts://broker.example.com:8883"
    # The document store: "firestore" (default) or "sqlite", the same client API over one
    # SQLite file at `sqlite_path`.
    db_backend: DbBackend = "firestore"
    sqlite_path: str = "data/relay/pager.sqlite"
    # The long-poll `wait` (s, 0..25) the relay asks the bridge phone to use on
    # `GET /bridge/outbox`, sent as `outboxWaitS`. 0 sends no hint.
    bridge_outbox_wait_s: int = 0
    # "none" (default: Cloud Scheduler calls `/internal/tick` and `/internal/sweep`) or
    # "internal" (the relay runs both itself, `app/scheduler.py`).
    scheduler_mode: SchedulerMode = "none"
    # VAPID pair, base64url (private: raw 32-byte P-256 scalar; public: 65-byte point). Unset =
    # no Web Push. `webpush_subject` is the VAPID `sub` (`mailto:`/`https:`).
    webpush_vapid_private_key: str | None = None
    webpush_vapid_public_key: str | None = None
    webpush_subject: str | None = None
    # The public URL of the family web app, for the links a reauthorization invite is shown
    # with. Optional; `WEB_BASE_URL`.
    web_base_url: str | None = None
    # "rest" keeps the relay off MQTT (REST publish + `/webhooks/mqtt`); "mqtt" makes it one
    # persistent client (`app/broker_mqtt.py`) logged in as `broker_mqtt_username`.
    broker_mode: BrokerMode = "rest"
    broker_mqtt_host: str = "localhost"
    broker_mqtt_port: int = 1883
    broker_mqtt_username: str = "relay"
    broker_mqtt_password: str | None = None
    # PEM of this relay's own TLS root for link invites (`FED_CA_PEM` / `FED_CA_PEM_FILE`); with
    # `TLS_MODE=selfsigned` it defaults to `BROKER_CA_PEM`. Unset: invites carry no `ca`.
    fed_ca_pem: str | None = None
    # The TCP port of the extra TLS listener a Soracom Beam pager reaches (`BEAM_PORT`; the home stack
    # sets it only when its web certificate is a public one). `None`: no Beam listener.
    beam_port: int | None = None

    @property
    def webpush_configured(self) -> bool:
        return bool(self.webpush_vapid_private_key and self.webpush_vapid_public_key)

    @property
    def soracom_configured(self) -> bool:
        return bool(self.soracom_auth_key_id and self.soracom_auth_key)

    @property
    def fw_bucket_name(self) -> str | None:
        """The bucket name inside `fw_bucket_base`."""
        if not self.fw_bucket_base:
            return None
        from app.firmware import bucket_name

        return bucket_name(self.fw_bucket_base)

    @property
    def base_path(self) -> str:
        """The path component of `public_base_url` without a trailing slash (`/pager`), `""` when
        the app is served at the root or the URL is unset. nginx strips it before proxying, so
        routes do not use it; it is for URLs handed out and for the static web export."""
        return urlsplit(self.public_base_url or "").path.rstrip("/")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            broker_api_url=os.environ.get(
                "BROKER_API_URL", "http://localhost:18083/api/v5"
            ),
            # .strip(): Secret Manager returns exactly the stored bytes, possibly with a
            # trailing newline, which an HTTP header cannot carry.
            broker_api_key=(os.environ.get("BROKER_API_KEY") or "").strip() or None,
            broker_api_secret=(os.environ.get("BROKER_API_SECRET") or "").strip() or None,
            webhook_key=os.environ.get("WEBHOOK_KEY", "").strip(),
            dev_mode=os.environ.get("DEV_MODE", "") == "1",
            google_cloud_project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None,
            firestore_emulator_host=os.environ.get("FIRESTORE_EMULATOR_HOST") or None,
            firebase_auth_emulator_host=os.environ.get("FIREBASE_AUTH_EMULATOR_HOST") or None,
            broker_host=os.environ.get("BROKER_HOST", "localhost"),
            broker_port=_port("BROKER_PORT", os.environ.get("BROKER_PORT", "")) or 8883,
            broker_ca_pem=os.environ.get("BROKER_CA_PEM") or _read_pem_file(os.environ.get("BROKER_CA_PEM_FILE")),
            public_base_url=(os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/") or None,
            push_backend=os.environ.get("PUSH_BACKEND", "null").strip().lower(),
            fw_index_url=(os.environ.get("FW_INDEX_URL") or "").strip() or None,
            fw_bucket_base=(os.environ.get("FW_BUCKET_BASE") or "").strip() or None,
            fw_cache=_fw_cache(os.environ.get("FW_CACHE", "")),
            fw_cache_dir=(os.environ.get("FW_CACHE_DIR") or "").strip() or "data/fw-cache",
            soracom_auth_key_id=(os.environ.get("SORACOM_AUTH_KEY_ID") or "").strip() or None,
            soracom_auth_key=(os.environ.get("SORACOM_AUTH_KEY") or "").strip() or None,
            soracom_beam_group=(os.environ.get("SORACOM_BEAM_GROUP") or "").strip() or "pager-beam",
            soracom_beam_destination=(os.environ.get("SORACOM_BEAM_DESTINATION") or "").strip()
            or "mqtts://broker.example.com:8883",
            db_backend=_db_backend(os.environ.get("DB_BACKEND", "")),
            sqlite_path=(os.environ.get("SQLITE_PATH") or "").strip() or "data/relay/pager.sqlite",
            bridge_outbox_wait_s=_bridge_outbox_wait_s(os.environ.get("BRIDGE_OUTBOX_WAIT_S", "")),
            scheduler_mode=_scheduler_mode(os.environ.get("SCHEDULER_MODE", "")),
            webpush_vapid_private_key=(os.environ.get("WEBPUSH_VAPID_PRIVATE_KEY") or "").strip()
            or None,
            webpush_vapid_public_key=(os.environ.get("WEBPUSH_VAPID_PUBLIC_KEY") or "").strip()
            or None,
            webpush_subject=(os.environ.get("WEBPUSH_SUBJECT") or "").strip()
            or _https_or_none(os.environ.get("PUBLIC_BASE_URL")),
            web_base_url=_https_or_none(os.environ.get("WEB_BASE_URL")),
            broker_mode=_broker_mode(os.environ.get("BROKER_MODE", "")),
            broker_mqtt_host=(os.environ.get("BROKER_MQTT_HOST") or "").strip() or "localhost",
            broker_mqtt_port=int((os.environ.get("BROKER_MQTT_PORT") or "").strip() or "1883"),
            broker_mqtt_username=(os.environ.get("BROKER_MQTT_USERNAME") or "").strip() or "relay",
            broker_mqtt_password=(os.environ.get("BROKER_MQTT_PASSWORD") or "").strip() or None,
            fed_ca_pem=os.environ.get("FED_CA_PEM")
            or _read_pem_file(os.environ.get("FED_CA_PEM_FILE"))
            or (
                os.environ.get("BROKER_CA_PEM") or _read_pem_file(os.environ.get("BROKER_CA_PEM_FILE"))
                if tls_mode() == "selfsigned"
                else None
            ),
            beam_port=_port("BEAM_PORT", os.environ.get("BEAM_PORT", "")),
        )


def auth_backend() -> AuthBackend:
    """`AUTH_BACKEND=firebase|home`, read from the environment on every call."""
    raw = os.environ.get("AUTH_BACKEND", "")
    value = raw.strip().lower() or "firebase"
    if value not in ("firebase", "home"):
        raise ValueError(f"AUTH_BACKEND must be 'firebase' or 'home', not {raw!r}")
    return value  # type: ignore[return-value]


def tls_mode() -> str:
    """`TLS_MODE` (`public`, `selfsigned`, `letsencrypt` or `external`), exposed on `GET /api/me`."""
    return (os.environ.get("TLS_MODE") or "").strip().lower() or "public"


def _bridge_outbox_wait_s(raw: str) -> int:
    raw = raw.strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"BRIDGE_OUTBOX_WAIT_S must be an integer, not {raw!r}") from None
    return max(0, min(25, value))


def _db_backend(raw: str) -> DbBackend:
    value = raw.strip().lower() or "firestore"
    if value not in ("firestore", "sqlite"):
        raise ValueError(f"DB_BACKEND must be 'firestore' or 'sqlite', not {raw!r}")
    return value  # type: ignore[return-value]


def _scheduler_mode(raw: str) -> SchedulerMode:
    value = raw.strip().lower() or "none"
    if value not in ("none", "internal"):
        raise ValueError(f"SCHEDULER_MODE must be 'none' or 'internal', not {raw!r}")
    return value  # type: ignore[return-value]


def _fw_cache(raw: str) -> FwCache:
    value = raw.strip().lower() or "gcs"
    if value not in ("gcs", "local"):
        raise ValueError(f"FW_CACHE must be 'gcs' or 'local', not {raw!r}")
    return value  # type: ignore[return-value]


def _port(name: str, raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a port number, not {raw!r}") from None
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be a port number, not {raw!r}")
    return value


def _broker_mode(raw: str) -> BrokerMode:
    value = raw.strip().lower() or "rest"
    if value not in ("rest", "mqtt"):
        raise ValueError(f"BROKER_MODE must be 'rest' or 'mqtt', not {raw!r}")
    return value  # type: ignore[return-value]


def _https_or_none(raw: str | None) -> str | None:
    value = (raw or "").strip().rstrip("/")
    return value if value.startswith("https://") else None


def _read_pem_file(path: str | None) -> str | None:
    """`BROKER_CA_PEM_FILE` names a CA file instead of carrying the PEM; `BROKER_CA_PEM` wins."""
    if not (path or "").strip():
        return None
    with open(path.strip(), encoding="ascii") as f:  # type: ignore[union-attr]
        return f.read() or None
