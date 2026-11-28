"""Broker CA resolution: `BROKER_CA_PEM` wins, else the CA is worked out from the served chain."""

from __future__ import annotations

import functools
import hashlib
import logging
import socket
import ssl
import time
from collections.abc import Sequence
from dataclasses import dataclass

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from app.config import Settings
from app.store import cas as cas_store

logger = logging.getLogger("relay.ca_resolve")

_CONNECT_TIMEOUT_S = 5.0
# A failed resolution is not retried sooner than this: each try can block a request for the
# connect timeout.
_RETRY_S = 30.0


@dataclass(frozen=True, slots=True)
class BrokerCa:
    """The resolved broker CA; `subject` is the RFC 4514 name shown as `brokerCaSubject`."""

    pem: str
    subject: str


# Resolved once, not on every request that wants the CA.
_cached: BrokerCa | None = None
_failed_at: float | None = None


def reset_cache() -> None:
    """Test-only: forgets the resolved CA and the memoized certifi bundle."""
    global _cached, _failed_at
    _cached = None
    _failed_at = None
    _load_ca_roots.cache_clear()


def current_pem(settings: Settings) -> str | None:
    """The CA a pager is told to pin: `BROKER_CA_PEM`, else what was resolved before, else, only
    with `BROKER_CA_RESOLVE`, a resolution made now from the certificate the pager port serves."""
    global _failed_at
    if settings.broker_ca_pem:
        return settings.broker_ca_pem
    if _cached is not None:
        return _cached.pem
    if not settings.broker_ca_resolve:
        return None
    now = time.monotonic()
    if _failed_at is not None and now - _failed_at < _RETRY_S:
        return None
    resolved = resolve_broker_ca(settings)
    _failed_at = None if resolved is not None else now
    return resolved.pem if resolved is not None else None


def get_broker_ca_pem() -> str | None:
    return _cached.pem if _cached else None


def get_broker_ca_subject() -> str | None:
    """What `GET /api/admin/settings`'s `brokerCaSubject` reports."""
    return _cached.subject if _cached else None


def resolve_broker_ca(settings: Settings) -> BrokerCa | None:
    """Resolves and caches the broker CA; on any failure caches nothing and returns `None`."""
    global _cached

    if settings.broker_ca_pem:
        cert = _try_parse_pem(settings.broker_ca_pem)
        if cert is None:
            logger.warning(
                "BROKER_CA_PEM is not a parseable certificate; leaving broker ca empty"
            )
            _cached = None
            return None
        _cached = BrokerCa(pem=settings.broker_ca_pem, subject=_subject_str(cert))
        return _cached

    try:
        chain_der = fetch_served_chain(settings.broker_host, settings.broker_port)
    except OSError as exc:
        logger.warning(
            "broker CA auto-resolve: could not connect to %s:%d (%s)",
            settings.broker_host,
            settings.broker_port,
            exc,
        )
        _cached = None
        return None

    resolved = resolve_ca_from_chain(chain_der)
    if resolved is None:
        logger.warning(
            "broker CA auto-resolve: no certifi root matches the chain served by %s:%d",
            settings.broker_host,
            settings.broker_port,
        )
    _cached = resolved
    return resolved


def fetch_served_chain(host: str, port: int, timeout: float = _CONNECT_TIMEOUT_S) -> list[bytes]:
    """Returns the DER chain the server sent, leaf first, with verification disabled."""
    # The stdlib exposes the full chain from 3.13 (`get_unverified_chain`); before that the same
    # call exists on the private `_sslobj` and returns certificate objects. Failing both, the leaf
    # alone suffices when its issuer is a certifi root, else resolution fails closed.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection((host, port), timeout=timeout) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        get_chain = getattr(tls, "get_unverified_chain", None) or getattr(
            getattr(tls, "_sslobj", None), "get_unverified_chain", None
        )
        if callable(get_chain):
            try:
                chain = [_der(cert) for cert in get_chain() or []]
            except (ssl.SSLError, TypeError, ValueError):
                chain = []
            if chain:
                return chain
        leaf = tls.getpeercert(binary_form=True)
        return [leaf] if leaf else []


def _der(cert: object) -> bytes:
    if isinstance(cert, bytes):
        return cert
    import _ssl

    return cert.public_bytes(_ssl.ENCODING_DER)  # type: ignore[attr-defined]


def resolve_ca_from_chain(chain_der: Sequence[bytes]) -> BrokerCa | None:
    """A self-signed cert in the chain is the CA; else the top issuer is looked up in certifi."""
    certs = _parse_der_chain(chain_der)
    if not certs:
        return None

    for cert in certs:
        if cert.subject == cert.issuer:
            return BrokerCa(pem=_to_pem(cert), subject=_subject_str(cert))

    topmost = certs[-1]
    root = _find_certifi_root_by_subject(topmost.issuer)
    if root is None:
        return None
    return BrokerCa(pem=_to_pem(root), subject=_subject_str(root))


def _parse_der_chain(chain_der: Sequence[bytes]) -> list[x509.Certificate]:
    certs = []
    for der in chain_der:
        try:
            certs.append(x509.load_der_x509_certificate(der))
        except ValueError:
            continue
    return certs


def _try_parse_pem(pem: str) -> x509.Certificate | None:
    try:
        return x509.load_pem_x509_certificate(pem.encode())
    except ValueError:
        return None


def _find_certifi_root_by_subject(subject: x509.Name) -> x509.Certificate | None:
    for root in _load_ca_roots():
        if root.subject == subject:
            return root
    return None


@functools.lru_cache(maxsize=1)
def _load_ca_roots() -> tuple[x509.Certificate, ...]:
    with open(certifi.where(), "rb") as f:
        data = f.read()
    roots = []
    for block in _split_pem_certificates(data):
        try:
            roots.append(x509.load_pem_x509_certificate(block))
        except ValueError:
            continue
    return tuple(roots)


def _split_pem_certificates(data: bytes) -> list[bytes]:
    blocks: list[bytes] = []
    current: list[bytes] = []
    in_block = False
    for line in data.splitlines(keepends=True):
        if line.startswith(b"-----BEGIN CERTIFICATE-----"):
            in_block = True
            current = [line]
        elif line.startswith(b"-----END CERTIFICATE-----"):
            current.append(line)
            blocks.append(b"".join(current))
            in_block = False
        elif in_block:
            current.append(line)
    return blocks


class PublicBaseUrlRequired(RuntimeError):
    """A CA is configured but `PUBLIC_BASE_URL` is not: an unpinned bundle is refused."""


def ca_pointer(pem: str, settings: Settings) -> tuple[str, bytes]:
    """`pem` -> `(url, sha256_digest)`; remembers `pem` in `cas/{sha256hex}` for `GET /ca/...`."""
    if not settings.public_base_url:
        raise PublicBaseUrlRequired(
            "a broker CA is configured but PUBLIC_BASE_URL is not set -- refusing to issue "
            "an unpinned bundle/push silently"
        )
    sha = hashlib.sha256(pem.encode("utf-8")).digest()
    sha_hex = sha.hex()
    cas_store.remember(sha_hex, pem)
    url = f"{settings.public_base_url.rstrip('/')}/ca/{sha_hex}.pem"
    return url, sha


def _to_pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _subject_str(cert: x509.Certificate) -> str:
    return cert.subject.rfc4514_string()
