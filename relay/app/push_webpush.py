"""Web Push (VAPID, RFC 8291) to browsers, with an SSRF guard on subscription endpoints."""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

import requests
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

from app.store import push_subscriptions as subs_store

logger = logging.getLogger("relay.push_webpush")

TTL_S = 14400
_HEADERS = {"Urgency": "high"}
_DEAD_STATUSES = (404, 410)
_TIMEOUT_S = 10


class EndpointRefused(ValueError):
    """The endpoint's host is not a public push service address."""


def _getaddrinfo(host: str) -> list[str]:
    return [ai[4][0] for ai in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


_resolver: Callable[[str], list[str]] = _getaddrinfo


def set_resolver(fn: Callable[[str], list[str]] | None) -> None:
    """Test seam: `fn(host) -> [address, ...]`; None restores getaddrinfo."""
    global _resolver
    _resolver = fn or _getaddrinfo


def _is_public(text: str) -> bool:
    try:
        addr = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_global


def check_endpoint(endpoint: str) -> None:
    """Raise `EndpointRefused` unless `endpoint` is https and every resolved address is global."""
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname
    except ValueError as exc:
        raise EndpointRefused("malformed endpoint") from exc
    if parts.scheme != "https" or not host:
        raise EndpointRefused("endpoint must be an https:// URL")
    try:
        addrs = [host] if _literal(host) else _resolver(host)
    except (OSError, UnicodeError) as exc:
        raise EndpointRefused(f"endpoint host {host!r} does not resolve") from exc
    if not addrs:
        raise EndpointRefused(f"endpoint host {host!r} does not resolve")
    bad = [a for a in addrs if not _is_public(a)]
    if bad:
        raise EndpointRefused(f"endpoint host {host!r} is not a public address")


def _literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


class BrowserPushSender(Protocol):
    # False when no VAPID key is set; `BrowserPush` then skips listing subscriptions.
    configured: bool

    def send(
        self, uid: str, subs: list[subs_store.PushSubscription], data: dict[str, str]
    ) -> None: ...


class NullWebPushSender:
    """No VAPID key configured: nothing is sent."""

    configured = False

    def send(
        self, uid: str, subs: list[subs_store.PushSubscription], data: dict[str, str]
    ) -> None:
        return None


class WebPushSender:
    configured = True

    def __init__(
        self, private_key: str, subject: str, session: requests.Session | None = None
    ) -> None:
        self._vapid = Vapid02.from_string(private_key)
        self._subject = subject
        self._session = session

    def send(
        self, uid: str, subs: list[subs_store.PushSubscription], data: dict[str, str]
    ) -> None:
        body = json.dumps(data, separators=(",", ":"))
        for sub in subs:
            try:
                check_endpoint(sub.endpoint)
            except EndpointRefused as exc:
                logger.warning("web push to subscription %s… skipped: %s", sub.id[:12], exc)
                continue
            try:
                webpush(
                    subscription_info={
                        "endpoint": sub.endpoint,
                        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    },
                    data=body,
                    vapid_private_key=self._vapid,
                    # fresh dict per send: pywebpush fills `aud` from the endpoint origin
                    vapid_claims={"sub": self._subject},
                    ttl=TTL_S,
                    headers=dict(_HEADERS),
                    timeout=_TIMEOUT_S,
                    requests_session=self._session,
                )
            except WebPushException as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in _DEAD_STATUSES:
                    subs_store.remove(uid, sub.id)
                    logger.info("web push subscription %s… gone (%s), removed", sub.id[:12], status)
                else:
                    logger.info("web push failed for subscription %s…: %s", sub.id[:12], exc)
            except requests.RequestException as exc:
                logger.info("web push failed for subscription %s…: %s", sub.id[:12], exc)


_sender: BrowserPushSender = NullWebPushSender()


def set_sender(sender: BrowserPushSender) -> None:
    """Replace the process-wide sender."""
    global _sender
    _sender = sender


def get_sender() -> BrowserPushSender:
    return _sender
