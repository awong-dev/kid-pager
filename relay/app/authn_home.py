"""Home sign-in: passwords (argon2id), passkeys, invite codes and sessions (`AUTH_BACKEND=home`).
Sessions and invite codes are stored by `sha256` digest, so a `data/` copy holds no live secret."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from google.cloud.firestore import FieldFilter, Transaction
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    options_to_json_dict,
    parse_authentication_credential_json,
)
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidJSONStructure,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.db.firestore import get_db, run_transaction
from app.store import families as families_store
from app.store import settings as settings_store
from app.store import users as users_store
from app.store.users import User

SESSION_IDLE = timedelta(days=30)
# `lastSeenAt` is written at most once a minute, so an active tab does not turn every API
# call into a write.
LAST_SEEN_RESOLUTION = timedelta(minutes=1)
INVITE_TTL = timedelta(days=7)
CHALLENGE_TTL = timedelta(minutes=5)
RP_NAME = "Pager"

# Crockford-style alphabet without 0/O/1/I/L: a code is read off one screen
# and typed on another.
_INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_INVITE_LEN = 12

_hasher = PasswordHasher()


class AuthError(Exception):
    """A ceremony or credential check failed."""


def _now() -> datetime:
    return datetime.now(UTC)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_uid() -> str:
    """A home account's uid: 28 characters, the length of a Firebase uid."""
    return secrets.token_hex(14)


def _credentials(uid: str):
    return get_db().collection("users").document(uid).collection("credentials")


def set_password(uid: str, password: str) -> None:
    _credentials(uid).document("password").set(
        {"kind": "password", "hash": _hasher.hash(password), "updatedAt": _now()}
    )


def verify_password(uid: str, password: str) -> bool:
    ref = _credentials(uid).document("password")
    snap = ref.get()
    if not snap.exists:
        return False
    stored = (snap.to_dict() or {}).get("hash", "")
    try:
        _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False
    if _hasher.check_needs_rehash(stored):
        ref.update({"hash": _hasher.hash(password), "updatedAt": _now()})
    return True


def has_password(uid: str) -> bool:
    return _credentials(uid).document("password").get().exists


def list_passkeys(uid: str) -> list[dict[str, Any]]:
    return [
        snap.to_dict() or {}
        for snap in _credentials(uid).where(filter=FieldFilter("kind", "==", "passkey")).stream()
    ]


def create_session(uid: str, user_agent: str | None) -> str:
    """Returns the opaque session token (the cookie value / bearer)."""
    token = secrets.token_urlsafe(32)
    now = _now()
    get_db().collection("sessions").document(_digest(token)).set(
        {"uid": uid, "createdAt": now, "lastSeenAt": now, "userAgent": (user_agent or "")[:256]}
    )
    return token


def session_uid(token: str) -> str | None:
    """The session's uid, or `None` if unknown or idle past `SESSION_IDLE` (then deleted)."""
    if not token:
        return None
    ref = get_db().collection("sessions").document(_digest(token))
    snap = ref.get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    last_seen = data.get("lastSeenAt")
    now = _now()
    if not isinstance(last_seen, datetime) or now - last_seen > SESSION_IDLE:
        ref.delete()
        return None
    if now - last_seen > LAST_SEEN_RESOLUTION:
        ref.update({"lastSeenAt": now})
    return data.get("uid")


def delete_session(token: str) -> None:
    if token:
        get_db().collection("sessions").document(_digest(token)).delete()


def _delete_user_sessions(uid: str) -> None:
    for snap in get_db().collection("sessions").where(filter=FieldFilter("uid", "==", uid)).stream():
        snap.reference.delete()


def delete_account(uid: str) -> None:
    """Credentials and sessions of a deleted user."""
    for snap in _credentials(uid).stream():
        snap.reference.delete()
    _delete_user_sessions(uid)


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def mint_invite(uid: str, created_by: str, db=None) -> tuple[str, datetime]:
    """Returns `(code, expiresAt)`; the code is shown once, any case/dashes accepted back."""
    raw = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(_INVITE_LEN))
    now = _now()
    expires = now + INVITE_TTL
    (db or get_db()).collection("inviteCodes").document(_digest(raw)).set(
        {"uid": uid, "createdBy": created_by, "createdAt": now, "expiresAt": expires}
    )
    return "-".join(raw[i : i + 4] for i in range(0, _INVITE_LEN, 4)), expires


def redeem_invite(code: str) -> str | None:
    """Consumes the code and returns its uid, or `None` if unknown, used or expired."""
    normalized = _normalize_code(code)
    if not normalized:
        return None
    ref = get_db().collection("inviteCodes").document(_digest(normalized))

    def _txn(transaction: Transaction) -> str | None:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        transaction.delete(ref)
        expires = data.get("expiresAt")
        if not isinstance(expires, datetime) or expires <= _now():
            return None
        return data.get("uid")

    return run_transaction(_txn)


def needs_setup() -> bool:
    """The first-run page exists only while `users/` is empty."""
    return not list(get_db().collection("users").limit(1).stream())


def create_first_admin(username: str, password: str) -> User:
    """`python -m app.bootstrap` for home: default family, a `super` with a password."""
    from app.bootstrap import DEFAULT_FAMILY_ID, DEFAULT_FAMILY_NAME

    if families_store.get_family(DEFAULT_FAMILY_ID) is None:
        families_store.create_family(
            name=DEFAULT_FAMILY_NAME, created_by="setup", family_id=DEFAULT_FAMILY_ID
        )
    user = users_store.create_user(
        uid=new_uid(),
        alias=username,
        display_name=username,
        role="super",
        family_id=DEFAULT_FAMILY_ID,
        kind="person",
    )
    set_password(user.uid, password)
    settings_store.ensure_meta_initialized()
    return user


@dataclass(frozen=True, slots=True)
class RelyingParty:
    """Relying party id = host of `PUBLIC_BASE_URL`; the expected origin is `PUBLIC_BASE_URL`."""

    id: str
    origin: str

    @classmethod
    def from_base_url(cls, base_url: str | None) -> RelyingParty:
        if not base_url:
            raise AuthError("PUBLIC_BASE_URL is not configured; passkeys are unavailable")
        host = urlsplit(base_url).hostname
        if not host:
            raise AuthError("PUBLIC_BASE_URL has no host; passkeys are unavailable")
        return cls(id=host, origin=base_url.rstrip("/"))


def _store_challenge(challenge: bytes, kind: str, uid: str | None) -> str:
    challenge_id = secrets.token_urlsafe(16)
    get_db().collection("webauthnChallenges").document(challenge_id).set(
        {
            "challenge": bytes_to_base64url(challenge),
            "kind": kind,
            "uid": uid,
            "expiresAt": _now() + CHALLENGE_TTL,
        }
    )
    return challenge_id


def _take_challenge(challenge_id: str, kind: str, uid: str | None) -> bytes:
    if not challenge_id:
        raise AuthError("unknown or expired challenge")
    ref = get_db().collection("webauthnChallenges").document(challenge_id)

    def _txn(transaction: Transaction) -> dict[str, Any] | None:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return None
        transaction.delete(ref)
        return snap.to_dict() or {}

    data = run_transaction(_txn)
    if (
        data is None
        or data.get("kind") != kind
        or data.get("uid") != uid
        or not isinstance(data.get("expiresAt"), datetime)
        or data["expiresAt"] <= _now()
    ):
        raise AuthError("unknown or expired challenge")
    return base64url_to_bytes(data["challenge"])


def begin_registration(user: User, rp: RelyingParty) -> tuple[str, dict[str, Any]]:
    """Returns `(challengeId, creation options as JSON)`; a resident credential is required."""
    existing = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["credentialId"]))
        for p in list_passkeys(user.uid)
    ]
    options = generate_registration_options(
        rp_id=rp.id,
        rp_name=RP_NAME,
        user_id=user.uid.encode("utf-8"),
        user_name=user.alias,
        user_display_name=user.displayName,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            require_resident_key=True,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=existing,
    )
    return _store_challenge(options.challenge, "register", user.uid), options_to_json_dict(options)


def finish_registration(
    uid: str, challenge_id: str, credential: dict[str, Any], rp: RelyingParty
) -> str:
    """Verifies the attestation and stores the passkey; returns its base64url credential id."""
    challenge = _take_challenge(challenge_id, "register", uid)
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.id,
            expected_origin=rp.origin,
        )
    except (InvalidRegistrationResponse, InvalidJSONStructure) as exc:
        raise AuthError(f"passkey registration failed: {exc}") from exc
    credential_id = bytes_to_base64url(verified.credential_id)
    transports = (credential.get("response") or {}).get("transports") or []
    now = _now()
    _credentials(uid).document(credential_id).set(
        {
            "kind": "passkey",
            "credentialId": credential_id,
            "publicKey": bytes_to_base64url(verified.credential_public_key),
            "signCount": verified.sign_count,
            "transports": [t for t in transports if t in {x.value for x in AuthenticatorTransport}],
            "createdAt": now,
            "lastUsedAt": None,
        }
    )
    return credential_id


def begin_login(rp: RelyingParty) -> tuple[str, dict[str, Any]]:
    """Returns `(challengeId, request options as JSON)` with no `allowCredentials`."""
    options = generate_authentication_options(
        rp_id=rp.id, user_verification=UserVerificationRequirement.PREFERRED
    )
    return _store_challenge(options.challenge, "login", None), options_to_json_dict(options)


def finish_login(challenge_id: str, credential: dict[str, Any], rp: RelyingParty) -> str:
    """Verifies the assertion against the stored key and sign count; returns the uid."""
    challenge = _take_challenge(challenge_id, "login", None)
    try:
        parsed = parse_authentication_credential_json(credential)
    except InvalidJSONStructure as exc:
        raise AuthError(f"passkey sign-in failed: {exc}") from exc
    user_handle = parsed.response.user_handle
    if not user_handle:
        raise AuthError("passkey sign-in failed: no user handle")
    uid = user_handle.decode("utf-8", errors="replace")
    credential_id = bytes_to_base64url(parsed.raw_id)
    ref = _credentials(uid).document(credential_id)
    snap = ref.get()
    stored = snap.to_dict() if snap.exists else None
    if not stored or stored.get("kind") != "passkey":
        raise AuthError("passkey sign-in failed: unknown credential")
    try:
        verified = verify_authentication_response(
            credential=parsed,
            expected_challenge=challenge,
            expected_rp_id=rp.id,
            expected_origin=rp.origin,
            credential_public_key=base64url_to_bytes(stored["publicKey"]),
            credential_current_sign_count=int(stored.get("signCount") or 0),
        )
    except InvalidAuthenticationResponse as exc:
        # Includes a sign count that did not advance (a cloned authenticator).
        raise AuthError(f"passkey sign-in failed: {exc}") from exc
    ref.update({"signCount": verified.new_sign_count, "lastUsedAt": _now()})
    return uid
