"""Serves the app under its mount prefix (`Settings.base_path`, e.g. `/kid-pager`), so a reverse
proxy can forward requests without rewriting them."""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send


class BasePathMiddleware:
    """A request under the prefix has it removed before routing; `/internal/` is refused there,
    because only a proxied request carries the prefix. A request without the prefix is routed as
    it is: callers on the compose network use bare paths, and so does a proxy that strips."""

    def __init__(self, app: ASGIApp, base_path: str) -> None:
        self.app = app
        self.base = base_path
        self.raw_base = base_path.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        if path == self.base:
            query = scope.get("query_string", b"")
            location = self.raw_base + b"/" + (b"?" + query if query else b"")
            await _respond(send, 308, [(b"location", location)])
            return
        if not path.startswith(self.base + "/"):
            await self.app(scope, receive, send)
            return
        rest = path[len(self.base) :]
        if rest.startswith("/internal/"):
            await _respond(send, 404, [])
            return
        scope = dict(scope)
        scope["path"] = rest
        raw = scope.get("raw_path")
        if isinstance(raw, bytes) and raw.startswith(self.raw_base):
            scope["raw_path"] = raw[len(self.raw_base) :]
        await self.app(scope, receive, send)


async def _respond(send: Send, status: int, headers: list[tuple[bytes, bytes]]) -> None:
    await send(
        {"type": "http.response.start", "status": status, "headers": [(b"content-length", b"0"), *headers]}
    )
    await send({"type": "http.response.body", "body": b""})
