"""Firebase Auth verification, the registry gate (401 bad token, 403 unregistered), admin claims.
Under `AUTH_BACKEND=home` a relay session (`app/authn_home.py`) is accepted instead."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any

import google.auth.jwt as google_jwt
import google.auth.transport.requests as google_requests
import google.oauth2.id_token as google_id_token
from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as fb_auth

from app.config import auth_backend
from app.store import users as users_store
from app.store.users import User

bearer_scheme = HTTPBearer(auto_error=False)

# The home session cookie, and the header every cookie-authenticated write must carry (a
# cross-site form cannot set it).
SESSION_COOKIE = "pager_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "pager"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True, slots=True)
class AuthedUser:
    """The verified token plus the matching registry row."""

    uid: str
    claims: dict
    user: User


def verify_id_token(token: str) -> dict:
    return fb_auth.verify_id_token(token, check_revoked=False)


def require_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> AuthedUser:
    if auth_backend() == "home":
        return _require_home_user(request, creds)
    if creds is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = verify_id_token(creds.credentials)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid or expired ID token") from exc

    uid = claims["uid"]
    user = users_store.get_user(uid)
    if user is None:
        # Registry gate: a stranger with a valid Firebase token gets a 403 and no data.
        raise HTTPException(status_code=403, detail="not registered")
    if user.disabled:
        raise HTTPException(status_code=403, detail="account disabled")
    return AuthedUser(uid=uid, claims=claims, user=user)


def _require_home_user(
    request: Request, creds: HTTPAuthorizationCredentials | None
) -> AuthedUser:
    """A bearer wins over the cookie; a cookie write needs `X-Requested-With: pager` (CSRF)."""
    from app import authn_home

    if creds is not None:
        token = creds.credentials
    else:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not token:
            raise HTTPException(status_code=401, detail="not signed in")
        if request.method not in _SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(status_code=403, detail=f"missing {CSRF_HEADER} header")
    uid = authn_home.session_uid(token)
    if uid is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    user = users_store.get_user(uid)
    if user is None:
        raise HTTPException(status_code=403, detail="not registered")
    if user.disabled:
        raise HTTPException(status_code=403, detail="account disabled")
    # No token claims at home; the same keys are reported so `/api/me`'s `claims` keeps its shape.
    claims = {"uid": uid, "role": user.role, "fam": user.familyId or ""}
    return AuthedUser(uid=uid, claims=claims, user=user)


@dataclass(frozen=True, slots=True)
class Principal:
    """The token's `role`/`fam` custom claims only; without them: `member`, no family."""

    uid: str
    role: str
    family_id: str | None


def principal_for(authed: AuthedUser) -> Principal:
    if auth_backend() == "home":
        # At home the document `require_user` already loaded is the source of truth.
        return Principal(uid=authed.uid, role=authed.user.role, family_id=authed.user.familyId)
    role = authed.claims.get("role") or "member"
    fam = authed.claims.get("fam") or None
    return Principal(uid=authed.uid, role=role, family_id=fam)


def require_super(
    authed: Annotated[AuthedUser, Depends(require_user)],
) -> AuthedUser:
    """Role `super` by claim only."""
    if principal_for(authed).role != "super":
        raise HTTPException(status_code=403, detail="super admin only")
    return authed


def require_family_admin(
    authed: Annotated[AuthedUser, Depends(require_user)],
    family: Annotated[str | None, Query()] = None,
    x_family: Annotated[str | None, Header(alias="X-Family")] = None,
) -> tuple[Principal, str]:
    """`admin` acts on their own family; `super` names any via `?family=`/`X-Family`."""
    principal = principal_for(authed)
    requested = family or x_family
    if principal.role == "admin":
        if principal.family_id is None:
            raise HTTPException(status_code=403, detail="family admin only")
        if requested is not None and requested != principal.family_id:
            raise HTTPException(status_code=403, detail="cannot act on another family")
        return principal, principal.family_id
    if principal.role == "super":
        if not requested:
            raise HTTPException(status_code=400, detail="?family= or X-Family is required")
        return principal, requested
    raise HTTPException(status_code=403, detail="family admin only")


def set_claims(uid: str, role: str, family_id: str | None) -> None:
    """Writes the `role` and `fam` custom claims; a no-op under `AUTH_BACKEND=home`."""
    if auth_backend() == "home":
        return
    fb_auth.set_custom_user_claims(uid, {"role": role, "fam": family_id or ""})


# `/internal/*` OIDC verification: a Google OIDC token from the Scheduler/Tasks service
# account, checked against an allow-list of caller emails.
def verify_internal_oidc_token(
    token: str,
    *,
    audience: str,
    allowed_emails: frozenset[str],
    certs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Raises `ValueError` on a bad signature/audience/issuer/expiry or unlisted caller email."""
    if not audience:
        raise ValueError("no OIDC_AUDIENCE configured for this deployment")
    if not allowed_emails:
        raise ValueError("no OIDC_ALLOWED_EMAILS configured for this deployment")
    if certs is not None:
        claims = google_jwt.decode(
            token, certs=dict(certs), audience=audience, clock_skew_in_seconds=30
        )
    else:
        request = google_requests.Request()
        claims = google_id_token.verify_oauth2_token(
            token, request, audience=audience, clock_skew_in_seconds=30
        )
    email = claims.get("email")
    if email not in allowed_emails:
        raise ValueError(f"unexpected caller email: {email!r}")
    return claims
