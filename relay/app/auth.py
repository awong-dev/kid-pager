"""Firebase Auth verification, the registry gate (401 bad token, 403 unregistered), admin claims.
Under `AUTH_BACKEND=home` a relay session (`app/authn_home.py`) is accepted instead."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any

import google.auth.jwt as google_jwt
import google.auth.transport.requests as google_requests
import google.oauth2.id_token as google_id_token
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as fb_auth

from app.store import users as users_store
from app.store.users import User

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class AuthedUser:
    """The verified token plus the matching registry row."""

    uid: str
    claims: dict
    user: User


def verify_id_token(token: str) -> dict:
    return fb_auth.verify_id_token(token, check_revoked=False)


def require_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> AuthedUser:
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


    authed: Annotated[AuthedUser, Depends(require_user)],
) -> AuthedUser:
    return authed


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
