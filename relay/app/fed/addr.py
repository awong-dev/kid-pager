"""Outbound address rules: `check_host` resolves a typed host and refuses it if any address is
loopback, link-local, multicast or reserved (private/CGNAT too on a cloud relay): an SSRF guard."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass

from app.config import auth_backend

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], list[str]]

_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class AddressNotAllowed(Exception):
    """The host does not resolve, or resolves to an address federation may not use."""


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise AddressNotAllowed(f"{host} does not resolve") from exc
    out: list[str] = []
    for info in infos:
        ip = str(info[4][0]).split("%", 1)[0]
        if ip not in out:
            out.append(ip)
    return out


@dataclass(frozen=True, slots=True)
class AddressPolicy:
    cloud: bool | None = None  # None: decided per call from AUTH_BACKEND
    resolver: Resolver = system_resolver
    allow_loopback: bool = False  # tests only: injected, never read from the environment

    def is_cloud(self) -> bool:
        return self.cloud if self.cloud is not None else auth_backend() != "home"


def _unmapped(ip: IPAddress) -> IPAddress:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def refusal(ip: IPAddress, *, cloud: bool, allow_loopback: bool = False) -> str | None:
    """Why `ip` may not be dialled, or None."""
    ip = _unmapped(ip)
    if ip.is_loopback:
        return None if allow_loopback else "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_reserved:
        return "reserved"
    if cloud and (
        ip.is_private or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT)
    ):
        return "private"
    return None


def check_host(host: str, port: int, policy: AddressPolicy | None = None) -> list[str]:
    """Resolve `host` and return its addresses, or raise `AddressNotAllowed` when any of them
    is refused (all of them must be fine: a name with one public and one internal record is
    refused, since which one the connection uses is not ours to leave to chance)."""
    policy = policy or AddressPolicy()
    try:
        literal: IPAddress | None = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    addresses = [str(literal)] if literal is not None else policy.resolver(host, port)
    if not addresses:
        raise AddressNotAllowed(f"{host} does not resolve")
    cloud = policy.is_cloud()
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise AddressNotAllowed(f"{host} resolves to {raw!r}") from exc
        why = refusal(ip, cloud=cloud, allow_loopback=policy.allow_loopback)
        if why is not None:
            raise AddressNotAllowed(f"{host} resolves to a {why} address")
    return addresses
