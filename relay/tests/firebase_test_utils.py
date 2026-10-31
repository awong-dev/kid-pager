"""Test helpers that get a real Firebase ID token from the Auth emulator: mint a custom token,
then exchange it through the emulator's Identity Toolkit REST endpoint."""

from __future__ import annotations

import os

import httpx
from firebase_admin import auth as fb_auth

AUTH_HOST = os.environ.get("FIREBASE_AUTH_EMULATOR_HOST", "localhost:9099")
FAKE_API_KEY = "fake-api-key"


def mint_id_token(uid: str) -> str:
    custom_token = fb_auth.create_custom_token(uid).decode("utf-8")
    resp = httpx.post(
        f"http://{AUTH_HOST}/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken",
        params={"key": FAKE_API_KEY},
        json={"token": custom_token, "returnSecureToken": True},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["idToken"]


def auth_header(uid: str) -> dict[str, str]:
    """A bearer for `uid`: a Firebase ID token, or a relay session token under
    `AUTH_BACKEND=home`."""
    if (os.environ.get("AUTH_BACKEND", "").strip().lower() or "firebase") == "home":
        import pytest

        from app import authn_home
        from app.store import users as users_store
        from tests.conftest import HOME_CLAIMS

        claims = HOME_CLAIMS.get(uid)
        user = users_store.get_user(uid)
        if claims is not None and user is not None and (
            (claims.get("role") or "member") != user.role
            or (claims.get("fam") or None) != user.familyId
        ):
            pytest.skip(
                "signs in with Firebase custom claims that differ from users/{uid}; at home "
                "role and family come from the document only"
            )
        return {"Authorization": f"Bearer {authn_home.create_session(uid, 'pytest')}"}
    return {"Authorization": f"Bearer {mint_id_token(uid)}"}


def seed_legacy_contact_request(
    device_id: str,
    owner_uid: str,
    req_id: str,
    name: str,
    phone: str,
    *,
    status: str = "pending",
    family_id: str | None = None,
) -> str:
    """Seeds a legacy `contactRequests` row (and, with `family_id`, its open `contact_request`
    alert); returns the row key."""
    from app.db.firestore import get_db
    from app.store import alerts as alerts_store
    from app.store import contacts as contacts_store

    doc_key = contacts_store.key(device_id, req_id)
    get_db().collection("contactRequests").document(doc_key).set(
        {
            "deviceId": device_id,
            "reqId": req_id,
            "ownerUid": owner_uid,
            "name": name,
            "phone": phone,
            "alias": None,
            "status": status,
        }
    )
    if family_id is not None:
        alerts_store.create(
            family_id,
            {
                "kind": "contact_request",
                "status": "open",
                "subjectUid": owner_uid,
                "subjectAlias": owner_uid,
                "peerPhone": phone,
                "preview": name,
                "contactRequestKey": doc_key,
            },
        )
    return doc_key
