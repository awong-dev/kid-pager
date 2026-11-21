"""`/fed/v1/*`: the relay-to-relay API between linked families, authenticated by signature.

Every signed route answers one indistinguishable 403 for any refusal."""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import hooks as hooks_module
from app.config import Settings
from app.fed import inbox, linking, moved
from app.fed.deps import (
    SignedRequest,
    app_settings,
    read_body,
    request_target,
    require_server_switch,
    signed_moved_request,
    signed_request,
)
from app.routing import Routing
from app.store import users as users_store
from app.store.families import FROZEN_REPLY, FamilyFrozen

router = APIRouter(prefix="/fed/v1")
logger = logging.getLogger("relay.fed.api")

PROTOCOL_VERSION = 2


@router.get("/info", dependencies=[Depends(require_server_switch)])
def info(settings: Annotated[Settings, Depends(app_settings)]) -> dict:
    """No identity. `web` is the public URL of this relay's family web app (R9), or null."""
    return {
        "v": PROTOCOL_VERSION,
        "hooks": bool(hooks_module.active_hooks()),
        "web": settings.web_base_url,
    }


@router.post("/link")
async def link(
    request: Request, settings: Annotated[Settings, Depends(app_settings)]
) -> Response:
    body = await read_body(request)
    try:
        reply = await run_in_threadpool(
            linking.handle_link_request,
            settings,
            request_target(request),
            request.headers,
            body,
            source=request.client.host if request.client else "unknown",
        )
    except linking.LinkingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    return Response(
        content=reply.body, media_type="application/json", headers=reply.headers
    )


@router.get("/directory")
def directory(signed: Annotated[SignedRequest, Depends(signed_request)]) -> list[dict]:
    """Exactly the people the local admins ticked for this link; nothing else."""
    out: list[dict] = []
    for uid in signed.link.shared:
        user = users_store.get_user(uid)
        if (
            user is None
            or user.kind != "person"
            or user.disabled
            or user.familyId != signed.link.familyId
        ):
            continue
        out.append(
            {
                "rid": user.uid,
                "alias": user.alias,
                "name": user.displayName,
                "kind": "person",
            }
        )
    out.sort(key=lambda e: e["alias"])
    return out


@router.post("/inbox")
async def post_inbox(
    request: Request, signed: Annotated[SignedRequest, Depends(signed_request)]
) -> Response:
    """Batched events, one result each; a frozen family answers 503 so the sender retries."""
    try:
        payload = json.loads(signed.body)
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError("version")
        events = payload["events"]
        if not isinstance(events, list):
            raise TypeError("events")
    except (ValueError, TypeError, KeyError):
        raise HTTPException(
            status_code=400, detail="expected {v: 1, events: [...]}"
        ) from None
    if len(events) > inbox.MAX_EVENTS:
        raise HTTPException(
            status_code=413, detail=f"at most {inbox.MAX_EVENTS} events"
        )
    routing: Routing = request.app.state.routing
    try:
        results = await run_in_threadpool(inbox.process, signed.link, events, routing)
    except FamilyFrozen:
        return JSONResponse(
            status_code=503,
            content={"detail": FROZEN_REPLY},
            headers={"Retry-After": "60"},
        )
    return JSONResponse({"results": results})


@router.post("/moved")
async def post_moved(
    signed: Annotated[SignedRequest, Depends(signed_moved_request)],
) -> dict:
    try:
        await run_in_threadpool(
            moved.handle_moved,
            signed.family.familyId,
            signed.link,
            signed.body,
            used_prev_key=signed.used_prev_key,
        )
    except moved.MovedError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    return {"ok": True}


@router.post("/unlink")
async def post_unlink(
    signed: Annotated[SignedRequest, Depends(signed_request)],
) -> dict:
    """The other family deleted its end: delete ours the same way."""

    await run_in_threadpool(linking.delete_link, signed.link, notify=False)
    return {"ok": True}
