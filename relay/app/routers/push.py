"""Web Push routes: VAPID public key and subscription upsert/delete (public hosts only)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import push_webpush
from app.auth import AuthedUser, require_user
from app.store import push_subscriptions as subs_store

router = APIRouter()


@router.get("/api/push/vapid-public-key")
def vapid_public_key(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    if not settings.webpush_configured:
        raise HTTPException(status_code=503, detail="web push is not configured")
    return {"publicKey": settings.webpush_vapid_public_key}


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=256)
    auth: str = Field(min_length=1, max_length=64)


class SubscriptionRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048, pattern=r"^https://")
    keys: SubscriptionKeys
    ua: str = Field(default="", max_length=512)


@router.put("/api/me/push-subscriptions")
def put_subscription(
    req: SubscriptionRequest, authed: Annotated[AuthedUser, Depends(require_user)]
) -> dict[str, object]:
    try:
        push_webpush.check_endpoint(req.endpoint)
    except push_webpush.EndpointRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    sid = subs_store.upsert(authed.uid, req.endpoint, req.keys.p256dh, req.keys.auth, req.ua)
    return {"ok": True, "id": sid}


@router.delete("/api/me/push-subscriptions/{sid}")
def delete_subscription(
    sid: str, authed: Annotated[AuthedUser, Depends(require_user)]
) -> dict[str, bool]:
    subs_store.remove(authed.uid, sid)
    return {"ok": True}
