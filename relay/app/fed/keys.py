"""Federation identity: Ed25519 key pair and `fedId` (`f_` + 20 base32 characters) per family;
keys are base64 of the raw 32 bytes."""

from __future__ import annotations

import base64
import hashlib
import os
import re

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

FED_ID_RE = re.compile(r"^f_[a-z2-7]{20}$")


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def new_fed_id() -> str:
    """`f_` + 20 random base32 characters (100 bits), lower case."""
    return "f_" + base64.b32encode(os.urandom(13)).decode("ascii").lower()[:20]


def generate() -> tuple[str, str]:
    """A fresh key pair as `(private_b64, public_b64)`."""
    private = Ed25519PrivateKey.generate()
    return private_b64(private), public_b64(private)


def private_b64(private: Ed25519PrivateKey) -> str:
    return b64(
        private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )


def private_from_b64(value: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(value, validate=True))


def public_b64(private: Ed25519PrivateKey) -> str:
    return b64(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )


def public_of(private_key_b64: str) -> str:
    return public_b64(private_from_b64(private_key_b64))


def public_from_b64(value: str) -> Ed25519PublicKey:
    """`ValueError` for anything that is not a base64 raw Ed25519 public key."""
    try:
        return Ed25519PublicKey.from_public_bytes(
            base64.b64decode(value, validate=True)
        )
    except Exception as exc:
        raise ValueError("not an Ed25519 public key") from exc


def fingerprint(public_key_b64: str) -> str:
    """What two admins compare out of band: the lower-case hex SHA-256 of the raw 32-byte Ed25519
    public key, in 4-character groups separated by spaces. `ValueError` for a bad key."""
    raw = base64.b64decode(public_key_b64, validate=True)
    if len(raw) != 32:
        raise ValueError("not an Ed25519 public key")
    digest = hashlib.sha256(raw).hexdigest()
    return " ".join(digest[i : i + 4] for i in range(0, len(digest), 4))
