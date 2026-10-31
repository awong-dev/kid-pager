"""Setup codes and the bootstrap bundle: Crockford token, HKDF derivation, AES-GCM bundle.
Implements `docs/PROTOCOL.md` §10 boot keys."""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app import ca_resolve, wirecbor
from app.broker import BrokerClient
from app.broker_admin import BrokerAdmin, make_broker_admin
from app.config import Settings
from app.store import setup_codes as setup_codes_store

TOKEN_BYTES = 8
TOKEN_CHARS = 13  # 65 bits / 5 bits per symbol
CODE_GROUP_SIZE = 4
DEFAULT_PORT = 8883
EXPIRY_MINUTES = 10

# Crockford base32; I/L/O decode as 1/1/0, U is rejected.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_VALUE_OF: dict[str, int] = {c: i for i, c in enumerate(_ALPHABET)}
_VALUE_OF["O"] = 0
_VALUE_OF["I"] = 1
_VALUE_OF["L"] = 1


def _char_value(c: str) -> int:
    try:
        return _VALUE_OF[c]
    except KeyError:
        raise ValueError(f"invalid setup-code character: {c!r}") from None


def _luhn_mod_n_generate(values: list[int], n: int = 32) -> int:
    factor = 2
    total = 0
    for v in reversed(values):
        addend = factor * v
        factor = 1 if factor == 2 else 2
        addend = (addend // n) + (addend % n)
        total += addend
    remainder = total % n
    return (n - remainder) % n


def _luhn_mod_n_validate(values_with_check: list[int], n: int = 32) -> bool:
    factor = 1
    total = 0
    for v in reversed(values_with_check):
        addend = factor * v
        factor = 1 if factor == 2 else 2
        addend = (addend // n) + (addend % n)
        total += addend
    return total % n == 0


def _encode_token(raw: bytes) -> str:
    """8 raw bytes -> 13 Crockford characters (64 bits plus a padding bit) plus a check character."""
    if len(raw) != TOKEN_BYTES:
        raise ValueError(f"token must be {TOKEN_BYTES} bytes, got {len(raw)}")
    n = int.from_bytes(raw, "big") << 1  # pad to 65 bits
    values = [(n >> shift) & 0x1F for shift in range(60, -5, -5)]
    check = _luhn_mod_n_generate(values)
    return "".join(_ALPHABET[v] for v in values) + _ALPHABET[check]


def _decode_token(token_str: str) -> bytes:
    """Inverse of `_encode_token`; raises `ValueError` on bad length, character or check."""
    if len(token_str) != TOKEN_CHARS + 1:
        raise ValueError(
            f"setup code token must be {TOKEN_CHARS + 1} characters, got {len(token_str)}"
        )
    values = [_char_value(c) for c in token_str]
    if not _luhn_mod_n_validate(values):
        raise ValueError("setup code check character mismatch (typo?)")
    n = 0
    for v in values[:-1]:
        n = (n << 5) | v
    n >>= 1  # drop the padding bit
    return n.to_bytes(TOKEN_BYTES, "big")


def token() -> tuple[bytes, str]:
    """A fresh token: `(raw_bytes, token_str)`; the string is 13 Crockford characters plus a check."""
    raw = secrets.token_bytes(TOKEN_BYTES)
    return raw, _encode_token(raw)


def format_code(token_str: str, *, host: str, port: int = DEFAULT_PORT, apn: str | None = None) -> str:
    """The displayed string: token in groups of four, ` @ host[:port]`, optional `;apn=...`."""
    groups = [token_str[i : i + CODE_GROUP_SIZE] for i in range(0, len(token_str), CODE_GROUP_SIZE)]
    host_part = host if port == DEFAULT_PORT else f"{host}:{port}"
    code = f"{'-'.join(groups)} @ {host_part}"
    if apn:
        code += f";apn={apn}"
    return code


def parse(code: str) -> tuple[bytes, str, int, str | None]:
    """Inverse of `format_code`: the token part tolerates spaces, hyphens and case; the host is verbatim.

    Returns `(token_bytes, host, port, apn)`; raises `ValueError` on anything malformed."""
    if "@" not in code:
        raise ValueError("setup code is missing '@ host'")
    token_part, rest = code.split("@", 1)
    token_str = "".join(ch for ch in token_part if not ch.isspace() and ch != "-").upper()
    raw = _decode_token(token_str)

    rest = rest.strip()
    apn: str | None = None
    if ";apn=" in rest:
        rest, apn = rest.split(";apn=", 1)
        rest = rest.strip()
        apn = apn.strip() or None

    if ":" in rest:
        host, port_str = rest.rsplit(":", 1)
        try:
            port = int(port_str.strip())
        except ValueError as exc:
            raise ValueError(f"invalid port in setup code: {port_str!r}") from exc
    else:
        host, port = rest, DEFAULT_PORT
    host = host.strip()
    if not host:
        raise ValueError("setup code is missing a host")
    return raw, host, port, apn


def _hkdf(token_bytes: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(token_bytes)


def derive(token_bytes: bytes) -> tuple[str, str, bytes]:
    """`(bid, bpw, bkey)`: HKDF-SHA256 of the token under labels "id" (hex[:12]), "pw", "bundle"."""
    bid = _hkdf(token_bytes, b"id").hex()[:12]
    bpw = _b64url(_hkdf(token_bytes, b"pw"))
    bkey = _hkdf(token_bytes, b"bundle")
    return bid, bpw, bkey


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii")


def boot_username(bid: str) -> str:
    """`boot-{bid}`, matching the broker admin's boot username."""
    return f"boot-{bid}"


def boot_topic_down(bid: str) -> str:
    return f"pager/boot/{bid}/down"


class BootstrapDevice(BaseModel):
    """The bundle's plaintext fields. `k` is the raw 32-byte HMAC key (a CBOR byte string).

    `ca_url` and `ca_sha` are both present (a pinned CA pointer) or both absent."""

    model_config = ConfigDict(extra="forbid")

    id: str
    pw: str
    k: bytes
    host: str
    port: int = DEFAULT_PORT
    ca_url: str | None = None
    ca_sha: bytes | None = None
    flags: int = 0
    label: str
    apn: str | None = None

    @field_validator("ca_sha")
    @classmethod
    def _check_ca_sha_len(cls, value: bytes | None) -> bytes | None:
        if value is not None and len(value) != 32:
            raise ValueError("ca_sha must be a 32-byte SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _check_ca_pair(self) -> BootstrapDevice:
        if (self.ca_url is None) != (self.ca_sha is None):
            raise ValueError("ca_url and ca_sha must be both present or both absent")
        return self


def bundle(device: BootstrapDevice, bkey: bytes) -> bytes:
    """CBOR-encode `device` (`docs/PROTOCOL.md` §10 boot keys), AES-256-GCM encrypt under `bkey`;
    returns `nonce ‖ ciphertext ‖ tag`."""
    plain_obj: dict[str, Any] = {
        "v": 1,
        "id": device.id,
        "pw": device.pw,
        "k": device.k,
        "host": device.host,
        "port": device.port,
        "flags": device.flags,
        "label": device.label,
    }
    if device.ca_url is not None:
        plain_obj["ca_url"] = device.ca_url
        plain_obj["ca_sha"] = device.ca_sha
    if device.apn is not None:
        plain_obj["apn"] = device.apn
    plaintext = wirecbor.encode(plain_obj)
    nonce = os.urandom(12)
    ct = AESGCM(bkey).encrypt(nonce, plaintext, None)
    return nonce + ct


def decrypt_bundle(bkey: bytes, blob: bytes) -> dict[str, Any]:
    """Inverse of `bundle()`; raises `InvalidTag` on a wrong key or corrupted ciphertext."""
    nonce, ct = blob[:12], blob[12:]
    plaintext = AESGCM(bkey).decrypt(nonce, ct, None)
    return wirecbor.decode(plaintext)


def issue(
    device_id: str,
    *,
    mqtt_password: str,
    hmac_key: bytes,
    host: str,
    ca_pem: str | None,
    label: str,
    port: int = DEFAULT_PORT,
    flags: int = 0,
    apn: str | None = None,
    settings: Settings | None = None,
    broker: BrokerClient | None = None,
    emqx: BrokerAdmin | None = None,
    now: datetime | None = None,
) -> str:
    """Pushes the bootstrap credential, publishes the encrypted bundle retained to
    `pager/boot/{bid}/down`, records `setupCodes/{bid}` (never the token), returns the setup code.

    `ca_pem` is pinned via a pointer (`ca_resolve.ca_pointer`); a missing `public_base_url` raises
    `PublicBaseUrlRequired` rather than sending an unpinned bundle."""
    settings = settings if settings is not None else Settings.from_env()
    broker = broker if broker is not None else BrokerClient(settings)
    emqx = emqx if emqx is not None else make_broker_admin(settings, broker)

    raw, token_str = token()
    bid, bpw, bkey = derive(raw)

    emqx.ensure_boot_user(bid, bpw)

    ca_url: str | None = None
    ca_sha: bytes | None = None
    if ca_pem:
        ca_url, ca_sha = ca_resolve.ca_pointer(ca_pem, settings)

    device = BootstrapDevice(
        id=device_id,
        pw=mqtt_password,
        k=hmac_key,
        host=host,
        port=port,
        ca_url=ca_url,
        ca_sha=ca_sha,
        flags=flags,
        label=label,
        apn=apn,
    )
    blob = bundle(device, bkey)
    broker.publish(boot_topic_down(bid), blob, qos=1, retain=True)

    now = now if now is not None else datetime.now(UTC)
    setup_codes_store.create(bid, device_id, now + timedelta(minutes=EXPIRY_MINUTES))

    return format_code(token_str, host=host, port=port, apn=apn)


def _clear_and_revoke(bid: str, broker: BrokerClient, emqx: BrokerAdmin) -> None:
    # Bundle first, then credential: a retry between the two never leaves a readable bundle
    # behind a live credential.
    broker.publish(boot_topic_down(bid), b"", qos=1, retain=True)
    emqx.delete_user(boot_username(bid))


def complete(
    bid: str,
    *,
    settings: Settings | None = None,
    broker: BrokerClient | None = None,
    emqx: BrokerAdmin | None = None,
) -> None:
    """Clear the retained bundle, delete the bootstrap credential and `setupCodes/{bid}`."""
    settings = settings if settings is not None else Settings.from_env()
    broker = broker if broker is not None else BrokerClient(settings)
    emqx = emqx if emqx is not None else make_broker_admin(settings, broker)
    _clear_and_revoke(bid, broker, emqx)
    setup_codes_store.delete(bid)


def expire(
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
    broker: BrokerClient | None = None,
    emqx: BrokerAdmin | None = None,
) -> int:
    """Delete every expired setup code's retained bundle and bootstrap credential; returns the count."""
    now = now if now is not None else datetime.now(UTC)
    settings = settings if settings is not None else Settings.from_env()
    broker = broker if broker is not None else BrokerClient(settings)
    emqx = emqx if emqx is not None else make_broker_admin(settings, broker)
    expired = setup_codes_store.list_expired(now)
    for code in expired:
        _clear_and_revoke(code.bid, broker, emqx)
        setup_codes_store.delete(code.bid)
    return len(expired)
