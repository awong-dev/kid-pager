"""Runtime configuration from environment variables (see relay/.env.example).
WEBHOOK_KEY defaults to "" so a misconfigured deployment fails closed."""

from __future__ import annotations

import os
from dataclasses import dataclass


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
    # Production MQTT/TLS host for bootstrap bundles; `app/ca_resolve.py` connects to :8883
    # to resolve the CA when BROKER_CA_PEM is unset.
    broker_host: str = "localhost"
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
            public_base_url=(os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/") or None,
            push_backend=os.environ.get("PUSH_BACKEND", "null").strip().lower(),
            fw_index_url=(os.environ.get("FW_INDEX_URL") or "").strip() or None,
            fw_bucket_base=(os.environ.get("FW_BUCKET_BASE") or "").strip() or None,
        )
