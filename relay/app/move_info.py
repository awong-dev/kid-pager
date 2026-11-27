"""The source's read of the target's public `move-info` (docs/MOVE_DESIGN.md §7.1): the one thing
the source ever learns about the target. The request goes through the federation client, so the
outbound address rules apply (loopback, link-local and, on a cloud relay, private addresses are
refused)."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.config import valid_base_path
from app.fed import client as fed_client
from app.movekeys import is_valid_move_id

ROUTE = "/api/public/move-info"
_HOST_RE = re.compile(r"[A-Za-z0-9._:\[\]-]{1,127}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
CA_URL_MAX_BYTES = 200
CA_PEM_MAX_CHARS = 64 * 1024


class MoveInfoError(Exception):
    """No usable `move-info`. `code` is a federation client error code or `bad_target` /
    `bad_response`."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str
    port: int

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        if _HOST_RE.fullmatch(v) is None:
            raise ValueError("bad host")
        return v

    @field_validator("port")
    @classmethod
    def _port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("bad port")
        return v


class CaPointer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    sha: str

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith("https://") or len(v.encode("utf-8")) > CA_URL_MAX_BYTES:
            raise ValueError("bad CA url")
        return v

    @field_validator("sha")
    @classmethod
    def _sha(cls, v: str) -> str:
        if _SHA_RE.fullmatch(v) is None:
            raise ValueError("bad CA sha")
        return v


class MoveRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    imported: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not is_valid_move_id(v):
            raise ValueError("bad move id")
        return v


class MoveInfo(BaseModel):
    """`GET /api/public/move-info`."""

    model_config = ConfigDict(extra="ignore")

    v: Literal[1]
    authBackend: Literal["home", "firebase"]
    schemaVersion: int
    relayVersion: str
    baseUrl: str
    broker: Endpoint
    ca: CaPointer | None = None
    beam: Endpoint | None = None
    empty: bool
    move: MoveRef | None = None


class TargetAddress(BaseModel):
    baseUrl: str
    host: str
    port: int
    path: str


def parse_target(base_url: str) -> TargetAddress:
    """`https://host[:port][/path]` as a canonical base URL; `MoveInfoError("bad_target")` otherwise."""
    try:
        parts = urlsplit(base_url.strip())
        port = parts.port or 443
    except ValueError as exc:
        raise MoveInfoError("bad_target", "not a web address") from exc
    if parts.scheme != "https" or not parts.hostname:
        raise MoveInfoError("bad_target", "the address must start with https://")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise MoveInfoError("bad_target", "no credentials, query or fragment")
    path = parts.path.rstrip("/")
    if not valid_base_path(path):
        raise MoveInfoError("bad_target", "unusable path")
    host = parts.hostname.lower()
    shown = f"[{host}]" if ":" in host else host
    netloc = shown if port == 443 else f"{shown}:{port}"
    return TargetAddress(baseUrl=f"https://{netloc}{path}", host=host, port=port, path=path)


def check_ca(ca_pem: str | None) -> str | None:
    """`ca_pem` as given, or `None` when empty; `ValueError` for text with no certificate in it or
    more than `CA_PEM_MAX_CHARS` long."""
    if not ca_pem or not ca_pem.strip():
        return None
    if len(ca_pem) > CA_PEM_MAX_CHARS:
        raise ValueError("caPem is too long")
    fed_client.tls_context(ca_pem)
    return ca_pem


def fetch(
    base_url: str,
    *,
    client: fed_client.FedClient | None = None,
    ca_pem: str | None = None,
) -> MoveInfo:
    """The target's `move-info`, its web certificate checked against `ca_pem` when given and the
    public roots otherwise. The answer's own `baseUrl` must be the address that was asked."""
    target = parse_target(base_url)
    try:
        reply = (client or fed_client.get_client()).public_get(
            ROUTE,
            host=target.host,
            port=target.port,
            base_path=target.path,
            tls_ca=ca_pem,
        )
    except fed_client.FedError as exc:
        raise MoveInfoError(exc.code, exc.detail) from exc
    if reply.status != 200:
        raise MoveInfoError("refused", f"move-info answered {reply.status}")
    try:
        info = MoveInfo.model_validate(reply.json())
    except (ValueError, TypeError, ValidationError) as exc:
        raise MoveInfoError("bad_response", "move-info is not in the expected shape") from exc
    try:
        named = parse_target(info.baseUrl).baseUrl
    except MoveInfoError:
        named = None
    if named != target.baseUrl:
        raise MoveInfoError("bad_response", "move-info names a different base URL")
    return info
