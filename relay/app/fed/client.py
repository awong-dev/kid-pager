"""Signed outbound HTTPS to a linked family's relay; connects to the address `check_host` approved,
trusts only the link's `tlsCa`, caps responses at 256 KiB and exposes only `ERROR_CODES`."""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from app.fed import addr, sign
from app.fed.keys import private_from_b64
from app.store.fed_families import FedFamily
from app.store.fed_links import FedLink

TIMEOUT_S = 10.0
API = "/fed/v1"
MAX_RESPONSE = 256 * 1024

ERROR_CODES = ("unreachable", "tls", "refused", "address_not_allowed", "bad_response")


class FedError(Exception):
    """A failed call. `code` is one of `ERROR_CODES`."""

    code = "unreachable"

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class FedTransportError(FedError):
    """No usable answer: address refused, DNS, connect, TLS, timeout, an over-long or
    unreadable response. Always retryable except `address_not_allowed` and `tls`."""


class FedHttpError(FedError):
    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__("refused" if status < 500 else "unreachable", f"{status} {detail}".strip())
        self.status = status


def tls_context(ca_pem: str | None) -> ssl.SSLContext | bool:
    """Trust exactly `ca_pem` (a PEM, possibly a bundle) and no other root; `True`
    (httpx's default verification with the system store) when there is none.
    `ValueError` for a PEM that holds no certificate."""
    if not ca_pem:
        return True
    try:
        ctx = ssl.create_default_context(cadata=ca_pem)
    except ssl.SSLError as exc:
        raise ValueError("tlsCa holds no certificate") from exc
    if not ctx.get_ca_certs():
        raise ValueError("tlsCa holds no certificate")
    return ctx


class InboxResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    st: str
    rsn: str | None = None


class DirectoryEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rid: str
    alias: str
    name: str
    kind: str = "person"


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    headers: httpx.Headers
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body or b"null")


def _ssl_cause(exc: BaseException | None) -> bool:
    seen = 0
    while exc is not None and seen < 6:
        if isinstance(exc, ssl.SSLError):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def _ip_url(ip: str, port: int, path: str) -> str:
    host = f"[{ip}]" if ":" in ip else ip
    return f"https://{host}:{port}{path}"


class FedClient:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        policy: addr.AddressPolicy | None = None,
    ) -> None:
        self._transport = transport
        self._policy = policy or addr.AddressPolicy()


    def call(
        self,
        method: str,
        path: str,
        *,
        host: str,
        port: int = 443,
        tls_ca: str | None = None,
        body: bytes = b"",
        sender: FedFamily | None = None,
        receiver: str | None = None,
        private_key_b64: str | None = None,
    ) -> Reply:
        """One request. `sender` (a family identity) and `receiver` (the remote fedId) sign it;
        without them it goes unsigned. `FedTransportError` when no response arrives or the host
        is not allowed; the caller judges the status (this never raises for 4xx/5xx)."""
        try:
            addresses = addr.check_host(host, port, self._policy)
        except addr.AddressNotAllowed as exc:
            raise FedTransportError("address_not_allowed", str(exc)) from exc
        headers = {
            "Accept": "application/json",
            "Host": host if port == 443 else f"{host}:{port}",
        }
        if body:
            headers["Content-Type"] = "application/json"
        if sender is not None:
            assert receiver is not None
            key = private_key_b64 or sender.privateKey
            headers.update(
                sign.sign(private_from_b64(key), sender.fedId, receiver, method, path, body)
            )
        try:
            verify = tls_context(tls_ca)
        except ValueError as exc:
            raise FedTransportError("tls", "unusable CA") from exc

        last: FedTransportError | None = None
        for ip in addresses:
            try:
                return self._request_once(method, ip, port, path, headers, body, host, verify)
            except FedTransportError as exc:
                if exc.code != "unreachable":
                    raise
                last = exc
        assert last is not None
        raise last

    def _request_once(
        self,
        method: str,
        ip: str,
        port: int,
        path: str,
        headers: dict[str, str],
        body: bytes,
        server_name: str,
        verify: ssl.SSLContext | bool,
    ) -> Reply:
        try:
            if self._transport is not None:
                client = httpx.Client(transport=self._transport, timeout=TIMEOUT_S)
            else:
                client = httpx.Client(verify=verify, timeout=TIMEOUT_S)
            with client, client.stream(
                method,
                _ip_url(ip, port, path),
                content=body or None,
                headers=headers,
                follow_redirects=False,
                extensions={"sni_hostname": server_name},
            ) as resp:
                chunks: list[bytes] = []
                size = 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE:
                        raise FedTransportError("bad_response", "response too large")
                    chunks.append(chunk)
                reply = Reply(
                    status=resp.status_code, headers=resp.headers, body=b"".join(chunks)
                )
        except FedTransportError:
            raise
        except (httpx.HTTPError, OSError, ssl.SSLError) as exc:
            code = "tls" if _ssl_cause(exc) else "unreachable"
            raise FedTransportError(code, type(exc).__name__) from exc
        if 300 <= reply.status < 400:
            raise FedTransportError("bad_response", "redirect")
        return reply


    def link_call(
        self,
        identity: FedFamily,
        link: FedLink,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        private_key_b64: str | None = None,
    ) -> Reply:
        return self.call(
            method,
            path,
            host=link.remote.host,
            port=link.remote.port,
            tls_ca=link.remote.tlsCa,
            body=body,
            sender=identity,
            receiver=link.remote.fedId,
            private_key_b64=private_key_b64,
        )

    def directory(self, identity: FedFamily, link: FedLink) -> list[DirectoryEntry]:
        """What the linked family shares with this one (`GET /fed/v1/directory`)."""
        reply = self.link_call(identity, link, "GET", f"{API}/directory")
        if reply.status != 200:
            raise FedHttpError(reply.status)
        try:
            return [DirectoryEntry.model_validate(e) for e in reply.json()]
        except (ValueError, TypeError) as exc:
            raise FedTransportError("bad_response", "directory") from exc

    def post_inbox(
        self, identity: FedFamily, link: FedLink, events: list[dict]
    ) -> list[InboxResult]:
        """`POST /fed/v1/inbox`: one result per event, in order. Non-200 raises
        `FedHttpError` (503 means retry the whole batch later); a transport failure
        raises `FedTransportError`."""
        body = json.dumps({"v": 1, "events": events}, separators=(",", ":")).encode()
        reply = self.link_call(identity, link, "POST", f"{API}/inbox", body)
        if reply.status != 200:
            raise FedHttpError(reply.status)
        try:
            return [InboxResult.model_validate(r) for r in reply.json()["results"]]
        except (ValueError, TypeError, KeyError) as exc:
            raise FedTransportError("bad_response", "inbox") from exc


_default: FedClient | None = None


def set_default_client(client: FedClient | None) -> None:
    """Tests: the client every router and the sender use."""
    global _default
    _default = client


def get_client() -> FedClient:
    return _default if _default is not None else FedClient()
