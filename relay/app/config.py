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

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            broker_api_url=os.environ.get(
                "BROKER_API_URL", "http://localhost:18083/api/v5"
            ),
            dev_mode=os.environ.get("DEV_MODE", "") == "1",
            google_cloud_project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None,
            firestore_emulator_host=os.environ.get("FIRESTORE_EMULATOR_HOST") or None,
            firebase_auth_emulator_host=os.environ.get("FIREBASE_AUTH_EMULATOR_HOST") or None,
        )
