"""`/internal/tick` and `/internal/sweep`, called by the scheduler.

`DEV_MODE=1` bypasses auth; otherwise a Google OIDC token (`aud` == `OIDC_AUDIENCE`, `email` in
`OIDC_ALLOWED_EMAILS`) is required, or `X-Relay-Webhook-Key` under `SCHEDULER_MODE=internal`.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, HTTPException, Request

from app import jobs
from app.auth import verify_internal_oidc_token
from app.routing import Routing

logger = logging.getLogger("relay.routers.internal")

router = APIRouter()


def _oidc_audience() -> str:
    return os.environ.get("OIDC_AUDIENCE", "")


def _oidc_allowed_emails() -> frozenset[str]:
    raw = os.environ.get("OIDC_ALLOWED_EMAILS", "")
    return frozenset(email.strip() for email in raw.split(",") if email.strip())


def _require_internal_caller(request: Request) -> None:
    settings = request.app.state.settings
    if settings.dev_mode:
        return
    if settings.scheduler_mode == "internal" and settings.webhook_key:
        presented = request.headers.get("x-relay-webhook-key", "")
        if presented and hmac.compare_digest(presented.encode(), settings.webhook_key.encode()):
            return
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    # `partition`: a bare `"Bearer "` header must be a 401, not an IndexError 500
    token = auth_header.partition(" ")[2].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        verify_internal_oidc_token(
            token, audience=_oidc_audience(), allowed_emails=_oidc_allowed_emails()
        )
    except Exception as exc:  # noqa: BLE001 -- verify_internal_oidc_token's own contract: any failure -> 401
        logger.warning("internal OIDC verification rejected: %r", exc)
        raise HTTPException(status_code=401, detail="invalid OIDC token") from None


@router.post("/internal/tick")
def tick(request: Request) -> jobs.TickResult:
    _require_internal_caller(request)
    routing: Routing = request.app.state.routing
    return jobs.tick(routing)


@router.post("/internal/sweep")
def sweep(request: Request) -> jobs.SweepResult:
    _require_internal_caller(request)
    return jobs.sweep()
