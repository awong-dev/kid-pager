"""`POST /api/dev/token` (`DEV_MODE=1` only, else 404): mints a custom or session token."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from firebase_admin import auth as fb_auth
from pydantic import BaseModel, model_validator

from app.store import users as users_store

router = APIRouter()


class DevTokenRequest(BaseModel):
    uid: str | None = None
    # Resolved server-side via the `aliases/{alias}` lookup.
    alias: str | None = None

    @model_validator(mode="after")
    def _require_one(self) -> DevTokenRequest:
        if not self.uid and not self.alias:
            raise ValueError("either uid or alias is required")
        return self


class DevTokenResponse(BaseModel):
    token: str
    uid: str


def mint_dev_token(req: DevTokenRequest, request: Request) -> DevTokenResponse:
    settings = request.app.state.settings
    if not settings.dev_mode:
        raise HTTPException(status_code=404, detail="not found")
    uid = req.uid
    if uid is None:
        assert req.alias is not None
        uid = users_store.get_uid_for_alias(req.alias)
        if uid is None:
            raise HTTPException(status_code=404, detail=f"unknown alias: {req.alias!r}")
    token = fb_auth.create_custom_token(uid)
    return DevTokenResponse(token=token.decode("utf-8"), uid=uid)
