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
    return {"Authorization": f"Bearer {mint_id_token(uid)}"}
