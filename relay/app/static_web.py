"""Serves the web static export from the relay the way web/firebase.json does on Hosting."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

WEB_ROOT = Path("/srv/web")

# The web app is one client-routed page (web/app/page.tsx): any path that is not a file is the
# shell. Build assets are the exception, so a stale chunk URL fails instead of returning HTML.
SHELL = "index.html"
_ASSET_PREFIXES = ("_next/",)
_RELAY_PREFIXES = ("api/", "webhooks/", "bridge/", "fed/", "hooks/", "internal/")
_HEADERS = {
    "sw.js": {"Service-Worker-Allowed": "/"},
    "firebase-messaging-sw.js": {"Service-Worker-Allowed": "/"},
}

mimetypes.add_type("application/manifest+json", ".webmanifest")


def _file(root: Path, rel: str) -> Path | None:
    if not rel or rel.endswith("/"):
        return None
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def resolve(root: Path, path: str) -> tuple[Path | None, int]:
    """`(file, status)` for a request path (no leading slash); `(None, 404)` if nothing matches."""
    root = root.resolve()
    rel = path.lstrip("/")
    found = (
        _file(root, rel)
        or _file(root, f"{rel}.html")
        or _file(root, f"{rel.rstrip('/')}/index.html" if rel else "index.html")
    )
    if found is not None:
        return found, 200
    if not rel.startswith(_ASSET_PREFIXES) and ".." not in rel.split("/"):
        shell = _file(root, SHELL)
        if shell is not None:
            return shell, 200
    return _file(root, "404.html"), 404


def mount(app: FastAPI, root: Path | None = None) -> None:
    root = root if root is not None else WEB_ROOT

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def static_web(path: str, request: Request) -> Response:
        if path.startswith(_RELAY_PREFIXES):
            raise HTTPException(status_code=404, detail="Not Found")
        found, status = resolve(root, path)
        if found is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(found, status_code=status, headers=_HEADERS.get(found.name))
