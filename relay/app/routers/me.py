"""`/api/me/*`: the caller's profile, directory, backends and push tokens.

`POST /api/me/backends` is rate limited per user (`RATE_LIMIT_BACKEND_CREATE_LIMIT` per
"""

from __future__ import annotations

import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from google.cloud.firestore import FieldFilter
from pydantic import BaseModel

from app.auth import AuthedUser, principal_for, require_user, set_claims
from app.backends.base import Backend as BackendImpl
from app.db.firestore import get_db
from app.store import backends as backends_store
from app.store import push_tokens as push_tokens_store
from app.store import rate_limits as rate_limits_store
from app.store import users as users_store
from app.store.backends import Backend, BackendKind
from app.store.users import User

logger = logging.getLogger("relay.routers.me")

router = APIRouter()

# Backend kinds with a link/verify flow: created `enabled=False`; `verify_backend` enables them.

DEFAULT_BACKEND_CREATE_LIMIT = 10
DEFAULT_BACKEND_CREATE_WINDOW_S = 3600


def _backend_create_rate_limit() -> tuple[int, int]:
    limit = int(os.environ.get("RATE_LIMIT_BACKEND_CREATE_LIMIT", str(DEFAULT_BACKEND_CREATE_LIMIT)))
    window_s = int(
        os.environ.get("RATE_LIMIT_BACKEND_CREATE_WINDOW_S", str(DEFAULT_BACKEND_CREATE_WINDOW_S))
    )
    return limit, window_s


def require_backend_create_rate_limit(
    authed: Annotated[AuthedUser, Depends(require_user)],
) -> AuthedUser:
    """Rate-limit dependency for `POST /api/me/backends`; returns the `AuthedUser`."""
    limit, window_s = _backend_create_rate_limit()
    if not rate_limits_store.check_and_increment(
        f"backends:{authed.uid}", limit=limit, window_s=window_s
    ):
        raise HTTPException(
            status_code=429, detail="too many backend creation attempts; try again later"
        )
    return authed


def get_backend_registry(request: Request) -> dict[str, BackendImpl]:
    return request.app.state.backend_registry


class MeResponse(BaseModel):
    user: User
    role: str
    claims: dict
    # true when the token's `role`/`fam` claims disagree with the user doc; re-issued server-side
    claimsStale: bool = False


@router.get("/api/me")
def get_me(authed: Annotated[AuthedUser, Depends(require_user)]) -> MeResponse:
    principal = principal_for(authed)
    stale = principal.role != authed.user.role or (
        principal.family_id or ""
    ) != (authed.user.familyId or "")
    if stale:
        set_claims(authed.uid, authed.user.role, authed.user.familyId)
    return MeResponse(
    )


class NotifyPatch(BaseModel):
    alerts: bool


class PatchMeRequest(BaseModel):
    notify: NotifyPatch | None = None


@router.patch("/api/me")
def patch_me(req: PatchMeRequest, authed: Annotated[AuthedUser, Depends(require_user)]) -> User:
    """Today's only field is `notify.alerts` (pushed only to admins and supers)."""
    notify_alerts = req.notify.alerts if req.notify is not None else None
    return users_store.update_user(authed.uid, notify_alerts=notify_alerts)


DIRECTORY_MAX_ENTRIES = 500


class DirectoryEntry(BaseModel):
    uid: str
    alias: str
    displayName: str
    kind: str
    familyId: str | None
    role: str
    phone: str | None = None


@router.get("/api/directory")
def get_directory(
    authed: Annotated[AuthedUser, Depends(require_user)],
    # Only consulted for a `super` caller; ignored for everyone else.
    family: Annotated[str | None, Query()] = None,
) -> dict:
    """Aliases the caller may resolve beyond its own family: allow-edge peers, participants
    and, for an admin or super naming a family, both ends of every edge tagged with that family.
    """
    me = authed.uid
    db = get_db()
    uids: set[str] = set()

    family_id = authed.user.familyId
    if family_id:
        for snap in db.collection("users").where(
            filter=FieldFilter("familyId", "==", family_id)
        ).stream():
            uids.add(snap.id)

    for snap in db.collection("allow").where(filter=FieldFilter("fromUid", "==", me)).stream():
        to_uid = (snap.to_dict() or {}).get("toUid")
        if to_uid:
            uids.add(to_uid)
    for snap in db.collection("allow").where(filter=FieldFilter("toUid", "==", me)).stream():
        from_uid = (snap.to_dict() or {}).get("fromUid")
        if from_uid:
            uids.add(from_uid)

    for snap in db.collection("conversations").where(
        filter=FieldFilter("uids", "array_contains", me)
    ).stream():
        for uid in (snap.to_dict() or {}).get("uids") or []:
            uids.add(uid)

    admin_scope_family_id = (
        family
        if (authed.user.role == "super" and family)
        else (family_id if authed.user.role == "admin" else None)
    )
    if admin_scope_family_id:
        for snap in db.collection("allow").where(
            filter=FieldFilter("familyIds", "array_contains", admin_scope_family_id)
        ).stream():
            data = snap.to_dict() or {}
            if data.get("fromUid"):
                uids.add(data["fromUid"])
            if data.get("toUid"):
                uids.add(data["toUid"])

    entries: list[DirectoryEntry] = []
    for uid in uids:
        user = users_store.get_user(uid)
        if user is None:
            continue
        entries.append(
            DirectoryEntry(
                uid=user.uid,
                alias=user.alias,
                displayName=user.displayName,
                kind=user.kind,
                familyId=user.familyId,
                role=user.role,
                phone=phone,
            )
        )

    entries.sort(key=lambda e: e.alias)
    # drop `phone` for non-externals (the web expects it only for externals); keep meaningful nulls
    out: list[dict] = []
    for entry in entries[:DIRECTORY_MAX_ENTRIES]:
        data = entry.model_dump()
        if data["phone"] is None:
            del data["phone"]
        out.append(data)
    return {"entries": out}


class CreateBackendRequest(BaseModel):
    kind: BackendKind
    config: dict = {}
    enabled: bool = True


class PatchBackendRequest(BaseModel):
    config: dict | None = None
    enabled: bool | None = None


@router.get("/api/me/backends")
def list_backends(authed: Annotated[AuthedUser, Depends(require_user)]) -> list[Backend]:
    existing = backends_store.list_backends(authed.uid)
    if not any(b.kind == "webapp" for b in existing):
        # backfill the implicit `webapp` backend for rows created without one
        backends_store.create_backend(authed.uid, kind="webapp", config={}, enabled=True)
        existing = backends_store.list_backends(authed.uid)
    return existing


@router.post("/api/me/backends")
def create_backend(
    req: CreateBackendRequest,
    authed: Annotated[AuthedUser, Depends(require_backend_create_rate_limit)],
    registry: Annotated[dict[str, BackendImpl], Depends(get_backend_registry)],
) -> Backend:
    if req.kind == "pager":
        # a pager backend's `config.deviceId` must name a device the user owns: admin-provisioned
        raise HTTPException(status_code=400, detail="pager backends are admin-provisioned")
    config = req.config
    # never trust the request's `enabled` for a link-flow kind
    enabled = False if req.kind in LINK_FLOW_KINDS else req.enabled
    created = backends_store.create_backend(
        authed.uid, kind=req.kind, config=config, enabled=enabled
    )
    impl = registry.get(req.kind)
    if impl is not None:
        try:
            impl.start_link(authed.user, created)
        except Exception:
            logger.exception("start_link failed for new %s backend %s", req.kind, created.id)
    refreshed = backends_store.get_backend(authed.uid, created.id)
    return refreshed if refreshed is not None else created


class VerifyBackendRequest(BaseModel):
    code: str


@router.post("/api/me/backends/{bid}/verify")
def verify_backend(
    bid: str,
    req: VerifyBackendRequest,
    authed: Annotated[AuthedUser, Depends(require_user)],
    registry: Annotated[dict[str, BackendImpl], Depends(get_backend_registry)],
) -> Backend:
    row = backends_store.get_backend(authed.uid, bid)
    if row is None:
        raise HTTPException(status_code=404, detail="no such backend")
    impl = registry.get(row.kind)
    if impl is None or not impl.complete_link(row, req.code):
        raise HTTPException(status_code=400, detail="invalid or expired code")
    updates: dict[str, object] = {"verified": True, "enabled": True}
    updated = backends_store.update_backend(authed.uid, bid, **updates)
    return updated


@router.patch("/api/me/backends/{bid}")
def patch_backend(
    bid: str, req: PatchBackendRequest, authed: Annotated[AuthedUser, Depends(require_user)]
) -> Backend:
    row = backends_store.get_backend(authed.uid, bid)
    if row is None:
        raise HTTPException(status_code=404, detail="no such backend")



@router.delete("/api/me/backends/{bid}")
def delete_backend(
    bid: str, authed: Annotated[AuthedUser, Depends(require_user)]
) -> dict[str, bool]:
    row = backends_store.get_backend(authed.uid, bid)
    if row is None:
        raise HTTPException(status_code=404, detail="no such backend")
    backends_store.delete_backend(authed.uid, bid)
    return {"ok": True}


class PushTokenRequest(BaseModel):
    token: str


@router.post("/api/me/push-tokens")
def add_push_token(
    req: PushTokenRequest, authed: Annotated[AuthedUser, Depends(require_user)]
) -> dict[str, bool]:
    push_tokens_store.add_token(authed.uid, req.token)
    return {"ok": True}


@router.delete("/api/me/push-tokens/{token}")
def remove_push_token(
    token: str, authed: Annotated[AuthedUser, Depends(require_user)]
) -> dict[str, bool]:
    push_tokens_store.remove_token(authed.uid, token)
    return {"ok": True}
