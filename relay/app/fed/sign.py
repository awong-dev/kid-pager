"""Request and response signing: Ed25519 over `{method}\\n{path}\\n{ts}\\n{from}\\n{to}\\n{sha256hex(body)}`
in the `X-KP-Fed-*` headers (a response uses method `RESPONSE`)."""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.fed.keys import FED_ID_RE

H_FROM = "X-KP-Fed-From"
H_TO = "X-KP-Fed-To"
H_TS = "X-KP-Fed-Ts"
H_SIG = "X-KP-Fed-Sig"
MAX_SKEW_S = 300
RESPONSE_METHOD = "RESPONSE"


class SignatureError(Exception):
    """`code`: `missing` (a header is absent or malformed), `skew`, `bad_sig`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical(
    method: str, path: str, ts: int, sender: str, receiver: str, body: bytes
) -> bytes:
    return (
        f"{method.upper()}\n{path}\n{ts}\n{sender}\n{receiver}\n"
        f"{hashlib.sha256(body).hexdigest()}"
    ).encode()


def sign(
    private: Ed25519PrivateKey,
    sender: str,
    receiver: str,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    ts: int | None = None,
) -> dict[str, str]:
    """The four headers for one request (or, with `method="RESPONSE"`, response)."""
    ts = int(time.time()) if ts is None else ts
    signature = private.sign(canonical(method, path, ts, sender, receiver, body))
    return {
        H_FROM: sender,
        H_TO: receiver,
        H_TS: str(ts),
        H_SIG: base64.b64encode(signature).decode("ascii"),
    }


@dataclass(frozen=True, slots=True)
class SignedHeaders:
    sender: str
    receiver: str
    ts: int
    signature: bytes


def parse_headers(headers: Mapping[str, str]) -> SignedHeaders:
    """`SignatureError("missing")` unless all four are present and well formed.
    `headers` must be case-insensitive (Starlette/httpx headers are)."""
    sender = (headers.get(H_FROM) or "").strip()
    receiver = (headers.get(H_TO) or "").strip()
    raw_ts, raw_sig = headers.get(H_TS), headers.get(H_SIG)
    if (
        not FED_ID_RE.match(sender)
        or not FED_ID_RE.match(receiver)
        or raw_ts is None
        or raw_sig is None
    ):
        raise SignatureError("missing")
    try:
        ts = int(raw_ts)
        signature = base64.b64decode(raw_sig, validate=True)
    except ValueError:
        raise SignatureError("missing") from None
    return SignedHeaders(sender=sender, receiver=receiver, ts=ts, signature=signature)


def verify(
    public: Ed25519PublicKey,
    parsed: SignedHeaders,
    method: str,
    path: str,
    body: bytes,
    *,
    now: float | None = None,
) -> None:
    """Raises `SignatureError("skew")` when |now - ts| > 300 s, `("bad_sig")` when
    the signature does not match (a tampered method, path, ts, from, to or body)."""
    now = time.time() if now is None else now
    if abs(now - parsed.ts) > MAX_SKEW_S:
        raise SignatureError("skew")
    try:
        public.verify(
            parsed.signature,
            canonical(
                method, path, parsed.ts, parsed.sender, parsed.receiver, body
            ),
        )
    except InvalidSignature:
        raise SignatureError("bad_sig") from None
