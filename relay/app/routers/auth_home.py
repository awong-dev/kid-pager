"""`/api/auth/*` and `/api/setup/*`: home sign-in, invite redemption, passkeys and first run.

Sessions are the `pager_session` cookie; every POST needs `X-Requested-With: pager` (CSRF).
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app import authn_home
from app.auth import (
    CSRF_HEADER,
    CSRF_VALUE,
    SESSION_COOKIE,
    AuthedUser,
    bearer_scheme,
    require_user,
)
from app.routers.webhooks import _client_ip
from app.store import rate_limits as rate_limits_store
from app.store import users as users_store
from app.store.users import User

DEFAULT_AUTH_IP_LIMIT = 30
DEFAULT_AUTH_IP_WINDOW_S = 600
DEFAULT_AUTH_USER_LIMIT = 10
DEFAULT_AUTH_USER_WINDOW_S = 600

PASSWORD_MIN = 8
PASSWORD_MAX = 256


def require_csrf_header(request: Request) -> None:
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=403, detail=f"missing {CSRF_HEADER} header")


router = APIRouter(prefix="/api/auth")
setup_router = APIRouter(prefix="/api/setup")


def _limit(name: str, default_limit: int, default_window: int) -> tuple[int, int]:
    limit = int(os.environ.get(f"RATE_LIMIT_{name}_LIMIT", str(default_limit)))
    window_s = int(os.environ.get(f"RATE_LIMIT_{name}_WINDOW_S", str(default_window)))
    return limit, window_s


def _check_ip(request: Request) -> None:
    limit, window_s = _limit("AUTH_IP", DEFAULT_AUTH_IP_LIMIT, DEFAULT_AUTH_IP_WINDOW_S)
    if not rate_limits_store.check_and_increment(
        f"auth_ip:{_client_ip(request)}", limit=limit, window_s=window_s
    ):
        raise HTTPException(status_code=429, detail="too many sign-in attempts; try again later")


def _check_username(username: str) -> None:
    limit, window_s = _limit("AUTH_USER", DEFAULT_AUTH_USER_LIMIT, DEFAULT_AUTH_USER_WINDOW_S)
    if not rate_limits_store.check_and_increment(
        f"auth_user:{username.lower()}", limit=limit, window_s=window_s
    ):
        raise HTTPException(status_code=429, detail="too many sign-in attempts; try again later")


class SignedIn(BaseModel):
    uid: str
    alias: str


def _sign_in(request: Request, response: Response, user: User) -> SignedIn:
    if user.disabled:
        raise HTTPException(status_code=403, detail="account disabled")
    token = authn_home.create_session(user.uid, request.headers.get("user-agent"))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(authn_home.SESSION_IDLE.total_seconds()),
        path=request.app.state.settings.base_path or "/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return SignedIn(uid=user.uid, alias=user.alias)


def _rp(request: Request) -> authn_home.RelyingParty:
    try:
        return authn_home.RelyingParty.from_base_url(request.app.state.settings.public_base_url)
    except authn_home.AuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


Password = Annotated[str, Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX)


@router.post("/login", dependencies=[Depends(require_csrf_header)])
def login(req: LoginRequest, request: Request, response: Response) -> SignedIn:
    username = req.username.strip()
    _check_ip(request)
    _check_username(username)
    user = users_store.get_user_by_alias(username)
    if user is None or not authn_home.verify_password(user.uid, req.password):
        raise HTTPException(status_code=401, detail="wrong username or password")
    return _sign_in(request, response, user)


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    # Optional: without one the person signs in and can add a passkey instead.
    password: Password | None = None


@router.post("/invite/redeem", dependencies=[Depends(require_csrf_header)])
def redeem_invite(req: RedeemRequest, request: Request, response: Response) -> SignedIn:
    _check_ip(request)
    uid = authn_home.redeem_invite(req.code)
    user = users_store.get_user(uid) if uid else None
    if user is None:
        raise HTTPException(status_code=401, detail="that invite code is not valid")
    if req.password is not None:
        authn_home.set_password(user.uid, req.password)
    return _sign_in(request, response, user)


@router.post("/logout", dependencies=[Depends(require_csrf_header)])
def logout(
    request: Request,
    response: Response,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> dict[str, bool]:
    token = creds.credentials if creds is not None else request.cookies.get(SESSION_COOKIE, "")
    authn_home.delete_session(token)
    response.delete_cookie(
        SESSION_COOKIE,
        path=request.app.state.settings.base_path or "/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


class SessionResponse(BaseModel):
    uid: str
    alias: str
    displayName: str
    hasPassword: bool
    passkeys: int


@router.get("/session")
def session(authed: Annotated[AuthedUser, Depends(require_user)]) -> SessionResponse:
    return SessionResponse(
        uid=authed.uid,
        alias=authed.user.alias,
        displayName=authed.user.displayName,
        hasPassword=authn_home.has_password(authed.uid),
        passkeys=len(authn_home.list_passkeys(authed.uid)),
    )


class CeremonyBegin(BaseModel):
    challengeId: str
    # WebAuthn options JSON, binary fields base64url.
    options: dict[str, Any]


class CeremonyFinish(BaseModel):
    challengeId: str
    # The browser's `PublicKeyCredential` as JSON.
    credential: dict[str, Any]


@router.post("/passkey/register/begin", dependencies=[Depends(require_csrf_header)])
def passkey_register_begin(
    request: Request, authed: Annotated[AuthedUser, Depends(require_user)]
) -> CeremonyBegin:
    challenge_id, options = authn_home.begin_registration(authed.user, _rp(request))
    return CeremonyBegin(challengeId=challenge_id, options=options)


@router.post("/passkey/register/finish", dependencies=[Depends(require_csrf_header)])
def passkey_register_finish(
    req: CeremonyFinish, request: Request, authed: Annotated[AuthedUser, Depends(require_user)]
) -> dict[str, str]:
    try:
        credential_id = authn_home.finish_registration(
            authed.uid, req.challengeId, req.credential, _rp(request)
        )
    except authn_home.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"credentialId": credential_id}


@router.post("/passkey/login/begin", dependencies=[Depends(require_csrf_header)])
def passkey_login_begin(request: Request) -> CeremonyBegin:
    _check_ip(request)
    challenge_id, options = authn_home.begin_login(_rp(request))
    return CeremonyBegin(challengeId=challenge_id, options=options)


@router.post("/passkey/login/finish", dependencies=[Depends(require_csrf_header)])
def passkey_login_finish(req: CeremonyFinish, request: Request, response: Response) -> SignedIn:
    _check_ip(request)
    try:
        uid = authn_home.finish_login(req.challengeId, req.credential, _rp(request))
    except authn_home.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user = users_store.get_user(uid)
    if user is None:
        raise HTTPException(status_code=401, detail="passkey sign-in failed: unknown account")
    return _sign_in(request, response, user)


class SetupStatus(BaseModel):
    needsSetup: bool


@setup_router.get("/status")
def setup_status() -> SetupStatus:
    return SetupStatus(needsSetup=authn_home.needs_setup())


class FirstAdminRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: Password


@setup_router.post("/first-admin", dependencies=[Depends(require_csrf_header)])
def first_admin(req: FirstAdminRequest, request: Request, response: Response) -> SignedIn:
    if not authn_home.needs_setup():
        raise HTTPException(status_code=409, detail="setup is already complete")
    try:
        user = authn_home.create_first_admin(req.username.strip(), req.password)
    except (users_store.AliasTaken, users_store.InvalidAlias) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _sign_in(request, response, user)
